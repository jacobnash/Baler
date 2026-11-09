"""
Process health for the alert evaluator (#277): every Baler process that
calls tracing.init_tracing records that it started, and counts the
unhandled exceptions it raises, in service_runtime (migration 0004).
/ops/status shows the counts, baler.alerts turns a burst of starts into a
"restart-looping" alert, and BALER_ERROR_TRACKING_URL (optional) gets each
exception as a JSON event.

Everything here is best effort and off the caller's thread: recording a
crash must never cause one, and a request that failed must not also wait
on a database write. Nothing is stored or sent but the exception's type,
its message (cut to MAX_MESSAGE characters) and the last frames' file and
line - no locals, no request bodies.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

# How many start times service_runtime keeps per service (restart-loop
# detection only needs the last few).
KEEP_STARTS = 20
MAX_MESSAGE = 500

_service: str | None = None
_installed = False
_lock = threading.Lock()


def _dsn() -> str:
    from baler import timeseries

    return timeseries.DEFAULT_DSN


def _write(sql: str, params: tuple) -> None:
    try:
        import psycopg

        with psycopg.connect(_dsn(), connect_timeout=3, autocommit=True) as conn:
            conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 - health reporting is best effort
        print(f"errors: couldn't record to service_runtime: {exc!r}", file=sys.stderr)


START_SQL = f"""
INSERT INTO service_runtime (service, starts, recent_starts, updated_at) VALUES (%s, 1, ARRAY[%s::timestamptz], now())
ON CONFLICT (service) DO UPDATE SET starts = service_runtime.starts + 1,
    recent_starts = (ARRAY[%s::timestamptz] || service_runtime.recent_starts)[1:{KEEP_STARTS}],
    updated_at = now()"""

ERROR_SQL = """
INSERT INTO service_runtime (service, errors, last_error_at, last_error, updated_at) VALUES (%s, 1, %s, %s, now())
ON CONFLICT (service) DO UPDATE SET errors = service_runtime.errors + 1,
    last_error_at = EXCLUDED.last_error_at, last_error = EXCLUDED.last_error, updated_at = now()"""


def describe(exc: BaseException) -> dict[str, Any]:
    """What is recorded and sent for an exception: type, message, and the
    innermost frames' location (file name and line, no source or locals)."""
    message = str(exc)
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + "..."
    frames = traceback.extract_tb(exc.__traceback__)[-5:] if exc.__traceback__ else []
    return {
        "type": type(exc).__name__,
        "message": message,
        "frames": [{"file": os.path.basename(f.filename), "line": f.lineno, "function": f.name} for f in frames],
    }


def _send_tracking(event: dict[str, Any]) -> None:
    url = os.environ.get("BALER_ERROR_TRACKING_URL", "").strip()
    if not url:
        return
    from baler import webhooks

    ok, err = webhooks.deliver(url, os.environ.get("BALER_ERROR_TRACKING_SECRET", ""), event, max_attempts=2,
                               allow_private=os.environ.get("BALER_ALLOW_PRIVATE_WEBHOOKS") == "1")
    if not ok:
        print(f"errors: error-tracking delivery failed: {err}", file=sys.stderr)


def _in_background(fn, *args) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, daemon=True, name="errors-record")
    t.start()
    return t


def record(exc: BaseException, service: str | None = None, *, wait: bool = False) -> None:
    """Count one unhandled exception for `service` (default: the name
    install() was given). `wait`: block until recorded (for an excepthook
    about to end the process)."""
    name = service or _service
    if not name:
        return
    info = describe(exc)
    now = datetime.now(timezone.utc)
    event = {"source": "baler", "service": name, "at": now.isoformat(), **info}

    def run() -> None:
        _write(ERROR_SQL, (name, now, f"{info['type']}: {info['message']}"))
        try:
            _send_tracking(event)
        except Exception as err:  # noqa: BLE001
            print(f"errors: error-tracking delivery failed: {err!r}", file=sys.stderr)

    t = _in_background(run)
    if wait:
        t.join(10)


def record_start(service: str) -> threading.Thread:
    now = datetime.now(timezone.utc)
    return _in_background(_write, START_SQL, (service, now, now))


def install(service: str) -> None:
    """Called once per process (tracing.init_tracing does it): records the
    start and chains sys.excepthook / threading.excepthook so an uncaught
    exception, on any thread, is counted before the usual traceback."""
    global _service, _installed
    with _lock:
        if _installed:
            return
        _installed = True
        _service = service
    if os.environ.get("BALER_ERRORS_DISABLED") == "1":
        return
    record_start(service)

    prev_hook = sys.excepthook

    def hook(exc_type, exc, tb):
        if exc is not None and not isinstance(exc, (KeyboardInterrupt, SystemExit)):
            record(exc, wait=True)
        prev_hook(exc_type, exc, tb)

    sys.excepthook = hook

    prev_thread_hook = threading.excepthook

    def thread_hook(args):
        if args.exc_value is not None and not isinstance(args.exc_value, SystemExit):
            record(args.exc_value)
        prev_thread_hook(args)

    threading.excepthook = thread_hook


def summary(conn) -> list[dict[str, Any]]:
    """service_runtime for /ops/status: [{service, starts, errors,
    last_error_at, last_error, last_start_at}], [] without the table."""
    try:
        rows = conn.execute(
            "SELECT service, starts, errors, last_error_at, last_error, recent_starts[1] "
            "FROM service_runtime ORDER BY service").fetchall()
    except Exception:  # noqa: BLE001 - migration 0004 not applied yet
        return []

    def iso(t):
        return t.astimezone(timezone.utc).isoformat() if t else None

    return [{"service": s, "starts": int(n), "errors": int(e), "last_error_at": iso(at), "last_error": msg,
             "last_start_at": iso(st)} for s, n, e, at, msg, st in rows]


def recent_starts(conn) -> dict[str, list[datetime]]:
    """{service: its last start times, newest first}, {} without the table."""
    try:
        return {s: list(ts or []) for s, ts in conn.execute("SELECT service, recent_starts FROM service_runtime")}
    except Exception:  # noqa: BLE001
        return {}
