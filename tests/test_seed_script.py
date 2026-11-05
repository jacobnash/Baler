"""
Regression test for scripts/seed_derivations_and_faults.py - a demo
script with zero coverage of its own effects until this file. Runs the
real script as a subprocess against the real gateway/derivation_api/
fault_api (same shape as test_gateway_auth.py: no in-process way to
exercise the real njs-enforced gateway, so this hits localhost:8080 for
real), then verifies its documented effects actually landed: the 4
derivations + 4 fault rules exist, and - not just "the row exists" - one
derivation (zone-temp-deviation) actually evaluates to a real computed
value when real matching graph/point_history data is present.

--skip-structure is passed deliberately: seed_site_structure() only acts
on urn:equip:fbf/ahu-*/vav-*/meter-* equipment that a real windrow ingest run
would have created, which this stack doesn't have running. Without
--skip-structure it degrades to a harmless no-op (prints "skipping..."),
so passing it just keeps this test from depending on unrelated ingest
state either way - it doesn't change what's being tested here (the
derivations/fault-rules POSTs, which don't depend on seed_site_structure
having run).
"""

import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
from rdflib import URIRef

from baler.remote_store import RemoteStore
from baler.store import BRICK
from baler.timeseries import connect, write_point_value

BASE = "http://localhost:8080"
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed_derivations_and_faults.py"

ORG_NAME_PREFIX = "Seed Script Test Org"

DERIVATION_NAMES = {"zone-temp-deviation", "ahu-delta-t", "fan-duty-cycle", "building-total-electric-usage"}
FAULT_RULE_NAMES = {"zone-temp-out-of-range", "stuck-damper-position", "discharge-air-temp-out-of-range", "stale-ahu-points"}


def _post_with_retry(url, json):
    """POST, retrying on 429. The gateway's /auth/login and /auth/orgs
    rate limits (gateway/nginx.conf, 5r/m + burst=5) are shared across
    every test file that logs in during a full-suite run - see the same
    helper in test_gateway_auth.py for the full rationale."""
    resp = None
    for attempt in range(10):
        resp = requests.post(url, json=json)
        if resp.status_code != 429:
            return resp
        if attempt < 9:
            time.sleep(8)
    return resp


def _cleanup_test_orgs():
    """Same direct-Postgres cleanup pattern as test_gateway_auth.py's
    _cleanup_test_orgs, with this file's own name prefix so the two
    don't collide when run concurrently."""
    conn = connect()
    conn.execute(f"""
        DELETE FROM user_sites WHERE user_id IN (
            SELECT id FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%')
        )
    """)
    conn.execute(f"""
        DELETE FROM revoked_tokens WHERE jti IN (
            SELECT jti FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%')
        )
    """)
    conn.execute(f"DELETE FROM api_keys WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%')")
    conn.execute(f"DELETE FROM sites WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%')")
    conn.execute(f"DELETE FROM users WHERE org_id IN (SELECT id FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%')")
    conn.execute(f"DELETE FROM orgs WHERE name LIKE '{ORG_NAME_PREFIX}%'")
    conn.close()


@pytest.fixture
def org_and_admin():
    _cleanup_test_orgs()
    unique = uuid.uuid4().hex[:8]
    email = f"admin@seed-script-test-{unique}.invalid"
    password = "correct-horse-battery-staple"
    org_resp = _post_with_retry(f"{BASE}/auth/orgs", json={"name": f"{ORG_NAME_PREFIX} {unique}", "admin_email": email, "admin_password": password})
    assert org_resp.status_code == 201, org_resp.text
    org = org_resp.json()
    login_resp = _post_with_retry(f"{BASE}/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200, login_resp.text
    token = login_resp.json()["token"]
    yield {"org": org, "token": token, "email": email, "password": password}
    _cleanup_test_orgs()


@pytest.fixture
def zone_temp_point_data():
    """Seeds exactly the graph shape scripts/seed_derivations_and_faults.py's
    'zone-temp-deviation' derivation selects for (?target brick:hasPoint a
    Zone_Air_Temperature_Sensor + a Zone_Air_Temperature_Setpoint), plus
    real point_history rows, so the derivation has something real to
    compute against - not just a row that exists and never evaluates.
    Directly asserts rdf:type via store.add_entity rather than going
    through Haystack-tag classification, same shortcut
    test_derivation_engine.py's non-e2e fixtures use."""
    unique = uuid.uuid4().hex[:8]
    target = URIRef(f"urn:equip:test-seed-script:zone-{unique}")
    temp_point = URIRef(f"urn:point:test-seed-script:zone-temp-{unique}")
    sp_point = URIRef(f"urn:point:test-seed-script:zone-sp-{unique}")

    store = RemoteStore()
    store.add_entity(temp_point, BRICK.Zone_Air_Temperature_Sensor)
    store.add_entity(sp_point, BRICK.Zone_Air_Temperature_Setpoint)
    store.add_relationship(target, BRICK.hasPoint, temp_point)
    store.add_relationship(target, BRICK.hasPoint, sp_point)

    ts_conn = connect()
    now = datetime.now(timezone.utc)
    write_point_value(ts_conn, str(temp_point), 72.0, now)
    write_point_value(ts_conn, str(sp_point), 70.0, now)

    yield {"target": str(target)}

    ts_conn.execute("DELETE FROM point_history WHERE point_uri LIKE 'urn:point:test-seed-script:%'")
    ts_conn.close()
    # Oxigraph triples (target/points) are deliberately left in place, same
    # as test_derivation_engine.py's e2e test - no delete-by-prefix helper
    # exists for RemoteStore, and unique test-prefixed URIs are harmless
    # left in the shared dev graph, matching existing repo convention.


def _run_seed_script(email: str, password: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "BALER_ADMIN_EMAIL": email, "BALER_ADMIN_PASSWORD": password}
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--skip-structure"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


@pytest.mark.integration
def test_seed_script_creates_derivations_and_fault_rules_and_one_evaluates(org_and_admin, zone_temp_point_data):
    result = _run_seed_script(org_and_admin["email"], org_and_admin["password"])
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    derivation_ids = dict(re.findall(r"derivation ([\w-]+): created id=(\S+)", result.stdout))
    rule_ids = dict(re.findall(r"fault rule ([\w-]+): created id=(\S+)", result.stdout))
    assert set(derivation_ids) == DERIVATION_NAMES, result.stdout
    assert set(rule_ids) == FAULT_RULE_NAMES, result.stdout

    headers = {"Authorization": f"Bearer {org_and_admin['token']}"}
    try:
        derivations_resp = requests.get(f"{BASE}/derivation/derivations", headers=headers)
        assert derivations_resp.status_code == 200
        derivations_by_id = {d["id"]: d for d in derivations_resp.json()}
        for name, deriv_id in derivation_ids.items():
            assert deriv_id in derivations_by_id, f"{name} ({deriv_id}) missing from GET /derivation/derivations"
            assert derivations_by_id[deriv_id]["name"] == name

        rules_resp = requests.get(f"{BASE}/fault/rules", headers=headers)
        assert rules_resp.status_code == 200
        rules_by_id = {r["id"]: r for r in rules_resp.json()}
        for name, rule_id in rule_ids.items():
            assert rule_id in rules_by_id, f"{name} ({rule_id}) missing from GET /fault/rules"
            assert rules_by_id[rule_id]["name"] == name

        # Not just "the row exists" - dry-run the seeded zone-temp-deviation
        # derivation against the real point data zone_temp_point_data wrote
        # and confirm it actually computes a real value (72.0 - 70.0 = 2.0).
        dry_run_resp = requests.post(
            f"{BASE}/derivation/derivations/dry-run", headers=headers, json={"id": derivation_ids["zone-temp-deviation"]},
        )
        assert dry_run_resp.status_code == 200, dry_run_resp.text
        trace = dry_run_resp.json()
        my_entry = next((t for t in trace if t.get("target") == zone_temp_point_data["target"]), None)
        assert my_entry is not None, f"no trace entry for our seeded target in {trace}"
        assert my_entry["computed_value"] == 2.0
    finally:
        for deriv_id in derivation_ids.values():
            requests.delete(f"{BASE}/derivation/derivations/{deriv_id}", headers=headers)
        for rule_id in rule_ids.values():
            requests.delete(f"{BASE}/fault/rules/{rule_id}", headers=headers)
