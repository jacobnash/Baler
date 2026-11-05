"""
Live smoke test for the Haxall drop-in (fantom-his-ext's BalerHisExt +
BalerHisSettings) - the one piece of the SkySpark/Haxall migration
story with no automated coverage at all before this, see
todo/skyspark-haxall-migration-gaps.md item 1. Would have caught the dead
hardcoded-URL regression the gateway's JWT rewrite introduced.

Opt-in, same convention as test_haystack_puller.py's real-Haxall test:
skipped unless HAXALL_SU_USERNAME is set, since Haxall isn't part of this
repo's docker-compose stack. Point it at any real Haxall instance with the
"his" lib built (docs-site/pages/haxall-drop-in.mdx's "Enabling it") and
`libAdd(["his"])`'d - same HAXALL_OSS_URL/HAXALL_SU_USERNAME/
HAXALL_SU_PASSWORD env vars e2e-haxall/ and test_haystack_puller.py use.
Also needs the real gateway running (docker compose up -d) - creates its
own throwaway org/admin account per run for the operator token
BalerHisExt needs.
"""

import os
import time
import uuid

import pytest
import requests

from baler.haystack_client import HaystackClient

HAXALL_OSS_URL = os.environ.get("HAXALL_OSS_URL", "http://localhost:8280")
HAXALL_SU_USERNAME = os.environ.get("HAXALL_SU_USERNAME")
HAXALL_SU_PASSWORD = os.environ.get("HAXALL_SU_PASSWORD", "")
GATEWAY_URL = os.environ.get("BALER_GATEWAY_URL", "http://localhost:8080")


def _eval_axon(client: HaystackClient, expr: str) -> list[dict]:
    """POST-based axon eval. HaystackClient is deliberately read-only
    (about/read/hisRead - see its own docstring), so this stays local to the
    test rather than growing that class's surface for a mutating op it's not
    meant to do; it reaches into the client's already-authenticated session
    for that reason. Haxall's v4 API dispatcher requires Hayson JSON
    (`_kind`-wrapped grid, `meta.ver` set) for POST bodies - confirmed
    empirically against a real Haxall 4.0.6, not the classic format `_get`
    gets back from GET-based ops. Clearing cookies is real too: a cookie
    left over from the SCRAM handshake routes this through the
    browser-session Attest-Key check instead of plain Authorization-header
    auth, and no Attest-Key means every non-GET request gets a 400.
    """
    if not client._authenticated:
        client.about()
    client._session.cookies.clear()
    body = {"_kind": "grid", "meta": {"ver": "3.0"}, "cols": [{"name": "expr"}], "rows": [{"expr": expr}]}
    resp = client._session.post(f"{client.base_url}/eval", json=body, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()["rows"]


@pytest.fixture(scope="module")
def haxall():
    return HaystackClient(f"{HAXALL_OSS_URL}/api/sys", HAXALL_SU_USERNAME, HAXALL_SU_PASSWORD)


@pytest.fixture(scope="module")
def operator_token():
    suffix = uuid.uuid4().hex[:8]
    email = f"smoke-{suffix}@tdverify.example"
    requests.post(
        f"{GATEWAY_URL}/auth/orgs",
        json={"name": f"SmokeTest-{suffix}", "admin_email": email, "admin_password": "smoke-test-pass"},
        timeout=10,
    ).raise_for_status()
    resp = requests.post(
        f"{GATEWAY_URL}/auth/login", json={"email": email, "password": "smoke-test-pass"}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()["token"]


@pytest.mark.integration
@pytest.mark.skipif(
    not HAXALL_SU_USERNAME,
    reason="requires a real running Haxall instance with the 'his' lib built - set HAXALL_SU_USERNAME "
    "(and HAXALL_SU_PASSWORD, HAXALL_OSS_URL if not localhost:8280), same env vars "
    "test_haystack_puller.py's real-Haxall test and e2e-haxall/ use",
)
def test_haxall_his_drop_in_round_trips_through_baler(haxall, operator_token):
    """hisWrite (via a curVal/hisCollectCov transition - the same path a
    real connector uses, see fantom-windrow-conn/fan/WindrowDispatch.fan) lands in
    Baler's Postgres, and hisRead reads it back - both through
    BalerHisExt, both through the real gateway with a Bearer token."""
    loaded = {row["name"] for row in _eval_axon(haxall, "libs()")}
    for lib in ("his", "hx.point"):
        if lib not in loaded:
            _eval_axon(haxall, f'libAdd(["{lib}"])')

    _eval_axon(
        haxall,
        f'extSettingsUpdate("his", {{authToken: "{operator_token}", '
        f'baseUri: `http://host.docker.internal:8080/ingest/`}})',
    )

    dis = f"TD Smoke Test {uuid.uuid4().hex[:8]}"
    _eval_axon(
        haxall,
        f'commit(diff(null, {{point, his, cur, writable, hisCollectCov, '
        f'kind:"Number", tz:"UTC", dis:"{dis}"}}, {{add}}))',
    )
    point_id = _eval_axon(haxall, f'read(point and dis=="{dis}")')[0]["id"]["val"]

    test_val = 71.5
    _eval_axon(
        haxall,
        f'commit(diff(readById(@{point_id}), {{curVal: {test_val}, curStatus: "ok"}}, {{transient}}))',
    )

    deadline = time.time() + 15
    items = []
    while time.time() < deadline and not items:
        resp = requests.get(
            f"{GATEWAY_URL}/ingest/history",
            params={"point": point_id},
            headers={"Authorization": f"Bearer {operator_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            time.sleep(1)

    assert items, "value never landed in Baler's Postgres via the Haxall drop-in"
    assert abs(items[-1]["value"] - test_val) < 1e-6

    his_items = haxall.his_read(f"@{point_id}", "today")
    assert his_items, "hisRead returned nothing - BalerHisExt.read() round-trip failed"
    assert abs(his_items[-1]["val"] - test_val) < 1e-6
