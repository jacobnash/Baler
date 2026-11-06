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
        f"""SELECT id, rule_id, point_uri, status, started_at, ended_at, detail,
                   severity, acked_at, acked_by, snoozed_until
            FROM faults {where} ORDER BY started_at DESC""",
        params,
    ).fetchall()
    return [
        {
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
        for r in rows
    ]


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
