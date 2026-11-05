"""
One full, realistic user journey through the REAL gateway (localhost:8080)
with a REAL Bearer JWT throughout - unlike test_history_routes.py's
live_server/X-Gateway-Secret shortcut (which fakes gateway auth by hitting
a locally-spun-up handler directly), every HTTP call here goes through the
actual nginx container, njs JWT verification included. MQTT goes over the
real compose-managed broker, picked up by the REAL mqtt_listener/
fault_detector/derivation_engine daemon containers - not synthetic
in-process calls like test_ingest.py's/test_fault_detector.py's/
test_derivation_engine.py's own unit-style tests.

Journey: create org+admin -> login -> publish a real MQTT reading -> read
it back via GET /ingest/history -> tag equipment into the RDF graph
(RemoteStore, no auth - same as scripts/seed_derivations_and_faults.py) ->
create a formula derivation over that equipment -> wait for the real
derivation_engine daemon to compute it -> create a fault rule + webhook ->
re-publish an out-of-range reading -> confirm the real fault_detector
daemon opens a fault and delivers a signed webhook to a receiver this test
stands up.

Needs `docker compose up -d` (gateway, auth_api, ingest_api, fault_api,
fault_detector, derivation_api, derivation_engine, postgres, oxigraph,
mosquitto all healthy) with BALER_ALLOW_PRIVATE_WEBHOOKS=1 already
set for fault_api/fault_detector - see CLAUDE.md.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests
from rdflib import URIRef

from baler.derivation_engine import _output_uri
from baler.ingest import (
    ingest_equip_tags,
    link_part_of,
    link_point_to_equip,
    topic_prefix_to_equip_uri,
    topic_to_point_uri,
)
from baler.mqtt_util import make_client
from baler.remote_store import RemoteStore
from baler.store import BRICK
from baler.timeseries import connect

BASE = "http://localhost:8080"
MQTT_HOST = "localhost"
MQTT_PORT = 1883

# Distinct from other test files' org-name prefixes (test_auth.py,
# test_gateway_auth.py's "Gateway Test Org", ...) so cleanup here never
# collides with theirs.
ORG_NAME_PREFIX = "E2E Journey Test Org"
ADMIN_EMAIL = "admin@e2e-journey-test.invalid"
ADMIN_PASSWORD = "correct-horse-battery-staple-e2e"


def _cleanup_orgs():
    """Same pattern as test_gateway_auth.py's _cleanup_test_orgs - direct
    Postgres deletes keyed on this file's own org-name prefix, run both
    before and after the test."""
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
def clean_org():
    _cleanup_orgs()
    yield
    _cleanup_orgs()


@pytest.fixture
def webhook_receiver():
    """Bound to 0.0.0.0, not 127.0.0.1 - fault_api/fault_detector run
    inside Docker containers, so this receiver must be reachable from the
    Docker bridge network via host.docker.internal, not just from this
    host process. See CLAUDE.md / docs-site/pages/haxall-drop-in.mdx."""
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), Receiver)
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], received
    server.shutdown()


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


def _publish(topic: str, value, ts: float) -> None:
    client = make_client()
    client.connect(MQTT_HOST, MQTT_PORT)
    client.publish(topic, json.dumps({"value": value, "ts": ts}), retain=True)
    client.disconnect()


@pytest.mark.integration
def test_full_user_journey_through_real_gateway(clean_org, webhook_receiver):
    receiver_port, received = webhook_receiver
    # Unique per run (not just per file) - lets this test be safely
    # re-run against the shared derivations.json/rules.json without its
    # RDF/MQTT identity colliding with a previous run's leftovers.
    tag = f"e2ejrny{int(time.time() * 1000)}"
    topic_prefix = f"fbf/{tag}"
    point_topic = f"{topic_prefix}/zone-temp"
    point_uri = topic_to_point_uri(point_topic)
    equip_uri = topic_prefix_to_equip_uri(topic_prefix)
    building_uri = URIRef(f"urn:equip:{tag}-building")
    store = RemoteStore()

    token = None
    created_derivation_id = None
    created_rule_id = None
    created_webhook_id = None

    try:
        # 1. POST /auth/orgs (public) - create a new org + admin user.
        org_resp = _post_with_retry(
            f"{BASE}/auth/orgs",
            json={"name": f"{ORG_NAME_PREFIX} {tag}", "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD},
        )
        assert org_resp.status_code == 201, org_resp.text

        # 2. POST /auth/login - real Bearer token, verified by njs
        # (gateway/njs/jwt.js), used for every call below.
        login_resp = _post_with_retry(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert login_resp.status_code == 200, login_resp.text
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 3. Publish a real MQTT reading over the real broker - same wire
        # envelope mqtt_listener.py consumes (see
        # test_ingest.py::test_full_round_trip_over_real_broker). The
        # already-running mqtt_listener daemon container ingests it, not a
        # listener this test spins up itself.
        first_ts = time.time()
        _publish(point_topic, 72.0, first_ts)

        # 4. GET /ingest/history through the real gateway with the Bearer
        # token - poll for the daemon to catch up rather than sleeping a
        # fixed amount.
        items = []
        for _ in range(50):  # up to ~10s
            resp = requests.get(f"{BASE}/ingest/history", params={"point": point_topic}, headers=headers)
            assert resp.status_code == 200, resp.text
            items = resp.json()
            if items:
                break
            time.sleep(0.2)
        assert len(items) == 1
        assert items[0]["value"] == 72.0
        # Postgres timestamptz is microsecond-resolution - exact float
        # equality against time.time() is a known pre-existing flake (see
        # tests/test_history_routes.py), not something to assert on here.
        assert items[0]["ts"] == pytest.approx(first_ts, abs=0.001)

        # 5. Tag equipment into the RDF graph directly via RemoteStore -
        # same helpers/pattern as scripts/seed_derivations_and_faults.py's
        # seed_site_structure, no gateway/auth involved.
        ingest_equip_tags(store, topic_prefix, {"ahu": True, "dis": f"E2E Journey AHU {tag}"})
        link_point_to_equip(store, point_uri, equip_uri)
        store.add_entity(building_uri, BRICK.Building)
        link_part_of(store, equip_uri, building_uri)

        # 6. POST /derivation/derivations through the gateway (operator+
        # role - org admin qualifies) - a simple formula over the tagged
        # point.
        derivation_body = {
            "name": f"e2e-journey-zone-temp-plus-10-{tag}",
            "kind": "formula",
            "select": f"""
                PREFIX brick: <https://brickschema.org/schema/Brick#>
                SELECT ?target ?zoneTemp WHERE {{
                    ?target brick:hasPoint ?zoneTemp .
                    FILTER(?zoneTemp = <{point_uri}>)
                }}
            """,
            "target_var": "target",
            "input_vars": ["zoneTemp"],
            "window_seconds": 3600,
            "interval_seconds": 5,
            "fn_source": "def run(inputs, row):\n    vals = inputs.get('zoneTemp') or []\n    return vals[-1][1] + 10 if vals else None",
            "test_cases": [
                {"inputs": {"zoneTemp": [["2026-01-01T00:00:00Z", 72.0]]}, "row": {}, "expected": 82.0},
            ],
            "output": {"unit": "degF", "label": f"E2E Journey Zone Temp Plus 10 {tag}"},
            "depends_on": [],
        }
        deriv_resp = requests.post(f"{BASE}/derivation/derivations", json=derivation_body, headers=headers)
        assert deriv_resp.status_code == 201, deriv_resp.text
        created_derivation_id = deriv_resp.json()["id"]

        # No synchronous "evaluate now" endpoint (POST /derivations/dry-run
        # computes but never writes) - this instead waits on the real
        # derivation_engine daemon container, which reloads
        # derivations.json on mtime change and ticks every 5s by default
        # (see derivation_engine.py's main()), same as a real deployment.
        output_topic = f"computed/{created_derivation_id}/{tag}"
        assert str(topic_to_point_uri(output_topic)) == _output_uri(created_derivation_id, str(equip_uri))

        computed_value = None
        for _ in range(40):  # up to ~20s - a few engine ticks
            resp = requests.get(f"{BASE}/ingest/history", params={"point": output_topic}, headers=headers)
            assert resp.status_code == 200, resp.text
            history = resp.json()
            if history:
                computed_value = history[-1]["value"]
                break
            time.sleep(0.5)
        assert computed_value == 82.0

        # 7. POST /fault/rules + POST /fault/webhooks through the gateway,
        # then re-publish an out-of-range reading via MQTT (matching
        # test_fault_detector.py's cur-mode trigger pattern) and confirm
        # the real fault_detector daemon delivers a webhook to a receiver
        # in this test process, reached via host.docker.internal since
        # fault_detector runs inside a container.
        rule_body = {
            "name": f"e2e-journey-zone-temp-range-{tag}",
            "mode": "cur",
            "type": "range",
            "applies_to": {"topic_glob": f"{topic_prefix}/*"},
            "min": 60.0,
            "max": 80.0,
        }
        rule_resp = requests.post(f"{BASE}/fault/rules", json=rule_body, headers=headers)
        assert rule_resp.status_code == 201, rule_resp.text
        created_rule_id = rule_resp.json()["id"]

        webhook_body = {
            "url": f"http://host.docker.internal:{receiver_port}/",
            "secret": "e2e-journey-secret",
            # Scoped to this run's own rule - the shared webhooks.json is
            # live infra other agents' concurrent activity can also
            # trigger deliveries against; an unfiltered webhook would pick
            # up their fault events too.
            "filter": {"rule_id": created_rule_id},
        }
        webhook_resp = requests.post(f"{BASE}/fault/webhooks", json=webhook_body, headers=headers)
        assert webhook_resp.status_code == 201, webhook_resp.text
        created_webhook_id = webhook_resp.json()["id"]

        _publish(point_topic, 95.0, time.time())  # out of the rule's 60-80 range -> opens a fault

        for _ in range(50):  # up to ~10s
            if received:
                break
            time.sleep(0.2)
        assert len(received) == 1
        assert received[0]["event"] == "fault.opened"
        assert received[0]["rule_id"] == created_rule_id
        assert received[0]["point_uri"] == str(point_uri)

        faults_resp = requests.get(
            f"{BASE}/fault/faults", params={"rule_id": created_rule_id, "status": "open"}, headers=headers
        )
        assert faults_resp.status_code == 200, faults_resp.text
        faults = faults_resp.json()
        assert len(faults) == 1
        assert faults[0]["point_uri"] == str(point_uri)

    finally:
        # DELETE via the real API endpoints (not raw edits to the shared
        # rules.json/webhooks.json/derivations.json files, which other
        # agents' processes may be concurrently reading/writing) - same
        # read-modify-write mechanism the API itself uses, just invoked
        # the supported way.
        if token:
            gw_headers = {"Authorization": f"Bearer {token}"}
            if created_webhook_id:
                requests.delete(f"{BASE}/fault/webhooks/{created_webhook_id}", headers=gw_headers)
            if created_rule_id:
                requests.delete(f"{BASE}/fault/rules/{created_rule_id}", headers=gw_headers)
            if created_derivation_id:
                requests.delete(f"{BASE}/derivation/derivations/{created_derivation_id}", headers=gw_headers)

        conn = connect()
        conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"%{tag}%",))
        conn.execute("DELETE FROM faults WHERE point_uri LIKE %s", (f"%{tag}%",))
        conn.execute("DELETE FROM derivation_target_health WHERE target_uri LIKE %s", (f"%{tag}%",))
        conn.close()

        # seed_derivations_and_faults.py never cleans up its own RDF
        # writes (it's a seed script, meant to persist) - this test isn't,
        # so it removes every triple touching a URI this run created,
        # subject or object, in one scoped SPARQL DELETE WHERE via
        # RemoteStore's underlying _update (no higher-level bulk-delete
        # helper exists on RemoteStore/Store yet).
        store._update(
            f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(CONTAINS(STR(?s), "{tag}") || CONTAINS(STR(?o), "{tag}")) }}'
        )
