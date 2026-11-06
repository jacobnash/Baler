"""
MQTT broker accounts for edge nodes (Baler#36): a Windrow on a Raspberry Pi
(or any site gateway) publishing into Baler from outside the Docker host.

Accounts live in Mosquitto's dynamic-security plugin, managed live over its
control topic - created, rotated and revoked from the auth API, no `.env`
edit or broker restart. Each account belongs to one site and may only:

  - publish under that site's topic prefixes (`<prefix>` and `<prefix>/#`),
  - publish and subscribe on its own command channel `cmd/<username>/#`
    (multi-site.mdx's "reaching back into a site").

So one site's node can't write into another site's topics - the broker now
enforces the same ownership the auth API's topic_prefixes describe. Changing
a site's prefixes re-applies its accounts' rights.

Passwords are never stored here: the plugin keeps a salted hash, and the
API returns a generated password once. The trusted backend account
(`baler`, MOSQUITTO_USERS / mosquitto/acl) is unchanged.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone

from baler import ddl, json_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_accounts (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    site_id     TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    username    TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    rotated_at  TIMESTAMPTZ
);
"""

CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
# password_file accounts (MOSQUITTO_USERS) and dynsec init's unused admin
RESERVED_USERNAMES = frozenset({"baler", "admin", "dynsec-admin", "windrow"})
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,62}")


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


class BrokerError(RuntimeError):
    """The broker refused or didn't answer a control command."""


class DynsecClient:
    """Sends dynamic-security commands over MQTT and waits for the reply.
    One short-lived connection per call batch: account changes are rare."""

    def __init__(self, host: str | None = None, port: int | None = None,
                 username: str | None = None, password: str | None = None, timeout: float = 10.0):
        self.host = host or os.environ.get("MQTT_HOST", "localhost")
        self.port = int(port or os.environ.get("MQTT_PORT", "1883"))
        # The backend account (mosquitto/acl grants it the control topic) -
        # no separate dynsec admin secret to keep in sync.
        self.username = username or os.environ.get("MQTT_USERNAME", "baler")
        self.password = password if password is not None else os.environ.get("MQTT_PASSWORD", "")
        self.timeout = timeout

    def run(self, commands: list[dict], tolerate_missing: bool = False) -> list[dict]:
        """Send a batch (the broker applies each command on its own). With
        tolerate_missing, "... not found" errors - deleting what's already
        gone - aren't failures."""
        import paho.mqtt.client as mqtt

        done = threading.Event()
        replies: list[dict] = []
        errors: list[str] = []

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"baler-dynsec-{uuid.uuid4().hex[:8]}")
        client.username_pw_set(self.username, self.password)

        def on_connect(c, userdata, flags, reason_code, properties=None):
            if reason_code != 0:
                errors.append(f"broker refused the dynsec admin login ({reason_code})")
                done.set()
                return
            c.subscribe(f"{CONTROL_TOPIC}/response", qos=1)

        def on_subscribe(c, userdata, mid, reason_codes, properties=None):
            c.publish(CONTROL_TOPIC, json.dumps({"commands": commands}), qos=1)

        def on_message(c, userdata, msg):
            try:
                replies.extend(json.loads(msg.payload).get("responses", []))
            except (ValueError, AttributeError):
                errors.append("unreadable dynsec response")
            done.set()

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        try:
            client.connect(self.host, self.port, keepalive=30)
        except OSError as exc:
            raise BrokerError(f"can't reach the MQTT broker at {self.host}:{self.port}: {exc}") from exc
        client.loop_start()
        try:
            if not done.wait(self.timeout):
                raise BrokerError("the MQTT broker didn't answer the dynsec command in time")
        finally:
            client.loop_stop()
            client.disconnect()
        if errors:
            raise BrokerError(errors[0])
        failed = [r for r in replies if r.get("error")
                  and not (tolerate_missing and "not found" in str(r["error"]).lower())]
        if failed:
            raise BrokerError("; ".join(f"{r.get('command')}: {r['error']}" for r in failed))
        return replies


# --- what an account may do -------------------------------------------------


def role_name(username: str) -> str:
    return f"edge-{username}"


def acls_for(username: str, prefixes: list[str]) -> list[dict]:
    """Publish under the site's prefixes; publish, subscribe and receive on
    its own command channel; nothing else.

    The explicit `#` denies matter: with mosquitto's password_file/acl_file
    also loaded (for the backend account), dynsec's *default* subscribe
    deny doesn't hold - a node could subscribe to `fbf/#` and read every
    site. Allows outrank the denies by priority (higher is checked first).
    """
    allow = 10
    acls: list[dict] = []
    for p in prefixes:
        p = p.rstrip("/")
        acls += [
            {"acltype": "publishClientSend", "topic": p, "priority": allow, "allow": True},
            {"acltype": "publishClientSend", "topic": f"{p}/#", "priority": allow, "allow": True},
        ]
    cmd = f"cmd/{username}/#"
    acls += [
        {"acltype": "publishClientSend", "topic": cmd, "priority": allow, "allow": True},
        {"acltype": "publishClientReceive", "topic": cmd, "priority": allow, "allow": True},
        {"acltype": "subscribePattern", "topic": cmd, "priority": allow, "allow": True},
    ]
    acls += [
        {"acltype": t, "topic": "#", "priority": 0, "allow": False}
        for t in ("publishClientSend", "publishClientReceive", "subscribePattern")
    ]
    return acls


def _role_commands(username: str, prefixes: list[str]) -> list[dict]:
    return [{"command": "createRole", "rolename": role_name(username), "acls": acls_for(username, prefixes)}]


# --- accounts -----------------------------------------------------------------


def _site(conn, org_id: str, site_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, name, topic_prefixes FROM sites WHERE id = %s AND org_id = %s", (site_id, org_id)
    ).fetchone()
    return {"id": row[0], "name": row[1], "topic_prefixes": list(row[2] or [])} if row else None


def _row(r) -> dict:
    return {"id": r[0], "site_id": r[1], "username": r[2], "description": r[3],
            "created_at": r[4].isoformat(), "rotated_at": r[5].isoformat() if r[5] else None}


_COLS = "id, site_id, username, description, created_at, rotated_at"


def connect_info() -> dict:
    """What an edge node is told to connect to (shown with a new password)."""
    host = os.environ.get("BALER_MQTT_PUBLIC_HOST") or None
    tls_port = os.environ.get("BALER_MQTT_PUBLIC_TLS_PORT") or None
    return {
        "host": host,
        "port": int(os.environ.get("BALER_MQTT_PUBLIC_PORT") or 1883),
        "tls_port": int(tls_port) if tls_port else None,
    }


def list_accounts(conn, org_id: str) -> list[dict]:
    rows = conn.execute(f"SELECT {_COLS} FROM broker_accounts WHERE org_id = %s ORDER BY username", (org_id,)).fetchall()
    return [_row(r) for r in rows]


def create_account(conn, broker: DynsecClient, org_id: str, site_id: str, username: str,
                   description: str | None = None) -> tuple[dict, str]:
    """(account, its generated password - shown once)."""
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise ValueError("username: 3-63 characters, lowercase letters, digits, . _ -")
    if username in RESERVED_USERNAMES or username == os.environ.get("MQTT_USERNAME", "baler"):
        raise ValueError("that username is reserved for the backend")
    site = _site(conn, org_id, site_id)
    if site is None:
        raise LookupError("no such site in your org")
    if not site["topic_prefixes"]:
        raise ValueError("give the site a topic prefix first - it's what the account may publish under")
    if conn.execute("SELECT 1 FROM broker_accounts WHERE username = %s", (username,)).fetchone():
        raise ValueError("that broker username is taken")
    # Row first, broker second, in one transaction: the row is rolled back
    # if the broker refuses, so the table never lists an account that
    # doesn't exist. (The reverse - a broker client with no row - only
    # happens if the commit itself fails after the broker said yes.)
    account_id = json_store.new_id()
    password = secrets.token_urlsafe(24)
    with conn.transaction():
        conn.execute(
            "INSERT INTO broker_accounts (id, org_id, site_id, username, description, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (account_id, org_id, site_id, username, description, datetime.now(timezone.utc)),
        )
        broker.run(_role_commands(username, site["topic_prefixes"]) + [{
            "command": "createClient", "username": username, "password": password,
            "roles": [{"rolename": role_name(username)}],
            "textdescription": (description or f"edge node for site {site['name']}")[:200],
        }])
    acct = get_account(conn, org_id, account_id)
    assert acct is not None
    return acct, password


def get_account(conn, org_id: str, account_id: str) -> dict | None:
    r = conn.execute(f"SELECT {_COLS} FROM broker_accounts WHERE id = %s AND org_id = %s", (account_id, org_id)).fetchone()
    return _row(r) if r else None


def _must_get(conn, org_id: str, account_id: str) -> dict:
    acct = get_account(conn, org_id, account_id)
    if acct is None:
        raise LookupError("no such broker account in your org")
    return acct


def rotate_password(conn, broker: DynsecClient, org_id: str, account_id: str) -> tuple[dict, str]:
    """A new password (returned once); the old one stops working at once."""
    acct = _must_get(conn, org_id, account_id)
    password = secrets.token_urlsafe(24)
    with conn.transaction():
        conn.execute("UPDATE broker_accounts SET rotated_at = %s WHERE id = %s", (datetime.now(timezone.utc), account_id))
        broker.run([{"command": "setClientPassword", "username": acct["username"], "password": password}])
    return _must_get(conn, org_id, account_id), password


def revoke(conn, broker: DynsecClient, org_id: str, account_id: str) -> None:
    acct = _must_get(conn, org_id, account_id)
    with conn.transaction():
        conn.execute("DELETE FROM broker_accounts WHERE id = %s", (account_id,))
        # deleting the client also drops any connection it has open
        broker.run([{"command": "deleteClient", "username": acct["username"]},
                    {"command": "deleteRole", "rolename": role_name(acct["username"])}], tolerate_missing=True)


def reapply_site(conn, broker: DynsecClient, org_id: str, site_id: str) -> int:
    """After a site's topic prefixes change: rebuild each of its accounts'
    role with the new prefixes. Returns how many accounts were updated."""
    site = _site(conn, org_id, site_id)
    if site is None:
        return 0
    accounts = [a for a in list_accounts(conn, org_id) if a["site_id"] == site_id]
    commands: list[dict] = []
    for a in accounts:
        u = a["username"]
        commands.append({"command": "deleteRole", "rolename": role_name(u)})
        commands += _role_commands(u, site["topic_prefixes"])
        commands += [{"command": "addClientRole", "username": u, "rolename": role_name(u)}]
    if commands:
        broker.run(commands, tolerate_missing=True)
    return len(accounts)
