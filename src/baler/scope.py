"""
Which points and equipment a caller may read, by site.

A site owns data by **topic prefix**: `fbf/hospital-a` owns
`urn:point:fbf/hospital-a/...` and `urn:equip:fbf/hospital-a/...` (and the
equip `urn:equip:fbf/hospital-a` itself). That's the namespace Windrow
already gives each building (multi-site.mdx), so no stored data changes.

Rules:

  - No site anywhere has a prefix: everything is visible (single-tenant
    installs, and every install before this existed).
  - Otherwise a caller sees only the prefixes of their sites: the token's
    `site_ids`, or every site of their org when it carries none (an
    org-wide user or API key). Anything outside those prefixes, including
    entities that aren't points or equipment, is invisible.
  - A request with no verifiable token sees nothing once scoping is on.

Backends verify the caller's own JWT here (not just trusting the gateway's
X-User header) because they need its `site_ids`/`org_id` claims.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import jwt as pyjwt

from baler import ddl

SCHEMA = "ALTER TABLE sites ADD COLUMN IF NOT EXISTS topic_prefixes TEXT[] NOT NULL DEFAULT '{}'"

_KEYED_PREFIXES = ("urn:point:", "urn:equip:")
_BEARER = re.compile(r"^bearer\s+(?:authToken=)?([^\s,]+)\s*$", re.IGNORECASE)


def ensure_schema(conn) -> None:
    ddl.run(conn, SCHEMA)


def entity_key(uri: str) -> str | None:
    """`urn:point:fbf/a/ahu-1/x` -> `fbf/a/ahu-1/x`; non-point/equip -> None."""
    for p in _KEYED_PREFIXES:
        if uri.startswith(p):
            return uri[len(p):]
    return None


def normalize_prefix(prefix: str) -> str:
    p = prefix.strip().rstrip("/")
    if not p:
        raise ValueError("topic prefix must not be empty")
    return p


@dataclass(frozen=True)
class Scope:
    # None = unrestricted; otherwise normalized prefixes (no trailing "/").
    prefixes: tuple[str, ...] | None

    @classmethod
    def everything(cls) -> Scope:
        return cls(None)

    @classmethod
    def nothing(cls) -> Scope:
        return cls(())

    @property
    def unrestricted(self) -> bool:
        return self.prefixes is None

    def allows(self, uri: str) -> bool:
        if self.prefixes is None:
            return True
        key = entity_key(uri)
        if key is None:
            return False
        return any(key == p or key.startswith(p + "/") for p in self.prefixes)

    def filter(self, uris):
        return list(uris) if self.prefixes is None else [u for u in uris if self.allows(u)]


def claims_from_header(header: str | None) -> dict | None:
    """Verify `Bearer <jwt>` or Haystack's `BEARER authToken=<jwt>`."""
    if not header:
        return None
    m = _BEARER.match(header)
    secret = os.environ.get("BALER_JWT_SECRET")
    if not m or not secret:
        return None
    try:
        return pyjwt.decode(m.group(1), secret, algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return None


def scope_for(conn, claims: dict | None) -> Scope:
    """One query: every site's prefixes, then the caller's share of them."""
    rows = conn.execute("SELECT id, org_id, topic_prefixes FROM sites").fetchall()
    if not any(prefixes for _, _, prefixes in rows):
        return Scope.everything()
    if not claims:
        return Scope.nothing()
    org_id = claims.get("org_id")
    wanted = set(claims.get("site_ids") or [])
    prefixes = {
        normalize_prefix(p)
        for site_id, site_org, site_prefixes in rows
        if site_org == org_id and (not wanted or site_id in wanted)
        for p in (site_prefixes or [])
    }
    return Scope(tuple(sorted(prefixes)))


def request_scope(conn, headers) -> Scope:
    """The calling request's scope, from its own Authorization header."""
    return scope_for(conn, claims_from_header(headers.get("Authorization")))


# Shared immutable default for "no restriction" (Scope is frozen).
EVERYTHING = Scope.everything()
