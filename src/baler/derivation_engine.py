"""
The derivation engine: computes synthetic/derived histories and attaches
them back into the ontology under their target entity, with lineage. Two
kinds share almost everything - a SPARQL query selects what to run over, a
sandboxed Python function computes the value, the result is written to
point_history and wired into the graph via ingest.link_point_to_equip and
store.add_derived_from (both already built for exactly this):

- "formula": select binds a target and named input points; one function
  call per matched row.
- "rollup": root_select binds one or more root entities; the engine walks
  a part_relationship tree under each root itself, computing a value at
  every node bottom-up from its own leaf points plus its already-computed
  children - for aggregation across however many children a node happens
  to have (meter -> equip -> zone -> floor -> building -> campus -> region,
  unbounded depth), which a fixed-arity formula can't express.

Failure isolation is per-(derivation, target) via derivation_health, not
per-derivation: a fn that returns cleanly (even with a bad value) is a
success; only an uncaught exception counts against a target's failure
count. A derivation-level failure (bad SPARQL, a fn_source that won't
compile) skips only that derivation for this pass, not the whole sweep -
same "skip and log" shape as depends_on cycle handling below.

Cur mode (a formula derivation with applies_to_topic_glob re-runs when one
of its inputs reports) reads the reading feed (baler.reading_feed, #243),
not a QoS 0 subscription to `#` that the broker silently dropped from as
soon as this daemon fell behind. Per committed batch, each derivation's
triggered rows are found in an index of its select's rows by input point
(CurTargets, refreshed every --select-refresh seconds or when the
derivation changes) and each runs once, however many of its inputs
reported in the batch - not a SPARQL query and a run per reading. A pass
reads open masking faults and disabled targets once (_Snapshot), not per
input and per target, and a computed point's graph lineage is written once
per process (_attach_once), not on every evaluation.
"""

import argparse
import contextlib
import contextvars
import fnmatch
import graphlib
import json
import os
import re
import threading
import time
import traceback
import weakref
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import cast

from rdflib import URIRef

from baler import (
    derivation_health,
    heartbeat,
    json_store,
    reading_feed,
    sandbox,
    timeseries,
    tracing,
)
from baler.faults import list_faults
from baler.ingest import link_point_to_equip
from baler.remote_store import RemoteStore
from baler.schemas import Derivation, TraceEntry
from baler.store import BRICK, TD

tracer = tracing.get_tracer(__name__)

_PREFIXES = "PREFIX brick: <https://brickschema.org/schema/Brick#>\nPREFIX td: <urn:baler:td#>\n"
_ROLLUP_NODE_CAP = 10_000


def _topological_order(edges: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm via stdlib graphlib.TopologicalSorter - no reason
    to hand-roll it. `edges[node] = [things that must run before node]`.
    Prunes (and logs by omission - callers should treat a missing id as
    skipped) any node whose declared dependency isn't itself a key in
    `edges` (deleted/typo'd id), then any node caught in a cycle, repeating
    until stable since pruning one bad node can cascade to whatever
    depended on it. Shared by cross-derivation depends_on ordering and by
    ordering nodes within one rollup's tree (there, `edges[parent] =
    [children]`, so children run before their parent)."""
    alive = dict(edges)
    changed = True
    while changed:
        changed = False
        for node_id, deps in list(alive.items()):
            if any(dep not in alive for dep in deps):
                del alive[node_id]
                changed = True

    try:
        return list(graphlib.TopologicalSorter(alive).static_order())
    except graphlib.CycleError as exc:
        cyclic = set(exc.args[1])
        return _topological_order({k: v for k, v in alive.items() if k not in cyclic})


def _local_name(uri: str) -> str:
    return re.split(r"[/:]", uri)[-1]


def _output_uri(derivation_id: str, target_uri: str) -> str:
    return f"urn:point:computed/{derivation_id}/{_local_name(target_uri)}"


# Data-validity rules (fault_detector's stale/stuck rules): the only faults
# that make an input count as absent. Reloaded when rules.json changes;
# main() points it at --rules-file.
VALIDITY_RULE_TYPES = ("stale", "stuck")
_rules = json_store.RuleCache(json_store.data_file("rules.json"))


class _Snapshot:
    """One evaluation pass's view of the masking faults and the disabled
    targets, read in two queries instead of one per input and one per
    target (#243). The same answers _masking_fault and is_target_disabled
    give, as of the start of the pass."""

    def __init__(self, fault_conn, health_conn):
        self.masks: dict[str, dict] | None = None
        if fault_conn is not None:
            self.masks = {}
            validity = sorted(r["id"] for r in _rules.get() if r.get("type") in VALIDITY_RULE_TYPES)
            if validity:
                for rule_id, point_uri in fault_conn.execute(
                        "SELECT rule_id, point_uri FROM faults WHERE status = 'open' AND rule_id = ANY(%s) "
                        "ORDER BY started_at DESC", (validity,)).fetchall():
                    self.masks.setdefault(point_uri, {"rule_id": rule_id, "point_uri": point_uri})
        self.disabled: set[tuple[str, str]] | None = None
        if health_conn is not None:
            self.disabled = {(d, t) for d, t in health_conn.execute(
                "SELECT derivation_id, target_uri FROM derivation_target_health WHERE disabled").fetchall()}


_snapshot: contextvars.ContextVar[_Snapshot | None] = contextvars.ContextVar("derivation_snapshot", default=None)


@contextlib.contextmanager
def _pass(fault_conn, health_conn) -> Iterator[None]:
    """Within it, masking faults and disabled targets come from one
    _Snapshot (nested passes share the outer one)."""
    if _snapshot.get() is not None:
        yield
        return
    token = _snapshot.set(_Snapshot(fault_conn, health_conn))
    try:
        yield
    finally:
        _snapshot.reset(token)


def _target_disabled(health_conn, derivation_id: str, target_uri: str) -> bool:
    if health_conn is None:
        return False
    snap = _snapshot.get()
    if snap is not None and snap.disabled is not None:
        return (derivation_id, target_uri) in snap.disabled
    return derivation_health.is_target_disabled(health_conn, derivation_id, target_uri)


def _masking_fault(fault_conn, point_uri: str) -> dict | None:
    """The open fault that masks this input, if any: a stale/stuck fault
    from a rule that still exists. Not any open fault (Baler#58): a
    range or comfort fault says the value is out of bounds, not that it's
    invalid, and a fault from a deleted rule can never resolve, so it
    would mask the input forever - which silently froze five AHUs' fan
    duty cycle for two months."""
    if fault_conn is None:
        return None
    snap = _snapshot.get()
    if snap is not None and snap.masks is not None:
        return snap.masks.get(point_uri)
    validity = {r["id"] for r in _rules.get() if r.get("type") in VALIDITY_RULE_TYPES}
    if not validity:
        return None
    for fault in list_faults(fault_conn, status="open", point_uri=point_uri):
        if fault["rule_id"] in validity:
            return fault
    return None


def _read_input_series(ts_conn, fault_conn, point_uri: str, start: datetime, end: datetime,
                       masked: dict[str, str] | None = None) -> list[tuple]:
    """An open stale/stuck fault on this point means "treat as absent this
    pass" - empty series, not a silently-passed bad value. Reuses
    fault_detector.py's existing signal instead of a second validity
    concept; see the plan's data-validity section. Masked inputs are
    recorded in `masked` (point -> rule id) for the trace."""
    fault = _masking_fault(fault_conn, point_uri)
    if fault is not None:
        if masked is not None:
            masked[point_uri] = fault["rule_id"]
        return []
    return timeseries.read_range(ts_conn, point_uri, start, end)


# Graph lineage already written by this process, per store: the triples
# are the same every evaluation (additive, idempotent), so writing them
# again was 2 + len(inputs) Oxigraph round trips per output per run for
# nothing (#243). Re-asserted every ATTACH_REFRESH_S anyway, so lineage
# removed from the graph by hand (Ops -> Orphaned points) comes back
# within the hour while the derivation still writes, as it did before.
ATTACH_REFRESH_S = 3600.0
_attached: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _attach_once(store, output_uri: str, target_uri: str, brick_class: str | None, input_uris: list[str]) -> None:
    key = (output_uri, target_uri, brick_class, tuple(input_uris))
    try:
        seen = _attached.setdefault(store, {})
    except TypeError:  # a store that can't be weakly referenced: no memo
        seen = {}
    at = seen.get(key)
    if at is not None and time.monotonic() - at < ATTACH_REFRESH_S:
        return
    output_ref = URIRef(output_uri)
    store.add_entity(output_ref, BRICK[brick_class] if brick_class else TD.ComputedPoint)
    link_point_to_equip(store, output_ref, URIRef(target_uri))
    for input_uri in input_uris:
        store.add_derived_from(output_ref, URIRef(input_uri))
    seen[key] = time.monotonic()


def _write_and_attach(store, ts_conn, output_uri: str, target_uri: str, value, now: datetime, output_cfg: dict, input_uris: list[str], span=None) -> None:
    timeseries.write_point_value(ts_conn, output_uri, value, now, unit=output_cfg.get("unit"), label=output_cfg.get("label"))
    _attach_once(store, output_uri, target_uri, output_cfg.get("brick_class"), input_uris)

    if span is not None:
        span.set_attribute("wrote_point", output_uri)
        span.set_attribute("attached_to", target_uri)
        span.set_attribute("derived_from", input_uris)


def _record_target_failure(health_conn, derivation_id: str, target_uri: str, dry_run: bool, exc: Exception, span) -> TraceEntry:
    """Shared by _evaluate_formula's and _walk_rollup_tree's per-target
    except clauses - only the exception-raising computation itself
    differs between formula and rollup, everything after "it failed" is
    identical bookkeeping."""
    span.set_attribute("error", str(exc))
    if health_conn is not None and not dry_run:
        derivation_health.record_target_failure(health_conn, derivation_id, target_uri, str(exc))
    return {"target": target_uri, "error": str(exc)}


def _record_and_trace_success(
    store, ts_conn, health_conn, derivation_id: str, target_uri: str, output_cfg: dict,
    now: datetime, dry_run: bool, value, derived_from: list[str], span,
) -> TraceEntry:
    """Shared by _evaluate_formula's and _walk_rollup_tree's post-compute
    handling: record the health outcome, build the matching trace entry,
    and write+attach the result if it's not None and dry_run is False."""
    if health_conn is not None and not dry_run:
        derivation_health.record_target_success(health_conn, derivation_id, target_uri)

    span.set_attribute("computed_value", "" if value is None else str(value))
    if value is None:
        return {"target": target_uri, "computed_value": None}

    output_uri = _output_uri(derivation_id, target_uri)
    if not dry_run:
        _write_and_attach(store, ts_conn, output_uri, target_uri, value, now, output_cfg, derived_from, span=span)
    return {
        "target": target_uri, "computed_value": value,
        "would_write_uri": output_uri, "would_attach_to": target_uri,
        "would_derive_from": derived_from,
    }


def _read_formula_inputs(ts_conn, fault_conn, input_uris: dict[str, str], input_windows: dict, window_seconds: float,
                         now: datetime, masked: dict[str, str] | None = None) -> dict[str, list[tuple]]:
    inputs = {}
    for name, point_uri in input_uris.items():
        window = input_windows.get(name, window_seconds)
        inputs[name] = _read_input_series(ts_conn, fault_conn, point_uri, now - timedelta(seconds=window), now, masked)
    return inputs


def _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation: Derivation, now: datetime, dry_run: bool,
                      trigger_point_uri: str | None = None, rows: list | None = None) -> list[TraceEntry]:
    """`rows`: the select's rows to run (Cur mode's triggered ones), instead
    of running the select."""
    fn = sandbox.compile_fn(derivation["fn_source"])
    derivation_id = derivation["id"]
    target_var = derivation["target_var"]
    input_vars = derivation["input_vars"]
    extra_vars = derivation.get("extra_vars", [])
    window_seconds = derivation["window_seconds"]
    input_windows = derivation.get("input_windows") or {}
    output_cfg = derivation.get("output", {})

    trace: list[TraceEntry] = []
    if rows is None:
        rows = list(store.query(derivation["select"]))
    for row in rows:
        target_uri = str(getattr(row, target_var))
        input_uris = {name: str(getattr(row, name)) for name in input_vars}

        if trigger_point_uri is not None and trigger_point_uri not in input_uris.values():
            continue
        if _target_disabled(health_conn, derivation_id, target_uri):
            continue

        with tracer.start_as_current_span("derivation_engine.evaluate_target") as span:
            span.set_attribute("derivation_id", derivation_id)
            span.set_attribute("target", target_uri)
            masked: dict[str, str] = {}
            try:
                inputs = _read_formula_inputs(ts_conn, fault_conn, input_uris, input_windows, window_seconds, now, masked)
                row_extra = {name: str(getattr(row, name)) for name in extra_vars}
                value = sandbox.run_with_timeout(fn, inputs, row_extra)
            except Exception as exc:  # noqa: BLE001 - one target's sandboxed formula failing must not stop evaluation of every other target
                trace.append(_record_target_failure(health_conn, derivation_id, target_uri, dry_run, exc, span))
                continue

            entry = _record_and_trace_success(
                store, ts_conn, health_conn, derivation_id, target_uri, output_cfg,
                now, dry_run, value, list(input_uris.values()), span,
            )
            if masked:
                # Visible, not silent: why a target produced nothing (#58).
                entry["masked_inputs"] = masked
                span.set_attribute("masked_inputs", [f"{p} (rule {r})" for p, r in masked.items()])
            trace.append(entry)

    return trace


def _rollup_node_inputs(
    store, ts_conn, fault_conn, derivation_id: str, node: str, leaf_class: str,
    edges: dict[str, list[str]], values: dict[str, object], window_start: datetime, now: datetime,
) -> tuple[list, list[str]]:
    """A node's own directly-attached leaf points, plus each already-
    evaluated child's own computed value folded in as a synthetic
    one-point series - see _walk_rollup_tree's module docstring on why
    a rollup node needs both."""
    leaf_uris = [str(r.p) for r in store.query(f"{_PREFIXES}SELECT ?p WHERE {{ <{node}> brick:hasPoint ?p . ?p a {leaf_class} }}")]
    child_series = [_read_input_series(ts_conn, fault_conn, p, window_start, now) for p in leaf_uris]
    child_uris = list(leaf_uris)
    for child in edges.get(node, []):
        if values.get(child) is not None:
            child_series.append([(now, values[child])])
            child_uris.append(_output_uri(derivation_id, child))
    return child_series, child_uris


def _walk_rollup_tree(store, ts_conn, fault_conn, health_conn, derivation: Derivation, root: str, now: datetime, dry_run: bool) -> list[TraceEntry]:
    derivation_id = derivation["id"]
    part_rel = derivation["part_relationship"]
    leaf_class = derivation["leaf_point_class"]
    window_seconds = derivation["window_seconds"]
    output_cfg = derivation.get("output", {})
    fn = sandbox.compile_fn(derivation["fn_source"])

    edge_rows = list(store.query(
        f"{_PREFIXES}SELECT ?parent ?child WHERE {{ <{root}> ({part_rel})* ?parent . ?parent {part_rel} ?child }}"
    ))
    edges: dict[str, list[str]] = {root: []}
    for row in edge_rows:
        parent, child = str(row.parent), str(row.child)
        edges.setdefault(parent, []).append(child)
        edges.setdefault(child, [])

    if len(edges) > _ROLLUP_NODE_CAP:
        return [{"target": root, "error": f"rollup tree exceeds {_ROLLUP_NODE_CAP}-node cap, aborted"}]

    order = _topological_order(edges)  # children before parents

    trace: list[TraceEntry] = []
    values: dict[str, object] = {}
    window_start = now - timedelta(seconds=window_seconds)

    for node in order:
        if _target_disabled(health_conn, derivation_id, node):
            continue

        with tracer.start_as_current_span("derivation_engine.evaluate_target") as span:
            span.set_attribute("derivation_id", derivation_id)
            span.set_attribute("target", node)
            try:
                child_series, child_uris = _rollup_node_inputs(store, ts_conn, fault_conn, derivation_id, node, leaf_class, edges, values, window_start, now)
                value = sandbox.run_with_timeout(fn, child_series, {"node": node})
            except Exception as exc:  # noqa: BLE001 - one node's sandboxed rollup failing must not stop evaluation of every other node
                trace.append(_record_target_failure(health_conn, derivation_id, node, dry_run, exc, span))
                values[node] = None
                continue

            values[node] = value
            trace.append(_record_and_trace_success(
                store, ts_conn, health_conn, derivation_id, node, output_cfg,
                now, dry_run, value, child_uris, span,
            ))

    return trace


def _evaluate_rollup(store, ts_conn, fault_conn, health_conn, derivation: Derivation, now: datetime, dry_run: bool) -> list[TraceEntry]:
    roots = [str(row.root) for row in store.query(derivation["root_select"])]
    trace: list[TraceEntry] = []
    for root in roots:
        trace.extend(_walk_rollup_tree(store, ts_conn, fault_conn, health_conn, derivation, root, now, dry_run))
    return trace


def evaluate_derivations(store, ts_conn, fault_conn, health_conn, derivations: list[Derivation], now: datetime | None = None, dry_run: bool = False, only_ids: set[str] | None = None) -> list[TraceEntry]:
    """Importable/synchronous - tests and --dry-run both call this
    directly. `only_ids`, when given, evaluates only those derivations
    (for per-derivation interval_seconds scheduling) while still ordering
    and pruning against the FULL `derivations` graph - a dependency that's
    simply not due this tick is not the same as a missing/deleted one, and
    must not be pruned as if it were."""
    now = now or datetime.now(timezone.utc)
    by_id = {d["id"]: d for d in derivations}
    edges = {d["id"]: d.get("depends_on", []) for d in derivations}
    order = _topological_order(edges)
    if only_ids is not None:
        order = [did for did in order if did in only_ids]

    trace: list[TraceEntry] = []
    with _pass(fault_conn, health_conn):
        for derivation_id in order:
            trace.extend(_evaluate_one(store, ts_conn, fault_conn, health_conn, by_id[derivation_id], now, dry_run))
    return trace


def _evaluate_one(store, ts_conn, fault_conn, health_conn, derivation: Derivation, now: datetime, dry_run: bool) -> list[TraceEntry]:
    derivation_id = derivation["id"]
    with tracer.start_as_current_span("derivation_engine.evaluate") as span:
        span.set_attribute("derivation_id", derivation_id)
        kind = derivation.get("kind", "formula")
        span.set_attribute("kind", kind)
        try:
            rows = _evaluate_rollup(store, ts_conn, fault_conn, health_conn, derivation, now, dry_run) if kind == "rollup" \
                else _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation, now, dry_run)
        except Exception as exc:  # noqa: BLE001 - one derivation failing (bad SPARQL, sandboxed code) must not stop the whole sweep
            span.set_attribute("error", str(exc))
            return []
        span.set_attribute("target_count", len(rows))
    return [{"derivation_id": derivation_id, **row} for row in rows]


class CurTargets:
    """Cur-mode formula derivations' select rows, indexed by input point:
    which rows a reading of point P re-runs. A derivation's select runs
    again after `ttl` seconds (new equipment, re-tagged points) or as soon
    as its select/vars change - not once per reading, as the per-message
    path did."""

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._cache: dict[str, tuple[tuple, float, list, dict[str, list[int]]]] = {}

    def rows_for(self, store, derivation: Derivation, triggered: set[str]) -> list:
        key = (derivation["select"], tuple(derivation["input_vars"]), derivation["target_var"])
        hit = self._cache.get(derivation["id"])
        if hit is None or hit[0] != key or time.monotonic() - hit[1] >= self.ttl:
            rows = list(store.query(derivation["select"]))
            by_input: dict[str, list[int]] = {}
            for i, row in enumerate(rows):
                for name in derivation["input_vars"]:
                    by_input.setdefault(str(getattr(row, name)), []).append(i)
            hit = (key, time.monotonic(), rows, by_input)
            self._cache[derivation["id"]] = hit
        index = sorted({i for uri in triggered for i in hit[3].get(uri, ())})
        return [hit[2][i] for i in index]


class _GlobMemo:
    """fnmatch(topic, glob), memoized per glob and topic."""

    def __init__(self):
        self._memo: dict[str, dict[str, bool]] = {}

    def __call__(self, glob: str, topic: str) -> bool:
        memo = self._memo.setdefault(glob, {})
        hit = memo.get(topic)
        if hit is None:
            hit = memo[topic] = fnmatch.fnmatch(topic, glob)
        return hit


def cur_derivations(derivations: list[Derivation]) -> list[Derivation]:
    """The ones a reading re-runs: formula derivations with a topic glob.
    Rollups have no Cur-mode analog (a single leaf message triggering a
    full tree re-walk would be wasteful)."""
    return [d for d in derivations if d.get("applies_to_topic_glob") and d.get("kind") != "rollup"]


def evaluate_cur_points(store, ts_conn, fault_conn, health_conn, derivations: list[Derivation], point_uris: set[str],
                        targets: CurTargets, globs: _GlobMemo | None = None, now: datetime | None = None) -> int:
    """Cur mode for one batch of readings: every formula derivation whose
    topic glob matches a point in `point_uris`, re-run for the rows those
    points feed, each row once. Returns how many rows ran."""
    globs = globs or _GlobMemo()
    ran = 0
    with _pass(fault_conn, health_conn):
        for derivation in cur_derivations(derivations):
            glob = derivation["applies_to_topic_glob"]
            hits = {u for u in point_uris if globs(glob, reading_feed.topic_of(u))}
            if not hits:
                continue
            with tracer.start_as_current_span("derivation_engine.evaluate_cur") as span:
                span.set_attribute("derivation_id", derivation["id"])
                span.set_attribute("triggered_points", len(hits))
                try:
                    rows = targets.rows_for(store, derivation, hits)
                    span.set_attribute("rows", len(rows))
                    if rows:
                        _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation,
                                          now or datetime.now(timezone.utc), dry_run=False, rows=rows)
                        ran += len(rows)
                except Exception as exc:  # noqa: BLE001 - one triggered derivation failing must not stop the others
                    span.set_attribute("error", str(exc))
    return ran


def make_on_message(store, ts_conn, fault_conn, health_conn, cache: json_store.RuleCache, targets: CurTargets | None = None):
    """Cur mode for one MQTT message: evaluate_cur_points for its point.
    main() reads the reading feed instead (a batch at a time); this stays
    for callers that hold a message."""
    targets = targets or CurTargets()
    globs = _GlobMemo()

    def on_message(client, userdata, msg):
        point_uri = f"{reading_feed.URI_PREFIX}{msg.topic}"
        evaluate_cur_points(store, ts_conn, fault_conn, health_conn, cast(list[Derivation], cache.get()), {point_uri},
                            targets, globs)

    return on_message


def run_feed(store, dsn: str, cache: json_store.RuleCache, targets: CurTargets, status: heartbeat.Status,
             stop: threading.Event | None = None) -> None:
    """The Cur-mode thread: the reading feed, a batch at a time, on its own
    connections (psycopg connections aren't for concurrent use)."""
    ts_conn, fault_conn, health_conn, feed_conn = (timeseries.connect(dsn) for _ in range(4))
    consumer = reading_feed.FeedConsumer(feed_conn, "derivation_engine", points_only=True)
    consumer.start()
    globs = _GlobMemo()

    def handle(batch):
        derivations = cast(list[Derivation], cache.get())
        if not batch.readings or not cur_derivations(derivations):
            return
        points = {uri for uri, *_ in batch.readings}
        t0 = time.monotonic()
        status.add("cur_rows", evaluate_cur_points(store, ts_conn, fault_conn, health_conn, derivations, points,
                                                   targets, globs))
        # cumulative seconds evaluating: its rate is how busy Cur mode keeps
        # this thread (the scale ladder's limiter evidence, #273)
        status.add("cur_busy_s", time.monotonic() - t0)

    def report(stats):
        status.update("feed", stats.as_dict())
        status.beat(feed_conn)

    consumer.run(handle, stop=stop, report=report)


class TickStats:
    """The tick loop's own timing, for the heartbeat (#273): the last pass
    (seconds, rows, the shortest interval among the derivations it ran, and
    how late the latest of them started against its schedule), plus running
    totals whose rates the scale ladder reads - passes, rows, seconds busy.
    A pass that takes longer than its interval falls further behind every
    time: `late_s` grows."""

    def __init__(self):
        self.passes, self.rows, self.busy_s = 0, 0, 0.0

    def record(self, seconds: float, rows: int, interval_s: float, late_s: float) -> dict:
        self.passes += 1
        self.rows += rows
        self.busy_s += seconds
        return {"last_s": round(seconds, 3), "last_rows": rows, "interval_s": interval_s, "late_s": round(late_s, 3),
                "passes": self.passes, "rows": self.rows, "busy_s": round(self.busy_s, 3)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--derivations-file", default=json_store.data_file("derivations.json"))
    parser.add_argument("--rules-file", default=json_store.data_file("rules.json"),
                        help="fault rules: open stale/stuck faults from these mask derivation inputs")
    parser.add_argument("--dsn", default=timeseries.DEFAULT_DSN)
    parser.add_argument("--oxigraph-url", default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"))
    parser.add_argument("--tick-seconds", type=float, default=5.0)
    parser.add_argument("--select-refresh", type=float, default=60.0,
                        help="seconds a Cur-mode derivation's select rows are reused before it runs again")
    parser.add_argument("--dry-run", action="store_true", help="run one pass against live data, print the trace, write nothing")
    parser.add_argument("--derivation-id", default=None, help="with --dry-run, evaluate only this derivation")
    args = parser.parse_args()

    tracing.init_tracing("baler-derivation-engine")

    store = RemoteStore(args.oxigraph_url)
    ts_conn = timeseries.connect(args.dsn)
    fault_conn = timeseries.connect(args.dsn)
    health_conn = timeseries.connect(args.dsn)
    derivation_health.ensure_schema(health_conn)
    heartbeat.ensure_schema(health_conn)

    cache = json_store.RuleCache(args.derivations_file)
    global _rules
    _rules = json_store.RuleCache(args.rules_file)

    if args.dry_run:
        derivations = cast(list[Derivation], cache.get())
        if args.derivation_id:
            derivations = [d for d in derivations if d["id"] == args.derivation_id]
        for entry in evaluate_derivations(store, ts_conn, fault_conn, health_conn, derivations, dry_run=True):
            print(json.dumps(entry, default=str))
        return

    status = heartbeat.Status("derivation_engine", tick_seconds=args.tick_seconds, feed={}, cur_rows=0, cur_busy_s=0.0,
                              tick={})

    def feed_thread():
        try:
            run_feed(RemoteStore(args.oxigraph_url), args.dsn, cache, CurTargets(args.select_refresh), status)
        except Exception:  # noqa: BLE001 - exit; the feed offset makes the restart lossless
            traceback.print_exc()
            os._exit(1)

    threading.Thread(target=feed_thread, name="reading-feed", daemon=True).start()

    next_run: dict[str, datetime] = {}
    ticks = TickStats()
    print(f"derivation engine ticking every {args.tick_seconds}s, Cur mode from the reading feed", flush=True)
    while True:
        now = datetime.now(timezone.utc)
        derivations = cast(list[Derivation], cache.get())
        due_ids = {d["id"] for d in derivations if now >= next_run.get(d["id"], now)}
        if due_ids:
            t0 = time.monotonic()
            late = max(((now - next_run[i]).total_seconds() for i in due_ids if i in next_run), default=0.0)
            trace = evaluate_derivations(store, ts_conn, fault_conn, health_conn, derivations, now=now, dry_run=False,
                                         only_ids=due_ids)
            for d in derivations:
                if d["id"] in due_ids:
                    next_run[d["id"]] = now + timedelta(seconds=d.get("interval_seconds") or args.tick_seconds)
            interval = min((d.get("interval_seconds") or args.tick_seconds) for d in derivations if d["id"] in due_ids)
            status.update("tick", ticks.record(time.monotonic() - t0, len(trace), interval, late))
        status.beat(health_conn)
        time.sleep(args.tick_seconds)


if __name__ == "__main__":
    main()
