"""
Seeds a stack at roughly the live nash-srv size, for measuring read paths
(Baler#54, #106, #107, #142, #150): two sites, 152 equips, 766 points,
~27M history rows over the last four days, ~14k fault rows, an org with an
admin, an operator and a site-scoped viewer, and (`cx`) a commissioning
project over one site with a completed 24 h run. Synthetic data, written
straight into Oxigraph and Postgres (history by generate_series,
server-side): about an hour on a busy 4-core box, nearly all of it the
history, where HTTP ingest would take days.

Run it against a throwaway stack only: it writes under `fbf/perf-*` keys
and its own org, and leaves them there.

  python scripts/seed_perf.py seed            # graph, history, faults, logins
  python scripts/seed_perf.py live            # keep ~live ingest flowing (Ctrl-C)
  python scripts/seed_perf.py cx              # a Cx project with a completed run (prints its id)

Needs BALER_PG_DSN / OXIGRAPH_URL (defaults: the compose ports on
localhost) and, for the logins and `cx`, the gateway (--url, default
http://localhost:8080). scripts/bench_reads.py times the reads afterwards.
Logins: admin@perf.test / operator@perf.test / viewer@perf.test, password
PASSWORD below.
"""

import argparse
import hashlib
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from rdflib import RDF, Literal, URIRef

from baler import faults, mapping, timeseries
from baler.remote_store import RemoteStore
from baler.store import BRICK, HAYSTACK, TD

PASSWORD = "perf-password-123"
SITES = ("perf-north", "perf-south")
# (equip kind, equip markers, count per site, [(point, markers, unit, kind)])
AHU = [("sat", "discharge air temp sensor", "°F", "Number"), ("sat-sp", "discharge air temp sp", "°F", "Number"),
       ("rat", "return air temp sensor", "°F", "Number"), ("mat", "mixed air temp sensor", "°F", "Number"),
       ("oat", "outside air temp sensor", "°F", "Number"), ("fan-status", "discharge fan run sensor", None, "Bool"),
       ("fan-cmd", "discharge fan run cmd", None, "Bool"), ("oa-damper", "outside air damper cmd", "%", "Number"),
       ("chw-valve", "chilled water valve cmd", "%", "Number"), ("dsp", "discharge air pressure sensor", "inH₂O", "Number"),
       ("dsp-sp", "discharge air pressure sp", "inH₂O", "Number")]
VAV = [("zone-temp", "zone air temp sensor", "°F", "Number"), ("zone-sp", "zone air temp sp", "°F", "Number"),
       ("damper", "damper cmd", "%", "Number"), ("airflow", "discharge air flow sensor", "cfm", "Number")]
METER = [("demand", "elec demand sensor", "kW", "Number"), ("energy", "elec energy sensor", "kWh", "Number"),
         ("pf", "elec pf sensor", None, "Number")]
PUMP = [("run", "pump run sensor", None, "Bool"), ("cmd", "pump run cmd", None, "Bool"),
        ("speed", "pump speed cmd", "%", "Number")]
EQUIPS = (("ahu", "ahu", 9, AHU), ("vav", "vav", 59, VAV), ("meter", "elec meter", 4, METER), ("pump", "pump", 4, PUMP))
EXTRA_POINTS = 48  # loose points with no equip (a few per site), to land on 766
STEP_S = 10


def site_uri(site):
    return URIRef(f"urn:location:{site}")


def plan():
    """[(site, equip_key, markers, [(point_key, markers, unit, kind)])]"""
    out = []
    for site in SITES:
        for kind, markers, n, points in EQUIPS:
            for i in range(1, n + 1):
                out.append((site, f"fbf/{site}/{kind}-{i}", markers, points))
    return out


def point_keys():
    keys = [f"{key}/{p}" for _s, key, _m, pts in plan() for p, *_ in pts]
    keys += [f"fbf/{SITES[i % 2]}/loose-{i}" for i in range(EXTRA_POINTS)]
    return keys


def seed_graph(store):
    point_rules, equip_rules = mapping.load_rules(), mapping.load_rules("rules/haystack_equip_to_brick.yaml")
    for site in SITES:
        store.add_many([(site_uri(site), RDF.type, BRICK.Site), (site_uri(site), HAYSTACK.dis, Literal(site))])
    n_equips = n_points = 0
    for site, key, markers, points in plan():
        equip = URIRef(f"urn:equip:{key}")
        triples = [(equip, HAYSTACK.hasTag, Literal(m)) for m in ["equip", *markers.split()]]
        triples += [(equip, HAYSTACK.dis, Literal(key.rsplit("/", 1)[1].upper())), (site_uri(site), BRICK.hasPart, equip)]
        uris = []
        for name, pmarkers, unit, kind in points:
            topic = f"{key}/{name}"
            p = URIRef(f"urn:point:{topic}")
            uris.append(p)
            triples += [(p, RDF.type, TD.RawPoint), (p, TD.sourceTopic, Literal(topic)),
                        (p, HAYSTACK.dis, Literal(name)), (p, HAYSTACK.kind, Literal(kind)),
                        (equip, BRICK.hasPoint, p), (p, BRICK.isPointOf, equip)]
            triples += [(p, HAYSTACK.hasTag, Literal(m)) for m in ["point", "his", "cur", *pmarkers.split()]]
            # What a BACnet-learned point carries besides its markers, so a
            # record is about as big as a live one (~800 bytes of Hayson).
            triples += [(p, HAYSTACK[tag], Literal(value)) for tag, value in (
                ("navName", name), ("bacnetCur", f"AV{len(uris)}"), ("windrowSite", site),
                ("windrowInstance", "perf-edge-1"), ("bacnetDesc", f"{key} {name} as learned from the controller"),
                ("objectName", f"{key.rsplit('/', 1)[1].upper()}.{name.upper()}"), ("precision", 2.0),
                ("minVal", 0.0), ("maxVal", 100.0), ("covIncrement", 0.5),
                ("bacnetObjectType", "analog-value"), ("bacnetDevice", f"device,{1000 + n_equips}"),
                ("haystackId", "p:perf:r:" + hashlib.sha1(topic.encode()).hexdigest()), ("importedAt", "2026-10-20T14:03:11Z"),
                ("connStatusText", "polling every 10 s over BACnet/IP via the site edge node"),
                ("notes", f"Commissioned with {key.rsplit('/', 1)[1]}; trended at 10 s; alarms via fault rules"))]
            if unit:
                triples.append((p, HAYSTACK.unit, Literal(unit)))
        store.add_many(triples)
        mapping.classify_point(store, equip, rules=equip_rules)
        for p in uris:
            mapping.classify_point(store, p, rules=point_rules)
        n_equips += 1
        n_points += len(uris)
    for i in range(EXTRA_POINTS):
        topic = f"fbf/{SITES[i % 2]}/loose-{i}"
        p = URIRef(f"urn:point:{topic}")
        store.add_many([(p, RDF.type, TD.RawPoint), (p, TD.sourceTopic, Literal(topic)),
                        (p, HAYSTACK.hasTag, Literal("point")), (p, HAYSTACK.hasTag, Literal("his"))])
        n_points += 1
    print(f"graph: {len(SITES)} sites, {n_equips} equips, {n_points} points")


def seed_history(conn, days: float):
    keys = [f"urn:point:{k}" for k in point_keys()]
    bools = [k for k in keys if k.rsplit("/", 1)[1] in ("fan-status", "fan-cmd", "run", "cmd")]
    nums = [k for k in keys if k not in set(bools)]
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    t = start
    while t < end:  # an hour per statement keeps each transaction modest
        nxt = min(t + timedelta(hours=1), end)
        conn.execute(
            """INSERT INTO point_history (point_uri, ts, value)
               SELECT u, ts, 50 + 20 * sin(extract(epoch FROM ts) / 3600.0 + length(u)) + random()
               FROM unnest(%s::text[]) u, generate_series(%s::timestamptz, %s::timestamptz - interval '1 microsecond',
                                                          make_interval(secs => %s)) ts
               ON CONFLICT DO NOTHING""", (nums, t, nxt, STEP_S))
        conn.execute(
            """INSERT INTO point_history (point_uri, ts, value_bool)
               SELECT u, ts, (extract(hour FROM ts)::int BETWEEN 6 AND 20)
               FROM unnest(%s::text[]) u, generate_series(%s::timestamptz, %s::timestamptz - interval '1 microsecond',
                                                          make_interval(secs => %s)) ts
               ON CONFLICT DO NOTHING""", (bools, t, nxt, STEP_S))
        t = nxt
        sys.stdout.write(f"\rhistory: up to {t:%Y-%m-%d %H:%M}")
        sys.stdout.flush()
    print()
    # What the policies would have done by now on a long-running install.
    for view in ("point_history_5m", "point_history_1h"):
        conn.execute("CALL refresh_continuous_aggregate(%s, %s, %s)", (view, start - timedelta(hours=1), end - timedelta(hours=1)))
    conn.execute("ANALYZE point_history")
    n = conn.execute("SELECT count(*) FROM point_history").fetchone()[0]
    print(f"history: {n:,} rows")


def seed_faults(conn, n: int):
    faults.ensure_schema(conn)
    keys = [f"urn:point:{k}" for k in point_keys()]
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(n):
        started = now - timedelta(seconds=rng.uniform(0, 14 * 86400))
        rows.append((f"rule-{i % 9}", rng.choice(keys), "resolved", started, started + timedelta(minutes=rng.uniform(1, 240)),
                     '{"value": 140.0, "message": "out of range for longer than the rule allows"}', "warning"))
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO faults (rule_id, point_uri, status, started_at, ended_at, detail, severity) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)
        for k in rng.sample(keys, 40):  # some open ones
            cur.execute("INSERT INTO faults (rule_id, point_uri, status, started_at, detail) VALUES ('x:perf-open', %s, 'open', %s, '{}') "
                        "ON CONFLICT DO NOTHING", (k, now - timedelta(hours=3)))
    print(f"faults: {n} resolved + 40 open")


def seed_logins(url: str):
    s = requests.Session()
    r = s.post(f"{url}/auth/orgs", json={"name": "perf", "admin_email": "admin@perf.test", "admin_password": PASSWORD})
    if r.status_code not in (201, 409):
        r.raise_for_status()
    tok = s.post(f"{url}/auth/login", json={"email": "admin@perf.test", "password": PASSWORD}).json()["token"]
    s.headers["Authorization"] = f"Bearer {tok}"
    have = {x["name"]: x for x in s.get(f"{url}/auth/sites").json()}
    site_ids = []
    for site in SITES:
        x = have.get(site) or s.post(f"{url}/auth/sites", json={"name": site, "topic_prefixes": [f"fbf/{site}"]}).json()
        site_ids.append(x["id"])
    for email, role, sites in (("operator@perf.test", "operator", None), ("viewer@perf.test", "viewer", site_ids)):
        body = {"email": email, "password": PASSWORD, "role": role, **({"site_ids": sites} if sites else {})}
        r = s.post(f"{url}/auth/users", json=body)
        if r.status_code not in (201, 409):
            raise SystemExit(f"create {email}: {r.status_code} {r.text}")
    print("logins: admin@, operator@, viewer@perf.test")


def seed_cx(url: str):
    """A Cx project over perf-north (383 points) and a completed 24 h run,
    whose failures become issues: what the project report reads."""
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + s.post(f"{url}/auth/login", json={
        "email": "operator@perf.test", "password": PASSWORD}).json()["token"]
    name = "perf north pilot"
    p = next((p for p in s.get(f"{url}/cx/projects").json() if p["name"] == name), None)
    if p is None:
        phases = [("design", "Design"), ("construction", "Construction"), ("startup", "Start-up"),
                  ("functional", "Functional testing"), ("acceptance", "Acceptance")]
        p = s.post(f"{url}/cx/projects", json={"name": name, "phase": "functional", "settings": {"prefixes": [f"fbf/{SITES[0]}"]},
                                               "phases": [{"key": k, "name": n} for k, n in phases]}).json()
    r = s.post(f"{url}/cx/projects/{p['id']}/runs", json={"window_hours": 24})
    r.raise_for_status()
    run_id = r.json().get("id") or r.json().get("run_id")
    for _ in range(300):
        run = s.get(f"{url}/cx/runs/{run_id}").json()["run"]
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    opened = s.post(f"{url}/cx/runs/{run_id}/issues", json={}).json().get("opened", [])
    print(f"cx: project {p['id']}, run {run['status']}, {len(opened)} issue(s) opened")


def live(conn):
    """One row per point every STEP_S seconds, like the live ingest rate."""
    keys = [f"urn:point:{k}" for k in point_keys()]
    print(f"writing {len(keys)} points every {STEP_S}s; Ctrl-C to stop")
    while True:
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO point_history (point_uri, ts, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            [(k, now, 50 + random.random()) for k in keys])
        time.sleep(STEP_S)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["seed", "live", "cx"])
    ap.add_argument("--days", type=float, default=4.0, help="history span (4 days x 766 points x 10 s = ~26.5M rows)")
    ap.add_argument("--faults", type=int, default=14_000)
    ap.add_argument("--url", default="http://localhost:8080")
    args = ap.parse_args()
    if args.cmd == "cx":
        seed_cx(args.url)
        return
    conn = timeseries.connect()
    if args.cmd == "live":
        live(conn)
        return
    seed_graph(RemoteStore())
    seed_logins(args.url)
    seed_faults(conn, args.faults)
    seed_history(conn, args.days)


if __name__ == "__main__":
    main()
