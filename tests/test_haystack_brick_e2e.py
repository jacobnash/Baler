"""
Full Haystack/Xeto -> Brick -> validate -> fault -> webhook journey - the
gap test_e2e_journey.py leaves open (that file covers MQTT-ingest -> tag ->
derivation -> fault -> webhook, but never haystack_puller, ontology loading
into the live Oxigraph, or SHACL validation). Real containers throughout: a
fake Haystack server (baler.fake_haystack_server, the same one
scripts/mock_haystack_server.py exposes standalone) stands in for a real
Haxall/SkySpark deployment, real gateway/Bearer auth, real fault_detector
daemon container.

haystack_client.py never sends `Xeto-Version: 5`, so it already pulls
identically from Xeto-backed and classic Haystack servers (see
haystack-puller.mdx's "One-time migration" section) - nothing Xeto-specific
to exercise beyond what this test already covers by pulling through the
same client.

His mode (not Cur) is the only way to fault on pulled data: haystack_puller
writes straight to Oxigraph/Postgres, never through MQTT, so a Cur-mode
rule (which only evaluates live MQTT messages) would never see it. Only
`stuck`/`stale` run in His mode - this uses `stale` against a seeded
history row with an already-old timestamp, so the very first His-mode
sweep after the pull opens a fault.

Needs `docker compose up -d` (gateway, auth_api, fault_api, fault_detector,
validate_api, postgres, oxigraph, mosquitto all healthy) with
BALER_ALLOW_PRIVATE_WEBHOOKS=1 set for fault_api/fault_detector -
see CLAUDE.md.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import requests

from baler.fake_haystack_server import (
    TEST_PASSWORD,
    TEST_USER,
    make_fake_haxall_server,
)
from baler.haystack_client import HaystackClient
from baler.haystack_pull_state import ensure_schema
from baler.haystack_puller import pull_once
from baler.ingest import topic_to_point_uri
from baler.remote_store import RemoteStore
from baler.timeseries import connect, read_latest

BASE = "http://localhost:8080"
BRICK_TTL = os.path.join(os.path.dirname(__file__), "..", "ontology", "Brick-only.ttl")

ORG_NAME_PREFIX = "E2E Haystack Brick Test Org"
ADMIN_EMAIL = "admin@e2e-haystack-brick-test.invalid"
ADMIN_PASSWORD = "correct-horse-battery-staple-hsbrick"


def _cleanup_orgs():
    """Same pattern as test_e2e_journey.py's _cleanup_orgs, scoped to this
    file's own org-name prefix so cleanup never collides with theirs."""
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
    """Bound to 0.0.0.0, not 127.0.0.1 - fault_detector runs inside a
    Docker container, so this receiver must be reachable from the Docker
    bridge network via host.docker.internal. Same fixture as
    test_e2e_journey.py's - not shared via conftest.py, per this repo's
    existing no-shared-fixtures convention."""
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
    """POST, retrying on 429 - the gateway's /auth/login and /auth/orgs
    rate limits are shared across every test file that logs in during a
    full-suite run. Same helper as test_e2e_journey.py/test_gateway_auth.py."""
    resp = None
    for attempt in range(10):
        resp = requests.post(url, json=json)
        if resp.status_code != 429:
            return resp
        if attempt < 9:
            time.sleep(8)
    return resp


@pytest.mark.integration
def test_haystack_pull_through_brick_classification_to_fault_webhook(clean_org, webhook_receiver):
    receiver_port, received = webhook_receiver
    tag = f"e2ehsbrick{int(time.time() * 1000)}"
    source_id = tag

    equip_ref = f"ahu-{tag}"
    zone_temp_ref = f"zone-temp-{tag}"
    mystery_ref = f"mystery-{tag}"

    server, _ = make_fake_haxall_server(
        {
            "read": {
                "equip": {"rows": [{"id": f"r:{equip_ref} AHU", "dis": "AHU", "ahu": "m:"}]},
                "point": {
                    "rows": [
                        {
                            "id": f"r:{zone_temp_ref} Zone Temp",
                            "dis": "Zone Temp",
                            "zone": "m:",
                            "air": "m:",
                            "temp": "m:",
                            "sensor": "m:",
                            "equipRef": f"r:{equip_ref}",
                        },
                        {
                            "id": f"r:{mystery_ref} Mystery",
                            "dis": "Mystery",
                            "totallyUnknownTag": "m:",
                            "equipRef": f"r:{equip_ref}",
                        },
                    ]
                },
            },
            "hisRead": {
                # A fixed, already-old timestamp - not "now minus a few
                # seconds" - so the very first His-mode stale sweep after
                # the pull fires without this test racing a clock.
                f"@{zone_temp_ref}": {"rows": [{"ts": "t:2026-08-20T00:00:00Z UTC", "val": "n:71.5"}]},
                f"@{mystery_ref}": {"rows": []},
            },
        }
    )

    store = RemoteStore()
    ts_conn = connect()
    ensure_schema(ts_conn)

    token = None
    created_rule_id = None
    created_webhook_id = None

    try:
        # 1. Load Brick into the live Oxigraph - idempotent PUT into its
        # own named graph (RemoteStore.load_ontology), proving the
        # architecture.mdx "not wired up by default" gap is actually
        # closeable, not just theoretically fixed.
        store.load_ontology(BRICK_TTL)

        # 2. Pull equip+points+history from the fake Haystack server
        # through the real HaystackClient (real SCRAM handshake) and the
        # real haystack_puller pipeline.
        client = HaystackClient(f"http://127.0.0.1:{server.server_address[1]}", TEST_USER, TEST_PASSWORD)
        result = pull_once(client, store, ts_conn, source_id, "point", "equip", backfill_days=30.0)
        assert result["equip"] == {"direct": 1}
        assert result["points"] == {"direct": 1, "miss": 1}

        # 3. SPARQL-query the live store: the pulled point carries a real
        # Brick class AND that class resolves against the ontology graph
        # just loaded in step 1 (not just a class-name string with nothing
        # backing it).
        zone_temp_uri = topic_to_point_uri(f"haystack/{source_id}/{zone_temp_ref}")
        rows = list(store.query(f"""
            PREFIX brick: <https://brickschema.org/schema/Brick#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT ?point WHERE {{
                ?point a brick:Zone_Air_Temperature_Sensor .
                brick:Zone_Air_Temperature_Sensor rdfs:subClassOf* brick:Point .
                FILTER(STR(?point) = "{zone_temp_uri}")
            }}
        """))
        assert len(rows) == 1

        # 4. History landed in Postgres.
        assert read_latest(ts_conn, str(zone_temp_uri)) == 71.5

        # 5. Real org/login through the real gateway, then POST
        # /validate/validate with the Bearer token (not Basic Auth -
        # validate.mdx's stale example fixed alongside this test).
        org_resp = _post_with_retry(
            f"{BASE}/auth/orgs",
            json={"name": f"{ORG_NAME_PREFIX} {tag}", "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD},
        )
        assert org_resp.status_code == 201, org_resp.text
        login_resp = _post_with_retry(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert login_resp.status_code == 200, login_resp.text
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        validate_resp = requests.post(f"{BASE}/validate/validate", headers=headers)
        assert validate_resp.status_code == 200, validate_resp.text
        validation = validate_resp.json()
        # Not asserting `conforms` either way - minimally-seeded test data
        # legitimately might not satisfy every Brick shape (see validate.mdx:
        # "genuinely possible to see go red on real data"). What matters
        # here is that the real gateway + Bearer auth + validate_api round
        # trip actually works and returns the documented shape.
        assert isinstance(validation.get("conforms"), bool)
        assert isinstance(validation.get("violations"), list)

        # 6. His-mode fault rule (topic_glob matches the urn:point:{topic}
        # URI in His mode, not the raw topic) + webhook, scoped to this
        # run's rule so other concurrent activity against the shared
        # webhooks.json can't trigger spurious deliveries here.
        rule_body = {
            "name": f"e2e-hsbrick-stale-{tag}",
            "mode": "his",
            "type": "stale",
            # Raw topic pattern, same as a Cur-mode glob - fault_detector.py
            # prepends "urn:point:" internally for the His-mode roster
            # match (see rules.json's own his-mode examples); a caller
            # writing the "urn:point:" prefix themselves double-prefixes it
            # and the rule silently never matches anything.
            "applies_to": {"topic_glob": f"haystack/{source_id}/*"},
            "max_age_seconds": 3600,
        }
        rule_resp = requests.post(f"{BASE}/fault/rules", json=rule_body, headers=headers)
        assert rule_resp.status_code == 201, rule_resp.text
        created_rule_id = rule_resp.json()["id"]

        webhook_body = {
            "url": f"http://host.docker.internal:{receiver_port}/",
            "secret": "e2e-hsbrick-secret",
            "filter": {"rule_id": created_rule_id},
        }
        webhook_resp = requests.post(f"{BASE}/fault/webhooks", json=webhook_body, headers=headers)
        assert webhook_resp.status_code == 201, webhook_resp.text
        created_webhook_id = webhook_resp.json()["id"]

        # 7. Wait for the real fault_detector daemon's periodic His-mode
        # sweep (default every 30s) to pick up the already-stale reading
        # and deliver a signed webhook.
        for _ in range(90):  # up to ~45s
            if received:
                break
            time.sleep(0.5)
        assert len(received) >= 1, "fault_detector's His-mode sweep never delivered a webhook for the pulled point"
        assert received[0]["event"] == "fault.opened"
        assert received[0]["rule_id"] == created_rule_id
        assert received[0]["point_uri"] == str(zone_temp_uri)

    finally:
        server.shutdown()
        if token:
            gw_headers = {"Authorization": f"Bearer {token}"}
            if created_webhook_id:
                requests.delete(f"{BASE}/fault/webhooks/{created_webhook_id}", headers=gw_headers)
            if created_rule_id:
                requests.delete(f"{BASE}/fault/rules/{created_rule_id}", headers=gw_headers)

        ts_conn.execute("DELETE FROM point_history WHERE point_uri LIKE %s", (f"%{tag}%",))
        ts_conn.execute("DELETE FROM haystack_pull_checkpoint WHERE point_uri LIKE %s", (f"%{tag}%",))
        ts_conn.execute("DELETE FROM faults WHERE point_uri LIKE %s", (f"%{tag}%",))
        ts_conn.close()

        # Scoped SPARQL DELETE WHERE, same pattern as test_e2e_journey.py -
        # the Brick ontology graph loaded in step 1 has no triples
        # containing this run's tag, so it's untouched (deliberately left
        # in place: shared, idempotent, not per-run state).
        store._update(
            f'DELETE {{ ?s ?p ?o }} WHERE {{ ?s ?p ?o . FILTER(CONTAINS(STR(?s), "{tag}") || CONTAINS(STR(?o), "{tag}")) }}'
        )
