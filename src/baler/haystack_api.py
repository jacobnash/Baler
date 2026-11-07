"""
The Project Haystack HTTP API, served from Baler's own stores, so
anything that speaks Haystack (SkySpark, Haxall's haystack connector,
nhaystack's client side, pyhaystack, phable, openhay's `hay`) can point at
Baler by changing only the host:

    GET/POST /api/{project}/{op}      (SkySpark layout; `/api/{op}` also works)

Wire formats, value types and the SCRAM math come from `openhay` (Rust core,
BSD-3). This file is only the mapping onto Baler's storage:

  - Records are rebuilt from the entity graph. `haystack:hasTag` becomes a
    Marker, `haystack:<tag>` literals become values, `rdf:type` becomes
    `brickClass`, and `brick:isPointOf` becomes `equipRef`. `point` and
    `equip` come from the URI kind. `curVal` and `unit` come from the
    newest history sample.
  - Filters go through hisquery's parser and SPARQL compiler, so a filter
    matches Haystack markers, Brick classes and Brick tag words exactly as
    `/ingest/his` does. `->` tag paths aren't supported there and are a
    clear error here too.
  - Ref ids are the entity URIs with every character outside Haystack's
    ref alphabet escaped as `~XX` (reversible, see uri_to_ref).
  - Auth: Haystack clients log in with HELLO/SCRAM against the *existing*
    users table. Baler's password hash (`salt$PBKDF2-SHA256`, 600k
    rounds, 32-byte key) is exactly SCRAM's SaltedPassword, so no password
    reset or new column is needed. A successful login returns the normal
    session JWT as the Haystack `authToken`. The gateway verifies it on
    every later request (`BEARER authToken=<jwt>` or plain `Bearer <jwt>`,
    so OAuth-style API-key clients work too).

Site scoping (scope.py): once any site owns a topic prefix, every read,
history, nav and watch op only sees points/equipment under the caller's
sites' prefixes; anything else reads as missing.

Not here yet: pointWrite/invokeAction (needs Windrow BACnet write),
Haystack 4 defs/libs ops.
"""

from __future__ import annotations

import argparse
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import openhay as oh

from baler import auth, gateway_auth, hisquery, timeseries, tracing
from baler import point_write as pw
from baler.http_handler_base import BaseAPIHandler
from baler.scope import EVERYTHING, Scope, claims_from_header, scope_for
from baler.timeseries import connect_pool, read_latest_many

tracer = tracing.get_tracer(__name__)

SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "haystack-api-openapi.yaml")
DEFAULT_TZ = os.environ.get("BALER_TZ") or "UTC"
HIS_LIMIT = hisquery.MAX_LIMIT

HAYSTACK_NS = "urn:baler:haystack#"
HUNIT_NS = "urn:baler:haystack-unit#"
HTZ_NS = "urn:baler:haystack-tz#"
DICT_NS = "urn:baler:dict:"
XSD = "http://www.w3.org/2001/XMLSchema#"
BRICK_NS = hisquery.BRICK_NS
TD_NS = "urn:baler:td#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Raw tags we never echo back verbatim: identity and references are
# recomputed from the graph (a pulled record's original `id`/`equipRef`
# point at the *source* server's ids, not ours), and live values come from
# history, not a snapshot taken at pull time.
_RECOMPUTED_TAGS = {"id", "equipRef", "siteRef", "spaceRef", "curVal", "writeVal", "unit", "mod"}

OPS = [
    ("about", "Summary information for server"),
    ("ops", "Operations supported by this server"),
    ("formats", "Grid data formats supported by this server"),
    ("read", "Read records by id or filter"),
    ("nav", "Navigate record tree"),
    ("hisRead", "Read time series history data"),
    ("hisWrite", "Write time series history data"),
    ("watchSub", "Subscribe to watch"),
    ("watchUnsub", "Unsubscribe from watch"),
    ("watchPoll", "Poll watch for changes"),
    ("close", "Close the current session"),
    ("defs", "Read def records from the Haystack 4 namespace"),
    ("libs", "Read lib def records"),
    ("filetypes", "Read filetype def records"),
]
DEFS_OPS = {"defs", "libs", "filetypes"}
_BRICK_LINK_TAGS = {BRICK_NS + "hasPart": "brickHasPart", BRICK_NS + "feeds": "brickFeeds"}
_FILETYPES_SERVED = {"filetype:zinc", "filetype:json", "filetype:trio", "filetype:csv"}
_SERVED_MIMES = ("text/zinc", "application/json", "text/trio", "text/csv", "text/plain", "*/*", "text/*", "application/*")
WRITE_OPS = {"hisWrite", "pointWrite", "invokeAction"}
# Ops that return no point/equip data, so need no site scope lookup.
_DATA_FREE_OPS = {"about", "ops", "formats", "close", "watchUnsub"} | DEFS_OPS


class OpError(Exception):
    """Rendered as a Haystack error grid (HTTP 200, `err` meta), per spec."""


# --------------------------------------------------------------------------- refs

_REF_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-.")


def uri_to_ref(uri: str) -> str:
    """`urn:point:fbf/ahu-3/analogValue,1` -> `urn:point:windrow~2Fahu-3~2FanalogValue~2C1`.
    Haystack ref ids allow only `[A-Za-z0-9_:\\-.~]`, so everything else (and
    `~` itself) is escaped byte-wise as `~XX`."""
    out = []
    for ch in uri:
        if ch in _REF_SAFE:
            out.append(ch)
        else:
            out.extend(f"~{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def _decode_refs(node):
    """A filter's @refs are ids as this API hands them out (`~XX`-escaped
    URIs); the graph wants the URIs. Unescaped ids pass through unchanged."""
    if isinstance(node, hisquery.Cmp) and isinstance(node.value, hisquery.Ref):
        return hisquery.Cmp(node.name, node.op, hisquery.Ref(ref_to_uri(node.value.id)))
    if isinstance(node, hisquery.Path):
        return hisquery.Path(node.hops, _decode_refs(node.leaf))
    if isinstance(node, hisquery.Not):
        return hisquery.Not(_decode_refs(node.node))
    if isinstance(node, (hisquery.And, hisquery.Or)):
        return type(node)(_decode_refs(node.left), _decode_refs(node.right))
    return node


def ref_to_uri(ref: str) -> str:
    ref = ref.lstrip("@")
    raw = bytearray()
    i = 0
    while i < len(ref):
        if ref[i] == "~" and i + 3 <= len(ref):
            try:
                raw.append(int(ref[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        raw.extend(ref[i].encode("utf-8"))
        i += 1
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- records


def _kind_of(uri: str) -> str | None:
    if uri.startswith("urn:point:"):
        return "point"
    if uri.startswith("urn:equip:"):
        return "equip"
    if uri.startswith("urn:site:"):
        return "site"
    return None


def _literal(value, datatype: str | None = None):
    """A stored literal -> a Haystack value. RemoteStore already hands back
    int/float/str; rdflib (tests, in-memory Store) hands back Literals. The
    datatype (projected by build_records' query, so it survives either
    store) restores what ingest typed: Refs, dates and times."""
    dt = str(datatype or "")
    if dt == HAYSTACK_NS + "Ref":
        rid = str(value)
        return oh.Ref(uri_to_ref(rid if rid.startswith("urn:") else f"urn:equip:haystack:{rid}"))
    if dt == XSD + "anyURI":
        return oh.Uri(str(value))
    if dt in (XSD + "date", XSD + "time", XSD + "dateTime"):
        if hasattr(value, "toPython"):
            value = value.toPython()
        if isinstance(value, str):
            parse = {XSD + "date": date.fromisoformat, XSD + "time": time.fromisoformat,
                     XSD + "dateTime": datetime.fromisoformat}[dt]
            value = parse(value.replace("Z", "+00:00"))
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    if hasattr(value, "toPython"):
        value = value.toPython()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return oh.Number(float(value))
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return str(value)


def _triples_of(store, values: str):
    """(subject, predicate, object, object is an IRI, object's datatype) for
    every triple of the VALUES-listed subjects, in one round trip. Against
    Oxigraph the raw JSON bindings already say what kind of term each object
    is and its datatype; projecting isIRI()/DATATYPE() as extra columns
    nearly doubled the result (7.5 MB vs 4 MB for 800 points) and its parse,
    Baler#107. Objects decode as RemoteStore's SparqlRow does (numbers to
    int/float, everything else its lexical form)."""
    if hasattr(store, "select"):
        for b in store.select(f"SELECT ?s ?p ?o WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o . }}"):
            o = b["o"]
            v, dt = o["value"], o.get("datatype")
            if dt and o["type"] == "literal":
                if dt.endswith(("integer", "int")):
                    v = int(v)
                elif dt.endswith(("double", "float", "decimal")):
                    v = float(v)
            yield b["s"]["value"], b["p"]["value"], v, o["type"] == "uri", dt
        return
    rows = store.query(f"SELECT ?s ?p ?o (isIRI(?o) AS ?iri) (DATATYPE(?o) AS ?dt) WHERE {{ VALUES ?s {{ {values} }} ?s ?p ?o . }}")
    for row in rows:
        is_iri = getattr(row, "iri", False)
        is_iri = is_iri if isinstance(is_iri, bool) else str(is_iri).lower() in ("true", "1")
        yield str(row.s), str(row.p), row.o, is_iri, getattr(row, "dt", None)


def _history_facts(ts_conn, point_uris: list[str]):
    """(latest sample, first sample time, latest unit/label) per point with
    history, for build_records."""
    latest = read_latest_many(ts_conn, point_uris) if ts_conn is not None and point_uris else {}
    first = read_first_ts_many(ts_conn, list(latest)) if latest else {}
    labels = timeseries.read_point_labels(ts_conn, list(latest)) if latest else {}
    return latest, first, labels


def _graph_records(store, values: str) -> tuple[dict[str, dict], dict[str, list[str]], dict[str, object]]:
    """build_records' graph half: every entity's stored tags, typed; plus
    its classes and any stored siteRef."""
    recs: dict[str, dict] = {}
    classes: dict[str, list[str]] = {}
    stored_site: dict[str, object] = {}  # a siteRef tag (Haystack imports), used when the graph has none
    units: dict[str, dict[str, str]] = {}
    tzs: dict[str, dict[str, str]] = {}
    dicts: dict[str, dict[str, str]] = {}  # entity -> tag -> nested Dict node
    for s, p, o, is_iri, dt in _triples_of(store, values):
        rec = recs.setdefault(s, {})
        if p.startswith(HUNIT_NS):
            units.setdefault(s, {})[p[len(HUNIT_NS):]] = str(o)
        elif p.startswith(HTZ_NS):
            tzs.setdefault(s, {})[p[len(HTZ_NS):]] = str(o)
        elif p.startswith(HAYSTACK_NS) and is_iri and str(o).startswith(DICT_NS):
            dicts.setdefault(s, {})[p[len(HAYSTACK_NS):]] = str(o)
        elif p == HAYSTACK_NS + "hasTag":
            rec[str(o)] = oh.MARKER
        elif p.startswith(HAYSTACK_NS) and not is_iri:
            tag = p[len(HAYSTACK_NS):]
            if tag == "siteRef":
                stored_site[s] = _literal(o, dt)
            if tag not in _RECOMPUTED_TAGS:
                val = _literal(o, dt)
                if tag in rec and isinstance(val, oh.Ref):
                    # a Ref list is stored one triple per Ref
                    prev = rec[tag]
                    rec[tag] = (prev if isinstance(prev, list) else [prev]) + [val]
                else:
                    rec[tag] = val
            elif tag == "unit":
                rec["unit"] = str(_literal(o))
        elif p == RDF_TYPE:
            o = str(o)
            if not o.startswith(TD_NS):
                classes.setdefault(s, []).append(o)
        elif p == BRICK_NS + "isPointOf":
            rec["equipRef"] = oh.Ref(uri_to_ref(str(o)))
        elif p in _BRICK_LINK_TAGS and is_iri:
            # Brick containment/feeds, as Ref lists: not standard Haystack
            # tags (clients ignore them), but they let a client - the UI's
            # tree - walk Site > Building > Floor > Zone and feeds through
            # this API instead of querying the graph directly.
            rec.setdefault(_BRICK_LINK_TAGS[p], []).append(oh.Ref(uri_to_ref(str(o))))

    for s, by_tag in units.items():
        for tag, unit in by_tag.items():
            v = recs.get(s, {}).get(tag)
            if isinstance(v, oh.Number):
                recs[s][tag] = oh.Number(v.val, unit)
    for s, by_tag in tzs.items():  # DateTimes back in their own Haystack timezone
        for tag, name in by_tag.items():
            v = recs.get(s, {}).get(tag)
            if isinstance(v, datetime):
                recs[s][tag] = v.astimezone(oh.haystack_tz(name))
    if dicts:  # nested Dicts, rebuilt from their nodes (recursively)
        nested = build_records(store, None, sorted({n for d in dicts.values() for n in d.values()}), values_only=True)
        for s, by_tag in dicts.items():
            for tag, node in by_tag.items():
                recs.setdefault(s, {})[tag] = nested.get(node, {})
    return recs, classes, stored_site


def build_records(store, ts_conn, uris: list[str], values_only: bool = False) -> dict[str, dict]:
    """One SPARQL round trip for every triple of every requested entity,
    plus one SQL round trip for the newest sample of every point."""
    if not uris:
        return {}
    values = " ".join(f"<{hisquery._uri(u)}>" for u in uris)
    # The site lookup (Oxigraph) and the history lookups (Postgres) don't
    # depend on the triples: they run alongside the main query, not after it.
    side = None
    if not values_only:
        side = ThreadPoolExecutor(max_workers=2)
        sites_f = side.submit(_site_of, store, uris)
        history_f = side.submit(_history_facts, ts_conn, [u for u in uris if _kind_of(u) == "point"])
    try:
        recs, classes, stored_site = _graph_records(store, values)
    finally:
        if side is not None:
            side.shutdown(wait=True)
    if values_only:
        return recs
    sites = sites_f.result()
    latest, first, labels = history_f.result()
    out = {}
    for uri in uris:
        if uri not in recs and uri not in latest:
            continue
        rec = recs.get(uri, {})
        kind = _kind_of(uri)
        if kind:
            rec[kind] = oh.MARKER
        rec["id"] = oh.Ref(uri_to_ref(uri), str(rec.get("dis") or hisquery._local_name(uri)))
        cls = sorted(classes.get(uri, []), key=lambda c: (not c.startswith(BRICK_NS), c))
        if cls:
            rec["brickClass"] = hisquery._local_name(cls[0])
        if BRICK_NS + "Site" in classes.get(uri, []):
            rec["site"] = oh.MARKER
        if uri in sites:
            rec["siteRef"] = oh.Ref(uri_to_ref(sites[uri]), hisquery._local_name(sites[uri]))
        elif isinstance(stored_site.get(uri), oh.Ref):
            rec["siteRef"] = stored_site[uri]
        if uri in latest:
            ts, val = latest[uri]
            unit, label = labels.get(uri, (None, None))
            unit = rec.get("unit") or unit
            if label and "dis" not in rec:
                rec["dis"] = label
            if isinstance(val, bool):
                rec["curVal"] = val
                rec.setdefault("kind", "Bool")
            elif isinstance(val, (int, float)):
                rec["curVal"] = oh.Number(float(val), unit)
                rec.setdefault("kind", "Number")
            elif val is not None:
                rec["curVal"] = str(val)
                rec.setdefault("kind", "Str")
            if unit:
                rec["unit"] = unit
            rec["his"] = oh.MARKER
            rec["curStatus"] = "ok"
            rec["mod"] = ts
            # history extent, as Haxall shows it (no hisSize: a count over
            # every row is the scan these probes exist to avoid)
            rec["hisEnd"] = ts
            if uri in first:
                rec["hisStart"] = first[uri]
        rec.setdefault("tz", DEFAULT_TZ)
        if "spec" not in rec:  # a stored spec (a Haystack 4 import) wins
            spec = best_spec(rec)
            if spec:
                rec["spec"] = oh.Ref(spec)
        out[uri] = rec
    return out


def best_spec(rec: dict) -> str | None:
    """The most specific ph Xeto spec the record fits (Baler#22): its own
    markers, plus those its Brick class implies (hisquery.xeto_spec_for)."""
    markers = {k for k, v in rec.items() if v is oh.MARKER}
    cls = rec.get("brickClass")
    return hisquery.xeto_spec_for(markers, cls if isinstance(cls, str) else None)


def _site_of(store, uris: list[str]) -> dict[str, str]:
    """Each entity's Brick Site (hisquery.SITE_MEMBER), in one query."""
    if not uris:
        return {}
    values = " ".join(f"<{hisquery._uri(u)}>" for u in uris)
    rows = store.query(
        hisquery.PREFIXES
        + f"SELECT ?e ?site WHERE {{ VALUES ?e {{ {values} }} ?site a brick:Site . ?site {hisquery.SITE_MEMBER} ?e }}"
    )
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(str(r.e), str(r.site))
    return out


def read_first_ts_many(conn, point_uris: list[str]) -> dict[str, datetime]:
    """Each point's oldest sample time: one forward primary-key probe each."""
    if not point_uris:
        return {}
    cur = conn.execute(
        """
        SELECT u.uri, h.ts FROM unnest(%s::text[]) AS u(uri)
        CROSS JOIN LATERAL (
            SELECT ts FROM point_history WHERE point_uri = u.uri ORDER BY ts LIMIT 1
        ) h
        """,
        (point_uris,),
    )
    return dict(cur.fetchall())


# ------------------------------------------------------------ live-tag filters

# Tags build_records derives from point_history, never stored in the graph.
_LIVE_TAGS = {"his", "curVal", "curStatus"}
# "every entity", for a filter whose relaxation constrains nothing (`not his`)
_ANY_ENTITY = hisquery.parse_query("point or equip or site or Site or Building or Floor or Zone").filter


def _live(node) -> bool:
    if isinstance(node, (hisquery.Has, hisquery.Cmp)):
        return node.name in _LIVE_TAGS
    if isinstance(node, hisquery.Not):
        return _live(node.node)
    if isinstance(node, (hisquery.And, hisquery.Or)):
        return _live(node.left) or _live(node.right)
    return False


def _relax(node):
    """A graph-only filter matching a superset of `node` (None: matches
    everything). Live terms, and any `not` over one, become "true" - sound
    because and/or are monotone and `not` only wraps exact subtrees."""
    if not _live(node):
        return node
    if isinstance(node, (hisquery.Has, hisquery.Cmp, hisquery.Not)):
        return None
    left, right = _relax(node.left), _relax(node.right)
    if isinstance(node, hisquery.And):
        if left is None or right is None:
            return right if left is None else left
        return hisquery.And(left, right)
    return None if left is None or right is None else hisquery.Or(left, right)


def _graph_sets(store, node, sets: dict[int, set[str]], scope: Scope = EVERYTHING) -> None:
    """The SPARQL answer for each maximal graph-only subtree (path hops
    scoped like any read)."""
    if not _live(node):
        sets[id(node)] = set(hisquery.match_entities(store, node, scope=scope))
    elif isinstance(node, hisquery.Not):
        _graph_sets(store, node.node, sets, scope)
    elif isinstance(node, (hisquery.And, hisquery.Or)):
        _graph_sets(store, node.left, sets, scope)
        _graph_sets(store, node.right, sets, scope)


def _eval_live(node, uri: str, rec: dict, sets: dict[int, set[str]]) -> bool:
    if id(node) in sets:
        return uri in sets[id(node)]
    if isinstance(node, hisquery.Has):
        return rec.get(node.name) is not None
    if isinstance(node, hisquery.Cmp):
        return _cmp_live(rec.get(node.name), node.op, node.value)
    if isinstance(node, hisquery.Not):
        return not _eval_live(node.node, uri, rec, sets)
    if isinstance(node, hisquery.And):
        return _eval_live(node.left, uri, rec, sets) and _eval_live(node.right, uri, rec, sets)
    return _eval_live(node.left, uri, rec, sets) or _eval_live(node.right, uri, rec, sets)


_CMP = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def _cmp_live(have, op: str, want) -> bool:
    """Haystack comparison semantics, as hisquery compiles them: never on a
    missing tag, never across kinds, Numbers only with an identical unit."""
    if have is None or op not in _CMP:
        return False
    if isinstance(have, oh.Number):
        if not isinstance(want, hisquery.Num) or (have.unit or None) != (want.unit or None):
            return False
        return _CMP[op](have.val, want.val)
    if isinstance(have, bool):
        return isinstance(want, bool) and op in ("==", "!=") and _CMP[op](have, want)
    if isinstance(have, str):
        return isinstance(want, str) and _CMP[op](have, want)
    return False


# --------------------------------------------------------------------------- backend


class Backend:
    """Haystack ops over (graph store, Postgres pool). No HTTP in here."""

    def __init__(self, store, ts_pool, max_watches: int = 1000, max_watch_ids: int = 10_000, commander=None):
        self.store = store
        self.ts_pool = ts_pool
        self.commander = commander if commander is not None else pw.Commander()
        self.watches = WatchRegistry(max_watches, max_watch_ids)
        self.boot = datetime.now(timezone.utc)

    def _conn(self):
        return self.ts_pool.connection()

    def read_filter(self, text: str, limit: int | None, scope: Scope = EVERYTHING) -> list[dict]:
        try:
            q = hisquery.parse_query(text)
        except hisquery.HisQueryError as exc:
            raise OpError(f"filter: {exc}") from exc
        if q.span_text is not None or q.rollup is not None or q.mode != "readAll":
            raise OpError("read takes a plain Haystack filter, not a hisRead/hisRollup expression")
        node = _decode_refs(q.filter)
        if not _live(node):
            with tracer.start_as_current_span("haystack.read.match_entities") as span:
                uris = hisquery.match_entities(self.store, node, scope=scope)
                span.set_attribute("matched", len(uris))
            if limit is not None:
                uris = uris[:limit]
            with self._conn() as conn, tracer.start_as_current_span("haystack.read.build_records") as span:
                span.set_attribute("records", len(uris))
                recs = build_records(self.store, conn, uris)
            return [recs[u] for u in uris if u in recs]
        # `his`/`curVal`/`curStatus` come from point_history, not the graph,
        # so SPARQL alone can't decide them. Candidates from a relaxed filter,
        # then the exact filter: graph-only parts by SPARQL set membership,
        # live tags against the built record.
        relaxed = _relax(node)
        cands = hisquery.match_entities(self.store, relaxed if relaxed is not None else _ANY_ENTITY, scope=scope)
        sets: dict[int, set[str]] = {}
        _graph_sets(self.store, node, sets, scope)
        with self._conn() as conn:
            recs = build_records(self.store, conn, cands)
        out = [recs[u] for u in cands if u in recs and _eval_live(node, u, recs[u], sets)]
        return out[:limit] if limit is not None else out

    def read_ids(self, refs: list[oh.Ref], scope: Scope = EVERYTHING) -> list[dict | None]:
        uris = [ref_to_uri(r.id) for r in refs]
        visible = hisquery.visible_uris(self.store, uris, scope)
        with self._conn() as conn:
            recs = build_records(self.store, conn, visible)
        return [recs.get(u) for u in uris]  # out-of-scope ids read as missing, never as "forbidden"

    def nav(self, nav_id: str | None, scope: Scope = EVERYTHING) -> list[dict]:
        """site -> equip -> point, like Haxall. Sites are Brick Sites; equips
        that no site reaches are listed at the root beside them."""
        if nav_id is None:
            rows = self.store.query(
                hisquery.PREFIXES
                + "SELECT DISTINCT ?s WHERE { { ?s a brick:Site } UNION { ?s ?p ?o . "
                "FILTER(STRSTARTS(STR(?s), \"urn:equip:\")) "
                f"FILTER NOT EXISTS {{ ?site a brick:Site . ?site {hisquery.SITE_MEMBER} ?s }} }} }}"
            )
            uris = hisquery.visible_uris(self.store, sorted({str(r.s) for r in rows}), scope)
        elif _kind_of(ref_to_uri(nav_id)) != "equip" and list(self.store.query(
                hisquery.PREFIXES + f"SELECT ?t WHERE {{ <{hisquery._uri(ref_to_uri(nav_id))}> a brick:Site . BIND(1 AS ?t) }}")):
            site = hisquery._uri(ref_to_uri(nav_id))
            rows = self.store.query(
                hisquery.PREFIXES
                + f"SELECT DISTINCT ?e WHERE {{ <{site}> {hisquery.SITE_MEMBER} ?e . "
                "FILTER(STRSTARTS(STR(?e), \"urn:equip:\")) }"
            )
            uris = hisquery.visible_uris(self.store, sorted({str(r.e) for r in rows}), scope)
        else:
            equip = ref_to_uri(nav_id)
            rows = self.store.query(
                hisquery.PREFIXES
                + f"SELECT DISTINCT ?p WHERE {{ {{ <{hisquery._uri(equip)}> brick:hasPoint ?p }}"
                f" UNION {{ ?p brick:isPointOf <{hisquery._uri(equip)}> }} }}"
            )
            uris = hisquery.visible_uris(self.store, sorted({str(r.p) for r in rows}), scope)
        with self._conn() as conn:
            recs = build_records(self.store, conn, uris)
        out = []
        for u in uris:
            rec = recs.get(u)
            if rec is None:
                continue
            if "equip" in rec or "site" in rec:
                rec["navId"] = uri_to_ref(u)
            out.append(rec)
        return out

    def display_name(self, user_id: str | None) -> str:
        """`who` for a write that didn't name one: the user's email (what a
        person reading the priority array recognises), not their id."""
        if not user_id:
            return "baler"
        try:
            with self._conn() as conn:
                user = auth.get_user(conn, user_id)
        except Exception:  # noqa: BLE001 - a lookup failure must not block the write
            user = None
        return user["email"] if user else user_id

    def _writable_point(self, ref: oh.Ref, scope: Scope) -> tuple[str, dict]:
        uri = ref_to_uri(ref.id)
        rec = self.read_ids([ref], scope)[0]
        if rec is None or _kind_of(uri) != "point":
            raise OpError(f"unknown rec: @{ref.id}")
        return uri, rec

    def point_write(self, ref: oh.Ref, level: int, val, who: str, user_sub: str | None,
                    scope: Scope = EVERYTHING, duration_s: float | None = None) -> None:
        """Command the point through the Windrow instance that owns it (see
        point_write.py), then record the level. val None relinquishes.
        Level 17 is the default (BACnet relinquish-default). At level 8 a
        duration makes a timed override, relinquished when it runs out
        (expire_overrides); without one it stays, as in haxall."""
        if not 1 <= level <= 17:
            raise OpError(f"level must be 1-17, got {level}")
        if level == 17 and val is None:
            raise OpError("level 17 (default) can be set but not relinquished")
        if duration_s is not None and level != 8:
            raise OpError("duration only applies to level 8")
        uri, rec = self._writable_point(ref, scope)
        if "writable" not in rec:
            raise OpError(f"@{ref.id} is not writable")
        equip_ref = rec.get("equipRef")
        equip_uri = ref_to_uri(equip_ref.id) if isinstance(equip_ref, oh.Ref) else None
        with self._conn() as conn:
            equip = build_records(self.store, conn, [equip_uri]).get(equip_uri, {}) if equip_uri else {}
        site, instance = equip.get("windrowSite"), equip.get("windrowInstance")
        if not isinstance(site, str) or not isinstance(instance, str):
            raise OpError(f"no command route for @{ref.id}: its Windrow instance runs without --site-id")
        value = _command_value(val)
        try:
            self.commander.point_write(site, instance, uri[len("urn:point:"):], value, level)
        except pw.CommandError as exc:
            raise OpError(str(exc)) from exc
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_s)
                      if duration_s is not None and value is not None else None)
        with self._conn() as conn:
            pw.record_level(conn, uri, level, value, who, user_sub, expires_at)

    def expire_overrides(self) -> int:
        """Relinquish every timed level-8 override whose duration is up.
        Called on a timer by main(); returns how many ended. A point whose
        site can't be reached right now is retried on the next tick."""
        with self._conn() as conn:
            due = pw.expired(conn, datetime.now(timezone.utc))
        ended = 0
        for uri, level, who in due:
            try:
                self.point_write(oh.Ref(uri_to_ref(uri)), level, None, who, None)
                ended += 1
            except OpError as exc:
                tracer.start_span("haystack.expire_override_failed",
                                  attributes={"point": uri, "error": str(exc)}).end()
        return ended

    def point_write_array(self, ref: oh.Ref, scope: Scope = EVERYTHING) -> oh.Grid:
        uri, _ = self._writable_point(ref, scope)
        with self._conn() as conn:
            stored = pw.levels(conn, uri)
        rows = []
        now = datetime.now(timezone.utc)
        for level in pw.LEVELS:
            row = {"level": oh.Number(level), "levelDis": pw.LEVEL_DIS.get(level, str(level))}
            if level in stored:
                v = stored[level]["val"]
                row["val"] = oh.Number(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                row["valDis"] = str(v)
                row["who"] = stored[level]["who"]
                exp = stored[level].get("expires_at")
                if exp is not None:
                    row["expires"] = oh.Number(max(0.0, (exp - now).total_seconds()), "s")
            rows.append(row)
        return oh.Grid.from_rows(rows, cols=["level", "levelDis", "val", "valDis", "who", "expires"])

    def his_read(self, ref: oh.Ref, range_text: str, scope: Scope = EVERYTHING) -> oh.Grid:
        uri = ref_to_uri(ref.id)
        if not scope.allows(uri):
            raise OpError(f"unknown rec: @{ref.id}")
        with self._conn() as conn:
            rec = build_records(self.store, conn, [uri]).get(uri)
            if rec is None:
                raise OpError(f"unknown rec: @{ref.id}")
            raw_tz = rec.get("tz")
            tz: str = raw_tz if isinstance(raw_tz, str) else DEFAULT_TZ
            span = _resolve_range(range_text, tz)
            history = timeseries.read_range_many(conn, [uri], span.start, span.end, HIS_LIMIT)
        rows, truncated = history.get(uri, ([], False))
        zone = oh.haystack_tz(tz)
        unit = rec["curVal"].unit if isinstance(rec.get("curVal"), oh.Number) else None
        out = []
        for ts, val in rows:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                val = oh.Number(float(val), unit)
            out.append({"ts": ts.astimezone(zone), "val": val})
        meta = {"id": rec["id"], "hisStart": span.start.astimezone(zone), "hisEnd": span.end.astimezone(zone)}
        if truncated:
            meta["hisLimit"] = oh.Number(HIS_LIMIT)
        return oh.Grid.from_rows(out, meta=meta, cols=["ts", "val"])

    def his_read_batch(self, refs: list[oh.Ref], range_text: str, tz_name: str | None,
                       scope: Scope = EVERYTHING) -> oh.Grid:
        """Batch hisRead (meta `range`, one `id` row per point): one grid,
        `ts` plus `v0..vN`, each `vN` column carrying its point's id. Like
        haxall, the points must share a timezone unless the request names
        one in meta `tz`."""
        uris = [ref_to_uri(r.id) for r in refs]
        if not uris:
            raise OpError("hisRead batch needs at least one id")
        for r, u in zip(refs, uris):
            if not scope.allows(u):
                raise OpError(f"unknown rec: @{r.id}")
        with self._conn() as conn:
            recs = build_records(self.store, conn, uris)
            missing = [r.id for r, u in zip(refs, uris) if u not in recs]
            if missing:
                raise OpError(f"unknown rec: @{missing[0]}")
            if tz_name is None:
                tzs: set[str] = {str(t) if isinstance(t := recs[u].get("tz"), str) else DEFAULT_TZ for u in uris}
                if len(tzs) > 1:
                    raise OpError("points do not share a timezone; pass tz in the request meta")
                tz_name = tzs.pop()
            span = _resolve_range(range_text, tz_name)
            history = timeseries.read_range_many(conn, uris, span.start, span.end, HIS_LIMIT)
        zone = oh.haystack_tz(tz_name)
        by_ts: dict[datetime, dict] = {}
        for i, u in enumerate(uris):
            unit = recs[u]["curVal"].unit if isinstance(recs[u].get("curVal"), oh.Number) else None
            for ts, val in history.get(u, ([], False))[0]:
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    val = oh.Number(float(val), unit)
                by_ts.setdefault(ts, {"ts": ts.astimezone(zone)})[f"v{i}"] = val
        cols = [oh.Col("ts")] + [oh.Col(f"v{i}", {"id": recs[u]["id"]}) for i, u in enumerate(uris)]
        meta = {"hisStart": span.start.astimezone(zone), "hisEnd": span.end.astimezone(zone)}
        grid = oh.Grid(meta=meta, cols=cols, rows=[by_ts[t] for t in sorted(by_ts)])
        return grid

    def his_write(self, ref: oh.Ref, rows: list[dict], scope: Scope = EVERYTHING) -> None:
        uri = ref_to_uri(ref.id)
        if not scope.allows(uri):
            raise OpError(f"unknown rec: @{ref.id}")
        if _kind_of(uri) != "point":
            raise OpError(f"hisWrite target is not a point: @{ref.id}")
        with self._conn() as conn:
            rec = build_records(self.store, conn, [uri]).get(uri, {})
            cur = rec.get("curVal")
            point_unit = rec.get("unit") or (cur.unit if isinstance(cur, oh.Number) else None)
            for row in rows:
                ts, val = row.get("ts"), row.get("val")
                if not isinstance(ts, datetime):
                    raise OpError("hisWrite row missing ts")
                unit = val.unit if isinstance(val, oh.Number) else None
                if unit and point_unit and unit != point_unit:
                    # As haxall does: no silent conversion, no mixed units
                    raise OpError(f"unit {unit} doesn't match the point's unit {point_unit}")
                if isinstance(val, oh.Number):
                    val = val.val
                elif val is not None and not isinstance(val, (bool, int, float, str)):
                    val = str(val)
                timeseries.write_point_value(conn, uri, val, ts, unit=unit)


def _iana(tz: str) -> str:
    z = oh.haystack_tz(tz)
    return getattr(z, "key", "UTC")


def _resolve_range(range_text: str, tz: str) -> hisquery.Span:
    """Every Haystack hisRead range: `today`, `yesterday`, `{date}`,
    `{date},{date}`, `{dateTime},{dateTime}` and `{dateTime}` (from then until
    now). Dates are whole local days in the point's tz; the end is exclusive."""
    text = range_text.strip().strip('"')
    if "T" in text:
        try:
            parts = [oh.from_zinc(p.strip()) for p in text.split(",", 1)]
        except ValueError as exc:
            raise OpError(f"range: {exc}") from exc
        if not all(isinstance(p, datetime) for p in parts):
            raise OpError(f"range: expected dateTime values in {range_text!r}")
        start = parts[0]
        end = parts[1] if len(parts) == 2 else datetime.now(timezone.utc)
        return hisquery.Span(start, end, text)
    try:
        return hisquery.resolve_span(_range_to_span(text), datetime.now(timezone.utc), _iana(tz))
    except hisquery.HisQueryError as exc:
        raise OpError(f"range: {exc}") from exc


def _range_to_span(text: str) -> str:
    """Haystack range syntax -> hisquery's DateSpan syntax.
    `2024-01-01,2024-01-31` -> `2024-01-01..2024-01-31`; named and single
    dates pass straight through."""
    text = text.strip().strip('"')
    if "," in text:
        a, b = (p.strip() for p in text.split(",", 1))
        return f"{a}..{b}"
    return text


# --------------------------------------------------------------------------- watches


class WatchRegistry:
    """Same semantics and bounds as openhay-server's: a watch is ids plus
    the last snapshot sent; a poll returns what changed."""

    def __init__(self, max_watches: int, max_ids: int, default_lease: float = 60.0):
        self.max_watches, self.max_ids, self.default_lease = max_watches, max_ids, default_lease
        self._lock = threading.Lock()
        self._w: dict[str, dict] = {}

    def _purge(self):
        now = _time.monotonic()
        for k in [k for k, w in self._w.items() if w["expires"] <= now]:
            del self._w[k]

    def subscribe(self, watch_id, dis, lease, refs):
        with self._lock:
            self._purge()
            if watch_id is None:
                if len(self._w) >= self.max_watches:
                    raise OpError(f"too many open watches (max {self.max_watches})")
                watch_id = "w-" + oh.random_token(12)
                self._w[watch_id] = {"dis": dis or "watch", "lease": lease or self.default_lease,
                                     "ids": [], "last": {}, "expires": 0}
            w = self._w.get(watch_id)
            if w is None:
                raise OpError(f"unknown watch: {watch_id}")
            if lease:
                w["lease"] = lease
            for r in refs:
                if r.id not in w["ids"]:
                    if len(w["ids"]) >= self.max_ids:
                        raise OpError(f"watch id limit reached (max {self.max_ids})")
                    w["ids"].append(r.id)
            w["expires"] = _time.monotonic() + w["lease"]
            return watch_id, w["lease"]

    def ids(self, watch_id) -> list[oh.Ref]:
        with self._lock:
            self._purge()
            w = self._w.get(watch_id)
            if w is None:
                raise OpError(f"unknown watch: {watch_id}")
            w["expires"] = _time.monotonic() + w["lease"]
            return [oh.Ref(i) for i in w["ids"]]

    def diff(self, watch_id, recs: list[dict], refresh: bool) -> list[dict]:
        with self._lock:
            w = self._w.get(watch_id)
            if w is None:
                return []
            out = []
            for rec in recs:
                key = rec["id"].id
                snap = repr(sorted(rec.items(), key=lambda kv: kv[0]))
                if refresh or w["last"].get(key) != snap:
                    w["last"][key] = snap
                    out.append(rec)
            return out

    def unsubscribe(self, watch_id, refs, close):
        with self._lock:
            if close or not refs:
                self._w.pop(watch_id, None)
            elif watch_id in self._w:
                drop = {r.id for r in refs}
                w = self._w[watch_id]
                w["ids"] = [i for i in w["ids"] if i not in drop]
                for i in drop:
                    w["last"].pop(i, None)


# --------------------------------------------------------------------------- dispatch


def _ref(v) -> oh.Ref:
    if isinstance(v, oh.Ref):
        return v
    if isinstance(v, str):
        return oh.Ref(v.lstrip("@"))
    raise OpError("expected a Ref id")


_SECONDS = {"ms": 0.001, "s": 1, "sec": 1, "min": 60, "h": 3600, "hr": 3600, "day": 86400, "d": 86400}


def _duration_secs(v) -> float:
    """A Haystack duration (`30min`, `1h`) in seconds."""
    if not isinstance(v, oh.Number) or v.unit not in _SECONDS:
        raise OpError("duration must be a Number with a time unit, e.g. 30min")
    return v.val * _SECONDS[v.unit]


def _lease_secs(v) -> float | None:
    if not isinstance(v, oh.Number):
        return None
    mult = {"min": 60, "h": 3600, "hr": 3600, "ms": 0.001}.get(v.unit or "", 1)
    return max(1.0, v.val * mult)


def _command_value(val):
    """Haystack value -> what Windrow's point_write takes. Units are not
    converted: a Number is sent as its bare value."""
    if val is None or isinstance(val, (bool, str)):
        return val
    if isinstance(val, oh.Number):
        return float(val.val)
    if isinstance(val, (int, float)):
        return float(val)
    raise OpError(f"can't command a point with a {type(val).__name__}")


def dispatch(backend: Backend, op: str, req: oh.Grid, scope: Scope = EVERYTHING, caller: dict | None = None) -> oh.Grid:
    row = req.rows[0] if req.rows else {}
    if op == "about":
        now = datetime.now(timezone.utc)
        return oh.Grid.from_rows([{
            "haystackVersion": "4.0", "serverName": "baler", "serverTime": now,
            "serverBootTime": backend.boot, "tz": DEFAULT_TZ, "productName": "Baler",
            "productUri": oh.Uri("https://github.com/jacobnash/baler"), "productVersion": "0.1.0",
            "vendorName": "Baler", "vendorUri": oh.Uri("https://github.com/jacobnash/baler"),
            "moduleName": "baler.haystack_api", "moduleVersion": "0.1.0",
        }])
    if op == "ops":
        # Like haxall: the `op` defs from the namespace (def ^op:about, doc,
        # is, lib, ...), limited to the ops this server actually serves.
        served = {f"op:{n}" for n, _ in OPS} | {"op:pointWrite", "op:invokeAction"}
        limit = row.get("limit")
        defs = oh.defs("defs", str(row["filter"]) if row.get("filter") else None)
        rows = [r for r in defs.rows if str(getattr(r.get("def"), "val", "")) in served]
        if isinstance(limit, oh.Number):
            rows = rows[: int(limit.val)]
        return oh.Grid(meta=defs.meta, cols=defs.cols, rows=rows)
    if op == "formats":
        return oh.Grid.from_rows([
            {"mime": "text/zinc", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "application/json", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "text/trio", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "text/csv", "send": oh.MARKER},
        ])
    if op in DEFS_OPS:
        # The standard Haystack 4 namespace (openhay bundles the official
        # defs). Baler's own Brick/PROJ classes aren't defs.
        limit = row.get("limit")
        n = int(limit.val) if isinstance(limit, oh.Number) else None
        try:
            full = oh.defs(op, str(row["filter"]) if row.get("filter") else None)
        except ValueError as exc:
            raise OpError(str(exc)) from exc
        rows = full.rows
        if op == "filetypes":
            # only what this server actually reads/writes (no RDF export yet)
            rows = [r for r in rows if str(getattr(r.get("def"), "val", "")) in _FILETYPES_SERVED]
        meta = dict(full.meta)
        if n is not None and len(rows) > n:
            rows = rows[:n]
            meta.update(incomplete=f"limit of {n} reached", limit=oh.Number(n))
        return oh.Grid(meta=meta, cols=full.cols, rows=rows)
    if op == "read":
        if "filter" in row:
            limit = row.get("limit")
            limit = int(limit.val) if isinstance(limit, oh.Number) else None
            return _rows_grid(backend.read_filter(str(row["filter"]), limit, scope))
        refs = [_ref(r["id"]) for r in req.rows if "id" in r]
        if not refs:
            raise OpError("read requires filter or id")
        return _ids_grid(backend.read_ids(refs, scope))
    if op == "nav":
        nav_id = row.get("navId")
        # a Str per the spec; some clients send the Ref they were handed
        nav_id = nav_id.id if isinstance(nav_id, oh.Ref) else nav_id
        return _rows_grid(backend.nav(str(nav_id) if nav_id else None, scope))
    if op == "hisRead":
        if "range" in req.meta:  # batch: meta range, one id row per point
            rng = req.meta["range"]
            tz = req.meta.get("tz")
            return backend.his_read_batch([_ref(r.get("id")) for r in req.rows],
                                          rng if isinstance(rng, str) else oh.to_zinc(rng),
                                          str(tz) if tz else None, scope)
        rng = row.get("range")
        if rng is None:
            raise OpError("missing range")
        return backend.his_read(_ref(row.get("id")), rng if isinstance(rng, str) else oh.to_zinc(rng), scope)
    if op == "hisWrite":
        if "id" in req.meta:
            backend.his_write(_ref(req.meta.get("id")), req.rows, scope)
            return oh.Grid()
        # batch: ts plus one column per point, each column's meta naming it
        cols = [c for c in req.cols if c.name != "ts" and "id" in c.meta]
        if not cols:
            raise OpError("hisWrite needs meta id, or columns with an id in their meta")
        for c in cols:
            rows = [{"ts": r.get("ts"), "val": r.get(c.name)} for r in req.rows if r.get(c.name) is not None]
            backend.his_write(_ref(c.meta["id"]), rows, scope)
        return oh.Grid()
    if op == "watchSub":
        refs = [_ref(r["id"]) for r in req.rows if "id" in r]
        wid, lease = backend.watches.subscribe(
            str(req.meta["watchId"]) if "watchId" in req.meta else None,
            str(req.meta.get("watchDis") or "watch"), _lease_secs(req.meta.get("lease")), refs)
        rows = backend.watches.diff(wid, [r for r in backend.read_ids(refs, scope) if r], refresh=True)
        g = _rows_grid(rows)
        g.meta = {"watchId": wid, "lease": oh.Number(lease, "s")}
        return g
    if op == "watchPoll":
        wid = str(req.meta.get("watchId") or "")
        refs = backend.watches.ids(wid)
        rows = backend.watches.diff(wid, [r for r in backend.read_ids(refs, scope) if r], "refresh" in req.meta)
        g = _rows_grid(rows)
        g.meta = {"watchId": wid}
        return g
    if op == "watchUnsub":
        backend.watches.unsubscribe(str(req.meta.get("watchId") or ""),
                                    [_ref(r["id"]) for r in req.rows if "id" in r], "close" in req.meta)
        return oh.Grid()
    if op == "close":
        # Haystack: close the session and invalidate its bearer token.
        if caller:
            with backend.ts_pool.connection() as conn:
                auth.revoke_session(conn, caller)
        return oh.Grid()
    if op == "pointWrite":
        ref = _ref(row.get("id"))
        level = row.get("level")
        if not isinstance(level, oh.Number):
            return backend.point_write_array(ref, scope)
        user_sub = (caller or {}).get("sub")
        raw_who = row.get("who")
        who: str = raw_who if isinstance(raw_who, str) else backend.display_name(user_sub)
        duration = row.get("duration")
        backend.point_write(ref, int(level.val), row.get("val"), who, user_sub, scope,
                            duration_s=_duration_secs(duration) if duration is not None else None)
        return oh.Grid()
    if op == "invokeAction":
        raise OpError("invokeAction is not supported yet")
    raise OpError(f"unknown op: {op}")


def _rows_grid(rows: list[dict]) -> oh.Grid:
    return oh.Grid.from_rows(rows) if rows else oh.Grid(cols=[oh.Col("empty")])


def _ids_grid(rows: list[dict | None]) -> oh.Grid:
    """Missing ids come back as all-null rows, per spec."""
    if all(r is None for r in rows):
        return oh.Grid.from_rows([{} for _ in rows], cols=["id"])
    return oh.Grid.from_rows([r or {} for r in rows])


def err_grid(msg: str) -> oh.Grid:
    return oh.Grid(meta={"err": oh.MARKER, "dis": msg, "errTrace": msg}, cols=[oh.Col("empty")])


# --------------------------------------------------------------------------- SCRAM


class ScramLogin:
    """HELLO / SCRAM against the existing users table. Handshake state lives
    in-process for two minutes; the issued authToken is a normal session JWT
    that the gateway verifies on later requests."""

    TTL = 120.0

    def __init__(self, ts_pool):
        self.ts_pool = ts_pool
        self._lock = threading.Lock()
        self._hs: dict[str, dict] = {}

    @staticmethod
    def credentials_from_hash(stored_hash: str) -> oh.ScramCredentials:
        salt_hex, dk_hex = stored_hash.split("$")
        return oh.ScramCredentials.from_salted_password(
            bytes.fromhex(dk_hex), bytes.fromhex(salt_hex), auth._PBKDF2_ITERATIONS)

    def _lookup(self, email: str):
        with self.ts_pool.connection() as conn:
            user = auth.get_user_by_email(conn, email)
            if not user:
                return None, None
            return user, auth.user_site_ids(conn, user["id"])

    def handle(self, scheme: str, params: dict) -> tuple[int, dict, dict | None]:
        """Returns (status, response headers, user-if-logged-in)."""
        now = _time.monotonic()
        with self._lock:
            for k in [k for k, h in self._hs.items() if now - h["at"] > self.TTL]:
                del self._hs[k]
        if scheme == "HELLO":
            try:
                email = oh.b64url_decode(params.get("username", "")).decode("utf-8")
            except ValueError:  # bad base64url or UTF-8 (UnicodeDecodeError is a ValueError)
                return 400, {}, None
            user, sites = self._lookup(email)
            # Unknown users get throwaway credentials so the handshake looks
            # identical and fails only at the proof (no user enumeration).
            # Random bytes, not a real PBKDF2 run: an attacker spraying HELLOs
            # for made-up usernames must not get 600k hash rounds per request.
            creds = (self.credentials_from_hash(user["password_hash"]) if user
                     else oh.ScramCredentials.from_salted_password(os.urandom(32), os.urandom(16),
                                                                   auth._PBKDF2_ITERATIONS))
            token = oh.random_token(18)
            with self._lock:
                self._hs[token] = {"email": email, "user": user, "sites": sites, "creds": creds,
                                   "state": None, "at": now}
            return 401, {"WWW-Authenticate": f"SCRAM handshakeToken={token}, hash=SHA-256"}, None
        if scheme == "SCRAM":
            token = params.get("handshakeToken", "")
            try:
                data = oh.b64url_decode(params.get("data", "")).decode("utf-8")
            except ValueError:  # bad base64url or UTF-8 (UnicodeDecodeError is a ValueError)
                return 401, {}, None
            with self._lock:
                h = self._hs.get(token)
            if h is None:
                return 401, {}, None
            if h["state"] is None:
                try:
                    if oh.ScramServer.username(data) != h["email"]:
                        raise oh.AuthError("username changed mid-handshake")
                    state, server_first = oh.ScramServer.start(data, h["creds"])
                except oh.AuthError:
                    with self._lock:
                        self._hs.pop(token, None)
                    return 401, {}, None
                h["state"] = state
                return 401, {"WWW-Authenticate":
                             f"SCRAM handshakeToken={token}, hash=SHA-256, data={oh.b64url(server_first.encode())}"}, None
            with self._lock:
                self._hs.pop(token, None)
            try:
                server_final = h["state"].finish(data, h["creds"])
            except oh.AuthError:
                return 403, {}, None
            if h["user"] is None:
                return 403, {}, None
            jwt = auth.issue_session_token(h["user"], h["sites"])
            return 200, {"Authentication-Info": f"authToken={jwt}, data={oh.b64url(server_final.encode())}"}, h["user"]
        return 401, {"WWW-Authenticate": "SCRAM hash=SHA-256"}, None


# --------------------------------------------------------------------------- HTTP


def make_handler(backend: Backend, login: ScramLogin | None):
    class HaystackHandler(BaseAPIHandler):
        TRACER = tracer
        ALLOWED_HEADERS = "Content-Type, Authorization, Accept"

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/openapi.yaml", "/api/openapi.yaml"):
                self.serve_openapi_spec("haystack.openapi", SPEC_PATH)
                return
            self._handle()

        def do_POST(self):
            self._handle()

        def _send(self, status: int, body: bytes = b"", ctype: str | None = None, headers: dict | None = None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            if len(parts) not in (2, 3) or parts[0] != "api":
                self._send(404, b"not found\n")
                return
            op = parts[-1]
            with tracer.start_as_current_span(f"haystack.{op}") as span:
                if not gateway_auth.request_came_through_gateway(self):
                    self._send(401, b"request did not come through the gateway\n")
                    return
                extra = {}
                scheme, params = oh.parse_auth_header(self.headers.get("Authorization", ""))
                if scheme in ("HELLO", "SCRAM"):
                    if login is None:
                        self._send(401, b"SCRAM login is disabled\n")
                        return
                    status, extra, _user = login.handle(scheme, params)
                    span.set_attribute("auth.step", scheme)
                    if status != 200:
                        self._send(status, b"", headers=extra)
                        return
                if op not in {n for n, _ in OPS} | {"pointWrite", "invokeAction"}:
                    self._send(404, f"unknown op: {op}\n".encode())
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                req_fmt = None
                try:
                    if self.command == "POST" and body.strip():
                        ctype = (self.headers.get("Content-Type") or "").lower()
                        req_fmt = ("trio" if ctype.startswith("text/trio")
                                   else "zinc" if ctype.startswith(("text/zinc", "text/plain")) else None)
                        req = oh.decode_grid(body, req_fmt)
                        req_fmt = req_fmt or oh.sniff(body)
                    else:
                        q = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
                        row = {}
                        for k, v in q.items():
                            try:
                                row[k] = oh.from_zinc(v)
                            except ValueError:
                                row[k] = v
                        req = oh.Grid.from_rows([row]) if row else oh.Grid()
                    claims = claims_from_header(self.headers.get("Authorization"))
                    with backend.ts_pool.connection() as conn:
                        # A token ended by `close` is refused here at once;
                        # the gateway's denylist catches up within 30s.
                        if claims and auth.is_revoked(conn, claims.get("jti")):
                            self._send(401, b"session closed\n")
                            return
                        caller_scope = EVERYTHING if op in _DATA_FREE_OPS else scope_for(conn, claims)
                    grid = dispatch(backend, op, req, caller_scope, claims)
                except OpError as exc:
                    span.set_attribute("error", str(exc))
                    grid = err_grid(str(exc))
                except ValueError as exc:
                    span.set_attribute("error", str(exc))
                    grid = err_grid(f"bad request: {exc}")
                flavor = req_fmt if req_fmt in ("json3", "hayson") else "hayson"
                accept = self.headers.get("Accept")
                if accept and not any(m in accept.lower() for m in _SERVED_MIMES):
                    self._send(406, f"can't produce {accept}; try text/zinc or application/json\n".encode())
                    return
                fmt = oh.negotiate(accept, flavor)
                try:
                    out = oh.encode_grid(grid, fmt).encode("utf-8")
                except ValueError as exc:
                    # One unencodable value must not drop the connection
                    # (a client sees a reset, not an error): say what failed.
                    span.set_attribute("error", f"encode: {exc}")
                    out = oh.encode_grid(err_grid(f"could not encode the response: {exc}"), fmt).encode("utf-8")
                self._send(200, out, f"{oh.mime(fmt)}; charset=utf-8", extra)

    return HaystackHandler


def main() -> None:
    from baler.remote_store import RemoteStore

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()

    tracing.init_tracing("baler-haystack-api")
    ts_pool = connect_pool(statement_timeout=timeseries.GATEWAY_STATEMENT_TIMEOUT)
    with ts_pool.connection() as conn:
        # Idempotent; also guarantees sites.topic_prefixes exists for
        # scope_for even if auth_api hasn't started yet.
        auth.ensure_schema(conn)
        pw.ensure_schema(conn)
    login = ScramLogin(ts_pool) if os.environ.get("BALER_JWT_SECRET") else None
    backend = Backend(RemoteStore(), ts_pool)
    hisquery.warm_caches()

    def expire_loop():
        while True:
            _time.sleep(5)
            try:
                backend.expire_overrides()
            except Exception as exc:  # noqa: BLE001 - never let the timer die
                print(f"expire_overrides: {exc}")

    threading.Thread(target=expire_loop, name="expire-overrides", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(backend, login))
    print(f"Haystack API listening on {args.host}:{args.port} (/api/{{project}}/{{op}})")
    server.serve_forever()


if __name__ == "__main__":
    main()
