"""
Seeds real Brick derivation and fault-rule definitions against a running
derivation_api.py (port 8003) and fault_api.py (port 8002), scoped to
exactly the equipment mix windrow.mock_hospital generates (AHUs, VAVs,
electric meters). No new engine code - every derivation/rule here is data
POSTed through the existing APIs, same shape as
derivation-api-openapi.yaml's/fault-api-openapi.yaml's own examples.

Run after ingest has flowed (mqtt_listener.py) and after
baler.autotag has run - a fault rule or derivation keyed on a Brick
class only fires against points the tagging pipeline has actually
classified into that class. This script lives inside the baler
repo/venv, so its structural setup talks to RemoteStore/ingest.py
directly rather than hand-rolling SPARQL over urllib - only the two
sibling APIs (derivation_api, fault_api) are reached over HTTP, since
that's their only interface.

Structural setup, seed_site_structure():
  - assigns each VAV equip a brick:hasPart parent AHU (round-robin over
    whichever AHU/VAV equip URIs already exist in the graph, queried
    live rather than hardcoded against windrow.mock_hospital's equipment
    counts) - nothing else in this dataset establishes VAV-to-AHU
    physical membership.
  - creates one Building entity and links every meter equip under it via
    brick:hasPart, and tags each meter equip {elec, meter} so the next
    autotag run classifies it Electrical_Meter. This is the documented
    extension point connection_manager._publish_equip_membership's own
    comment calls out ("a human or future heuristic can add this later
    via the same topic") - meter equipment gets zero tags automatically
    by design, same as every other BACnet-sourced equip.
  - same tagging for chiller/boiler/pump/exhaust-fan equip (real Haystack
    markers, real Brick classes - see rules/haystack_equip_to_brick.yaml),
    parented under the Building as central-plant equipment.
  - builds a real Site -> Building -> Floor -> Zone spatial hierarchy
    (seed_spatial_hierarchy()), one Floor per AHU and one Zone per VAV,
    reusing the exact AHU/VAV grouping above rather than inventing a
    second assignment - and links AHU/VAV to their Floor/Zone via
    brick:feeds, not just hasPart.

Timings below are demo-scaled (60-120s), not "realistic" production
values - nothing else in this project treats those numbers as load-
bearing, so there's no separate --demo flag; these are just sensible
numbers for a session you're going to actually watch.

Not implemented: short-cycling-fan detection. fault_detector.py only
expresses range/stuck/stale fault types - short-cycling is a
transition-rate check, a fourth type it doesn't have. Composing it as a
transition-counting derivation feeding a range fault rule is possible but
not built here.
ponytail: real gap, not silently worked around - add a `rate` fault type
(or the composed derivation) if short-cycling detection is ever needed.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

from rdflib import URIRef

from baler.ingest import ingest_equip_tags, link_part_of
from baler.remote_store import RemoteStore
from baler.store import BRICK as BRICK_NS

BRICK = str(BRICK_NS)
BUILDING_URI = URIRef("urn:equip:site/building-1")

# Cached at module scope - one login per script run, not one per POST.
# Both derivation_api and fault_api are behind the gateway's
# js_access-enforced /derivation//fault/ locations now (see
# gateway/nginx.conf), which need a real Bearer JWT, not the old
# write.htpasswd Basic auth.
_bearer_token: str | None = None


def _login(auth_api_url: str) -> str:
    global _bearer_token
    if _bearer_token is not None:
        return _bearer_token
    email = os.environ.get("BALER_ADMIN_EMAIL")
    password = os.environ.get("BALER_ADMIN_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "BALER_ADMIN_EMAIL/BALER_ADMIN_PASSWORD must be set - "
            "an existing admin account this script logs in as (see POST /auth/orgs "
            "in the README to create one if you don't have one yet)."
        )
    req = urllib.request.Request(
        f"{auth_api_url}/login",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Retry on 429 - the gateway's /auth/login rate limit (5r/m, burst=5,
    # gateway/nginx.conf) is tight enough that a script run shortly after
    # other login traffic (another run of this script, a test suite) can
    # get throttled; retrying with backoff is correct client behavior
    # against a real rate limit, not a workaround.
    for attempt in range(10):
        try:
            with urllib.request.urlopen(req) as resp:
                _bearer_token = json.loads(resp.read())["token"]
            return _bearer_token
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 9:
                raise
            time.sleep(8)


def _post(url: str, body: dict, auth_api_url: str) -> dict | None:
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_login(auth_api_url)}",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        body_bytes = resp.read()
        return json.loads(body_bytes) if body_bytes else None


def _equip_uris(store, topic_prefix: str) -> list[URIRef]:
    """URIRef, not bare str - RemoteStore/Store's add_relationship treats a
    plain Python str as a Literal (see remote_store._to_sparql_term), so
    passing one where a URI is expected would silently corrupt the triple
    (a Literal can't sit in subject position)."""
    rows = store.query(f"""
        SELECT DISTINCT ?equip WHERE {{
            ?equip ?p ?o .
            FILTER(STRSTARTS(STR(?equip), "urn:equip:{topic_prefix}"))
        }}
    """)
    return sorted({URIRef(row.equip) for row in rows})


def localname(uri: URIRef) -> str:
    return str(uri).rsplit("/", 1)[-1]


def _feeds(store, upstream: URIRef, downstream: URIRef) -> None:
    """brick:feeds/isFedBy - both directions explicit, same reasoning as
    ingest.link_part_of: Oxigraph doesn't do OWL reasoning, so a query
    can't lean on inverseOf unless both triples actually exist."""
    store.add_relationship(upstream, BRICK_NS.feeds, downstream)
    store.add_relationship(downstream, BRICK_NS.isFedBy, upstream)


def seed_site_structure(store) -> None:
    ahus = _equip_uris(store, "fbf/ahu-")
    vavs = _equip_uris(store, "fbf/vav-")
    if ahus and vavs:
        # Equip-level tags are never auto-derived from BACnet driver
        # metadata (connection_manager._publish_equip_membership always
        # publishes tags: {} by design - see its own docstring), so
        # without this, `ahu`/`vav` classification never fires for any
        # real hospital equipment; only leftover test-fixture equip
        # entities happen to carry those markers already.
        for ahu in ahus:
            ingest_equip_tags(store, ahu[len("urn:equip:") :], {"ahu": True})
        for vav in vavs:
            ingest_equip_tags(store, vav[len("urn:equip:") :], {"vav": True})
        for i, vav in enumerate(vavs):
            link_part_of(store, vav, ahus[i % len(ahus)])
        print(f"tagged {len(ahus)} AHUs + {len(vavs)} VAVs, linked VAVs under their AHUs")
    else:
        print("skipping AHU/VAV tagging + parenting: no ahu/vav equip found yet (has ingest run?)")

    meters = _equip_uris(store, "fbf/meter-")
    if meters:
        store.add_entity(BUILDING_URI, BRICK_NS.Building)
        for meter in meters:
            topic_prefix = meter[len("urn:equip:") :]
            ingest_equip_tags(store, topic_prefix, {"elec": True, "meter": True})
            link_part_of(store, meter, BUILDING_URI)
        print(f"created {BUILDING_URI}, tagged + linked {len(meters)} meters under it")
    else:
        print("skipping Building/meter wiring: no meter equip found yet (has ingest run?)")

    # Central plant equipment - not zone-specific, so it hangs directly off
    # the Building (a Mechanical_Room location would be more precise, but
    # that's a real corner to cut: nothing here needs to distinguish rooms
    # within the plant, only "this is central plant, not out in a zone").
    for prefix, tags in (
        ("fbf/chiller-", {"chiller": True}),
        ("fbf/boiler-", {"boiler": True}),
        ("fbf/chw-pump-", {"pump": True}),
        ("fbf/hw-pump-", {"pump": True}),
        ("fbf/ef-", {"exhaust": True, "fan": True}),
    ):
        equips = _equip_uris(store, prefix)
        for equip in equips:
            ingest_equip_tags(store, equip[len("urn:equip:") :], tags)
            link_part_of(store, equip, BUILDING_URI)
        if equips:
            print(f"tagged + linked {len(equips)} {prefix.rstrip('-').split('/')[-1]} under Building")

    seed_spatial_hierarchy(store, ahus, vavs)


# Brick's own Site/Building/Floor/Zone/Room classes are all marked
# owl:deprecated (as of Brick 1.4, in favor of RealEstateCore's rec:Site
# etc. - confirmed live against brickschema.org/schema/Brick.ttl's own
# brick:isReplacedBy triples) but are still real, still-published classes,
# not invented ones - and this project already has one live Building
# entity (BUILDING_URI, used by the electric-usage rollup) typed this way.
# Using rec: instead would mean introducing a second ontology/namespace
# for four classes; staying in one ontology and flagging the deprecation
# here is the smaller, more consistent choice for now.
def seed_spatial_hierarchy(store, ahus: list[URIRef], vavs: list[URIRef]) -> None:
    if not ahus or not vavs:
        print("skipping spatial hierarchy: no ahu/vav equip found yet (has ingest run?)")
        return

    site_uri = URIRef("urn:location:site-1")
    store.add_entity(site_uri, BRICK_NS.Site)
    link_part_of(store, BUILDING_URI, site_uri)

    # One Floor per AHU, reusing the same AHU grouping seed_site_structure
    # already assigned VAVs to (round-robin by index) - a floor's zones are
    # exactly the VAVs already parented under that floor's AHU, so this
    # doesn't invent a second, inconsistent assignment.
    floor_by_ahu: dict[URIRef, URIRef] = {}
    for ahu in ahus:
        floor_uri = URIRef(f"urn:location:floor-{localname(ahu)}")
        store.add_entity(floor_uri, BRICK_NS.Floor)
        link_part_of(store, floor_uri, BUILDING_URI)
        _feeds(store, ahu, floor_uri)
        floor_by_ahu[ahu] = floor_uri

    for i, vav in enumerate(vavs):
        ahu = ahus[i % len(ahus)]
        zone_uri = URIRef(f"urn:location:zone-{localname(vav)}")
        store.add_entity(zone_uri, BRICK_NS.Zone)
        link_part_of(store, zone_uri, floor_by_ahu[ahu])
        _feeds(store, vav, zone_uri)

    print(f"built site -> building -> {len(ahus)} floors -> {len(vavs)} zones, with AHU/VAV feeds")


DERIVATIONS = [
    {
        "name": "zone-temp-deviation",
        "kind": "formula",
        "select": (
            f"PREFIX brick: <{BRICK}> "
            "SELECT ?target ?zoneTemp ?zoneSp WHERE { "
            "?target brick:hasPoint ?zoneTemp, ?zoneSp . "
            "?zoneTemp a brick:Zone_Air_Temperature_Sensor . "
            "?zoneSp a brick:Zone_Air_Temperature_Setpoint . }"
        ),
        "target_var": "target",
        "input_vars": ["zoneTemp", "zoneSp"],
        "window_seconds": 3600,
        "interval_seconds": 60,
        "fn_source": (
            "def run(inputs, row):\n"
            "    temp = inputs.get('zoneTemp') or []\n"
            "    sp = inputs.get('zoneSp') or []\n"
            "    if not temp or not sp:\n"
            "        return None\n"
            "    return temp[-1][1] - sp[-1][1]"
        ),
        "test_cases": [
            {
                "inputs": {"zoneTemp": [["2026-01-01T00:00:00Z", 72.0]], "zoneSp": [["2026-01-01T00:00:00Z", 70.0]]},
                "row": {},
                "expected": 2.0,
            }
        ],
        "output": {"brick_class": None, "unit": "°F", "label": "Zone Air Temperature Deviation"},
        "depends_on": [],
    },
    {
        "name": "ahu-delta-t",
        "kind": "formula",
        "select": (
            f"PREFIX brick: <{BRICK}> "
            "SELECT ?target ?dischargeTemp ?returnTemp WHERE { "
            "?target a brick:Air_Handling_Unit . "
            "?target brick:hasPoint ?dischargeTemp, ?returnTemp . "
            "?dischargeTemp a brick:Discharge_Air_Temperature_Sensor . "
            "?returnTemp a brick:Return_Air_Temperature_Sensor . }"
        ),
        "target_var": "target",
        "input_vars": ["dischargeTemp", "returnTemp"],
        "window_seconds": 3600,
        "interval_seconds": 60,
        "fn_source": (
            "def run(inputs, row):\n"
            "    discharge = inputs.get('dischargeTemp') or []\n"
            "    ret = inputs.get('returnTemp') or []\n"
            "    if not discharge or not ret:\n"
            "        return None\n"
            "    return ret[-1][1] - discharge[-1][1]"
        ),
        "test_cases": [
            {
                "inputs": {
                    "dischargeTemp": [["2026-01-01T00:00:00Z", 58.0]],
                    "returnTemp": [["2026-01-01T00:00:00Z", 72.0]],
                },
                "row": {},
                "expected": 14.0,
            }
        ],
        "output": {"brick_class": None, "unit": "°F", "label": "AHU Coil Delta-T"},
        "depends_on": [],
    },
    {
        "name": "fan-duty-cycle",
        "kind": "formula",
        "select": (
            f"PREFIX brick: <{BRICK}> "
            "SELECT ?target ?fanStatus WHERE { "
            "?target brick:hasPoint ?fanStatus . "
            "?fanStatus a brick:Fan_Status . }"
        ),
        "target_var": "target",
        "input_vars": ["fanStatus"],
        "window_seconds": 3600,
        "interval_seconds": 60,
        # No sampling-interval is exposed to fn_source, so this reports a
        # fraction of samples "active" rather than claiming actual hours.
        # MQTT ingest stores BACnet active/inactive as True/False (#81);
        # history from before that is still the text.
        "fn_source": (
            "def run(inputs, row):\n"
            "    series = inputs.get('fanStatus') or []\n"
            "    if not series:\n"
            "        return None\n"
            "    active = sum(1 for _, v in series if v is True or v == 'active')\n"
            "    return active / len(series)"
        ),
        "test_cases": [
            {
                "inputs": {
                    "fanStatus": [
                        ["2026-01-01T00:00:00Z", "active"],
                        ["2026-01-01T00:05:00Z", "inactive"],
                        ["2026-01-01T00:10:00Z", True],
                        ["2026-01-01T00:15:00Z", False],
                    ]
                },
                "row": {},
                "expected": 2 / 4,
            }
        ],
        # 0..1 - Haystack has no "fraction" unit, and an unknown unit made
        # whole /api grids fail before openhay learned to drop it.
        "output": {"brick_class": None, "unit": None, "label": "Fan Duty Cycle"},
        "depends_on": [],
    },
    {
        "name": "building-total-electric-usage",
        "kind": "rollup",
        "root_select": f"PREFIX brick: <{BRICK}> SELECT ?root WHERE {{ ?root a brick:Building }}",
        "part_relationship": "brick:hasPart",
        "leaf_point_class": "brick:Electric_Energy_Sensor",
        "window_seconds": 3600,
        "interval_seconds": 60,
        # Sum, not average - this is a real "total across every meter",
        # not a synthetic stand-in metric.
        "fn_source": (
            "def run(inputs, row):\n"
            "    vals = [s[-1][1] for s in inputs if s]\n"
            "    return sum(vals) if vals else None"
        ),
        "test_cases": [
            {
                "inputs": [[["2026-01-01T00:00:00Z", 1200.0]], [["2026-01-01T00:00:00Z", 850.5]]],
                "row": {},
                "expected": 2050.5,
            }
        ],
        "output": {"brick_class": None, "unit": "kWh", "label": "Building Total Electric Usage"},
        "depends_on": [],
    },
]

FAULT_RULES = [
    {
        "name": "zone-temp-out-of-range",
        "mode": "cur",
        "type": "range",
        "applies_to": {"brick_class": "Zone_Air_Temperature_Sensor"},
        "min": 60.0,
        "max": 80.0,
    },
    {
        "name": "stuck-damper-position",
        "mode": "his",
        "type": "stuck",
        "applies_to": {"brick_class": "Damper_Position_Sensor"},
        "stuck_seconds": 120,
    },
    {
        "name": "discharge-air-temp-out-of-range",
        "mode": "cur",
        "type": "range",
        "applies_to": {"brick_class": "Discharge_Air_Temperature_Sensor"},
        "min": 50.0,
        "max": 70.0,
    },
    {
        "name": "stale-ahu-points",
        "mode": "his",
        "type": "stale",
        "applies_to": {"topic_glob": "fbf/ahu-*/*"},
        "max_age_seconds": 120,
    },
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derivation-api", default="http://localhost:8080/derivation")
    parser.add_argument("--fault-api", default="http://localhost:8080/fault")
    parser.add_argument("--auth-api", default="http://localhost:8080/auth")
    parser.add_argument("--oxigraph", default="http://localhost:7878")
    parser.add_argument("--skip-structure", action="store_true", help="skip the hasPart/equip-tagging structural setup")
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="only tag/link equipment, skip posting derivations/rules - run baler.autotag in between this and a second pass with --skip-structure, since equip tagging must land before autotag's classification sweep",
    )
    args = parser.parse_args()

    if not args.skip_structure:
        seed_site_structure(RemoteStore(args.oxigraph))
    if args.structure_only:
        return

    for derivation in DERIVATIONS:
        result = _post(f"{args.derivation_api}/derivations", derivation, args.auth_api)
        print(f"derivation {derivation['name']}: created id={result['id']}")

    for rule in FAULT_RULES:
        result = _post(f"{args.fault_api}/rules", rule, args.auth_api)
        print(f"fault rule {rule['name']}: created id={result['id']}")


if __name__ == "__main__":
    main()
