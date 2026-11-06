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
"""

import argparse
import fnmatch
import graphlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import cast

from rdflib import URIRef

from baler import (
    derivation_health,
    json_store,
    mqtt_util,
    sandbox,
    timeseries,
    tracing,
)
from baler.faults import list_faults
from baler.ingest import link_point_to_equip, topic_to_point_uri
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


def _read_input_series(ts_conn, fault_conn, point_uri: str, start: datetime, end: datetime) -> list[tuple]:
    """An open stale/stuck fault on this point means "treat as absent this
    pass" - empty series, not a silently-passed bad value. Reuses
    fault_detector.py's existing signal instead of a second validity
    concept; see the plan's data-validity section."""
    if fault_conn is not None and list_faults(fault_conn, status="open", point_uri=point_uri):
        return []
    return timeseries.read_range(ts_conn, point_uri, start, end)


def _write_and_attach(store, ts_conn, output_uri: str, target_uri: str, value, now: datetime, output_cfg: dict, input_uris: list[str], span=None) -> None:
    timeseries.write_point_value(ts_conn, output_uri, value, now, unit=output_cfg.get("unit"), label=output_cfg.get("label"))

    output_ref = URIRef(output_uri)
    brick_class = output_cfg.get("brick_class")
    store.add_entity(output_ref, BRICK[brick_class] if brick_class else TD.ComputedPoint)
    link_point_to_equip(store, output_ref, URIRef(target_uri))
    for input_uri in input_uris:
        store.add_derived_from(output_ref, URIRef(input_uri))

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


def _read_formula_inputs(ts_conn, fault_conn, input_uris: dict[str, str], input_windows: dict, window_seconds: float, now: datetime) -> dict[str, list[tuple]]:
    inputs = {}
    for name, point_uri in input_uris.items():
        window = input_windows.get(name, window_seconds)
        inputs[name] = _read_input_series(ts_conn, fault_conn, point_uri, now - timedelta(seconds=window), now)
    return inputs


def _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation: Derivation, now: datetime, dry_run: bool, trigger_point_uri: str | None = None) -> list[TraceEntry]:
    fn = sandbox.compile_fn(derivation["fn_source"])
    derivation_id = derivation["id"]
    target_var = derivation["target_var"]
    input_vars = derivation["input_vars"]
    extra_vars = derivation.get("extra_vars", [])
    window_seconds = derivation["window_seconds"]
    input_windows = derivation.get("input_windows") or {}
    output_cfg = derivation.get("output", {})

    trace: list[TraceEntry] = []
    rows = list(store.query(derivation["select"]))
    for row in rows:
        target_uri = str(getattr(row, target_var))
        input_uris = {name: str(getattr(row, name)) for name in input_vars}

        if trigger_point_uri is not None and trigger_point_uri not in input_uris.values():
            continue
        if health_conn is not None and derivation_health.is_target_disabled(health_conn, derivation_id, target_uri):
            continue

        with tracer.start_as_current_span("derivation_engine.evaluate_target") as span:
            span.set_attribute("derivation_id", derivation_id)
            span.set_attribute("target", target_uri)
            try:
                inputs = _read_formula_inputs(ts_conn, fault_conn, input_uris, input_windows, window_seconds, now)
                row_extra = {name: str(getattr(row, name)) for name in extra_vars}
                value = sandbox.run_with_timeout(fn, inputs, row_extra)
            except Exception as exc:  # noqa: BLE001 - one target's sandboxed formula failing must not stop evaluation of every other target
                trace.append(_record_target_failure(health_conn, derivation_id, target_uri, dry_run, exc, span))
                continue

            trace.append(_record_and_trace_success(
                store, ts_conn, health_conn, derivation_id, target_uri, output_cfg,
                now, dry_run, value, list(input_uris.values()), span,
            ))

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
        if health_conn is not None and derivation_health.is_target_disabled(health_conn, derivation_id, node):
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
    for derivation_id in order:
        derivation = by_id[derivation_id]
        with tracer.start_as_current_span("derivation_engine.evaluate") as span:
            span.set_attribute("derivation_id", derivation_id)
            kind = derivation.get("kind", "formula")
            span.set_attribute("kind", kind)
            try:
                rows = _evaluate_rollup(store, ts_conn, fault_conn, health_conn, derivation, now, dry_run) if kind == "rollup" \
                    else _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation, now, dry_run)
            except Exception as exc:  # noqa: BLE001 - one derivation failing (bad SPARQL, sandboxed code) must not stop the whole sweep
                span.set_attribute("error", str(exc))
                continue
            span.set_attribute("target_count", len(rows))
        trace.extend({"derivation_id": derivation_id, **row} for row in rows)
    return trace


def make_on_message(store, ts_conn, fault_conn, health_conn, cache: json_store.RuleCache):
    """Cur-mode: a cheap fnmatch prefilter (mirrors fault_detector.py's
    Cur-mode) decides which formula derivations even bother re-running
    their select, then trigger_point_uri narrows evaluation to the row(s)
    the incoming point actually feeds - not a full re-sweep. Rollups have
    no Cur-mode analog (a single leaf message triggering a full tree
    re-walk would be wasteful); they're filtered out below."""
    def on_message(client, userdata, msg):
        topic = msg.topic
        point_uri = str(topic_to_point_uri(topic))
        for derivation in cast(list[Derivation], cache.get()):
            glob = derivation.get("applies_to_topic_glob")
            if not glob or derivation.get("kind") == "rollup" or not fnmatch.fnmatch(topic, glob):
                continue
            with tracer.start_as_current_span("derivation_engine.evaluate_cur") as span:
                span.set_attribute("derivation_id", derivation["id"])
                span.set_attribute("topic", topic)
                try:
                    _evaluate_formula(store, ts_conn, fault_conn, health_conn, derivation, datetime.now(timezone.utc), dry_run=False, trigger_point_uri=point_uri)
                except Exception as exc:  # noqa: BLE001 - one MQTT-triggered derivation failing must not crash the listener callback
                    span.set_attribute("error", str(exc))

    return on_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--derivations-file", default=json_store.data_file("derivations.json"))
    parser.add_argument("--dsn", default=timeseries.DEFAULT_DSN)
    parser.add_argument("--oxigraph-url", default=os.environ.get("OXIGRAPH_URL", "http://localhost:7878"))
    parser.add_argument("--tick-seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true", help="run one pass against live data, print the trace, write nothing")
    parser.add_argument("--derivation-id", default=None, help="with --dry-run, evaluate only this derivation")
    args = parser.parse_args()

    tracing.init_tracing("baler-derivation-engine")

    store = RemoteStore(args.oxigraph_url)
    ts_conn = timeseries.connect(args.dsn)
    fault_conn = timeseries.connect(args.dsn)
    health_conn = timeseries.connect(args.dsn)
    derivation_health.ensure_schema(health_conn)

    cache = json_store.RuleCache(args.derivations_file)

    if args.dry_run:
        derivations = cast(list[Derivation], cache.get())
        if args.derivation_id:
            derivations = [d for d in derivations if d["id"] == args.derivation_id]
        for entry in evaluate_derivations(store, ts_conn, fault_conn, health_conn, derivations, dry_run=True):
            print(json.dumps(entry, default=str))
        return

    client = mqtt_util.make_client()
    client.on_message = make_on_message(store, ts_conn, fault_conn, health_conn, cache)
    client.connect(args.mqtt_host, args.mqtt_port)
    client.subscribe("#")  # per-derivation applies_to_topic_glob is the real filter, applied in on_message
    client.loop_start()

    next_run: dict[str, datetime] = {}
    print(f"derivation engine ticking every {args.tick_seconds}s")
    try:
        while True:
            now = datetime.now(timezone.utc)
            derivations = cast(list[Derivation], cache.get())
            due_ids = {d["id"] for d in derivations if now >= next_run.get(d["id"], now)}
            if due_ids:
                evaluate_derivations(store, ts_conn, fault_conn, health_conn, derivations, now=now, dry_run=False, only_ids=due_ids)
                for d in derivations:
                    if d["id"] in due_ids:
                        next_run[d["id"]] = now + timedelta(seconds=d.get("interval_seconds") or args.tick_seconds)
            time.sleep(args.tick_seconds)
    finally:
        client.loop_stop()


if __name__ == "__main__":
    main()
