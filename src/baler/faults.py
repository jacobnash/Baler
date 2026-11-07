"""
Fault instances and webhook delivery health - same shape as timeseries.py.
The `faults` table itself IS the fault-detection state (no in-memory
roster) - a restart never desyncs from reality. `webhook_health` tracks
per-webhook delivery failures separately from webhooks.json: two
processes (fault_api.py for CRUD, fault_detector.py for delivery
attempts) would otherwise both write the same JSON file, which
json_store.py's "single writer per file" assumption doesn't support.
"""

import json
from datetime import datetime

import psycopg

from baler import ddl
from baler.schemas import WebhookHealth

SCHEMA = """
CREATE TABLE IF NOT EXISTS faults (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    point_uri   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    detail      JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS faults_open_unique
    ON faults (rule_id, point_uri) WHERE ended_at IS NULL;

-- Alarm-console fields (ack/snooze/severity), added after the table
-- above already shipped - ADD COLUMN IF NOT EXISTS instead of a separate
-- migration file/framework, same idempotent-DDL style as every other
-- ensure_schema() in this repo.
ALTER TABLE faults ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'warning';
ALTER TABLE faults ADD COLUMN IF NOT EXISTS acked_at TIMESTAMPTZ;
ALTER TABLE faults ADD COLUMN IF NOT EXISTS acked_by TEXT;
ALTER TABLE faults ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ;
-- GET /faults' order (newest first), which it pages through.
CREATE INDEX IF NOT EXISTS faults_started ON faults (started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS webhook_health (
    webhook_id            TEXT PRIMARY KEY,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    disabled              BOOLEAN NOT NULL DEFAULT FALSE,
    last_error            TEXT
);
"""


def ensure_schema(conn: psycopg.Connection) -> None:
    ddl.run(conn, SCHEMA)


def open_fault(
    conn: psycopg.Connection,
    rule_id: str,
    point_uri: str,
    started_at: datetime,
    detail: dict | None = None,
    severity: str = "warning",
) -> int | None:
    """Returns the new fault's id if this is a genuinely new opening (the
    caller should fire webhooks), or None if a fault for this
    (rule_id, point_uri) was already open (no-op). Atomic and race-safe:
    the partial unique index is the ON CONFLICT target, not a
    check-then-insert race between concurrent callers. severity is
    captured at open time from the rule's own config, not looked up
    live later - editing a rule's severity afterward doesn't retroactively
    relabel faults it already raised, same reasoning as `detail`."""
    row = conn.execute(
        """
        INSERT INTO faults (rule_id, point_uri, status, started_at, detail, severity)
        VALUES (%s, %s, 'open', %s, %s, %s)
        ON CONFLICT (rule_id, point_uri) WHERE ended_at IS NULL DO NOTHING
        RETURNING id
        """,
        (rule_id, point_uri, started_at, json.dumps(detail) if detail is not None else None, severity),
    ).fetchone()
    return row[0] if row else None


def fault_point_uri(conn: psycopg.Connection, fault_id: int) -> str | None:
    row = conn.execute("SELECT point_uri FROM faults WHERE id = %s", (fault_id,)).fetchone()
    return row[0] if row else None


def ack_fault(conn: psycopg.Connection, fault_id: int, acked_by: str, now: datetime) -> bool:
    """True if a fault with this id exists (whether or not it was already
    acked - re-acking just updates who/when, same as SkySpark's console)."""
    row = conn.execute(
        "UPDATE faults SET acked_at = %s, acked_by = %s WHERE id = %s RETURNING id",
        (now, acked_by, fault_id),
    ).fetchone()
    return row is not None


def snooze_fault(conn: psycopg.Connection, fault_id: int, until: datetime) -> bool:
    """True if a fault with this id exists. Snoozing only affects display/
    notification suppression the UI applies client-side - it does not stop
    fault_detector.py from evaluating the underlying rule, and a fault that
    resolves and reopens while snoozed gets a fresh row (open_fault's
    ON CONFLICT target is (rule_id, point_uri) WHERE ended_at IS NULL, not
    aware of snoozed_until) with snoozed_until reset to NULL."""
    row = conn.execute(
        "UPDATE faults SET snoozed_until = %s WHERE id = %s RETURNING id",
        (until, fault_id),
    ).fetchone()
    return row is not None


def close_fault(conn: psycopg.Connection, rule_id: str, point_uri: str, ended_at: datetime) -> int | None:
    """Mirror of open_fault: returns the closed fault's id, or None if
    nothing was open for this (rule_id, point_uri) - same no-op-on-miss,
    atomic-on-hit shape."""
    row = conn.execute(
        """
        UPDATE faults SET ended_at = %s, status = 'resolved'
        WHERE rule_id = %s AND point_uri = %s AND ended_at IS NULL
        RETURNING id
        """,
        (ended_at, rule_id, point_uri),
    ).fetchone()
    return row[0] if row else None


def update_open_fault(conn: psycopg.Connection, rule_id: str, point_uri: str, detail: dict, severity: str) -> bool:
    """Refresh the detail/severity of the open fault for (rule_id,
    point_uri), for a source whose fault changes while it stays open (a
    BACnet alarm going high-limit -> low-limit). True if one was open and
    anything changed."""
    row = conn.execute(
        """
        UPDATE faults SET detail = %s, severity = %s
        WHERE rule_id = %s AND point_uri = %s AND ended_at IS NULL
          AND (detail IS DISTINCT FROM %s::jsonb OR severity IS DISTINCT FROM %s)
        RETURNING id
        """,
        (json.dumps(detail), severity, rule_id, point_uri, json.dumps(detail), severity),
    ).fetchone()
    return row is not None


def close_rule_faults(conn: psycopg.Connection, rule_id: str, ended_at: datetime) -> int:
    """Resolve every open fault a rule raised - for when the rule is
    deleted: nothing will ever evaluate it again, so they'd stay open
    forever (and mask derivation inputs, Baler#58). Returns how many."""
    return conn.execute(
        "UPDATE faults SET ended_at = %s, status = 'resolved' WHERE rule_id = %s AND ended_at IS NULL",
        (ended_at, rule_id),
    ).rowcount


def close_orphaned_faults(conn: psycopg.Connection, known_rule_ids: set[str], ended_at: datetime) -> int:
    """Resolve open faults whose rule no longer exists (deleted before
    close_rule_faults existed, or lost with rules.json). Namespaced ids
    (`x:...`, e.g. faults mirrored from another source) aren't
    fault_detector's to judge and are left alone."""
    return conn.execute(
        "UPDATE faults SET ended_at = %s, status = 'resolved' "
        "WHERE ended_at IS NULL AND position(':' in rule_id) = 0 AND NOT (rule_id = ANY(%s))",
        (ended_at, list(known_rule_ids)),
    ).rowcount


def list_faults(
    conn: psycopg.Connection,
    status: str | None = None,
    point_uri: str | None = None,
    rule_id: str | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if point_uri is not None:
        clauses.append("point_uri = %s")
        params.append(point_uri)
    if rule_id is not None:
        clauses.append("rule_id = %s")
        params.append(rule_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        f"""SELECT {_FAULT_COLUMNS} FROM faults {where} ORDER BY started_at DESC""",
        params,
    ).fetchall()
    return [_fault_row(r) for r in rows]


_FAULT_COLUMNS = "id, rule_id, point_uri, status, started_at, ended_at, detail, severity, acked_at, acked_by, snoozed_until"

# GET /faults pages by default: the whole table (every resolved fault ever
# raised) was a 5 MB answer on nash-srv by its second week (Baler#107).
DEFAULT_PAGE = 500
MAX_PAGE = 5000


def list_faults_page(
    conn: psycopg.Connection,
    status: str | None = None,
    point_uri: str | None = None,
    rule_id: str | None = None,
    *,
    limit: int = DEFAULT_PAGE,
    before: int | None = None,
    since: datetime | None = None,
    allows=None,
) -> tuple[list[dict], int, int | None]:
    """One page of list_faults' answer (same rows, same order: newest
    started_at first, ties by id): the faults after fault `before` in that
    order, at most `limit` of them. `allows(point_uri)` is the caller's
    scope, applied before paging so a page is never short because rows
    were hidden. Returns (page, how many match in all, the `before` for
    the next page or None when this is the last).

    Ids and point URIs of every match come back first (a few hundred kB
    at 100k faults); only the page's rows are fetched whole."""
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE}")
    clauses: list[str] = []
    params: list = []
    for column, value in (("status", status), ("point_uri", point_uri), ("rule_id", rule_id)):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    if since is not None:
        clauses.append("started_at >= %s")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    keys = conn.execute(f"SELECT id, point_uri FROM faults {where} ORDER BY started_at DESC, id DESC", params).fetchall()
    ids = [fid for fid, uri in keys if allows is None or allows(uri)]
    start = 0
    if before is not None:
        try:
            start = ids.index(before) + 1
        except ValueError:
            raise ValueError(f"before: fault {before} is not in this listing") from None
    page_ids = ids[start:start + limit]
    rows = conn.execute(
        f"SELECT {_FAULT_COLUMNS} FROM faults WHERE id = ANY(%s) ORDER BY started_at DESC, id DESC", (page_ids,)
    ).fetchall() if page_ids else []
    more = start + limit < len(ids)
    return [_fault_row(r) for r in rows], len(ids), (page_ids[-1] if more else None)


def _fault_row(r) -> dict:
    return {
        "id": r[0],
        "rule_id": r[1],
        "point_uri": r[2],
        "status": r[3],
        "started_at": r[4].isoformat(),
        "ended_at": r[5].isoformat() if r[5] else None,
        "detail": r[6],
        "severity": r[7],
        "acked_at": r[8].isoformat() if r[8] else None,
        "acked_by": r[9],
        "snoozed_until": r[10].isoformat() if r[10] else None,
    }


def record_webhook_failure(conn: psycopg.Connection, webhook_id: str, error: str, disable_threshold: int = 5) -> bool:
    """Returns True if this failure just crossed the threshold and
    disabled the webhook - no auto re-enable (that needs a scheduler this
    project doesn't have); a human re-enables via enable_webhook()."""
    row = conn.execute(
        """
        INSERT INTO webhook_health (webhook_id, consecutive_failures, last_error)
        VALUES (%s, 1, %s)
        ON CONFLICT (webhook_id) DO UPDATE SET
            consecutive_failures = webhook_health.consecutive_failures + 1,
            last_error = EXCLUDED.last_error
        RETURNING consecutive_failures
        """,
        (webhook_id, error),
    ).fetchone()
    failures = row[0]  # type: ignore[index]  # INSERT...ON CONFLICT DO UPDATE...RETURNING always returns exactly one row; mypy/psycopg's typing can't express that SQL guarantee
    if failures >= disable_threshold:
        conn.execute("UPDATE webhook_health SET disabled = TRUE WHERE webhook_id = %s", (webhook_id,))
        return True
    return False


def record_webhook_success(conn: psycopg.Connection, webhook_id: str) -> None:
    conn.execute(
        """
        INSERT INTO webhook_health (webhook_id, consecutive_failures)
        VALUES (%s, 0)
        ON CONFLICT (webhook_id) DO UPDATE SET consecutive_failures = 0
        """,
        (webhook_id,),
    )


def is_webhook_disabled(conn: psycopg.Connection, webhook_id: str) -> bool:
    row = conn.execute("SELECT disabled FROM webhook_health WHERE webhook_id = %s", (webhook_id,)).fetchone()
    return bool(row[0]) if row else False


def list_webhook_health(conn: psycopg.Connection) -> dict[str, WebhookHealth]:
    """Every webhook's health in one query, keyed by webhook_id - the
    GET /webhooks list endpoint needs this for every row, not just one,
    so a single batch query beats is_webhook_disabled()'s per-id lookup
    N+1'd across the whole list. A webhook with no row here has never
    recorded a delivery attempt - callers should treat that as
    not-disabled/0-failures, same as is_webhook_disabled's no-row case."""
    rows = conn.execute("SELECT webhook_id, disabled, consecutive_failures, last_error FROM webhook_health").fetchall()
    return {r[0]: {"disabled": r[1], "consecutive_failures": r[2], "last_error": r[3]} for r in rows}


def enable_webhook(conn: psycopg.Connection, webhook_id: str) -> None:
    conn.execute(
        "UPDATE webhook_health SET disabled = FALSE, consecutive_failures = 0 WHERE webhook_id = %s",
        (webhook_id,),
    )
