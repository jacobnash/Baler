"""
Realistic ladder profiles and the synthetic load (Baler#273): derivations,
Cur- and His-mode rules per equip, a share of points that actually fault,
and what that load costs.

Jacob's target (2026-11-09, #190): most points report every 1-5 min, and
each equip carries 5-10 synthetic points, rules or derivations; one server
per ~1M points. 5 s readings stay as the stress test. So a profile is

  stress-5s     5 s readings, no synthetic load, no faults (the ladder before #273)
  typical-1min  60 s readings, 5 synthetic items per equip, 1% of points faulting
  typical-5min  300 s readings, the same

and `--synthetic-per-equip N`, `--synthetic-mix D:C:H` (derivations, Cur-mode
rules, His-mode rules, as weights) and `--fault-share` override them.

Rules and derivations in Baler are templates, not per-equip objects: one
derivation's SPARQL select binds a row per equip, one rule's topic glob
matches a point on every equip. So "N items per equip" is N templates, each
applying once to every equip, and the load scales with the equips:

- derivations come in chains of three, so dependency fan-out costs what it
  costs (#273 R3):
    L1  Cur mode, two raw inputs (multi-input), runs when either reports;
    L2  Cur mode, L1's output and a third raw input (chained: reads another
        derivation's output from history), runs when that input reports;
    L3  tick mode every reporting interval, depends_on L2, reads L2's and
        L1's outputs (the chain top).
  N=5 at the default mix 60:20:20 is one chain, one Cur rule, one His rule;
  N=10 is two chains, two of each. A derivation's output is a point with
  history, so each derivation adds one synthetic point per equip: a step's
  size counts them (#273 R6), and its real points are what's left.
- Cur-mode rules: a range rule per template on one label, bounds wide
  enough never to fire (on top of the ladder's own scale-range rule, which
  every reading meets).
- His-mode rules: stuck and stale, alternately, on one label each, over
  windows of ten reporting intervals.
- Faults: `--fault-share` of the points (numeric ones, chosen by index)
  read out of scale-range's bounds for FAULT_HALF_PERIOD readings, then in
  them for as many, so faults open and resolve all soak long, and every
  transition is written and goes to a webhook (a receiver on the host,
  BALER_ALLOW_PRIVATE_WEBHOOKS=1 in the scale stack only).

How far the chain top falls behind its inputs is measured in the data
itself: every synthetic function returns the newest *origin* timestamp it
saw (a raw input's sample ts; a derived input's value, which is its own
origin ts), so for any derived row, `ts - value` is how stale the newest
reading behind it was when it was computed.
"""

from __future__ import annotations

import json
import math
import secrets
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import common
import limiter

# ------------------------------------------------------------------ profiles

PROFILES: dict[str, dict] = {
    # The ladder as it was before #273: the stress test.
    "stress-5s": {"interval": 5.0, "synthetic_per_equip": 0, "synthetic_mix": "60:20:20", "fault_share": 0.0,
                  "warmup": 60.0, "soak": 900.0},
    # Warm-up: every point reports once and the Cur selects refresh (60 s),
    # then the chain top has ticked; soak: at least six reporting cycles.
    "typical-1min": {"interval": 60.0, "synthetic_per_equip": 5, "synthetic_mix": "60:20:20", "fault_share": 0.01,
                     "warmup": 180.0, "soak": 900.0},
    "typical-5min": {"interval": 300.0, "synthetic_per_equip": 5, "synthetic_mix": "60:20:20", "fault_share": 0.01,
                     "warmup": 660.0, "soak": 1800.0},
}
DEFAULT_PROFILE = "stress-5s"

# Labels the synthetic load reads: numeric sensors whose values always move
# (sine/walk), so the stuck rules never fire on them; matched in SPARQL by
# their Haystack markers, which are unique within an equip's ten templates.
LABEL_MARKERS = {
    "sat": ("discharge", "temp", "sensor"),
    "rat": ("return", "temp", "sensor"),
    "mat": ("mixed", "temp", "sensor"),
    "zone-temp": ("zone", "temp", "sensor"),
    "dsp": ("pressure", "sensor"),
}
LABELS = tuple(LABEL_MARKERS)
NAME_PREFIX = "scale-syn-"
LEVELS = 3  # derivations per chain


def add_args(p) -> None:
    """The profile options, on every subcommand that drives the generator."""
    p.add_argument("--profile", choices=sorted(PROFILES), default=None,
                   help=f"named load profile (default {DEFAULT_PROFILE}); the options below override it")
    p.add_argument("--synthetic-per-equip", type=int, default=None,
                   help="synthetic items (derivations + Cur rules + His rules) per equip, e.g. 5 or 10")
    p.add_argument("--synthetic-mix", default=None,
                   help="D:C:H weights of derivations, Cur-mode rules and His-mode rules, e.g. 60:20:20")
    p.add_argument("--fault-share", type=float, default=None, help="share of points that fault, e.g. 0.01")


def apply_profile(args) -> None:
    """Fills every option left unset from the profile; records the profile."""
    if not hasattr(args, "synthetic_per_equip"):
        return
    args.profile = args.profile or DEFAULT_PROFILE
    for key, value in PROFILES[args.profile].items():
        if hasattr(args, key) and getattr(args, key) is None:
            setattr(args, key, value)
    plan_for(args)  # validates the combination


def parse_mix(text: str) -> tuple[float, float, float]:
    parts = [float(x) for x in text.replace(",", ":").split(":")]
    if len(parts) != 3 or any(x < 0 for x in parts) or not sum(parts):
        raise SystemExit(f"--synthetic-mix {text!r}: want three non-negative weights D:C:H, e.g. 60:20:20")
    total = sum(parts)
    return parts[0] / total, parts[1] / total, parts[2] / total


def split_items(n: int, mix: tuple[float, float, float]) -> tuple[int, int, int]:
    """N items into (derivations, Cur rules, His rules), largest remainder."""
    raw = [n * w for w in mix]
    out = [math.floor(x) for x in raw]
    for i in sorted(range(3), key=lambda i: -(raw[i] - out[i]))[: n - sum(out)]:
        out[i] += 1
    return out[0], out[1], out[2]


@dataclass(frozen=True)
class Plan:
    profile: str
    interval: float
    per_equip: int
    derivations: int
    cur_rules: int
    his_rules: int
    points_per_equip: int
    fault_share: float

    @property
    def chains(self) -> int:
        return math.ceil(self.derivations / LEVELS)

    def levels(self) -> list[tuple[int, int]]:
        """(chain, level) of every derivation, level 1..3."""
        return [(i // LEVELS, i % LEVELS + 1) for i in range(self.derivations)]

    def points(self, total: int) -> dict:
        """A step of `total` points: real points the generator publishes,
        and the derivations' outputs, one per derivation per equip."""
        if not self.derivations:
            return {"real": total, "synthetic": 0, "total": total, "equips": math.ceil(total / self.points_per_equip)}
        equips = round(total / (self.points_per_equip + self.derivations))
        real = equips * self.points_per_equip
        return {"real": real, "synthetic": equips * self.derivations, "total": real + equips * self.derivations,
                "equips": equips}

    def real_points(self, total: int) -> int:
        return self.points(total)["real"]

    def expected(self, total: int) -> dict:
        """What the configured load should do per second at this step."""
        pts = self.points(total)
        shape = common.Shape(pts["real"], self.points_per_equip)
        cur_derivs = sum(1 for _, lvl in self.levels() if lvl < LEVELS)
        tick_derivs = self.derivations - cur_derivs
        faulting = faulting_points(shape)(self.fault_share)
        return {
            "readings_per_s": pts["real"] / self.interval,
            # scale-range meets every reading; each Cur template one per equip
            "cur_rule_evaluations_per_s": (pts["real"] + pts["equips"] * self.cur_rules) / self.interval,
            # a Cur row runs at least once per interval (twice if its inputs
            # land in different listener batches)
            "cur_rows_per_s": pts["equips"] * cur_derivs / self.interval,
            "tick_rows_per_s": pts["equips"] * tick_derivs / self.interval,
            "derivation_rows_per_s": pts["equips"] * self.derivations / self.interval,
            "faulting_points": faulting,
            "fault_transitions_per_s": faulting / (FAULT_HALF_PERIOD * self.interval),
        }

    def describe(self) -> dict:
        cur = sum(1 for _, lvl in self.levels() if lvl < LEVELS)
        return {"profile": self.profile, "interval_s": self.interval, "per_equip": self.per_equip,
                "derivations": self.derivations, "derivations_cur": cur, "derivations_tick": self.derivations - cur,
                "chains": self.chains, "cur_rules": self.cur_rules, "his_rules": self.his_rules,
                "fault_share": self.fault_share}


def plan_for(args) -> Plan:
    d, c, h = split_items(args.synthetic_per_equip or 0, parse_mix(args.synthetic_mix or "60:20:20"))
    if not 0 <= (args.fault_share or 0) <= 1:
        raise SystemExit(f"--fault-share {args.fault_share}: want a share between 0 and 1")
    if (d or c or h) and args.points_per_equip < len(common.TEMPLATES):
        raise SystemExit(f"synthetic load reads an equip's {', '.join(LABELS)} points: "
                         f"--points-per-equip must be at least {len(common.TEMPLATES)}")
    return Plan(args.profile or DEFAULT_PROFILE, args.interval, args.synthetic_per_equip or 0, d, c, h,
                args.points_per_equip, args.fault_share or 0.0)


# ------------------------------------------------------------------ faults

FAULT_HALF_PERIOD = common.FAULT_HALF_PERIOD


def faulting_points(shape: common.Shape):
    """share -> how many of the shape's points the generator makes fault."""
    def count(share: float) -> int:
        if not share:
            return 0
        n = 0
        for g in range(shape.equips):
            for idx, _label, t in shape.equip_points(g):
                if common.is_faulting(idx, t, share):
                    n += 1
        return n
    return count


# ------------------------------------------------------------------ definitions

HS = "PREFIX hs: <urn:baler:haystack#>\nPREFIX brick: <https://brickschema.org/schema/Brick#>\n"

# Every synthetic function: the newest origin timestamp behind its inputs
# (see the module docstring). Inputs named d* are derived.
FN_SOURCE = """def run(inputs, row):
    seen = []
    for name, series in inputs.items():
        if series:
            ts, value = series[-1]
            seen.append(float(value) if name.startswith("d") else ts.timestamp())
    return max(seen) if seen else None
"""
_T0, _T1 = "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"
_E0 = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def _point_pattern(var: str, label: str) -> str:
    tags = " ; ".join(f'hs:hasTag "{m}"' for m in (*LABEL_MARKERS[label], "scale"))
    return f"?equip brick:hasPoint ?{var} . ?{var} {tags} ."


def _computed(var: str, derivation_id: str) -> str:
    """A derivation's output for this equip, bound without a lookup: the
    engine's own naming (derivation_engine._output_uri)."""
    return (f'BIND(IRI(CONCAT("urn:point:computed/{derivation_id}/", '
            f'REPLACE(STR(?equip), "^.*[/:]", ""))) AS ?{var})')


def chain_labels(chain: int) -> tuple[str, str, str]:
    return LABELS[chain % len(LABELS)], LABELS[(chain + 1) % len(LABELS)], LABELS[(chain + 2) % len(LABELS)]


def derivation_body(plan: Plan, chain: int, level: int, ids: dict[tuple[int, int], str]) -> dict:
    """POST /derivation/derivations body for one chain level; `ids` holds
    the ids of the levels below it."""
    a, b, c = chain_labels(chain)
    window = 2 * plan.interval + 30
    if level == 1:
        where, inputs = _point_pattern("a", a) + " " + _point_pattern("b", b), ["a", "b"]
        cases = [{"inputs": {"a": [[_T0, 70.0]], "b": [[_T1, 71.0]]}, "expected": _E0 + 60}]
    elif level == 2:
        where, inputs = _point_pattern("c", c) + " " + _computed("d1", ids[(chain, 1)]), ["d1", "c"]
        cases = [{"inputs": {"d1": [[_T1, _E0 + 5]], "c": [[_T0, 70.0]]}, "expected": _E0 + 5}]
    else:
        # one row per equip (anchored on its `a` point), both inputs derived
        where = f"{_point_pattern('p', a)} {_computed('d2', ids[(chain, 2)])} {_computed('d1', ids[(chain, 1)])}"
        inputs = ["d2", "d1"]
        cases = [{"inputs": {"d2": [[_T1, _E0 + 7]], "d1": [[_T0, _E0 + 9]]}, "expected": _E0 + 9}]
    body = {
        "name": f"{NAME_PREFIX}c{chain}-l{level}", "kind": "formula",
        "select": f"{HS}SELECT ?equip {' '.join('?' + v for v in inputs)} WHERE {{ {where} }}",
        "target_var": "equip", "input_vars": inputs, "fn_source": FN_SOURCE, "test_cases": cases,
        "window_seconds": window, "output": {"label": f"syn c{chain} l{level}"},
        "depends_on": [ids[(chain, level - 1)]] if level > 1 else [],
    }
    if level < LEVELS:
        body["applies_to_topic_glob"] = f"{common.TOPIC_ROOT}/s*"
    else:
        body["interval_seconds"] = plan.interval
    return body


def rule_bodies(plan: Plan) -> list[dict]:
    out = []
    for i in range(plan.cur_rules):
        label = LABELS[i % len(LABELS)]
        out.append({"name": f"{NAME_PREFIX}cur-{i}", "mode": "cur", "type": "range",
                    "applies_to": {"topic_glob": f"{common.TOPIC_ROOT}/s*/{label}"}, "min": -1e6, "max": 1e6})
    for i in range(plan.his_rules):
        label = LABELS[(i + 1) % len(LABELS)]
        rule = {"name": f"{NAME_PREFIX}his-{i}", "mode": "his", "applies_to": {"topic_glob": f"{common.TOPIC_ROOT}/s*/{label}"}}
        if i % 2 == 0:
            rule.update(type="stuck", stuck_seconds=10 * plan.interval)
        else:
            rule.update(type="stale", max_age_seconds=10 * plan.interval)
        out.append(rule)
    return out


def ensure(gateway: str, s, plan: Plan) -> dict:
    """The plan's rules and derivations exist, and no other synthetic ones
    (a ladder run with another N removes the extra). Idempotent. Returns
    {"derivations": {(chain, level): id}, "rules": [ids]}."""
    rules = {r.get("name"): r for r in s.get(f"{gateway}/fault/rules", timeout=60).json()}
    want_rules = rule_bodies(plan)
    for body in want_rules:
        if body["name"] not in rules:
            r = s.post(f"{gateway}/fault/rules", json=body, timeout=60)
            r.raise_for_status()
            rules[body["name"]] = r.json()
    names = {b["name"] for b in want_rules}
    for name, r in list(rules.items()):
        if (name or "").startswith(NAME_PREFIX) and name not in names:
            s.delete(f"{gateway}/fault/rules/{r['id']}", timeout=60).raise_for_status()
            del rules[name]

    have = {d.get("name"): d for d in s.get(f"{gateway}/derivation/derivations", timeout=60).json()}
    ids: dict[tuple[int, int], str] = {}
    wanted: set[str] = set()
    for chain, level in plan.levels():
        body = derivation_body(plan, chain, level, ids)
        wanted.add(body["name"])
        cur = have.get(body["name"])
        same = cur is not None and all(cur.get(k) == body.get(k) for k in
                                       ("select", "input_vars", "interval_seconds", "window_seconds", "depends_on",
                                        "applies_to_topic_glob", "fn_source"))
        if not same:
            if cur is not None:
                s.delete(f"{gateway}/derivation/derivations/{cur['id']}", timeout=60).raise_for_status()
            r = s.post(f"{gateway}/derivation/derivations", json=body, timeout=120)
            if not r.ok:
                raise SystemExit(f"creating derivation {body['name']}: HTTP {r.status_code} {r.text[:500]}")
            cur = r.json()
        ids[(chain, level)] = cur["id"]
    for name, d in have.items():
        if (name or "").startswith(NAME_PREFIX) and name not in wanted:
            s.delete(f"{gateway}/derivation/derivations/{d['id']}", timeout=60).raise_for_status()
    return {"derivations": ids, "rules": [rules[n]["id"] for n in sorted(names)]}


# ------------------------------------------------------------------ webhooks

class WebhookSink:
    """Counts webhook deliveries: an HTTP server on the scale stack's
    network gateway address (reachable from fault_detector, not from the
    LAN), registered with fault_api as a webhook for every fault."""

    def __init__(self, network: str):
        self.count = 0
        self.events: dict[str, int] = {}
        self.lock = threading.Lock()
        self.host = docker_gateway(network)
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    event = json.loads(body).get("event", "?")
                except (ValueError, AttributeError):
                    event = "?"
                with sink.lock:
                    sink.count += 1
                    sink.events[event] = sink.events.get(event, 0) + 1
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer((self.host, 0), Handler)
        self.url = f"http://{self.host}:{self.server.server_address[1]}/scale-webhook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return {"received": self.count, **{f"received_{k}": v for k, v in self.events.items()}}

    def register(self, gateway: str, s) -> str:
        """Replaces any earlier scale webhook (an old run's port is gone)."""
        for w in s.get(f"{gateway}/fault/webhooks", timeout=60).json():
            if "/scale-webhook" in (w.get("url") or ""):
                s.delete(f"{gateway}/fault/webhooks/{w['id']}", timeout=60)
        r = s.post(f"{gateway}/fault/webhooks", json={"url": self.url, "secret": secrets.token_hex(16)}, timeout=60)
        if not r.ok:
            raise SystemExit(f"registering the scale webhook {self.url}: HTTP {r.status_code} {r.text[:300]} "
                             "(BALER_ALLOW_PRIVATE_WEBHOOKS=1 in the scale stack?)")
        return r.json()["id"]

    def close(self) -> None:
        self.server.shutdown()


def docker_gateway(network: str) -> str:
    out = subprocess.run(["docker", "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}"],
                         capture_output=True, text=True, check=False).stdout.strip()
    if not out:
        raise SystemExit(f"no gateway address for docker network {network!r} (is the scale stack up?)")
    return out


# ------------------------------------------------------------------ measuring

def engines(conn) -> dict:
    """derivation_engine's and fault_detector's heartbeat details."""
    try:
        return {s: d for s, d in conn.execute(
            "SELECT service, detail FROM service_heartbeats WHERE service IN ('fault_detector', 'derivation_engine')")}
    except Exception:  # noqa: BLE001
        conn.rollback()
        return {}


def settle(read_engines, start: dict, interval: float, limit: float = 900.0, every: float = 5.0,
           sleep=time.sleep, clock=time.monotonic) -> dict:
    """Waits (up to `limit` s) until derivation_engine has finished two tick
    passes since `start` (its heartbeat then) and its last pass took less
    than the interval: a step's first passes over its new equips write each
    new output's lineage to the graph once, a burst that isn't the steady
    load the soak measures. Returns how long it waited and the longest pass
    seen meanwhile (that burst)."""
    t0 = clock()
    passes0 = ((start.get("derivation_engine") or {}).get("tick") or {}).get("passes") or 0
    longest = 0.0
    while True:
        tick = (read_engines().get("derivation_engine") or {}).get("tick") or {}
        longest = max(longest, tick.get("last_s") or 0.0)
        done = (tick.get("passes") or 0) >= passes0 + 2 and (tick.get("last_s") or 0) < interval
        if done or clock() - t0 >= limit:
            return {"waited_s": round(clock() - t0, 1), "settled": done, "longest_pass_s": round(longest, 2)}
        sleep(every)


def fault_counts(conn, a: float, b: float) -> dict:
    lo, hi = datetime.fromtimestamp(a, tz=timezone.utc), datetime.fromtimestamp(b, tz=timezone.utc)
    opened, resolved, open_now = conn.execute(
        "SELECT count(*) FILTER (WHERE started_at >= %s AND started_at < %s), "
        "count(*) FILTER (WHERE ended_at >= %s AND ended_at < %s), count(*) FILTER (WHERE ended_at IS NULL) "
        "FROM faults WHERE point_uri LIKE %s", (lo, hi, lo, hi, f"urn:point:{common.TOPIC_ROOT}/%")).fetchone()
    return {"opened": opened, "resolved": resolved, "open_at_end": open_now}


def _pcts(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    n = len(xs)
    return {"n": n, "p50_s": round(xs[n // 2], 2), "p95_s": round(xs[min(n - 1, int(n * 0.95))], 2),
            "max_s": round(xs[-1], 2)}


def sample_equips(shape: common.Shape, n: int = 40) -> list[str]:
    """Local names (as the engine's output URIs carry them) of n equips
    spread evenly over the shape."""
    step = max(1, shape.equips // n)
    return [shape.equip_prefix(g).rsplit("/", 1)[-1] for g in range(0, shape.equips, step)][:n]


def chain_lag(conn, ids: dict[tuple[int, int], str], equips: list[str], a: float, b: float) -> dict:
    """Per chain level: `ts - value` of every row the level wrote for the
    sampled equips in [a, b) - how stale the newest reading behind it was."""
    out: dict[str, dict] = {}
    lo, hi = datetime.fromtimestamp(a, tz=timezone.utc), datetime.fromtimestamp(b, tz=timezone.utc)
    by_level: dict[int, list[str]] = {}
    for (_chain, level), did in ids.items():
        by_level.setdefault(level, []).extend(f"urn:point:computed/{did}/{e}" for e in equips)
    for level, uris in sorted(by_level.items()):
        rows = conn.execute(
            "SELECT extract(epoch FROM ts) - value FROM point_history "
            "WHERE point_uri = ANY(%s) AND ts >= %s AND ts < %s AND value IS NOT NULL", (uris, lo, hi)).fetchall()
        out[f"L{level}"] = {**_pcts([float(r[0]) for r in rows]), "points": len(uris)}
    return out


def _rate(h0: dict, h1: dict, key: str, dt: float) -> float | None:
    if key not in h1:
        return None
    return round((float(h1.get(key) or 0) - float(h0.get(key) or 0)) / dt, 2) if dt > 0 else None


def summarize(plan: Plan, total: int, samples: list[dict], hb0: dict, hb1: dict, dt: float, faults: dict,
              hooks: dict | None, lag: dict, canary: dict) -> dict:
    """step["synthetic"]: the configured load, what it measured as, and the
    expected rates beside them. Pure (tested)."""
    exp = plan.expected(total)
    de0, de1 = hb0.get("derivation_engine") or {}, hb1.get("derivation_engine") or {}
    fd0, fd1 = hb0.get("fault_detector") or {}, hb1.get("fault_detector") or {}
    t0, t1 = de0.get("tick") or {}, de1.get("tick") or {}
    cur_rows = _rate(de0, de1, "cur_rows", dt)
    tick_rows = _rate(t0, t1, "rows", dt)
    rows = round((cur_rows or 0) + (tick_rows or 0), 2) if cur_rows is not None or tick_rows is not None else None
    passes = (t1.get("passes") or 0) - (t0.get("passes") or 0)
    busy = (t1.get("busy_s") or 0) - (t0.get("busy_s") or 0)
    ticks = [x["synthetic"]["derivation_engine"].get("tick") or {} for x in samples
             if (x.get("synthetic") or {}).get("derivation_engine")]
    last = [t["last_s"] for t in ticks if t.get("last_s") is not None]
    late = [t["late_s"] for t in ticks if t.get("late_s") is not None]
    evals = _rate(fd0, fd1, "cur_evaluations", dt)
    his = [((x.get("synthetic") or {}).get("fault_detector") or {}).get("his") or {} for x in samples]
    sweeps = [h["sweep_s"] for h in his if h.get("sweep_s") is not None]

    def ratio(x, want):
        return round(x / want, 3) if x is not None and want else None

    out: dict = {
        "plan": plan.describe(), "points": plan.points(total), "expected": {k: round(v, 2) for k, v in exp.items()},
        "derivations": {
            "rows_per_s": rows, "cur_rows_per_s": cur_rows, "tick_rows_per_s": tick_rows,
            "rows_ratio": ratio(rows, exp["derivation_rows_per_s"]),
            "cur_busy": ratio(_rate(de0, de1, "cur_busy_s", dt), 1),
            "tick": {"interval_s": t1.get("interval_s"), "passes": passes,
                     "mean_s": round(busy / passes, 3) if passes else None, "busy": round(busy / dt, 3) if dt else None,
                     "last_s": _pcts(last), "late_s_max": round(max(late), 2) if late else None,
                     "rows_per_pass": round(((t1.get("rows") or 0) - (t0.get("rows") or 0)) / passes) if passes else None},
        },
        "rules": {
            "cur_evaluations_per_s": evals, "cur_ratio": ratio(evals, exp["cur_rule_evaluations_per_s"]),
            "cur_busy": ratio(_rate(fd0, fd1, "cur_busy_s", dt), 1),
            "his": {"rules": (fd1.get("his") or {}).get("rules"), "interval_s": fd1.get("interval_seconds"),
                    "sweep_s_max": max(sweeps) if sweeps else None,
                    "points_faulted": (fd1.get("his") or {}).get("points_faulted")},
        },
        "faults": {**faults, "faulting_points": exp["faulting_points"],
                   "transitions_per_s": round((faults.get("opened", 0) + faults.get("resolved", 0)) / dt, 3) if dt else None,
                   "expected_transitions_per_s": round(exp["fault_transitions_per_s"], 3),
                   "webhooks": hooks},
        "chain_lag": lag,
    }
    top = lag.get(f"L{LEVELS}") or lag.get(f"L{max((int(k[1:]) for k in lag), default=1)}") if lag else None
    if top and top.get("n"):
        out["chain_lag"]["top"] = {k: top[k] for k in ("p50_s", "p95_s", "max_s")}
        if canary.get("p50_s") is not None:
            out["chain_lag"]["inputs_ingest_p50_s"] = canary["p50_s"]
            out["chain_lag"]["top_minus_ingest_p50_s"] = round(top["p50_s"] - canary["p50_s"], 2)
    return out


# Pass conditions for the synthetic load (#273). The chain top ticks once
# per interval, so its rows can be up to one interval behind their newest
# input, plus the chain below it; past this it is falling behind.
def chain_lag_budget(interval: float) -> float:
    return 2 * interval + 30.0


MIN_LOAD_RATIO = 0.8  # measured / configured rows or evaluations


def reasons(step: dict) -> list[str]:
    """Why the synthetic load failed a step (ladder.evaluate)."""
    syn = step.get("synthetic") or {}
    if not syn or not (syn.get("plan") or {}).get("per_equip"):
        return []
    out = []
    plan, d, r = syn["plan"], syn.get("derivations") or {}, syn.get("rules") or {}
    tick = d.get("tick") or {}
    iv = tick.get("interval_s") or plan["interval_s"]
    if (tick.get("late_s_max") or 0) > iv:
        out.append(f"derivation ticks behind: a pass started up to {tick['late_s_max']} s late (interval {iv:g} s, "
                   f"mean pass {tick.get('mean_s')} s)")
    top = (syn.get("chain_lag") or {}).get("top") or {}
    if top.get("p95_s") is not None and top["p95_s"] > chain_lag_budget(plan["interval_s"]):
        out.append(f"derivation chain top lags its inputs: p95 {top['p95_s']} s (> {chain_lag_budget(plan['interval_s']):g} s)")
    if plan.get("derivations") and d.get("rows_ratio") is not None and d["rows_ratio"] < MIN_LOAD_RATIO:
        out.append(f"derivation rows {d['rows_per_s']}/s are {d['rows_ratio']:.0%} of the configured "
                   f"{syn['expected']['derivation_rows_per_s']}/s")
    if r.get("cur_ratio") is not None and r["cur_ratio"] < MIN_LOAD_RATIO:
        out.append(f"Cur-mode rule evaluations {r['cur_evaluations_per_s']}/s are {r['cur_ratio']:.0%} of the configured "
                   f"{syn['expected']['cur_rule_evaluations_per_s']}/s")
    his = r.get("his") or {}
    if his.get("sweep_s_max") and his.get("interval_s") and his["sweep_s_max"] > his["interval_s"]:
        out.append(f"His-mode sweep took {his['sweep_s_max']} s, longer than its {his['interval_s']:g} s interval")
    return out


def median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def table(results: list[dict]) -> list[str]:
    """Markdown rows of the synthetic load per step, for `ladder.py table`
    (after its main table): what was configured, what it measured as."""
    head = ("| Run | Profile | N | Points (real + synthetic) | Status | Derivation rows/s (expected) | Tick pass / interval | "
            "Rule evals/s (expected) | Faults opened / resolved / webhooks | Chain top lag p50 / p95 (ingest p50) | Limiter |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for r in results:
        for st in r.get("steps", []):
            syn = st.get("synthetic")
            if not syn:
                continue
            p, d, ru, f = syn["points"], syn["derivations"], syn["rules"], syn["faults"]
            tick, top, cl = d.get("tick") or {}, (syn.get("chain_lag") or {}).get("top") or {}, syn.get("chain_lag") or {}
            hooks = (f.get("webhooks") or {}).get("received", "-")
            lines.append(
                f"| {r['run_id']} | {syn['plan']['profile']} | {syn['plan']['per_equip']} | {p['total']:,} ({p['real']:,} + "
                f"{p['synthetic']:,}) | {st['status']} | {d.get('rows_per_s')} ({syn['expected']['derivation_rows_per_s']}) | "
                f"{tick.get('mean_s')} s / {tick.get('interval_s')} s | {ru.get('cur_evaluations_per_s')} "
                f"({syn['expected']['cur_rule_evaluations_per_s']}) | {f.get('opened')} / {f.get('resolved')} / {hooks} | "
                f"{top.get('p50_s', '-')} / {top.get('p95_s', '-')} s ({cl.get('inputs_ingest_p50_s', '-')} s) | "
                f"{limiter.short(st.get('limiter')).replace('|', '/')} |")
    return lines if len(lines) > 2 else []
