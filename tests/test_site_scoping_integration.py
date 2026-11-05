"""
Site scoping end to end through the real gateway: two orgs, each owning a
topic prefix, each seeing only its own points via /api (Haystack),
/ingest/his and /ingest/history - and the cross-org prefix-overlap guard.

Integration: needs `docker compose up -d`. Removes its orgs and sites
afterwards; that matters, because while any site owns a prefix every
other test's reads are scoped too.
"""

import time
import uuid

import pytest
import requests
from test_gateway_auth import BASE, _post_with_retry

pytestmark = pytest.mark.integration

ORG_PREFIX = "Scope Test Org"


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


def _org(label: str, prefix: str, marker: str) -> dict:
    email = f"admin-{label}@scope-test.invalid"
    r = _post_with_retry(f"{BASE}/auth/orgs", json={"name": f"{ORG_PREFIX} {label}", "admin_email": email, "admin_password": "pw-" + label * 8})
    assert r.status_code == 201, r.text
    r = _post_with_retry(f"{BASE}/auth/login", json={"email": email, "password": "pw-" + label * 8})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r = requests.post(f"{BASE}/auth/sites", headers=h, json={"name": f"site {label}", "topic_prefixes": [prefix]})
    assert r.status_code == 201, r.text
    topic = f"{prefix}/ahu-1/dat"
    r = requests.post(f"{BASE}/ingest/tags", headers=h, json={"point": topic, "tags": {marker: True, "temp": True}})
    assert r.status_code in (200, 204), r.text
    r = requests.post(f"{BASE}/ingest/ingest", headers=h, json={"point": topic, "value": 1.0, "ts": time.time() - 60})
    assert r.status_code == 204, r.text
    return {"headers": h, "topic": topic}


@pytest.fixture(scope="module")
def two_orgs():
    _cleanup()
    run = uuid.uuid4().hex[:6]
    marker = "scopeProbe" + run
    a = _org("a", f"scopetest-{run}/a", marker)
    b = _org("b", f"scopetest-{run}/b", marker)
    yield {"a": a, "b": b, "marker": marker, "run": run}
    _cleanup()


def _haystack_read(headers, flt):
    r = requests.get(f"{BASE}/api/demo/read", params={"filter": flt}, headers={**headers, "Accept": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_haystack_read_sees_only_own_site(two_orgs):
    for me, other in (("a", "b"), ("b", "a")):
        rows = _haystack_read(two_orgs[me]["headers"], two_orgs["marker"])
        ids = [str(r["id"]) for r in rows]
        assert len(rows) == 1, ids
        assert two_orgs[me]["topic"].replace("/", "~2F") in ids[0]
        assert two_orgs[other]["topic"].replace("/", "~2F") not in ids[0]


def test_history_of_another_orgs_point_is_empty(two_orgs):
    a, b = two_orgs["a"], two_orgs["b"]
    mine = requests.get(f"{BASE}/ingest/history", params={"point": a["topic"]}, headers=a["headers"])
    theirs = requests.get(f"{BASE}/ingest/history", params={"point": b["topic"]}, headers=a["headers"])
    assert mine.status_code == theirs.status_code == 200
    assert len(mine.json()) == 1 and theirs.json() == []


def test_his_query_sees_only_own_site(two_orgs):
    a = two_orgs["a"]
    r = requests.get(f"{BASE}/ingest/his", params={"expr": f"readAll({two_orgs['marker']}).hisRead(today)"}, headers=a["headers"])
    assert r.status_code == 200, r.text
    points = [s["point"] for s in r.json()["series"]]
    assert points == [a["topic"]]


def test_cannot_claim_a_prefix_overlapping_another_org(two_orgs):
    a = two_orgs["a"]
    parent = f"scopetest-{two_orgs['run']}"
    r = requests.post(f"{BASE}/auth/sites", headers=a["headers"], json={"name": "grab", "topic_prefixes": [parent]})
    assert r.status_code == 400, r.text
    assert "overlaps" in r.text
