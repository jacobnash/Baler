"""GET /ops/freshness and GET /ops/gateway-errors (Baler#178): the read-only
signals tools/live-check's post-deploy verification reads instead of the
database or docker. The freshness SQL needs docker-compose's postgres."""

import json
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import pytest
import requests
from conftest import assert_matches_schema, load_spec

from baler import ops_freshness
from baler.ops_api import OPENAPI_SPEC_PATH, make_handler
from baler.scope import Scope

GATEWAY_SECRET = "test-gateway-secret"


def test_check_prefix():
    assert ops_freshness.check_prefix("fbf/home/") == "fbf/home"
    assert ops_freshness.check_prefix("/fbf/home-1/a_b.c~d") == "fbf/home-1/a_b.c~d"
    for bad in ["", "fbf//home", "fbf/home/%", "fbf/../x", "fbf home", "fbf/home'--"]:
        with pytest.raises(ValueError):
            ops_freshness.check_prefix(bad)


def test_like_escapes_wildcards():
    assert ops_freshness._like("fbf/a_b") == "urn:point:fbf/a\\_b/%"


def test_fold_route():
    assert ops_freshness.fold_route("/cx/projects/9ddf1d04-aa11-4b2c/report?x=1") == "/cx/projects/:id/report"
    assert ops_freshness.fold_route("/ops/status") == "/ops/status"


def _log(tmp_path, lines):
    p = tmp_path / "5xx.log"
    p.write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
    return str(p)


def test_gateway_errors_counts_the_window_only(tmp_path):
    path = _log(tmp_path, [
        {"ts": "2026-11-07T21:59:59+00:00", "status": 502, "method": "GET", "uri": "/api/read"},
        {"ts": "2026-11-07T22:00:05+00:00", "status": 502, "method": "GET", "uri": "/api/read", "upstream_status": "502"},
        {"ts": "2026-11-07T22:00:06+00:00", "status": 502, "method": "GET", "uri": "/api/read"},
        {"ts": "2026-11-07T22:01:00+00:00", "status": 504, "method": "GET", "uri": "/cx/projects/0123456789ab/report"},
        {"ts": "2026-11-07T22:05:01+00:00", "status": 500, "method": "POST", "uri": "/ingest/ingest"},
    ])
    since = datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc)
    out = ops_freshness.gateway_errors(since, since + timedelta(minutes=5), path)
    assert out["available"] is True
    assert out["count"] == 3
    assert out["by_status"] == {"502": 2, "504": 1}
    assert out["by_route"][0] == {"route": "GET /api/read", "status": 502, "count": 2}
    assert out["by_route"][1]["route"] == "GET /cx/projects/:id/report"
    assert out["first"] == "2026-11-07T22:00:05+00:00"
    assert out["last"] == "2026-11-07T22:01:00+00:00"
    assert ops_freshness.gateway_errors(None, None, path)["count"] == 5


def test_gateway_errors_without_a_log_is_unavailable_not_zero(tmp_path):
    out = ops_freshness.gateway_errors(None, None, str(tmp_path / "missing" / "5xx.log"))
    assert out["available"] is False
    assert "no gateway 5xx log" in out["detail"]


def test_gateway_errors_reads_only_the_tail(tmp_path):
    path = tmp_path / "5xx.log"
    line = json.dumps({"ts": "2026-11-07T22:00:00+00:00", "status": 502, "method": "GET", "uri": "/x"}) + "\n"
    path.write_text(line * 100)
    out = ops_freshness._tail_lines(str(path), limit=len(line) * 10 + 5)
    assert len(out) == 10  # the cut first line is dropped


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("BALER_GATEWAY_SECRET", GATEWAY_SECRET)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_endpoints_need_the_gateway_and_validate_input(live_server):
    assert requests.get(f"{live_server}/freshness?prefix=fbf").status_code == 401
    assert requests.get(f"{live_server}/gateway-errors").status_code == 401
    h = {"X-Gateway-Secret": GATEWAY_SECRET}
    assert requests.get(f"{live_server}/freshness?prefix=a%25b", headers=h).status_code == 400
    assert requests.get(f"{live_server}/gateway-errors?since=yesterday", headers=h).status_code == 400


def test_gateway_errors_matches_documented_schema(live_server, tmp_path):
    spec = load_spec(OPENAPI_SPEC_PATH)
    path = _log(tmp_path, [{"ts": "2026-11-07T22:00:05+00:00", "status": 502, "method": "GET", "uri": "/api/read"}])
    with patch.object(ops_freshness, "GATEWAY_ERROR_LOG", path):
        resp = requests.get(f"{live_server}/gateway-errors?since=2026-11-07T22:00:00Z",
                            headers={"X-Gateway-Secret": GATEWAY_SECRET})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert_matches_schema(resp.json(), spec["paths"]["/gateway-errors"]["get"]["responses"]["200"]["content"]
                          ["application/json"]["schema"], spec)


# --- the SQL, against docker-compose's postgres --------------------------------

PREFIX = "test-fresh/site"


@pytest.fixture
def conn():
    from baler.timeseries import connect

    c = connect()
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"urn:point:{PREFIX}/%",))
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:point:test-fresh/site_x/%",))
    c.commit()
    yield c
    c.execute("DELETE FROM point_history WHERE point_uri LIKE %s", ("urn:point:test-fresh/%",))
    c.commit()
    c.close()


def _write(conn, uri, ages_s):
    now = datetime.now(timezone.utc)
    for a in ages_s:
        conn.execute("INSERT INTO point_history (point_uri, ts, value) VALUES (%s, %s, 1) ON CONFLICT DO NOTHING",
                     (uri, now - timedelta(seconds=a)))
    conn.commit()


@pytest.mark.integration
def test_freshness_reports_last_sample_poll_and_quiet_points(conn):
    _write(conn, f"urn:point:{PREFIX}/pi/a", [2, 12, 22, 32, 42])          # every 10 s, fresh
    _write(conn, f"urn:point:{PREFIX}/pi/b", [300, 305, 310])              # every 5 s, quiet 5 min
    _write(conn, f"urn:point:{PREFIX}/old/c", [7200, 7260, 7320])          # quiet 2 h: only the rollup knows it
    _write(conn, "urn:point:test-fresh/site_x/d", [1])                     # LIKE's _ must not match this
    try:
        conn.execute("CALL refresh_continuous_aggregate('point_history_5m', now() - interval '1 day', now())")
        conn.execute("CALL refresh_continuous_aggregate('point_history_1h', now() - interval '1 day', now())")
        conn.commit()
        rollups = True
    except Exception:  # noqa: BLE001 - no TimescaleDB rollups in this database
        conn.rollback()
        rollups = False
    out = ops_freshness.freshness(conn, PREFIX)
    by = {p["uri"].rsplit("/", 2)[-2] + "/" + p["uri"].rsplit("/", 1)[-1]: p for p in out["points"]}
    assert "site_x/d" not in by and all("site_x" not in p["uri"] for p in out["points"])
    assert by["pi/a"]["poll_s"] == pytest.approx(10, abs=0.5)
    assert by["pi/a"]["age_s"] < 10
    assert by["pi/a"]["samples_last_hour"] == 5
    assert by["pi/b"]["age_s"] == pytest.approx(300, abs=10)
    assert by["pi/b"]["poll_s"] == pytest.approx(5, abs=0.5)
    if rollups:
        assert by["old/c"]["age_s"] == pytest.approx(7200, abs=30)
        assert by["old/c"]["samples_last_hour"] == 0
    assert out["prefix_rows_last_60s"] == 5
    assert out["rows_last_60s"] >= 6
    # a caller limited to other sites sees none of it
    hidden = ops_freshness.freshness(conn, PREFIX, Scope(("elsewhere",)))
    assert hidden["points"] == [] and hidden["prefix_rows_last_60s"] == 0
