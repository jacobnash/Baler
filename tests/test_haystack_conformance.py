"""
Haystack conformance through the real gateway, with a client we didn't
write: phable (an independent Haystack client library). If phable can log
in with SCRAM, read, hisRead and hisWrite against /api/{project}/{op} in
both Hayson and Zinc, so can other standard Haystack tooling - which is
the whole claim of haystack_api.py. Our own openhay client can't prove
that; it shares our assumptions (it took phable to find four wire-format
mismatches, see openhay's interop commit).

Integration: needs `docker compose up -d` (real nginx/njs gateway,
haystack_api, auth_api, ingest_api, Postgres, Oxigraph). Creates and
removes its own throwaway org.
"""

import datetime as dt
import time
import uuid

import phable  # a dev dependency: a plain import, so a missing phable fails, never silently skips
import pytest
import requests
from test_gateway_auth import BASE, _post_with_retry

pytestmark = pytest.mark.integration

ORG_PREFIX = "Haystack Conformance Org"
EMAIL = "admin@haystack-conformance.invalid"
PASSWORD = "correct-horse-battery-staple"


def _cleanup():
    from baler.timeseries import connect

    conn = connect()
    sub = f"SELECT id FROM orgs WHERE name LIKE '{ORG_PREFIX}%'"
    conn.execute(f"DELETE FROM user_sites WHERE user_id IN (SELECT id FROM users WHERE org_id IN ({sub}))")
    conn.execute(f"DELETE FROM api_keys WHERE org_id IN ({sub})")
    conn.execute(f"DELETE FROM sites WHERE org_id IN ({sub})")
    conn.execute(f"DELETE FROM users WHERE org_id IN ({sub})")
    conn.execute(f"DELETE FROM orgs WHERE name LIKE '{ORG_PREFIX}%'")
    conn.close()


@pytest.fixture(scope="module")
def probe():
    """A throwaway admin, plus one tagged point with two samples today."""
    _cleanup()
    r = _post_with_retry(f"{BASE}/auth/orgs", json={"name": ORG_PREFIX, "admin_email": EMAIL, "admin_password": PASSWORD})
    assert r.status_code == 201, r.text
    r = _post_with_retry(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    h = {"Authorization": f"Bearer {token}"}

    marker = "probe" + uuid.uuid4().hex[:8]
    topic = f"conformance/{marker}/dat"
    r = requests.post(f"{BASE}/ingest/tags", headers=h, json={
        "point": topic,
        "tags": {marker: True, "temp": True, "sensor": True, "dis": "Conformance DAT", "unit": "°F"},
    })
    assert r.status_code in (200, 204), r.text
    now = time.time()
    r = requests.post(f"{BASE}/ingest/ingest", headers=h, json=[
        {"point": topic, "value": 71.0, "ts": now - 120},
        {"point": topic, "value": 72.5, "ts": now - 60},
    ])
    assert r.status_code == 204, r.text
    yield {"token": token, "marker": marker}
    _cleanup()


@pytest.mark.parametrize("content_type", ["json", "zinc"])
def test_phable_scram_read_and_history(probe, content_type):
    marker = probe["marker"]
    with phable.open_haystack_client(f"{BASE}/api/demo", EMAIL, PASSWORD, content_type=content_type) as c:
        about = c.about()
        assert about["productName"] == "Baler"

        rows = c.read_all(marker).rows
        assert len(rows) == 1, rows
        rec = rows[0]
        assert rec["dis"] == "Conformance DAT"
        # 72.5 from the fixture, or the other format's hisWrite if it ran first
        assert rec["curVal"].val in (72.5, 73.25)
        assert "point" in rec and "his" in rec

        assert c.read_by_id(rec["id"])["id"].val == rec["id"].val

        today = dt.datetime.now(dt.timezone.utc).date()
        his = c.his_read_by_id(rec["id"], today)
        vals = [r["val"].val for r in his.rows]
        assert 71.0 in vals and 72.5 in vals, vals

        ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(seconds=30)
        c.his_write_by_id(rec["id"], [{"ts": ts, "val": phable.Number(73.25, "°F")}])
        his = c.his_read_by_id(rec["id"], today)
        assert 73.25 in [r["val"].val for r in his.rows]


def test_oauth_style_bearer_with_session_jwt(probe):
    r = requests.get(
        f"{BASE}/api/demo/read",
        params={"filter": probe["marker"]},
        headers={"Authorization": f"Bearer {probe['token']}", "Accept": "text/zinc"},
    )
    assert r.status_code == 200, r.text
    assert r.text.startswith('ver:"3.0"') and "Conformance DAT" in r.text


def test_wrong_password_is_rejected(probe):
    with pytest.raises(phable.AuthError), phable.open_haystack_client(f"{BASE}/api/demo", EMAIL, "wrong-password"):
        pass


def test_unauthenticated_is_401():
    r = requests.get(f"{BASE}/api/demo/read", params={"filter": "point"})
    assert r.status_code == 401
