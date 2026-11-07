"""
Seeds commissioning (Cx) demo projects through the gateway, HTTP only, so a
stakeholder can walk Design -> Construction -> Start-up -> Functional ->
Acceptance -> Warranty -> Operations -> Maintenance and watch data come online
in the equipment tree, Alarms, Derivations and the Commissioning tab.

Everything it creates is named or keyed `demo-cx` (equip/point keys under
`fbf/demo-cx/<project>/...`, fault rules and derivations `demo-cx-...`, Cx
projects `demo-cx ...`) so `--cleanup` can find it. Defects are planted on
purpose and the equipment is labelled DEMO.

  python -m scripts.seed_cx_demo up                 # build everything at its end-state
  python -m scripts.seed_cx_demo advance newbuild   # move one project one phase, live
  python -m scripts.seed_cx_demo live               # keep readings flowing (Ctrl-C)
  python -m scripts.seed_cx_demo scope              # scope existing projects to their own prefix
  python -m scripts.seed_cx_demo cleanup            # admin login needed

Login: BALER_URL, BALER_EMAIL, BALER_PASSWORD. An operator can do everything
except cleanup (deleting records and Cx projects is admin-only).
"""

import argparse
import os
import random
import sys
import time

import requests

ROOT = "fbf/demo-cx"
PHASES = [("design", "Design"), ("construction", "Construction / installation"),
          ("startup", "Start-up / power-on"), ("functional", "Functional testing"),
          ("acceptance", "Acceptance"), ("warranty", "Warranty"),
          ("operations", "Operations"), ("maintenance", "Maintenance")]
STEP = 900  # seconds between backfilled samples (15 min)

# (point name, brick class, nominal value, noise) - the seven points the
# AHU Cx template expects (ontology/cx-expected-points.ttl).
AHU_POINTS = [("sat", "Supply_Air_Temperature_Sensor", 55.0, 1.0),
              ("sat-sp", "Supply_Air_Temperature_Setpoint", 55.0, 0.0),
              ("fan-status", "Fan_Status", 1.0, 0.0),
              ("fan-cmd", "Fan_Command", 1.0, 0.0),
              ("rat", "Return_Air_Temperature_Sensor", 73.0, 0.6),
              ("mat", "Mixed_Air_Temperature_Sensor", 62.0, 1.5),
              ("oat", "Outside_Air_Temperature_Sensor", 58.0, 2.0)]
ZONE_POINTS = [("zone-temp", "Zone_Air_Temperature_Sensor", 72.0, 0.5),
               ("zone-sp", "Zone_Air_Temperature_Setpoint", 72.0, 0.0)]

# slug -> (display name, kind, AHU count, phase it ends in)
PROJECTS = {"newbuild": ("Riverside Clinic (new build)", "newbuild", 2, "operations"),
            "retrofit": ("Harbor Office (retrofit)", "retrofit", 2, "operations")}
CAMPUS = 24


def prefix(slug):
    """The topic prefix a project's equipment lives under."""
    return f"{ROOT}/{slug}"


def equips(slug, n_ahu):
    """[(key, dis, brickClass, [(point, class, nominal, noise)])] for a project."""
    out = []
    for i in range(1, n_ahu + 1):
        out.append((f"{ROOT}/{slug}/ahu-{i}", f"AHU-{i} (DEMO)", "Air_Handling_Unit", AHU_POINTS))
        out.append((f"{ROOT}/{slug}/vav-{i}", f"VAV-{i} (DEMO)", "Variable_Air_Volume_Box", ZONE_POINTS))
    return out


def sample(nominal, noise, rng):
    return round(nominal + rng.uniform(-noise, noise), 2) if noise else nominal


def readings(slug, n_ahu, start, end, defects, rng):
    """Backfilled readings from start..end (epoch s). `defects` maps a point key
    suffix ('ahu-2/sat') to 'stuck' (constant 55.0; a fault rule catches it, so it
    shows in Alarms), 'range' (reads 140; also Alarms) or 'stale' (reported only
    BEFORE the window, so a Cx liveness check sees no reading in it: a point that
    stopped reporting)."""
    out = []
    for key, _dis, _cls, points in equips(slug, n_ahu):
        for name, _c, nominal, noise in points:
            defect = defects.get(f"{key.rsplit('/', 1)[1]}/{name}")
            ts, stop = start, end
            if defect == "stale":
                ts, stop = start - 48 * 3600, start - 30 * 3600
            while ts <= stop:
                value = {"stuck": 55.0, "range": 140.0}.get(defect) or sample(nominal, noise, rng)
                out.append({"point": f"{key}/{name}", "value": value, "ts": ts})
                ts += STEP
    return out


class Baler:
    def __init__(self, url, email, password):
        self.url, self.s = url.rstrip("/"), requests.Session()
        r = self.s.post(f"{self.url}/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        self.s.headers["Authorization"] = "Bearer " + r.json()["token"]

    def req(self, method, path, **kw):
        """One request, paced to stay under ~4 a second (operators get 300 a minute
        per user at the gateway, njs/ratelimit.js, shared with every session on that
        login). On a 429 stop at once: retrying while limited keeps the gateway's
        counter alive (Baler#71), so waiting out Retry-After in a loop never recovers."""
        time.sleep(0.25)
        r = self.s.request(method, self.url + path, timeout=90, **kw)
        if r.status_code == 429:
            raise SystemExit(f"{method} {path}: rate limited (429). Stop sending requests as this login "
                             f"for at least {r.headers.get('Retry-After', '60')}s of silence, then rerun; "
                             "the script is safe to rerun.")
        return r

    def call(self, method, path, ok=(200, 201, 202, 204), **kw):
        r = self.req(method, path, **kw)
        if r.status_code not in ok:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def create_records(self, slug, n_ahu):
        for key, dis, cls, points in equips(slug, n_ahu):
            r = self.req("POST", "/ingest/records", json={"kind": "equip", "key": key, "dis": dis, "brickClass": cls})
            if r.status_code not in (201, 409):  # 409: already there, fine
                raise SystemExit(f"equip {key} -> {r.status_code}: {r.text[:200]}")
            for name, pcls, _n, _x in points:
                r = self.req("POST", "/ingest/records", json={
                    "kind": "point", "key": f"{key}/{name}", "dis": f"{name} (DEMO)",
                    "brickClass": pcls, "equip": f"urn:equip:{key}"})
                if r.status_code not in (201, 409):
                    raise SystemExit(f"point {key}/{name} -> {r.status_code}: {r.text[:200]}")

    def ingest(self, items):
        for i in range(0, len(items), 500):
            self.call("POST", "/ingest/ingest", json=items[i:i + 500])

    def project(self, title, phase, slug):
        """The project for `title`, scoped to its own equipment by topic prefix
        (settings.prefixes, Baler#75): created if missing, re-scoped if it exists
        without it. Without a prefix an operator's project covers the whole org."""
        want = [prefix(slug)]
        for p in self.call("GET", "/cx/projects"):
            if p["name"] == title:
                if (p.get("settings") or {}).get("prefixes") != want:
                    p = self.call("PATCH", f"/cx/projects/{p['id']}", json={"settings": {**(p.get("settings") or {}), "prefixes": want}})
                return p
        return self.call("POST", "/cx/projects", json={
            "name": title, "phase": phase, "settings": {"prefixes": want},
            "phases": [{"key": k, "name": n} for k, n in PHASES]})

    def go_phase(self, pid, key):
        self.call("POST", f"/cx/projects/{pid}/phase", json={"key": key})

    def run(self, pid, window_hours):
        r = self.req("POST", f"/cx/projects/{pid}/runs", json={"window_hours": window_hours})
        if r.status_code not in (202, 409):  # 409: one is already going, wait for that one
            raise SystemExit(f"run -> {r.status_code}: {r.text[:200]}")
        run_id = r.json().get("id") or r.json().get("run_id")
        for _ in range(90):
            got = self.call("GET", f"/cx/runs/{run_id}")["run"]
            if got["status"] in ("completed", "failed"):
                return got
            time.sleep(2)
        raise SystemExit(f"run {run_id} still going after 3 min")


# What each phase brings, and the window the run looks at.
def apply_phase(b, slug, n_ahu, phase, rng):
    now = time.time()
    if phase == "construction":
        b.create_records(slug, n_ahu)
    elif phase == "startup":  # first readings arrive
        b.ingest(readings(slug, n_ahu, now - 2 * 3600, now, {}, rng))
    elif phase == "functional":  # a day of trends, with planted defects
        b.ingest(readings(slug, n_ahu, now - 24 * 3600, now,
                          {"ahu-2/sat": "stuck", "ahu-2/mat": "stale", "ahu-1/oat": "range"}, rng))
    elif phase in ("acceptance", "warranty", "operations", "maintenance"):  # defects fixed
        b.ingest(readings(slug, n_ahu, now - 2 * 3600, now, {}, rng))


def install_ops(b):
    """Fault rules + a derivation scoped to the demo prefix, once."""
    have = {r["name"] for r in b.call("GET", "/fault/rules")}
    for rule in ({"name": "demo-cx-sat-range", "mode": "cur", "type": "range",
                  "applies_to": {"topic_glob": f"{ROOT}/*/ahu-*/sat"}, "min": 45.0, "max": 65.0},
                 {"name": "demo-cx-oat-range", "mode": "cur", "type": "range",
                  "applies_to": {"topic_glob": f"{ROOT}/*/ahu-*/oat"}, "min": -20.0, "max": 120.0},
                 {"name": "demo-cx-stuck", "mode": "his", "type": "stuck",
                  "applies_to": {"topic_glob": f"{ROOT}/*/ahu-*/sat"}, "stuck_seconds": 3600},
                 {"name": "demo-cx-stale", "mode": "his", "type": "stale",
                  "applies_to": {"topic_glob": f"{ROOT}/*/ahu-*/mat"}, "max_age_seconds": 1800}):
        if rule["name"] not in have:
            b.call("POST", "/fault/rules", json=rule)


def cmd_up(b, rng):
    install_ops(b)
    for slug, (title, _kind, n_ahu, end) in PROJECTS.items():
        pid = b.project(title, "design", slug)["id"]
        for key, _ in PHASES[1:PHASES.index(next(p for p in PHASES if p[0] == end)) + 1]:
            apply_phase(b, slug, n_ahu, key, rng)
            b.go_phase(pid, key)
            if key in ("functional", "acceptance", "warranty"):
                got = b.run(pid, 24 if key == "functional" else 1)
                print(f"{title}: {key} run ->", (got or {}).get("status"), (got or {}).get("summary"))
        print(f"{title}: ended in {end}")
    for i in range(1, CAMPUS + 1):  # the campus: many small projects at mixed phases
        slug, phase = f"campus-{i:02d}", rng.choice(PHASES[1:7])[0]
        title = f"demo-cx campus building {i:02d}"
        pid = b.project(title, "design", slug)["id"]
        b.create_records(slug, 1)
        defects = rng.choice([{}, {}, {"ahu-1/sat": "stuck"}, {"ahu-1/mat": "stale"}])
        now = time.time()
        b.ingest(readings(slug, 1, now - 2 * 3600, now, defects, rng))
        b.go_phase(pid, phase)
    print(f"campus: {CAMPUS} projects")


def cmd_scope(b):
    """Scope every existing demo project to its own prefix; no data is written."""
    for slug, (title, *_rest) in PROJECTS.items():
        b.project(title, "design", slug)
    for i in range(1, CAMPUS + 1):
        b.project(f"demo-cx campus building {i:02d}", "design", f"campus-{i:02d}")
    print("projects scoped to their own prefixes")


def cmd_advance(b, slug, rng):
    title, _k, n_ahu, _e = PROJECTS[slug]
    p = b.project(title, "design", slug)
    keys = [k for k, _ in PHASES]
    cur = next(x["key"] for x in p["phases"] if x["status"] == "active")
    nxt = keys[min(keys.index(cur) + 1, len(keys) - 1)]
    apply_phase(b, slug, n_ahu, nxt, rng)
    b.go_phase(p["id"], nxt)
    print(f"{title}: {cur} -> {nxt}")


def cmd_live(b, rng):
    install_ops(b)
    print("posting a fresh round of readings every 60s; Ctrl-C to stop")
    while True:
        now = time.time()
        items = []
        for slug in [*PROJECTS, *(f"campus-{i:02d}" for i in range(1, CAMPUS + 1))]:
            n_ahu = PROJECTS[slug][2] if slug in PROJECTS else 1
            items += readings(slug, n_ahu, now, now, {}, rng)
        b.ingest(items)
        time.sleep(60)


def cmd_cleanup(b):
    for r in b.call("GET", "/fault/rules"):
        if r["name"].startswith("demo-cx"):
            b.call("DELETE", f"/fault/rules/{r['id']}")
    for p in b.call("GET", "/cx/projects"):
        if p["name"].startswith(("demo-cx", "Riverside Clinic", "Harbor Office")):
            b.call("DELETE", f"/cx/projects/{p['id']}")
    for slug, (_t, _k, n_ahu, _e) in {**PROJECTS, **{f"campus-{i:02d}": (0, 0, 1, 0) for i in range(1, CAMPUS + 1)}}.items():
        for key, _d, _c, points in equips(slug, n_ahu):
            for name, *_ in points:
                b.call("DELETE", f"/ingest/records?id=urn:point:{key}/{name}", ok=(204, 404))
            b.call("DELETE", f"/ingest/records?id=urn:equip:{key}", ok=(204, 404))
    print("cleaned up (history rows stay by design)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "advance", "live", "scope", "cleanup"])
    ap.add_argument("project", nargs="?", choices=list(PROJECTS))
    ap.add_argument("--seed", type=int, default=7, help="same seed, same demo data")
    a = ap.parse_args()
    b = Baler(os.environ["BALER_URL"], os.environ["BALER_EMAIL"], os.environ["BALER_PASSWORD"])
    rng = random.Random(a.seed)
    {"up": lambda: cmd_up(b, rng), "advance": lambda: cmd_advance(b, a.project or sys.exit("advance needs a project"), rng),
     "live": lambda: cmd_live(b, rng), "scope": lambda: cmd_scope(b), "cleanup": lambda: cmd_cleanup(b)}[a.cmd]()


if __name__ == "__main__":
    main()
