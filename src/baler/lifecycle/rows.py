"""
Row storage for the lifecycle record: one small surface (insert, get,
find, update, delete, audit) with two backends, the pattern
commissioning.db uses:

  - PgRows: the shared Postgres. Tables come from numbered migrations
    (baler/migrations/), never from here.
  - MemoryRows: an in-process twin, so every feature and its routes are
    unit-testable with no services.

A feature module declares its tables with `register(name, cols, json)`.
Queries that only SQL can answer well (full-text search, history reads)
live in the feature module, which checks `rows.pg` and falls back to a
plain-Python version over MemoryRows.

Audit: `audit()` writes to record_edits (the table hand edits to the graph
already log to, Baler#19), keyed by a `urn:baler:<kind>:<id>` entity uri,
so GET /ingest/records/edits shows lifecycle changes like any other edit.
"""

from __future__ import annotations

import contextlib
import copy
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from baler import json_store


@dataclass(frozen=True)
class Table:
    cols: tuple[str, ...]
    json: frozenset[str]
    order: str


TABLES: dict[str, Table] = {}


def register(name: str, cols: tuple[str, ...], json_cols: tuple[str, ...] = (), order: str = "created_at") -> None:
    if "id" not in cols or order not in cols or not set(json_cols) <= set(cols):
        raise ValueError(f"bad table registration for {name}")
    TABLES[name] = Table(tuple(cols), frozenset(json_cols), order)


def new_id() -> str:
    return json_store.new_id()


def entity_uri(kind: str, row_id: str) -> str:
    return f"urn:baler:{kind}:{row_id}"


def public(row: dict | None) -> dict | None:
    """JSON-ready: dates and datetimes as ISO strings, Decimals as floats."""
    if row is None:
        return None

    def conv(v):
        if isinstance(v, (datetime, date)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        return v

    return {k: conv(v) for k, v in row.items()}


def _table(name: str) -> Table:
    try:
        return TABLES[name]
    except KeyError:
        raise ValueError(f"unknown table {name}") from None


def _matches(row: dict, eq: dict) -> bool:
    for k, v in eq.items():
        if isinstance(v, (list, tuple, set, frozenset)):
            if row.get(k) not in v:
                return False
        elif row.get(k) != v:
            return False
    return True


def _sort_key(order: str):
    def key(r: dict):
        v = r.get(order)
        return (v is None, str(v) if isinstance(v, (date, datetime)) else v, r["id"])
    return key


class MemoryRows:
    pg = False

    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, dict]] = {}
        self.audits: list[dict] = []

    def insert(self, table: str, row: dict) -> dict:
        t = _table(table)
        full = {c: row.get(c) for c in t.cols}
        with self._lock:
            store = self._rows.setdefault(table, {})
            row_id = str(full["id"])
            if row_id in store:
                raise ValueError(f"duplicate id in {table}")
            store[row_id] = copy.deepcopy(full)
        return copy.deepcopy(full)

    def get(self, table: str, row_id: str) -> dict | None:
        _table(table)
        with self._lock:
            r = self._rows.get(table, {}).get(row_id)
            return None if r is None else copy.deepcopy(r)

    def find(self, table: str, order: str | None = None, **eq) -> list[dict]:
        t = _table(table)
        order = order or t.order
        if order not in t.cols or any(k not in t.cols for k in eq):
            raise ValueError("unknown column")
        with self._lock:
            out = [copy.deepcopy(r) for r in self._rows.get(table, {}).values() if _matches(r, eq)]
        return sorted(out, key=_sort_key(order))

    def update(self, table: str, row_id: str, changes: dict) -> dict | None:
        t = _table(table)
        with self._lock:
            r = self._rows.get(table, {}).get(row_id)
            if r is None:
                return None
            r.update({k: copy.deepcopy(v) for k, v in changes.items() if k in t.cols and k != "id"})
            return copy.deepcopy(r)

    def delete(self, table: str, **eq) -> int:
        _table(table)
        if not eq:
            raise ValueError("delete needs a condition")
        with self._lock:
            store = self._rows.get(table, {})
            hit = [k for k, r in store.items() if _matches(r, eq)]
            for k in hit:
                del store[k]
        return len(hit)

    def audit(self, uri: str, action: str, who: dict, detail: dict) -> None:
        with self._lock:
            self.audits.append({"ts": datetime.now().astimezone().isoformat(), "entity_uri": uri, "action": action,
                                "who": who.get("email") or who.get("sub"), "detail": copy.deepcopy(detail)})

    def audit_log(self, uri: str, limit: int = 100) -> list[dict]:
        with self._lock:
            hits = [dict(a) for a in self.audits if a["entity_uri"] == uri]
        return [{k: v for k, v in a.items() if k != "entity_uri"} for a in reversed(hits)][:limit]


def _encode(t: Table, col: str, v):
    return json.dumps(v, default=str) if col in t.json and v is not None else v


class PgRows:
    pg = True

    def __init__(self, pool):
        self.pool = pool

    @contextlib.contextmanager
    def conn(self) -> Iterator:
        with self.pool.connection() as conn:
            yield conn

    def insert(self, table: str, row: dict) -> dict:
        t = _table(table)
        full = {c: row.get(c) for c in t.cols}
        with self.conn() as conn:
            conn.execute(f"INSERT INTO {table} ({', '.join(t.cols)}) VALUES ({', '.join(['%s'] * len(t.cols))})",
                         tuple(_encode(t, c, full[c]) for c in t.cols))
        return full

    def get(self, table: str, row_id: str) -> dict | None:
        rows = self.find(table, id=row_id)
        return rows[0] if rows else None

    def find(self, table: str, order: str | None = None, **eq) -> list[dict]:
        t = _table(table)
        order = order or t.order
        if order not in t.cols or any(k not in t.cols for k in eq):
            raise ValueError("unknown column")
        where, params = [], []
        for k, v in eq.items():
            if isinstance(v, (list, tuple, set, frozenset)):
                where.append(f"{k} = ANY(%s)")
                params.append(list(v))
            elif v is None:
                where.append(f"{k} IS NULL")
            else:
                where.append(f"{k} = %s")
                params.append(v)
        with self.conn() as conn:
            got = conn.execute(f"SELECT {', '.join(t.cols)} FROM {table} WHERE {' AND '.join(where) or 'TRUE'} "
                               f"ORDER BY {order} NULLS LAST, id", tuple(params)).fetchall()
        return [dict(zip(t.cols, r, strict=True)) for r in got]

    def update(self, table: str, row_id: str, changes: dict) -> dict | None:
        t = _table(table)
        keys = [c for c in changes if c in t.cols and c != "id"]
        if keys:
            with self.conn() as conn:
                conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = %s' for c in keys)} WHERE id = %s",
                             (*[_encode(t, c, changes[c]) for c in keys], row_id))
        return self.get(table, row_id)

    def delete(self, table: str, **eq) -> int:
        t = _table(table)
        if not eq or any(k not in t.cols for k in eq):
            raise ValueError("delete needs a condition on known columns")
        with self.conn() as conn:
            cur = conn.execute(f"DELETE FROM {table} WHERE {' AND '.join(f'{k} = %s' for k in eq)}", tuple(eq.values()))
            return cur.rowcount

    def audit(self, uri: str, action: str, who: dict, detail: dict) -> None:
        from baler import record_edit

        with self.conn() as conn:
            record_edit.log_edit(conn, uri, action, who.get("sub"), who.get("email"), detail)

    def audit_log(self, uri: str, limit: int = 100) -> list[dict]:
        from baler import record_edit

        with self.conn() as conn:
            return record_edit.list_edits(conn, uri, limit)
