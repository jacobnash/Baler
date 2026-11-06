"""
Source registry (Baler#20): what feeds this system, how each feed is
configured, and whether it's working - in one place the UI can show and
edit, instead of env vars and compose profiles.

Each *kind* (haystack, sql, openmeteo, energystar) declares its settings
schema in KINDS. A *source* is a configured instance of a kind: settings,
enabled flag, and health from its last runs, which `source_runner` (one
daemon for every kind) records as it pulls. A source missing a required
setting can't be enabled, and a failing one says why - the "fails every
cycle silently against an empty URL" mode of the env-configured pullers
is gone.

Secret settings (passwords, DSNs) are encrypted at rest with a key derived
from BALER_GATEWAY_SECRET (or BALER_SOURCES_KEY when set), and never
returned by the API: reads show only whether one is set.

The per-kind pullers (`python -m baler.haystack_puller ...`) still work
from env/flags for one-off `--once` imports.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from baler import ddl, json_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT false,
    settings     JSONB NOT NULL DEFAULT '{}',
    secrets      TEXT,                -- Fernet token of a JSON object
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at  TIMESTAMPTZ,
    last_ok_at   TIMESTAMPTZ,
    last_error   TEXT,
    last_result  JSONB
);
"""


def _field(name, label, *, required=False, secret=False, kind="str", default=None, help=""):
    return {"name": name, "label": label, "required": required, "secret": secret, "type": kind,
            "default": default, "help": help}


# kind -> label, what it does, and its settings. `type` is str|int|float|bool.
KINDS: dict[str, dict] = {
    "haystack": {
        "label": "Haystack server",
        "help": "Pulls equipment, points and history from a Haystack server (SkySpark, Haxall, nhaystack).",
        "fields": [
            _field("url", "Server URL", required=True, help="e.g. https://skyspark.example.com/api/demo"),
            _field("user", "Username", required=True),
            _field("password", "Password", secret=True),
            _field("source_id", "Source id", default="haxall", help="prefixes this source's ids in Baler"),
            _field("point_filter", "Point filter", default="point"),
            _field("equip_filter", "Equip filter", default="equip"),
            _field("tick_seconds", "Poll every (s)", kind="float", default=300.0),
            _field("backfill_days", "Backfill (days)", kind="float", default=7.0),
        ],
    },
    "sql": {
        "label": "SQL database",
        "help": "Runs a query returning (point, ts, value) rows against another database.",
        "fields": [
            _field("dsn", "Connection string", required=True, secret=True, help="postgresql://user:pass@host/db"),
            _field("query", "Query", required=True, help="must return point, ts, value columns"),
            _field("source_id", "Source id", default="sql"),
            _field("tick_seconds", "Poll every (s)", kind="float", default=300.0),
            _field("backfill_days", "Backfill (days)", kind="float", default=7.0),
        ],
    },
    "openmeteo": {
        "label": "Open-Meteo weather",
        "help": "Outside conditions for a location (free, no key).",
        "fields": [
            _field("latitude", "Latitude", required=True, kind="float"),
            _field("longitude", "Longitude", required=True, kind="float"),
            _field("station_id", "Station id", default="openmeteo"),
            _field("tick_seconds", "Poll every (s)", kind="float", default=900.0),
        ],
    },
    "energystar": {
        "label": "ENERGY STAR Portfolio Manager",
        "help": "Monthly scores and intensities.",
        "fields": [
            _field("username", "Username", required=True),
            _field("password", "Password", required=True, secret=True),
            _field("account_id", "Account id", help="discovered from the login if blank"),
            _field("live", "Live (not test) API", kind="bool", default=False),
            _field("metrics", "Metrics", default="score,siteIntensity,sourceIntensity"),
            _field("tick_seconds", "Poll every (s)", kind="float", default=86400.0),
        ],
    },
}


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


# --- secrets ---------------------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet

    secret = os.environ.get("BALER_SOURCES_KEY") or os.environ.get("BALER_GATEWAY_SECRET")
    if not secret:
        raise RuntimeError("BALER_GATEWAY_SECRET (or BALER_SOURCES_KEY) must be set to store source secrets")
    key = hashlib.sha256(b"baler-sources:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(values: dict) -> str | None:
    return _fernet().encrypt(json.dumps(values).encode()).decode() if values else None


def _open(token: str | None) -> dict:
    return json.loads(_fernet().decrypt(token.encode())) if token else {}


# --- validation -------------------------------------------------------------


def _coerce(field: dict, value):
    if value is None or value == "":
        return None
    t = field["type"]
    try:
        if t == "float":
            return float(value)
        if t == "int":
            return int(value)
        if t == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field['label']}: expected a {t}") from exc
    return str(value)


def missing_required(kind: str, settings: dict, secret_names: set[str]) -> list[str]:
    return [f["name"] for f in KINDS[kind]["fields"]
            if f["required"] and (f["name"] not in secret_names if f["secret"] else settings.get(f["name"]) in (None, ""))]


def _split(kind: str, values: dict) -> tuple[dict, dict]:
    """Validated (plain settings, secret settings) from a request body."""
    fields = {f["name"]: f for f in KINDS[kind]["fields"]}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"unknown settings for {kind}: {', '.join(sorted(unknown))}")
    plain: dict = {}
    secret: dict = {}
    for name, raw in values.items():
        v = _coerce(fields[name], raw)
        (secret if fields[name]["secret"] else plain)[name] = v
    return plain, secret


# --- CRUD ---------------------------------------------------------------------

_COLS = "id, kind, name, enabled, settings, secrets, created_at, updated_at, last_run_at, last_ok_at, last_error, last_result"


def _row(r) -> dict:
    secrets = _open(r[5]) if r[5] else {}
    return {"id": r[0], "kind": r[1], "name": r[2], "enabled": r[3], "settings": r[4] or {}, "_secrets": secrets,
            "created_at": r[6], "updated_at": r[7], "last_run_at": r[8], "last_ok_at": r[9],
            "last_error": r[10], "last_result": r[11]}


def public(src: dict) -> dict:
    """What the API shows: secrets as set/not set, health summarised."""
    kind = KINDS.get(src["kind"], {"fields": []})
    secret_names = {f["name"] for f in kind["fields"] if f["secret"]}
    missing = missing_required(src["kind"], src["settings"], {k for k, v in src["_secrets"].items() if v}) \
        if src["kind"] in KINDS else []
    if not src["enabled"]:
        health = "disabled"
    elif missing:
        health = "misconfigured"
    elif src["last_run_at"] is None:
        health = "pending"
    elif src["last_error"] and (src["last_ok_at"] is None or src["last_ok_at"] < src["last_run_at"]):
        health = "failing"
    else:
        health = "ok"
    iso = lambda d: d.isoformat() if isinstance(d, datetime) else d
    return {
        "id": src["id"], "kind": src["kind"], "name": src["name"], "enabled": src["enabled"],
        "settings": src["settings"],
        "secrets_set": sorted(n for n in secret_names if src["_secrets"].get(n)),
        "missing": missing, "health": health,
        "last_run_at": iso(src["last_run_at"]), "last_ok_at": iso(src["last_ok_at"]),
        "last_error": src["last_error"], "last_result": src["last_result"],
        "created_at": iso(src["created_at"]), "updated_at": iso(src["updated_at"]),
    }


def list_sources(conn, enabled_only: bool = False) -> list[dict]:
    sql = f"SELECT {_COLS} FROM sources" + (" WHERE enabled" if enabled_only else "") + " ORDER BY kind, name"
    return [_row(r) for r in conn.execute(sql).fetchall()]


def get_source(conn, source_id: str) -> dict | None:
    r = conn.execute(f"SELECT {_COLS} FROM sources WHERE id = %s", (source_id,)).fetchone()
    return _row(r) if r else None


def create_source(conn, kind: str, name: str, settings: dict, enabled: bool = False) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown source kind {kind!r}; one of {', '.join(KINDS)}")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    plain, secret = _split(kind, settings or {})
    plain = {k: v for k, v in plain.items() if v is not None}
    secret = {k: v for k, v in secret.items() if v is not None}
    if enabled and missing_required(kind, plain, set(secret)):
        raise ValueError(f"can't enable: missing {', '.join(missing_required(kind, plain, set(secret)))}")
    sid = json_store.new_id()
    conn.execute(
        "INSERT INTO sources (id, kind, name, enabled, settings, secrets) VALUES (%s, %s, %s, %s, %s, %s)",
        (sid, kind, name.strip(), enabled, json.dumps(plain), _seal(secret)),
    )
    src = get_source(conn, sid)
    assert src is not None
    return src


def update_source(conn, source_id: str, name: str | None = None, settings: dict | None = None,
                  enabled: bool | None = None) -> dict | None:
    """Merge settings (a null/"" value clears one; secrets are replaced only
    when sent). Enabling needs every required setting."""
    src = get_source(conn, source_id)
    if src is None:
        return None
    plain, secret = dict(src["settings"]), dict(src["_secrets"])
    if settings:
        new_plain, new_secret = _split(src["kind"], settings)
        for k, v in new_plain.items():
            plain.pop(k, None) if v is None else plain.__setitem__(k, v)
        for k, v in new_secret.items():
            secret.pop(k, None) if v is None else secret.__setitem__(k, v)
    now_enabled = src["enabled"] if enabled is None else bool(enabled)
    missing = missing_required(src["kind"], plain, set(secret))
    if now_enabled and missing:
        raise ValueError(f"can't enable: missing {', '.join(missing)}")
    conn.execute(
        "UPDATE sources SET name = %s, enabled = %s, settings = %s, secrets = %s, updated_at = now() WHERE id = %s",
        ((name or src["name"]).strip(), now_enabled, json.dumps(plain), _seal(secret), source_id),
    )
    return get_source(conn, source_id)


def delete_source(conn, source_id: str) -> bool:
    return conn.execute("DELETE FROM sources WHERE id = %s", (source_id,)).rowcount > 0


def record_run(conn, source_id: str, ok: bool, result=None, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE sources SET last_run_at = %s, last_ok_at = CASE WHEN %s THEN %s ELSE last_ok_at END, "
        "last_error = %s, last_result = %s WHERE id = %s",
        (now, ok, now, None if ok else (error or "failed")[:2000], json.dumps(result, default=str) if ok else None,
         source_id),
    )


def effective_settings(src: dict) -> dict:
    """Defaults, then stored settings, then secrets - what a runner uses."""
    out = {f["name"]: f["default"] for f in KINDS[src["kind"]]["fields"] if f["default"] is not None}
    out.update(src["settings"])
    out.update(src["_secrets"])
    return out
