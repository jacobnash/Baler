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


def _seed_faults_and_targets(two_orgs):
    """A fault and a derivation target on each org's point, straight into
    Postgres (what fault_detector/derivation_engine would write)."""
    from datetime import datetime, timezone

    from baler import derivation_health, faults
    from baler.timeseries import connect

    conn = connect()
    derivation_id = f"scope-{two_orgs['run']}"
    ids = {}
    for k in ("a", "b"):
        uri = f"urn:point:{two_orgs[k]['topic']}"
        ids[k] = faults.open_fault(conn, f"scope-rule-{two_orgs['run']}", uri, datetime.now(timezone.utc))
        derivation_health.record_target_failure(conn, derivation_id, f"urn:equip:{two_orgs[k]['topic'].rsplit('/', 1)[0]}", "boom")
    conn.close()
    return ids, derivation_id


@pytest.fixture(scope="module")
def seeded(two_orgs):
    from baler.timeseries import connect

    ids, derivation_id = _seed_faults_and_targets(two_orgs)
    yield ids, derivation_id
    conn = connect()
    conn.execute("DELETE FROM faults WHERE rule_id = %s", (f"scope-rule-{two_orgs['run']}",))
    conn.execute("DELETE FROM derivation_target_health WHERE derivation_id = %s", (derivation_id,))
    conn.close()


def test_faults_list_and_ack_are_scoped(two_orgs, seeded):
    ids, _ = seeded
    a = two_orgs["a"]
    r = requests.get(f"{BASE}/fault/faults", params={"rule_id": f"scope-rule-{two_orgs['run']}"}, headers=a["headers"])
    assert r.status_code == 200, r.text
    assert [f["id"] for f in r.json()] == [ids["a"]]
    # Another org's fault is a 404, exactly like a missing one.
    assert requests.post(f"{BASE}/fault/faults/{ids['b']}/ack", headers=a["headers"]).status_code == 404
    assert requests.post(f"{BASE}/fault/faults/{ids['b']}/snooze", headers=a["headers"], json={"minutes": 5}).status_code == 404
    assert requests.post(f"{BASE}/fault/faults/{ids['a']}/ack", headers=a["headers"]).status_code == 204


def test_derivation_targets_are_scoped(two_orgs, seeded):
    _, derivation_id = seeded
    a, b = two_orgs["a"], two_orgs["b"]
    r = requests.get(f"{BASE}/derivation/derivations/{derivation_id}/targets", headers=a["headers"])
    assert r.status_code == 200, r.text
    assert [t["target_uri"] for t in r.json()] == [f"urn:equip:{a['topic'].rsplit('/', 1)[0]}"]
    theirs = requests.utils.quote(f"urn:equip:{b['topic'].rsplit('/', 1)[0]}", safe="")
    r = requests.post(f"{BASE}/derivation/derivations/{derivation_id}/targets/{theirs}/enable", headers=a["headers"])
    assert r.status_code == 404
    # ...while the same call on its own target goes through (so the 404 above is the scope check).
    mine = requests.utils.quote(f"urn:equip:{a['topic'].rsplit('/', 1)[0]}", safe="")
    r = requests.post(f"{BASE}/derivation/derivations/{derivation_id}/targets/{mine}/enable", headers=a["headers"])
    assert r.status_code == 200, r.text


def test_validate_reports_only_own_sites_violations(two_orgs):
    a, b = two_orgs["a"], two_orgs["b"]
    r = requests.post(f"{BASE}/validate/validate", headers=a["headers"])
    assert r.status_code == 200, r.text
    nodes = [v["focus_node"] for v in r.json()["violations"]]
    assert not any(b["topic"] in n for n in nodes), nodes
    assert all(a["topic"].split("/")[0] in n for n in nodes), nodes
