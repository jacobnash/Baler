"""
Seeds commissioning (Cx) demo projects through the gateway, HTTP only, so a
stakeholder can walk Design -> Construction -> Start-up -> Functional ->
Acceptance -> Warranty -> Operations -> Maintenance and watch data come online
in the equipment tree, Alarms and the Commissioning tab.

Everything it creates is named or keyed `demo-cx` (equip/point keys under
`fbf/demo-cx/<project>/...`, fault rules `demo-cx-...`, Cx
projects `demo-cx ...`) so `--cleanup` can find it. Defects are planted on
purpose and the equipment is labelled DEMO.

Issues follow the server's rules (Baler#86): a passing run only *resolves*
an issue (ready_for_retest), a later run over data from after the fix
records the retest, and only a person closes it. The new build's issues are
closed by this script as the logged-in user, with a note saying so; the
retrofit is left in functional testing with its issues resolved and
retested, for you to close and then `advance retrofit` into acceptance. Both
projects use blocking phase gates (settings.phase_gates "block").

  python -m scripts.seed_cx_demo up                 # build everything at its end-state
  python -m scripts.seed_cx_demo advance newbuild   # move one project one phase, live
  python -m scripts.seed_cx_demo live               # keep readings flowing (Ctrl-C)
  python -m scripts.seed_cx_demo scope              # scope existing projects to their own prefix
  python -m scripts.seed_cx_demo report newbuild    # save the project's Cx report as HTML
  python -m scripts.seed_cx_demo cleanup            # admin login needed

Login: BALER_URL, BALER_EMAIL, BALER_PASSWORD. An operator can do everything
except cleanup (deleting records and Cx projects is admin-only).
"""

import argparse
import os
import random
import sys
import time
from urllib.parse import urlsplit

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
# The VAV template also expects a damper position and an airflow point. They
# are left out at construction (an installation gap the functional run finds)
# and installed at acceptance, which is the fix that resolves those issues.
VAV_FIX_POINTS = [("damper-pos", "Damper_Position_Command", 45.0, 5.0),
                  ("airflow", "Air_Flow_Sensor", 400.0, 30.0)]
DEFECTS = {"ahu-2/sat": "stuck", "ahu-2/mat": "stale", "ahu-1/oat": "range"}
GATED = {"phase_gates": "block"}  # the walkthrough projects: an unmet gate refuses the move
RETEST_SECONDS = 120  # the retest window: data from after the fix only

# slug -> (display name, kind, AHU count, phase `up` takes it to, whether
# `up` closes its retested issues; False leaves that to a person)
PROJECTS = {"newbuild": ("Riverside Clinic (new build)", "newbuild", 2, "operations", True),
            "retrofit": ("Harbor Office (retrofit)", "retrofit", 2, "acceptance", False)}
CAMPUS = 24


def prefix(slug):
    """The topic prefix a project's equipment lives under."""
    return f"{ROOT}/{slug}"


def equips(slug, n_ahu, fixed=False):
    """[(key, dis, brickClass, [(point, class, nominal, noise)])] for a project;
    `fixed`: with the VAV points installed at acceptance."""
    out = []
    for i in range(1, n_ahu + 1):
        out.append((f"{ROOT}/{slug}/ahu-{i}", f"AHU-{i} (DEMO)", "Air_Handling_Unit", AHU_POINTS))
        out.append((f"{ROOT}/{slug}/vav-{i}", f"VAV-{i} (DEMO)", "Variable_Air_Volume_Box",
                    ZONE_POINTS + (VAV_FIX_POINTS if fixed else [])))
    return out


def sample(nominal, noise, rng):
    return round(nominal + rng.uniform(-noise, noise), 2) if noise else nominal


def readings(slug, n_ahu, start, end, defects, rng, fixed=False, step=STEP):
    """Backfilled readings from start..end (epoch s), one every `step` seconds.
    `defects` maps a point key suffix ('ahu-2/sat') to 'stuck' (constant 55.0; a
    fault rule catches it, so it shows in Alarms), 'range' (a constant 140:
    liveness fails it as flat) or 'stale' (reported only BEFORE the window, so a Cx liveness check
    sees no reading in it: a point that stopped reporting)."""
    out = []
    for key, _dis, _cls, points in equips(slug, n_ahu, fixed):
        for name, _c, nominal, noise in points:
            defect = defects.get(f"{key.rsplit('/', 1)[1]}/{name}")
            ts, stop = start, end
            if defect == "stale":
                ts, stop = start - 48 * 3600, start - 30 * 3600
            while ts <= stop:
                value = {"stuck": 55.0, "range": 140.0}.get(defect) or sample(nominal, noise, rng)
                out.append({"point": f"{key}/{name}", "value": value, "ts": ts})
                ts += step
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
        login). On a 429 stop at once rather than retry: the limit is shared with
        that login's other sessions, and a server older than the Baler#71 fix
        keeps the counter alive for as long as requests keep arriving."""
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

    def create_records(self, slug, n_ahu, fixed=False):
        for key, dis, cls, points in equips(slug, n_ahu, fixed):
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

    def project(self, title, phase, slug, settings=None):
        """The project for `title`, scoped to its own equipment by topic prefix
        (settings.prefixes, Baler#75) plus any `settings` given: created if
        missing, patched if it exists without them. Without a prefix an
        operator's project covers the whole org."""
        want = {"prefixes": [prefix(slug)], **(settings or {})}
        for p in self.call("GET", "/cx/projects"):
            if p["name"] == title:
                have = p.get("settings") or {}
                if any(have.get(k) != v for k, v in want.items()):
                    p = self.call("PATCH", f"/cx/projects/{p['id']}", json={"settings": {**have, **want}})
                return p
        return self.call("POST", "/cx/projects", json={
            "name": title, "phase": phase, "settings": want,
            "phases": [{"key": k, "name": n} for k, n in PHASES]})

    def go_phase(self, pid, key):
        """Move the project. A blocking gate that isn't met answers 409 with
        what it lists; stop there rather than override it."""
        r = self.req("POST", f"/cx/projects/{pid}/phase", json={"key": key})
        if r.status_code == 409:
            raise SystemExit(f"the {key} gate isn't met:\n{gate_text(r.json().get('gate') or {})}")
        if r.status_code != 200:
            raise SystemExit(f"phase {key} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def gate(self, pid, key):
        """Preview the gate into `key` without moving."""
        return self.call("GET", f"/cx/projects/{pid}/gate", params={"to": key})["gate"]

    def open_issues(self, run_id):
        """An issue for every failing result of the run not already tracked."""
        return self.call("POST", f"/cx/runs/{run_id}/issues", json={})

    def issues(self, pid, *statuses):
        return self.call("GET", f"/cx/projects/{pid}/issues", params={"status": ",".join(statuses)} if statuses else None)

    def close(self, issue_id, note):
        """A person closing a resolved, retested issue (the API refuses
        anything else for an issue tied to a check)."""
        return self.call("PATCH", f"/cx/issues/{issue_id}", json={"status": "closed", "note": note})

    def run(self, pid, window_hours):
        r = self.req("POST", f"/cx/projects/{pid}/runs", json={"window_hours": window_hours})
        if r.status_code not in (202, 409):  # 409: one is already going, wait for that one
            raise SystemExit(f"run -> {r.status_code}: {r.text[:200]}")
        run_id = r.json().get("id") or r.json().get("run_id")
        for _ in range(90):  # (on a 409 this waits for the other run, then returns that one)
            got = self.call("GET", f"/cx/runs/{run_id}")["run"]
            if got["status"] in ("completed", "failed"):
                return got
            time.sleep(2)
        raise SystemExit(f"run {run_id} still going after 3 min")


def gate_text(gate):
    """The unmet part of a gate, one line per rule."""
    return "\n".join(f"  - {u['rule']}: {u['message']}" for u in gate.get("unmet", [])) or "  (met)"


def accept(b, pid, slug, n_ahu, rng, close):
    """Take a project from functional testing into acceptance the way the
    server's issue rules allow (Baler#86): fix, a run that resolves, a retest
    over data from after the fix, a person closes, then the gate. Returns
    True once the project is in acceptance; False when issues still wait
    for a person (or for a fix that didn't take), with the gate printed."""
    if b.issues(pid, "open", "in_progress"):
        # The fix: the missing VAV points are installed and every sensor
        # reports healthy values. A passing run *resolves* the issues it
        # judged (ready_for_retest); it never closes them.
        b.create_records(slug, n_ahu, fixed=True)
        now = time.time()
        b.ingest(readings(slug, n_ahu, now - 2 * 3600, now, {}, rng, fixed=True))
        got = b.run(pid, 1)
        print(f"  fix run -> {got.get('status')}: issues resolved, pending a retest")
        # The retest: a run whose whole window comes after the fix, so it
        # rests on new data. Wait it out, then send a short burst of readings.
        time.sleep(RETEST_SECONDS + 15)
        now = time.time()
        b.ingest(readings(slug, n_ahu, now - RETEST_SECONDS + 10, now, {}, rng, fixed=True, step=15))
        got = b.run(pid, RETEST_SECONDS / 3600)
        print(f"  retest run -> {got.get('status')}")
    waiting = b.issues(pid, "ready_for_retest")
    if close:
        for issue in waiting:
            if issue.get("retest"):
                b.close(issue["id"], "Closed by scripts/seed_cx_demo.py as this login after its passing retest "
                                     "(demo data).")
    left = b.issues(pid, "open", "in_progress", "ready_for_retest")
    if left:
        retested = sum(1 for i in left if i["status"] == "ready_for_retest" and i.get("retest"))
        print(f"  {len(left)} issue(s) not closed ({retested} resolved and retested, waiting for a person to close "
              f"them in the Issues tab). The acceptance gate says:\n{gate_text(b.gate(pid, 'acceptance'))}")
        return False
    b.ingest(readings(slug, n_ahu, time.time() - 2 * 3600, time.time(), {}, rng, fixed=True))
    got = b.run(pid, 1)  # the acceptance record: the gate judges the latest run
    print(f"  acceptance run -> {got.get('status')}")
    b.go_phase(pid, "acceptance")
    return True


def enter(b, pid, slug, n_ahu, phase, rng, close=False):
    """Move one project into `phase`, doing what that move needs first (the
    gates check the records) and what happens in the phase after. Returns
    False if it stopped short (acceptance waiting on a person)."""
    now = time.time()
    if phase == "construction":  # equipment and points are installed
        b.go_phase(pid, phase)
        b.create_records(slug, n_ahu)
    elif phase == "startup":
        # Point-to-point checkout: first readings, and the run the gate asks
        # for. Backdated a day, so the functional run's 24 h window holds only
        # the trend test and its planted defects.
        b.ingest(readings(slug, n_ahu, now - 28 * 3600, now - 26 * 3600, {}, rng))
        b.run(pid, 30)
        b.go_phase(pid, phase)
    elif phase == "functional":  # a day of trends with planted defects; failures become issues
        b.go_phase(pid, phase)
        b.ingest(readings(slug, n_ahu, now - 24 * 3600, now, DEFECTS, rng))
        got = b.run(pid, 24)
        opened = b.open_issues(got["id"])
        print(f"  functional run -> {got.get('status')}: {len(opened['opened'])} issue(s) opened")
    elif phase == "acceptance":
        return accept(b, pid, slug, n_ahu, rng, close)
    elif phase == "warranty":  # the 10-month review: a longer window
        b.go_phase(pid, phase)
        b.ingest(readings(slug, n_ahu, now - 24 * 3600, now, {}, rng, fixed=True))
        b.run(pid, 24)
    else:  # operations, maintenance: monitoring carries on
        b.go_phase(pid, phase)
        b.ingest(readings(slug, n_ahu, now - 2 * 3600, now, {}, rng, fixed=True))
    return True


def install_ops(b):
    """Fault rules scoped to the demo prefix, once. The range rules are cur
    mode (MQTT readings only), so on this HTTP-fed data only stuck and stale
    fire."""
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


def current_phase(p):
    return next((x["key"] for x in p["phases"] if x["status"] == "active"), PHASES[0][0])


def cmd_up(b, rng):
    install_ops(b)
    keys = [k for k, _ in PHASES]
    for slug, (title, _kind, n_ahu, end, close) in PROJECTS.items():
        p = b.project(title, "design", slug, GATED)
        cur = current_phase(p)
        for key in keys[keys.index(cur) + 1:keys.index(end) + 1]:  # a rerun carries on from where it stopped
            print(f"{title}: -> {key}")
            if not enter(b, p["id"], slug, n_ahu, key, rng, close):
                break
        print(f"{title}: now in {current_phase(b.project(title, 'design', slug, GATED))}; "
              f"report: python -m scripts.seed_cx_demo report {slug}")
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
        b.project(title, "design", slug, GATED)
    for i in range(1, CAMPUS + 1):
        b.project(f"demo-cx campus building {i:02d}", "design", f"campus-{i:02d}")
    print("projects scoped to their own prefixes")


def cmd_advance(b, slug, rng):
    """One phase forward. Into acceptance it never closes an issue: that is
    yours to do (Issues tab), and advance moves on once none is left open."""
    title, _k, n_ahu, _e, _c = PROJECTS[slug]
    p = b.project(title, "design", slug, GATED)
    keys = [k for k, _ in PHASES]
    cur = current_phase(p)
    if cur == keys[-1]:
        raise SystemExit(f"{title} is already in {cur}")
    nxt = keys[keys.index(cur) + 1]
    moved = enter(b, p["id"], slug, n_ahu, nxt, rng, close=False)
    print(f"{title}: {cur} -> {nxt}" if moved else f"{title}: still in {cur}")


def cmd_report(b, slug):
    """Save the project's Cx report (GET /cx/projects/{id}/report?format=html)."""
    title = PROJECTS[slug][0]
    p = b.project(title, "design", slug, GATED)
    # origin: the saved file's trend links point at this Baler, not at a
    # root-relative path that means nothing in a file (Baler#125)
    r = b.req("GET", f"/cx/projects/{p['id']}/report", params={"format": "html", "origin": "{0.scheme}://{0.netloc}".format(urlsplit(b.url))})
    if r.status_code != 200:
        raise SystemExit(f"report -> {r.status_code}: {r.text[:200]}")
    path = f"cx-report-{slug}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"{title}: report saved to {path}")


def cmd_live(b, rng):
    install_ops(b)
    print("posting a fresh round of readings every 60s; Ctrl-C to stop")
    while True:
        now = time.time()
        items = []
        for slug in [*PROJECTS, *(f"campus-{i:02d}" for i in range(1, CAMPUS + 1))]:
            n_ahu = PROJECTS[slug][2] if slug in PROJECTS else 1
            items += readings(slug, n_ahu, now, now, {}, rng, fixed=slug in PROJECTS)
        b.ingest(items)
        time.sleep(60)


def cmd_cleanup(b):
    for r in b.call("GET", "/fault/rules"):
        if r["name"].startswith("demo-cx"):
            b.call("DELETE", f"/fault/rules/{r['id']}")
    for p in b.call("GET", "/cx/projects"):
        if p["name"].startswith(("demo-cx", "Riverside Clinic", "Harbor Office")):
            b.call("DELETE", f"/cx/projects/{p['id']}")
    for slug, (_t, _k, n_ahu, *_rest) in {**PROJECTS, **{f"campus-{i:02d}": (0, 0, 1) for i in range(1, CAMPUS + 1)}}.items():
        for key, _d, _c, points in equips(slug, n_ahu, fixed=True):
            for name, *_ in points:
                b.call("DELETE", f"/ingest/records?id=urn:point:{key}/{name}", ok=(204, 404))
            b.call("DELETE", f"/ingest/records?id=urn:equip:{key}", ok=(204, 404))
    print("cleaned up (history rows stay by design)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "advance", "live", "scope", "report", "cleanup"])
    ap.add_argument("project", nargs="?", choices=list(PROJECTS))
    ap.add_argument("--seed", type=int, default=7, help="same seed, same demo data")
    a = ap.parse_args()
    b = Baler(os.environ["BALER_URL"], os.environ["BALER_EMAIL"], os.environ["BALER_PASSWORD"])
    rng = random.Random(a.seed)
    {"up": lambda: cmd_up(b, rng), "advance": lambda: cmd_advance(b, a.project or sys.exit("advance needs a project"), rng),
     "live": lambda: cmd_live(b, rng), "scope": lambda: cmd_scope(b),
     "report": lambda: cmd_report(b, a.project or sys.exit("report needs a project")),
     "cleanup": lambda: cmd_cleanup(b)}[a.cmd]()


if __name__ == "__main__":
    main()
