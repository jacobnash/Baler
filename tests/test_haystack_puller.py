"""
Pull-cycle logic tested against a lightweight fake HaystackClient (a plain
stub returning canned read()/his_read() rows, same shape HaystackClient's
public methods already return - the SCRAM handshake itself is proven by
test_haystack_client.py, no need to re-prove it here) plus an in-memory
Store() (same idiom as test_mapping.py/test_ingest.py) and real Postgres for
history + checkpoints.
"""

import os
import time
from datetime import datetime, timezone

import pytest

from baler.haystack_pull_state import ensure_schema, get_checkpoint
from baler.haystack_puller import pull_equip, pull_once, pull_points
from baler.ingest import topic_to_point_uri
from baler.mapping import load_rules
from baler.store import BRICK, Store
from baler.timeseries import connect, read_latest

SOURCE_ID = "test-haxall"


class FakeHaystackClient:
    def __init__(self, points=None, equip=None, history=None):
        self._points = points or []
        self._equip = equip or []
        self._history = history or {}  # ref (no "@") -> list of {"ts": epoch, "val": ...}
        self.his_read_calls = []

    def read(self, filter, limit=None):
        return self._equip if filter == "equip" else self._points

    def his_read(self, id, range):
        self.his_read_calls.append((id, range))
        return self._history.get(id.lstrip("@"), [])


@pytest.fixture
def ts_conn():
    conn = connect()
    ensure_schema(conn)
    conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"urn:point:haystack/{SOURCE_ID}/%",))
    conn.execute("DELETE FROM haystack_pull_checkpoint WHERE point_uri LIKE %s", (f"urn:point:haystack/{SOURCE_ID}/%",))
    return conn


@pytest.mark.integration
def test_first_seen_point_backfills_the_configured_lookback_window(ts_conn):
    store = Store()
    client = FakeHaystackClient(points=[{"id": "p1", "point": True}])

    pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=3.0)

    assert len(client.his_read_calls) == 1
    called_id, called_range = client.his_read_calls[0]
    assert called_id == "@p1"
    start_iso, end_iso = called_range.split(",")
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    assert 2.9 * 86400 < (end - start).total_seconds() < 3.1 * 86400


@pytest.mark.integration
def test_already_checkpointed_point_pulls_only_the_incremental_range(ts_conn):
    store = Store()
    client = FakeHaystackClient(points=[{"id": "p1", "point": True}])

    pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=7.0)
    first_call_range = client.his_read_calls[0][1]
    checkpoint_after_first = get_checkpoint(ts_conn, str(topic_to_point_uri(f"haystack/{SOURCE_ID}/p1")))
    assert checkpoint_after_first is not None

    time.sleep(0.05)
    pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=7.0)
    second_call_range = client.his_read_calls[1][1]

    second_start = datetime.fromisoformat(second_call_range.split(",")[0])
    checkpoint_dt = datetime.fromtimestamp(checkpoint_after_first, tz=timezone.utc)
    assert abs((second_start - checkpoint_dt).total_seconds()) < 1.0
    assert second_call_range != first_call_range


@pytest.mark.integration
def test_equip_ref_gets_namespaced_and_resolves_via_link_equip_ref(ts_conn):
    store = Store()
    client = FakeHaystackClient(
        equip=[{"id": "e1", "equip": True}],
        points=[{"id": "p1", "point": True, "equipRef": "e1"}],
    )

    pull_once(client, store, ts_conn, SOURCE_ID, "point", "equip", backfill_days=1.0)

    point_uri = topic_to_point_uri(f"haystack/{SOURCE_ID}/p1")
    rows = list(store.query(f"""
        PREFIX brick: <https://brickschema.org/schema/Brick#>
        SELECT ?equip WHERE {{ <{point_uri}> brick:isPointOf ?equip }}
    """))
    assert len(rows) == 1
    assert str(rows[0].equip) == f"urn:equip:haystack:{SOURCE_ID}:e1"


@pytest.mark.integration
def test_realistic_point_tags_classify_into_a_real_brick_class(ts_conn):
    rules = load_rules()
    rule = next(r for r in rules if r["brick_class"] == "Zone_Air_Temperature_Sensor")

    store = Store()
    row = {"id": "p1", **{t: True for t in rule["tags"]}}
    client = FakeHaystackClient(points=[row])

    pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=1.0)

    point_uri = topic_to_point_uri(f"haystack/{SOURCE_ID}/p1")
    rows = list(store.query(f"SELECT ?type WHERE {{ <{point_uri}> a ?type }}"))
    assert str(BRICK["Zone_Air_Temperature_Sensor"]) in {str(r.type) for r in rows}


@pytest.mark.integration
def test_history_rows_land_in_postgres(ts_conn):
    store = Store()
    now = time.time()
    client = FakeHaystackClient(
        points=[{"id": "p1", "point": True}],
        history={"p1": [{"ts": now - 60, "val": 71.5}]},
    )

    pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=1.0)

    point_uri = topic_to_point_uri(f"haystack/{SOURCE_ID}/p1")
    assert read_latest(ts_conn, str(point_uri)) == 71.5


@pytest.mark.integration
def test_pull_points_and_pull_equip_tally_classification_outcomes(ts_conn):
    """--once's migration summary is only as good as these counts - a
    direct-match point/equip and a no-overlap-at-all point/equip should
    land in different buckets, not get silently merged."""
    rules = load_rules()
    rule = next(r for r in rules if r["brick_class"] == "Zone_Air_Temperature_Sensor")
    equip_rules = load_rules("rules/haystack_equip_to_brick.yaml")
    equip_rule = equip_rules[0]

    store = Store()
    client = FakeHaystackClient(
        points=[
            {"id": "p1", **{t: True for t in rule["tags"]}},
            {"id": "p2", "totallyUnknownTag": True},
        ],
        equip=[{"id": "e1", **{t: True for t in equip_rule["tags"]}}],
    )

    point_counts = pull_points(client, store, ts_conn, SOURCE_ID, "point", backfill_days=1.0)
    equip_counts = pull_equip(client, store, SOURCE_ID, "equip")

    assert point_counts == {"direct": 1, "miss": 1}
    assert equip_counts == {"direct": 1}


def test_equip_with_matching_tags_classifies_into_a_real_brick_equip_class():
    from baler.mapping import load_rules as load_mapping_rules

    equip_rules = load_mapping_rules("rules/haystack_equip_to_brick.yaml")
    rule = equip_rules[0]

    store = Store()
    client = FakeHaystackClient(equip=[{"id": "e1", **{t: True for t in rule["tags"]}}])

    pull_equip(client, store, SOURCE_ID, "equip")

    equip_uri = f"urn:equip:haystack:{SOURCE_ID}:e1"
    rows = list(store.query(f"SELECT ?type WHERE {{ <{equip_uri}> a ?type }}"))
    assert str(BRICK[rule["brick_class"]]) in {str(r.type) for r in rows}


HAXALL_OSS_URL = os.environ.get("HAXALL_OSS_URL", "http://localhost:8280")
HAXALL_SU_USERNAME = os.environ.get("HAXALL_SU_USERNAME")
HAXALL_SU_PASSWORD = os.environ.get("HAXALL_SU_PASSWORD", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not HAXALL_SU_USERNAME,
    reason="requires a real running Haxall instance - set HAXALL_SU_USERNAME (and HAXALL_SU_PASSWORD, "
    "HAXALL_OSS_URL if not localhost:8280) to the same env vars e2e-haxall/ and test_haystack_client.py use to run this",
)
def test_pull_once_against_a_real_running_haxall_instance(ts_conn):
    from baler.haystack_client import HaystackClient

    store = Store()
    client = HaystackClient(HAXALL_OSS_URL, HAXALL_SU_USERNAME, HAXALL_SU_PASSWORD)

    pull_once(client, store, ts_conn, "e2e-haxall", "point", "equip", backfill_days=1.0)

    rows = list(store.query("""
        SELECT ?point ?type WHERE {
            ?point a ?type .
            FILTER(STRSTARTS(STR(?point), "urn:point:haystack/e2e-haxall/"))
        }
    """))
    assert len(rows) > 0, "expected at least one pulled point to have landed with a type in the store"
