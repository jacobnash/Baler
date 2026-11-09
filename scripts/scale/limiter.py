"""
The limiter of a ladder step (Baler#269): which resource or code path
stopped it, the evidence, and how sure the rule is. Pure: reads a step
result (ladder.run_step's dict, with `evidence` from evidence.Sampler) and
returns

  {"limiter": "ingest listener: mqtt_listener CPU-capped (throttled 38%)",
   "component": "ingest listener", "confidence": "high",
   "evidence": ["broker dropped 1,204 deliveries", "mqtt_listener throttled in 38% of CFS periods", ...],
   "also": [...other saturated parts...], "headroom": [...], "next_suspect": "..."}

The rules, in order (documented in docs-site/pages/scale-test.mdx, "Reading
the limiter"; tested in tests/test_scale_limiter.py on recorded samples):

1. Stops that are not a Baler ceiling: live-safety, the Docker disk low, the
   generator behind schedule or short of the target rate.
2. A container restarted or was OOM-killed.
3. The graph didn't absorb a step's tags in time.
4. Ingest symptoms (broker drops, readings missing, lag p95 or growing
   backlog, lost canaries, listener queue near its cap): attributed to the
   most saturated part of the ingest chain - the listener's CPU (CFS
   throttling against its cap, or its busiest thread against one core: the
   GIL), mosquitto's CPU, the disk, Postgres CPU, Postgres waits (WAL/IO,
   locks), or the listener's writer busy all the time (Postgres sets the
   pace). One saturated part = high confidence; several = medium; none =
   low ("unattributed").
5. A rule daemon behind on, or missing readings from, the reading feed:
   attributed to its own CPU the same way.
6. Read probes over budget: the worst one (median / budget) and the span
   that took most of its time, from its Jaeger trace, plus the API's
   throttling during the probes.
7. Any other failure reason: named as unattributed.

A passing step's limiter is "none found"; `next_suspect` is then the part
closest to its limit (`headroom`, highest ratio first).
"""

from __future__ import annotations

# Thresholds. A ratio of 1.0 in `headroom` means "at this threshold".
THROTTLED = 0.20        # share of CFS periods throttled: CPU-capped...
THROTTLE_BUSY = 0.60    # ...when its CPU p90 is at least this share of its cap
NEAR_CAP = 0.90         # CPU p90 / cap: at the cap even without throttling counts
HOT_THREAD = 0.85       # busiest thread, cores: a GIL-bound Python process
ONE_CORE_PROC = 0.90    # a single-threaded-by-design process's CPU, cores
DISK_UTIL = 0.90        # device busy share (diskstats io_ticks), p90
PG_WAIT_SHARE = 0.40    # share of active-backend samples in one wait class
PG_ACTIVE_MIN = 1.0     # ...with at least this many active backends per sample
LOCK_WAITS = 1.0        # mean ungranted locks per sample
WRITER_BUSY = 0.80      # listener's share of time inside flush()
LISTENER_QUEUE = 0.80   # listener queue peak / its cap
MEM = 0.95              # memory peak / cap
LAG_S = 30.0            # p95 lag pass condition (ladder.MAX_P95_LAG_S)

ONE_CORE = ("mqtt_listener", "fault_detector", "derivation_engine", "haystack_api", "ingest_api",
            "commissioning_api", "ops_api", "mosquitto")
# Observability and one-shot containers: in the evidence, never a limiter
# or a next suspect (Grafana near its 256 MB cap says nothing about Baler).
NOT_BALER = ("grafana", "jaeger", "migrate")
WRITE_WAITS = ("IO:", "LWLock:WAL", "LWLock:Buffer", "IO:WAL", "LWLock:Checkpoint")
PROBE_API = {"api": "haystack_api", "ingest": "ingest_api", "ops": "ops_api", "fault": "fault_api",
             "cx": "commissioning_api"}


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _phase(step: dict, name: str) -> dict:
    return ((step.get("evidence") or {}).get("phases") or {}).get(name) or {}


def interval_s(step: dict) -> float:
    rps = step.get("offered_target_rps")
    return step["points"] / rps if rps else 5.0


def gen_behind(step: dict) -> float | None:
    """The generator's worst lag behind its schedule during the soak (the
    readings phase's own max includes its warm-up)."""
    gen = step.get("generator") or {}
    v = gen.get("behind_soak_max_s")
    return v if v is not None else gen.get("behind_max_s")


def cpu_saturation(svc: str, c: dict) -> tuple[float, str] | None:
    """How close a container is to its CPU limit (1.0 = at the threshold)
    and why: throttling, CPU at the cap, or (single-threaded services) a
    process or thread at one core."""
    if not c:
        return None
    cands = []
    # Throttling counts only for a container using most of its cap: a
    # nearly idle API under a 0.25-CPU cap is throttled in ~45% of its
    # (few) CFS periods by its own healthcheck's start-up burst.
    busy = (c["cpu_p90"] / c["cap"]) if c.get("cap") and c.get("cpu_p90") is not None else 1.0
    if c.get("throttled") is not None and busy >= THROTTLE_BUSY:
        cands.append((c["throttled"] / THROTTLED, f"{svc} CPU-capped (throttled {_pct(c['throttled'])})"))
    if c.get("cap") and c.get("cpu_p90") is not None:
        cands.append((c["cpu_p90"] / c["cap"] / NEAR_CAP,
                      f"{svc} CPU-capped (p90 {c['cpu_p90']:.2f} cores of its {c['cap']:g}-core cap)"))
    procs = c.get("procs") or []
    if procs and svc != "postgres":
        p = procs[0]
        cands.append((p.get("hot_thread_mean", 0) / HOT_THREAD,
                      f"{svc} CPU-bound (busiest thread {_pct(p.get('hot_thread_mean', 0))} of a core)"))
        if svc in ONE_CORE:
            cands.append((p.get("cpu_mean", 0) / ONE_CORE_PROC,
                          f"{svc} CPU-bound (one process at {_pct(p.get('cpu_mean', 0))} of a core)"))
    elif svc in ONE_CORE and c.get("cpu_p90") is not None:
        cands.append((c["cpu_p90"] / ONE_CORE_PROC, f"{svc} CPU-bound ({_pct(c['cpu_p90'])} of one core, p90)"))
    return max(cands, default=None)


def pg_wait_saturation(pg: dict) -> tuple[float, str] | None:
    if not pg or (pg.get("active_per_tick") or 0) < PG_ACTIVE_MIN:
        return None
    waits = pg.get("waits") or {}
    write = {k: v for k, v in waits.items() if k.startswith(WRITE_WAITS)}
    share = sum(write.values())
    if not write:
        return None
    top = max(write, key=write.get)
    return share / PG_WAIT_SHARE, f"postgres write-bound ({top} {_pct(write[top])}, write waits {_pct(share)} of active samples)"


def ingest_symptoms(step: dict, soak: dict) -> list[str]:
    out = []
    ing, lag = step.get("ingest") or {}, step.get("lag") or {}
    if (ing.get("broker_dropped_delta") or 0) > 0:
        out.append(f"broker dropped {ing['broker_dropped_delta']:,} deliveries")
    ratio = ing.get("stored_ratio")
    if ing and (ratio is None or ratio < 0.99):
        out.append(f"stored {ratio if ratio is None else _pct(ratio)} of acknowledged readings")
    if lag.get("lost"):
        out.append(f"{lag['lost']} lag canaries lost")
    if lag and (lag.get("p95_s") is None or lag["p95_s"] >= LAG_S):
        out.append(f"p95 lag {lag.get('p95_s')} s")
    elif lag and lag.get("last_third_median_s", 0) > lag.get("first_third_median_s", 0) + 10:
        out.append(f"lag growing {lag['first_third_median_s']} -> {lag['last_third_median_s']} s")
    li = soak.get("listener") or {}
    if li.get("queue_cap") and (li.get("queue_max") or 0) >= LISTENER_QUEUE * li["queue_cap"]:
        out.append(f"listener queue peaked at {li['queue_max']:,} of {li['queue_cap']:,}")
    br = soak.get("broker") or {}
    if br.get("queued_last", 0) > max(1000, 2 * br.get("queued_first", 0)) and br.get("queued_last", 0) > 10 * interval_s(step):
        out.append(f"broker queue grew {br['queued_first']:,} -> {br['queued_last']:,}")
    return out


def attribute_ingest(soak: dict) -> tuple[str, list[str], list[str], str]:
    """(limiter, evidence, also-saturated, confidence) for an ingest-chain
    stop."""
    cs = soak.get("containers") or {}
    cands: list[tuple[float, str, str]] = []  # (score, text, component)
    for svc, comp in (("mqtt_listener", "ingest listener"), ("mosquitto", "broker"), ("postgres", "postgres")):
        s = cpu_saturation(svc, cs.get(svc) or {})
        if s:
            cands.append((s[0], s[1], comp))
    disk = soak.get("disk") or {}
    if disk.get("util_p90") is not None:
        cands.append((disk["util_p90"] / DISK_UTIL,
                      f"disk (util p90 {_pct(disk['util_p90'])}, {disk.get('write_mb_s_mean', 0):.0f} MB/s written)", "disk"))
    pg = soak.get("pg") or {}
    w = pg_wait_saturation(pg)
    if w:
        cands.append((w[0], w[1], "postgres"))
    if (pg.get("lock_waits_mean") or 0) > 0:
        cands.append((pg["lock_waits_mean"] / LOCK_WAITS, f"postgres locks ({pg['lock_waits_mean']} waiting per sample)",
                      "postgres"))
    li = soak.get("listener") or {}
    if li.get("flush_busy_mean") is not None:
        cands.append((li["flush_busy_mean"] / WRITER_BUSY,
                      f"postgres write-bound (listener writing {_pct(li['flush_busy_mean'])} of the time)", "postgres"))
    hot = sorted((c for c in cands if c[0] >= 1.0), key=lambda c: -c[0])
    evidence = [f"{t} [{s:.2f}x threshold]" for s, t, _ in sorted(cands, key=lambda c: -c[0])[:5]]
    if not hot:
        return "ingest listener (unattributed: nothing at its limit)", evidence, [], "low"
    # Consequences don't outrank their cause: the listener writing all the
    # time is explained by anything else at its limit (Postgres CPU or
    # waits, the disk, the listener's own CPU), and Postgres write waits by
    # a saturated disk.
    causes = [c for c in hot if "listener writing" not in c[1]] or hot
    if any(c[2] == "disk" for c in causes):
        causes = [c for c in causes if "write waits" not in c[1]]
    best = causes[0]
    also = [t for s, t, comp in causes[1:] if comp != best[2]]
    conf = "high" if not also else "medium"
    if "listener writing" in best[1]:
        conf = "medium"  # inferred from the listener's side only
    prefix = {"ingest listener": "ingest listener: ", "broker": "broker: ", "postgres": "", "disk": ""}[best[2]]
    return prefix + best[1], evidence, also, conf


def worst_probe(step: dict) -> dict | None:
    bad = []
    for p in step.get("probes") or []:
        if p.get("skipped"):
            continue
        if p.get("status") != 200:
            bad.append((float("inf"), p))
        elif p.get("budget_s") and p["median_s"] > p["budget_s"]:
            bad.append((p["median_s"] / p["budget_s"], p))
    return max(bad, key=lambda b: b[0])[1] if bad else None


def read_limiter(step: dict, p: dict) -> tuple[str, list[str], str]:
    if p.get("status") != 200:
        head = f"reads: {p['name']} HTTP {p.get('status')}"
    else:
        head = f"reads: {p['name']} ({p['median_s']} s > {p['budget_s']} s)"
    ev, conf = [], "medium"
    tr = p.get("trace") or {}
    spans = tr.get("spans") or []
    if spans:
        top = spans[0]
        head += f" {top['span']} {_pct(top['share'])}" if top.get("share") is not None else f" {top['span']}"
        ev.append("spans (self time, median request): " + ", ".join(
            f"{s['span']} {s['self_s']} s" for s in spans[:4]) + (f"; outside spans {tr['outside_spans_s']} s"
                                                                  if tr.get("outside_spans_s") is not None else ""))
        conf = "high"
    else:
        ev.append("no trace breakdown for this probe")
    api = PROBE_API.get((p.get("path") or "/").strip("/").split("/")[0])
    c = (_phase(step, "probes").get("containers") or {}).get(api) if api else None
    s = cpu_saturation(api, c) if c else None
    if s and s[0] >= 1.0:
        head += f"; {s[1]}"
        ev.append(f"{s[1]} during the probes")
    return head, ev, conf


def headroom(step: dict) -> list[dict]:
    """Every measured part and how close it ran to its limit (ratio 1.0 =
    at the threshold), highest first."""
    soak = _phase(step, "soak")
    out = []
    for svc, c in (soak.get("containers") or {}).items():
        if svc in NOT_BALER:
            continue
        s = cpu_saturation(svc, c)
        if s:
            out.append({"part": svc, "ratio": round(s[0], 3), "detail": s[1].replace(" CPU-capped", " CPU").replace(
                " CPU-bound", " CPU")})
        if c.get("mem_of_cap_max") is not None:
            out.append({"part": f"{svc} memory", "ratio": round(c["mem_of_cap_max"] / MEM, 3),
                        "detail": f"{svc} memory {_pct(c['mem_of_cap_max'])} of its cap"})
    disk = soak.get("disk") or {}
    if disk.get("util_p90") is not None:
        out.append({"part": "disk", "ratio": round(disk["util_p90"] / DISK_UTIL, 3),
                    "detail": f"disk util p90 {_pct(disk['util_p90'])}"})
    w = pg_wait_saturation(soak.get("pg") or {})
    if w:
        out.append({"part": "postgres waits", "ratio": round(w[0], 3), "detail": w[1]})
    li = soak.get("listener") or {}
    if li.get("flush_busy_mean") is not None:
        out.append({"part": "listener writer", "ratio": round(li["flush_busy_mean"] / WRITER_BUSY, 3),
                    "detail": f"listener writing {_pct(li['flush_busy_mean'])} of the time"})
    lag = step.get("lag") or {}
    if lag.get("p95_s") is not None:
        out.append({"part": "lag", "ratio": round(lag["p95_s"] / LAG_S, 3), "detail": f"p95 lag {lag['p95_s']} s"})
    for svc, r in (step.get("rules") or {}).items():
        f = r.get("feed") or {}
        if f.get("lag_s_max") is not None:
            out.append({"part": f"{svc} feed", "ratio": round(f["lag_s_max"] / LAG_S, 3),
                        "detail": f"{svc} feed lag max {f['lag_s_max']} s"})
    behind = gen_behind(step)
    if behind is not None:
        out.append({"part": "generator", "ratio": round(behind / interval_s(step), 3),
                    "detail": f"generator behind_s max {behind} s"})
    for p in step.get("probes") or []:
        if not p.get("skipped") and p.get("budget_s") and p.get("status") == 200:
            tr = (p.get("trace") or {}).get("spans") or []
            out.append({"part": f"probe {p['name']}", "ratio": round(p["median_s"] / p["budget_s"], 3),
                        "detail": f"{p['name']} {p['median_s']} s of {p['budget_s']} s"
                                  + (f" ({tr[0]['span']} {_pct(tr[0]['share'])})" if tr and tr[0].get("share") else "")})
    out.sort(key=lambda h: -h["ratio"])
    return out


def verdict(step: dict) -> dict:
    soak = _phase(step, "soak")
    ev: list[str] = []
    res = {"limiter": None, "component": None, "confidence": None, "evidence": ev, "also": []}
    hr = headroom(step)
    res["headroom"] = hr[:6]
    gen = step.get("generator") or {}
    iv = interval_s(step)

    def done(name, comp, conf, also=()):
        res.update(limiter=name, component=comp, confidence=conf, also=list(also))
        if name == "none found":
            res["next_suspect"] = hr[0]["detail"] if hr else None
        return res

    # 1. not a Baler ceiling
    if step.get("live_safety_tripped"):
        ev.append(f"live install degraded: {step['live_safety_tripped']}")
        return done("live-safety (the live install on this host degraded)", "host", "high")
    if step.get("disk_low"):
        ev.append(f"Docker data root free {step['disk_low']} GB")
        return done(f"disk space (Docker data root {step['disk_low']} GB free)", "host", "high")
    behind = gen_behind(step)
    if (gen.get("offered_ratio") is not None and gen["offered_ratio"] < 0.99) or (behind or 0) > iv:
        bits = []
        if behind is not None:
            bits.append(f"behind_s max {behind} > {iv:g} s" if behind > iv else f"behind_s max {behind}")
        if gen.get("offered_ratio") is not None:
            bits.append(f"offered {_pct(gen['offered_ratio'])} of target")
        ev.extend(bits)
        return done(f"generator-bound ({', '.join(bits)})", "generator", "high")

    # 2. restarts / OOM
    for svc, c in (step.get("containers") or {}).items():
        if c.get("oom_killed") or c.get("restarts_delta"):
            mem = (soak.get("containers") or {}).get(svc, {}).get("mem_of_cap_max")
            what = "OOM-killed" if c.get("oom_killed") else f"restarted {c['restarts_delta']}x"
            if mem is not None:
                ev.append(f"{svc} memory peak {_pct(mem)} of its cap")
            return done(f"{svc} {what}" + (f" (memory {_pct(mem)} of cap)" if mem is not None else ""), svc,
                        "high" if c.get("oom_killed") or (mem or 0) >= MEM else "medium")

    # 3. graph
    if step.get("graph_timeout"):
        return done(f"graph ingest (tags not in Oxigraph after {step['graph_timeout']} s)", "graph", "low")

    # 4. ingest chain
    symptoms = ingest_symptoms(step, soak)
    if symptoms:
        ev.extend(symptoms)
        name, more, also, conf = attribute_ingest(soak)
        ev.extend(more)
        pw = (step.get("evidence") or {}).get("pg_writes") or {}
        if pw.get("checkpoints_requested"):
            ev.append(f"{pw['checkpoints_requested']} checkpoints requested (WAL reached max_wal_size), "
                      f"{pw.get('wal_fpi')} full-page images in WAL")
        if name.startswith("ingest listener (unattributed") and (step.get("ingest") or {}).get("broker_dropped_delta"):
            name = f"broker drops ({step['ingest']['broker_dropped_delta']:,}; subscriber unattributed)"
        return done(name, name.split(":")[0].split(" (")[0], conf, also)

    # 5. rule daemons on the reading feed
    for svc, r in (step.get("rules") or {}).items():
        f = r.get("feed") or {}
        if f.get("missed_readings_delta") or (f.get("lag_s_last") or 0) > LAG_S:
            what = (f"missed {f['missed_readings_delta']:,} readings" if f.get("missed_readings_delta")
                    else f"{f['lag_s_last']} s behind")
            ev.append(f"{svc} {what} on the reading feed")
            s = cpu_saturation(svc, (soak.get("containers") or {}).get(svc) or {})
            if s and s[0] >= 1.0:
                ev.append(s[1])
                return done(f"{svc} behind on the reading feed: {s[1].split(' ', 1)[1]}", svc, "high")
            return done(f"{svc} behind on the reading feed ({what}; CPU not at its limit)", svc, "low")

    # 6. reads
    p = worst_probe(step)
    if p:
        name, more, conf = read_limiter(step, p)
        ev.extend(more)
        others = [q["name"] for q in step.get("probes") or [] if q is not p and not q.get("skipped")
                  and (q.get("status") != 200 or q["median_s"] > q.get("budget_s", float("inf")))]
        if others:
            ev.append("also over budget: " + ", ".join(others))
        return done(name, "reads", conf, others)

    # 7. anything else evaluate() failed it for
    if step.get("status") == "fail" and step.get("reasons"):
        return done(f"unattributed: {step['reasons'][0]}", "unknown", "low")
    return done("none found", None, None)


def short(lim: dict | None) -> str:
    """One cell for `ladder.py table`: the limiter, or the next suspect."""
    if not lim:
        return "-"
    if lim.get("limiter") == "none found":
        return f"none found; next: {lim.get('next_suspect') or '-'}"
    conf = f" [{lim['confidence']}]" if lim.get("confidence") else ""
    return f"{lim['limiter']}{conf}"
