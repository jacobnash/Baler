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
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

import openhay as oh

from baler import auth, gateway_auth, hisquery, timeseries, tracing
from baler import point_write as pw
from baler.http_handler_base import BaseAPIHandler
from baler.scope import EVERYTHING, Scope, claims_from_header, scope_for
from baler.timeseries import connect_pool

tracer = tracing.get_tracer(__name__)

SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "haystack-api-openapi.yaml")
DEFAULT_TZ = os.environ.get("BALER_TZ") or "UTC"
HIS_LIMIT = hisquery.MAX_LIMIT

HAYSTACK_NS = "urn:baler:haystack#"
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
]
WRITE_OPS = {"hisWrite", "pointWrite", "invokeAction"}
# Ops that return no point/equip data, so need no site scope lookup.
_DATA_FREE_OPS = {"about", "ops", "formats", "close", "watchUnsub"}


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


def _literal(value):
    """A stored literal -> a Haystack value. RemoteStore already hands back
    int/float/str; rdflib (tests, in-memory Store) hands back Literals."""
    if hasattr(value, "toPython"):
        value = value.toPython()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return oh.Number(float(value))
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return str(value)


def build_records(store, ts_conn, uris: list[str]) -> dict[str, dict]:
    """One SPARQL round trip for every triple of every requested entity,
    plus one SQL round trip for the newest sample of every point."""
    if not uris:
        return {}
    values = " ".join(f"<{hisquery._uri(u)}>" for u in uris)
    rows = store.query(
        f"""SELECT ?s ?p ?o (isIRI(?o) AS ?iri) WHERE {{
            VALUES ?s {{ {values} }}
            ?s ?p ?o .
        }}"""
    )
    recs: dict[str, dict] = {}
    classes: dict[str, list[str]] = {}
    for row in rows:
        s, p, o = str(row.s), str(row.p), row.o
        is_iri = getattr(row, "iri", False)
        is_iri = is_iri if isinstance(is_iri, bool) else str(is_iri).lower() in ("true", "1")
        rec = recs.setdefault(s, {})
        if p == HAYSTACK_NS + "hasTag":
            rec[str(o)] = oh.MARKER
        elif p.startswith(HAYSTACK_NS) and not is_iri:
            tag = p[len(HAYSTACK_NS):]
            if tag not in _RECOMPUTED_TAGS:
                rec[tag] = _literal(o)
            elif tag == "unit":
                rec["unit"] = str(_literal(o))
        elif p == RDF_TYPE:
            o = str(o)
            if not o.startswith(TD_NS):
                classes.setdefault(s, []).append(o)
        elif p == BRICK_NS + "isPointOf":
            rec["equipRef"] = oh.Ref(uri_to_ref(str(o)))

    latest = read_latest_many(ts_conn, [u for u in uris if _kind_of(u) == "point"]) if ts_conn is not None else {}
    labels = timeseries.read_point_labels(ts_conn, list(latest)) if latest else {}
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
        rec.setdefault("tz", DEFAULT_TZ)
        out[uri] = rec
    return out


def read_latest_many(conn, point_uris: list[str]) -> dict[str, tuple[datetime, object]]:
    if not point_uris:
        return {}
    cur = conn.execute(
        """
        SELECT DISTINCT ON (point_uri) point_uri, ts, value, value_text, value_bool
        FROM point_history WHERE point_uri = ANY(%s)
        ORDER BY point_uri, ts DESC
        """,
        (point_uris,),
    )
    return {uri: (ts, timeseries._row_to_value((v, vt, vb))) for uri, ts, v, vt, vb in cur.fetchall()}


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
        uris = scope.filter(hisquery.match_entities(self.store, q.filter))
        if limit is not None:
            uris = uris[:limit]
        with self._conn() as conn:
            recs = build_records(self.store, conn, uris)
        return [recs[u] for u in uris if u in recs]

    def read_ids(self, refs: list[oh.Ref], scope: Scope = EVERYTHING) -> list[dict | None]:
        uris = [ref_to_uri(r.id) for r in refs]
        visible = scope.filter(uris)
        with self._conn() as conn:
            recs = build_records(self.store, conn, visible)
        return [recs.get(u) for u in uris]  # out-of-scope ids read as missing, never as "forbidden"

    def nav(self, nav_id: str | None, scope: Scope = EVERYTHING) -> list[dict]:
        """equip -> point. (Sites aren't modelled as graph entities yet.)"""
        if nav_id is None:
            rows = self.store.query(
                "SELECT DISTINCT ?s WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?s), \"urn:equip:\")) }"
            )
            uris = scope.filter(sorted({str(r.s) for r in rows}))
        else:
            equip = ref_to_uri(nav_id)
            rows = self.store.query(
                hisquery.PREFIXES
                + f"SELECT DISTINCT ?p WHERE {{ {{ <{hisquery._uri(equip)}> brick:hasPoint ?p }}"
                f" UNION {{ ?p brick:isPointOf <{hisquery._uri(equip)}> }} }}"
            )
            uris = scope.filter(sorted({str(r.p) for r in rows}))
        with self._conn() as conn:
            recs = build_records(self.store, conn, uris)
        out = []
        for u in uris:
            rec = recs.get(u)
            if rec is None:
                continue
            if "equip" in rec:
                rec["navId"] = uri_to_ref(u)
            out.append(rec)
        return out

    def _writable_point(self, ref: oh.Ref, scope: Scope) -> tuple[str, dict]:
        uri = ref_to_uri(ref.id)
        rec = self.read_ids([ref], scope)[0]
        if rec is None or _kind_of(uri) != "point":
            raise OpError(f"unknown rec: @{ref.id}")
        return uri, rec

    def point_write(self, ref: oh.Ref, level: int, val, who: str, user_sub: str | None,
                    scope: Scope = EVERYTHING) -> None:
        """Command the point through the Windrow instance that owns it (see
        point_write.py), then record the level. val None relinquishes."""
        if not 1 <= level <= 16:
            raise OpError(f"level must be 1-16, got {level}")
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
        with self._conn() as conn:
            pw.record_level(conn, uri, level, value, who, user_sub)

    def point_write_array(self, ref: oh.Ref, scope: Scope = EVERYTHING) -> oh.Grid:
        uri, _ = self._writable_point(ref, scope)
        with self._conn() as conn:
            stored = pw.levels(conn, uri)
        rows = []
        for level in range(1, 17):
            row = {"level": oh.Number(level), "levelDis": pw.LEVEL_DIS.get(level, str(level))}
            if level in stored:
                v = stored[level]["val"]
                row["val"] = oh.Number(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                row["who"] = stored[level]["who"]
            rows.append(row)
        return oh.Grid.from_rows(rows, cols=["level", "levelDis", "val", "who"])

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
            try:
                span = hisquery.resolve_span(_range_to_span(range_text), datetime.now(timezone.utc), _iana(tz))
            except hisquery.HisQueryError as exc:
                raise OpError(f"range: {exc}") from exc
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

    def his_write(self, ref: oh.Ref, rows: list[dict], scope: Scope = EVERYTHING) -> None:
        uri = ref_to_uri(ref.id)
        if not scope.allows(uri):
            raise OpError(f"unknown rec: @{ref.id}")
        if _kind_of(uri) != "point":
            raise OpError(f"hisWrite target is not a point: @{ref.id}")
        with self._conn() as conn:
            for row in rows:
                ts, val = row.get("ts"), row.get("val")
                if not isinstance(ts, datetime):
                    raise OpError("hisWrite row missing ts")
                unit = val.unit if isinstance(val, oh.Number) else None
                if isinstance(val, oh.Number):
                    val = val.val
                elif val is not None and not isinstance(val, (bool, int, float, str)):
                    val = str(val)
                timeseries.write_point_value(conn, uri, val, ts, unit=unit)


def _iana(tz: str) -> str:
    z = oh.haystack_tz(tz)
    return getattr(z, "key", "UTC")


def _range_to_span(text: str) -> str:
    """Haystack range syntax -> hisquery's DateSpan syntax.
    `2024-01-01,2024-01-31` -> `2024-01-01..2024-01-31`; named and single
    dates pass straight through."""
    text = text.strip().strip('"')
    if "," in text:
        a, b = (p.strip() for p in text.split(",", 1))
        if "T" in a or "T" in b:
            raise hisquery.HisQueryError("datetime ranges aren't supported yet - use dates or today/yesterday/...")
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
        }])
    if op == "ops":
        return oh.Grid.from_rows([{"name": n, "summary": s} for n, s in OPS])
    if op == "formats":
        return oh.Grid.from_rows([
            {"mime": "text/zinc", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "application/json", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "text/trio", "receive": oh.MARKER, "send": oh.MARKER},
            {"mime": "text/csv", "send": oh.MARKER},
        ])
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
        return _rows_grid(backend.nav(str(nav_id) if nav_id else None, scope))
    if op == "hisRead":
        rng = row.get("range")
        if rng is None:
            raise OpError("missing range")
        return backend.his_read(_ref(row.get("id")), rng if isinstance(rng, str) else oh.to_zinc(rng), scope)
    if op == "hisWrite":
        backend.his_write(_ref(req.meta.get("id")), req.rows, scope)
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
        return oh.Grid()
    if op == "pointWrite":
        ref = _ref(row.get("id"))
        level = row.get("level")
        if not isinstance(level, oh.Number):
            return backend.point_write_array(ref, scope)
        user_sub = (caller or {}).get("sub")
        raw_who = row.get("who")
        who: str = raw_who if isinstance(raw_who, str) else (user_sub or "baler")
        backend.point_write(ref, int(level.val), row.get("val"), who, user_sub, scope)
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
                    if op in _DATA_FREE_OPS:
                        caller_scope = EVERYTHING
                    else:
                        with backend.ts_pool.connection() as conn:
                            caller_scope = scope_for(conn, claims)
                    grid = dispatch(backend, op, req, caller_scope, claims)
                except OpError as exc:
                    span.set_attribute("error", str(exc))
                    grid = err_grid(str(exc))
                except ValueError as exc:
                    span.set_attribute("error", str(exc))
                    grid = err_grid(f"bad request: {exc}")
                flavor = req_fmt if req_fmt in ("json3", "hayson") else "hayson"
                fmt = oh.negotiate(self.headers.get("Accept"), flavor)
                out = oh.encode_grid(grid, fmt).encode("utf-8")
                self._send(200, out, f"{oh.mime(fmt)}; charset=utf-8", extra)

    return HaystackHandler


def main() -> None:
    from baler.remote_store import RemoteStore

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()

    tracing.init_tracing("baler-haystack-api")
    ts_pool = connect_pool()
    with ts_pool.connection() as conn:
        # Idempotent; also guarantees sites.topic_prefixes exists for
        # scope_for even if auth_api hasn't started yet.
        auth.ensure_schema(conn)
        pw.ensure_schema(conn)
    login = ScramLogin(ts_pool) if os.environ.get("BALER_JWT_SECRET") else None
    backend = Backend(RemoteStore(), ts_pool)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(backend, login))
    print(f"Haystack API listening on {args.host}:{args.port} (/api/{{project}}/{{op}})")
    server.serve_forever()


if __name__ == "__main__":
    main()
