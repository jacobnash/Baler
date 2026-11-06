"""
GET /his - the Axon-style `readAll(filter).hisRead(span)` route - round
trip against live Oxigraph + Postgres, same live_server-against-real-spec
pattern as test_history_routes.py. hisquery.py's pure layers (parser,
compiler, spans) are covered service-free in test_hisquery.py; this file
is only what needs the real stores: equipment -> point expansion, the
multi-point range read, and the SQL rollup.
"""

import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest
import requests
from conftest import assert_matches_schema, load_spec
from rdflib import URIRef

from baler.ingest import ingest_equip_tags, ingest_tags, link_point_to_equip
from baler.ingest_api import OPENAPI_SPEC_PATH, make_handler
from baler.remote_store import RemoteStore
from baler.timeseries import connect, connect_pool

GATEWAY_SECRET = "test-gateway-secret"


@pytest.fixture(scope="module")
def spec() -> dict:
    return load_spec(OPENAPI_SPEC_PATH)


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    store = RemoteStore()
    ts_pool = connect_pool()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, ts_pool))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", store
    server.shutdown()
    ts_pool.close()


@pytest.fixture
def gw():
    s = requests.Session()
    s.headers["X-Gateway-Secret"] = GATEWAY_SECRET
    return s


@pytest.fixture
def ts_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def ahu(live_server, gw, ts_conn):
    """One AHU with a temp sensor (numeric, 4 samples spread over today
    UTC), a fan status (bool), and a sibling VAV so filters have something
    to exclude. Unique-per-run marker tag so this test's own data is the
    only thing its filters can match, regardless of what else the shared
    dev DB holds - and so teardown can delete exactly this run's triples
    and rows from both stores (same scoped-cleanup pattern as
    test_haystack_brick_e2e.py), leaving no "Test AHU" behind for a
    developer's own `readAll(ahu)` to trip over."""
    base_url, store = live_server
    run = f"hisq{int(time.time() * 1000)}"
    prefix = f"test-his/{run}"
    try:
        yield _seed_ahu(base_url, store, gw, run, prefix)
    finally:
        ts_conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"%{run}%",))
        store._update(
            f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(CONTAINS(STR(?s), "{run}") || CONTAINS(STR(?o), "{run}")) }}'
        )


def _seed_ahu(base_url, store, gw, run, prefix):
    equip_uri = ingest_equip_tags(store, f"{prefix}/ahu", {"ahu": True, run: True, "dis": "Test AHU"})
    vav_uri = ingest_equip_tags(store, f"{prefix}/vav", {"vav": True, run: True})

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # Samples strictly inside today (UTC) - hours 1..4 - plus one yesterday
    # that a `today` span must NOT include.
    temp_topic, fan_topic, zone_topic = f"{prefix}/ahu/dat", f"{prefix}/ahu/fan", f"{prefix}/vav/zt"
    readings = [{"point": temp_topic, "value": 50.0 + i, "ts": (today + timedelta(hours=1 + i)).timestamp()} for i in range(4)]
    readings.append({"point": temp_topic, "value": 99.0, "ts": (today - timedelta(hours=1)).timestamp()})
    readings += [{"point": fan_topic, "value": i % 2 == 0, "ts": (today + timedelta(hours=1 + i)).timestamp()} for i in range(4)]
    readings.append({"point": zone_topic, "value": 72.0, "ts": (today + timedelta(hours=2)).timestamp()})
    assert gw.post(f"{base_url}/ingest", json=readings).status_code == 204

    for topic, tags, equip in [
        (temp_topic, {"discharge": True, "air": True, "temp": True, "sensor": True, "unit": "°F", "dis": "DAT", run: True}, equip_uri),
        (fan_topic, {"fan": True, "run": True, "sensor": True, "kind": "Bool", run: True}, equip_uri),
        (zone_topic, {"zone": True, "air": True, "temp": True, "sensor": True, run: True}, vav_uri),
    ]:
        point_uri = ingest_tags(store, topic, tags)
        link_point_to_equip(store, point_uri, URIRef(equip))
    return {"run": run, "equip": str(equip_uri), "vav": str(vav_uri), "temp": temp_topic, "fan": fan_topic, "zone": zone_topic}


def _his(gw, base_url, expr, **params):
    return gw.get(f"{base_url}/his", params={"expr": expr, **params})


@pytest.mark.integration
def test_his_without_gateway_secret_is_401(live_server):
    base_url, _ = live_server
    assert requests.get(f"{base_url}/his", params={"expr": "ahu"}).status_code == 401


@pytest.mark.integration
def test_equipment_match_expands_to_its_points_and_matches_schema(live_server, gw, spec, ahu):
    base_url, _ = live_server
    resp = _his(gw, base_url, f"readAll(ahu and {ahu['run']}).hisRead(today)")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_matches_schema(body, spec["paths"]["/his"]["get"]["responses"]["200"]["content"]["application/json"]["schema"], spec)

    assert body["mode"] == "readAll" and body["filter"] == f"ahu and {ahu['run']}"
    assert body["matched"] == [ahu["equip"]] and body["matchedCount"] == 1
    assert body["rollup"] is None
    assert body["span"]["label"] == "today" and body["span"]["tz"] == "UTC"

    by_point = {s["point"]: s for s in body["series"]}
    assert set(by_point) == {ahu["temp"], ahu["fan"]}  # the VAV's zone temp is not part of this AHU
    dat = by_point[ahu["temp"]]
    assert dat["dis"] == "DAT" and dat["unit"] == "°F" and dat["kind"] == "Number"
    assert dat["brickClass"] is None  # nothing classified these; the field is still present
    assert dat["equip"] == ahu["equip"] and dat["equipDis"] == "Test AHU"
    assert set(dat["tags"]) >= {"air", "discharge", "sensor", "temp"}
    assert [r["value"] for r in dat["history"]] == [50.0, 51.0, 52.0, 53.0]  # yesterday's 99.0 excluded
    assert dat["truncated"] is False
    fan = by_point[ahu["fan"]]
    assert fan["kind"] == "Bool" and [r["value"] for r in fan["history"]] == [True, False, True, False]


@pytest.mark.integration
def test_point_filter_reads_points_directly(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"readAll(temp and sensor and {ahu['run']}).hisRead(today)").json()
    assert sorted(body["matched"]) == sorted([f"urn:point:{ahu['temp']}", f"urn:point:{ahu['zone']}"])
    assert {s["point"] for s in body["series"]} == {ahu["temp"], ahu["zone"]}


@pytest.mark.integration
def test_read_singular_takes_one_match_and_404s_on_none(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"read({ahu['run']} and equip).hisRead(today)").json()
    assert body["mode"] == "read" and body["matchedCount"] == 1 and len(body["matched"]) == 1

    resp = _his(gw, base_url, f"read({ahu['run']} and nothing_has_this_tag).hisRead(today)")
    assert resp.status_code == 404
    assert "no rec matches" in resp.json()["error"]


@pytest.mark.integration
def test_readall_with_no_match_is_empty_not_an_error(live_server, gw):
    base_url, _ = live_server
    resp = _his(gw, base_url, "readAll(nothing_has_this_tag_either).hisRead(today)")
    assert resp.status_code == 200
    assert resp.json()["series"] == [] and resp.json()["matchedCount"] == 0


@pytest.mark.integration
def test_id_ref_selects_equipment_by_uri(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"readAll(id == @{ahu['vav']}).hisRead(today)").json()
    assert [s["point"] for s in body["series"]] == [ahu["zone"]]


@pytest.mark.integration
def test_rollup_folds_in_sql_including_bools_as_0_1(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"readAll(ahu and {ahu['run']}).hisRead(today).hisRollup(avg, 2hr)").json()
    assert body["rollup"] == {"fold": "avg", "interval": "2hr", "auto": False}
    by_point = {s["point"]: s for s in body["series"]}
    # Samples at hours 1,2 -> bucket [0,2) has 50.0; bucket [2,4) has 51,52; bucket [4,6) has 53.
    assert [r["value"] for r in by_point[ahu["temp"]]["history"]] == [50.0, 51.5, 53.0]
    # Bool rollup: the same buckets over T,F,T,F -> 1.0, 0.5, 0.0
    assert by_point[ahu["fan"]]["kind"] == "Number"
    assert [r["value"] for r in by_point[ahu["fan"]]["history"]] == [1.0, 0.5, 0.0]
    # Buckets are stamped with their start, aligned to the span start (midnight).
    start = body["span"]["start"]
    assert [r["ts"] - start for r in by_point[ahu["temp"]]["history"]] == [0.0, 7200.0, 14400.0]


@pytest.mark.integration
def test_calendar_rollup_uses_date_trunc(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"readAll(temp and {ahu['run']} and discharge).hisRead(thisMonth).hisRollup(count, 1mo)").json()
    (series,) = body["series"]
    # today's 4 samples, plus yesterday's 1 if yesterday is still this month
    assert series["history"][-1]["value"] in (4.0, 5.0)


@pytest.mark.integration
def test_computed_points_take_unit_and_label_from_their_history_rows(live_server, gw, ahu):
    """derivation_engine writes unit/label onto point_history rows, never as
    graph tags (see _write_and_attach) - a computed point attached to this
    AHU must still come back with a real display name and unit."""
    base_url, store = live_server
    from baler import timeseries

    computed = f"urn:point:computed/{ahu['run']}/ahu"
    link_point_to_equip(store, URIRef(computed), URIRef(ahu["equip"]))
    with timeseries.connect() as conn:
        timeseries.write_point_value(conn, computed, 12.5, datetime.now(timezone.utc) - timedelta(minutes=1), unit="°F", label="AHU Coil Delta-T")

    body = _his(gw, base_url, f"readAll(ahu and {ahu['run']}).hisRead(today)").json()
    (series,) = [s for s in body["series"] if s["id"] == computed]
    assert series["dis"] == "AHU Coil Delta-T" and series["unit"] == "°F"
    assert series["point"] == f"computed/{ahu['run']}/ahu"
    assert "_dis_from_tag" not in series
    # A point that has its own dis tag keeps it - the history label is only a fallback.
    (dat,) = [s for s in body["series"] if s["point"] == ahu["temp"]]
    assert dat["dis"] == "DAT"


@pytest.mark.integration
def test_brick_only_entities_are_found_by_brick_words_classes_and_aliases(live_server, gw, ahu):
    """An entity that only ever got Brick rdf:types - no Haystack tags at
    all (a loaded Brick model, a derivation output) - must be reachable
    with the words a person would type, through the real Oxigraph. Uses
    the run's own equipment so the assertion is scoped, but the point is
    typed brick:RTU (Brick's alias for Rooftop_Unit, a subclass of
    Air_Handling_Unit) and never tagged."""
    from baler import timeseries
    from baler.store import BRICK
    from baler.timeseries import connect

    base_url, store = live_server
    rtu = URIRef(f"urn:equip:brick/{ahu['run']}/rtu")
    zt = URIRef(f"urn:point:brick/{ahu['run']}/rtu/zt")
    store.add_entity(rtu, BRICK.RTU)
    store.add_entity(zt, BRICK.Zone_Air_Temperature_Sensor)
    link_point_to_equip(store, zt, rtu)
    with connect() as conn:
        timeseries.write_point_value(conn, str(zt), 71.5, datetime.now(timezone.utc) - timedelta(minutes=1))

    def ids(expr):
        return {s["id"] for s in _his(gw, base_url, expr).json()["series"]}

    site_scope = f"id == @urn:equip:brick/{ahu['run']}/rtu"
    # Class names, any case, alias or canonical, any ancestor - all expand the RTU to its point.
    for expr in ("readAll(RTU and " + site_scope + ")", "readAll(rooftop_unit and " + site_scope + ")",
                 "readAll(Air_Handling_Unit and " + site_scope + ")", "readAll(AHU and " + site_scope + ")",
                 "readAll(Equipment and " + site_scope + ")"):
        assert ids(expr) == {str(zt)}, expr
    # Words - Brick's own and Haystack's - reach the untagged point directly.
    for expr in ("Zone_Air_Temperature_Sensor", "Temperature_Sensor", "zone and air and temperature and sensor", "zone and temp and sensor"):
        assert str(zt) in ids(f"readAll({expr})"), expr
    assert str(zt) not in ids("readAll(setpoint)")
    # `ahu` the marker word finds it too (Brick tag:AHU on Air_Handling_Unit's subtree) - and the tagged AHU alongside.
    matched = _his(gw, base_url, f"readAll(ahu and ({site_scope} or {ahu['run']}))").json()["matched"]
    assert set(matched) == {str(rtu), ahu["equip"]}

    # Near-miss spelling: nothing matches, but the response says what was probably meant.
    body = _his(gw, base_url, "readAll(Air_Handeling_Unit and temperture)").json()
    assert body["matchedCount"] == 0
    assert {h["token"]: h["suggestions"][0] for h in body["hints"]} == {"Air_Handeling_Unit": "Air_Handling_Unit", "temperture": "temperature"}


@pytest.mark.integration
def test_limit_truncates_per_point_and_flags_it(live_server, gw, ahu):
    base_url, _ = live_server
    body = _his(gw, base_url, f"readAll(ahu and {ahu['run']}).hisRead(today)", limit=2).json()
    assert body["limit"] == 2
    for s in body["series"]:
        assert len(s["history"]) == 2 and s["truncated"] is True


@pytest.mark.integration
def test_tz_shifts_the_day_boundary(live_server, gw, ahu):
    base_url, _ = live_server
    today_utc = datetime.now(timezone.utc).date().isoformat()
    expr = f"readAll(discharge and {ahu['run']}).hisRead({today_utc})"

    utc_values = [r["value"] for r in _his(gw, base_url, expr, tz="UTC").json()["series"][0]["history"]]
    assert utc_values == [50.0, 51.0, 52.0, 53.0]

    # Etc/GMT-1 is UTC+1 (POSIX sign convention), no DST: that calendar day
    # starts at 23:00 UTC the day before - exactly where the 99.0 sample sits.
    body = _his(gw, base_url, expr, tz="Etc/GMT-1").json()
    assert body["span"]["tz"] == "Etc/GMT-1"
    assert [r["value"] for r in body["series"][0]["history"]] == [99.0, 50.0, 51.0, 52.0, 53.0]


@pytest.mark.integration
@pytest.mark.parametrize(
    "params, fragment",
    [
        ({}, "missing required query param: expr"),
        ({"expr": "readAll(ahu).hisRead(nope)"}, "unknown span"),
        ({"expr": "readAll(ahu).hisRead(today)", "tz": "Mars/Olympus"}, "unknown timezone"),
        ({"expr": "readAll(ahu).hisRead(today)", "limit": "x"}, "limit must be an integer"),
        ({"expr": "readAll(ahu).hisRead(today)", "limit": "0"}, "limit must be between"),
        ({"expr": "readAll(ahu).hisRead(today).hisRollup(avg, 2mo)"}, "calendar rollups support"),
    ],
)
def test_bad_requests_are_documented_400s(live_server, gw, spec, params, fragment):
    base_url, _ = live_server
    resp = gw.get(f"{base_url}/his", params=params)
    assert resp.status_code == 400, resp.text
    assert_matches_schema(resp.json(), spec["paths"]["/his"]["get"]["responses"]["400"]["content"]["application/json"]["schema"], spec)
    assert fragment in resp.json()["error"]
