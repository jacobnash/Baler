"""
Which points and equipment a caller may read, by site.

A site owns data by **topic prefix**: `fbf/hospital-a` owns
`urn:point:fbf/hospital-a/...` and `urn:equip:fbf/hospital-a/...` (and the
equip `urn:equip:fbf/hospital-a` itself). That's the namespace Windrow
already gives each building (multi-site.mdx), so no stored data changes.

Rules (per org - Baler#16):

  - No site anywhere has a prefix: everything is visible (single-tenant
    installs, and every install before this existed).
  - The caller's org has prefixed sites: they see only those prefixes -
    the token's `site_ids`, or every site of their org when it carries
    none (an org-wide user or API key).
  - The caller's org has none: they see everything *except* what other
    orgs' sites have claimed. (Partitioning one org doesn't blind the
    others, as a global switch did.)
  - Locations (Site/Building/Floor/Zone, anything that isn't a point or
    equipment) have no prefix of their own: one is visible when it
    contains something visible (hisquery.SITE_MEMBER reaches it), and
    hidden when it contains something another org claimed.
  - A request with no verifiable token sees nothing once scoping is on.

Graph reads (/api read and nav, /his) apply scope inside the SPARQL, to the
result and to every record a path hop reaches - see Scope.sparql - so a
filter can't test tags on records the caller can't see.

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


def _under(key: str, prefixes) -> bool:
    return any(key == p or key.startswith(p + "/") for p in prefixes)


# Same order and names as hisquery.SITE_MEMBER (kept here, not imported,
# because hisquery imports nothing from scope and must stay that way).
_SITE_MEMBER = "(<https://brickschema.org/schema/Brick#hasPart>|^<https://brickschema.org/schema/Brick#feeds>|<https://brickschema.org/schema/Brick#hasPoint>)+"


@dataclass(frozen=True)
class Scope:
    # None = no allow-list; otherwise only these normalized prefixes.
    prefixes: tuple[str, ...] | None
    # Claimed by other orgs: hidden even without an allow-list.
    exclude: tuple[str, ...] = ()

    @classmethod
    def everything(cls) -> Scope:
        return cls(None)

    @classmethod
    def nothing(cls) -> Scope:
        return cls(())

    @property
    def unrestricted(self) -> bool:
        return self.prefixes is None and not self.exclude

    def allows(self, uri: str) -> bool:
        """Points and equipment by prefix. A location can't be judged
        without the graph: allowed unless there's an allow-list (graph
        reads decide it properly, in Scope.sparql)."""
        key = entity_key(uri)
        if key is None:
            return self.prefixes is None
        if self.prefixes is not None:
            return _under(key, self.prefixes)
        return not _under(key, self.exclude)

    def sparql(self, var: str) -> str | None:
        """A SPARQL FILTER expression: ?var is visible. None when everything is."""
        if self.unrestricted:
            return None

        def under(v: str, prefixes) -> str:
            tests = []
            for kind in _KEYED_PREFIXES:
                for p in prefixes:
                    full = (kind + p).replace("\\", "\\\\").replace('"', '\\"')
                    tests.append(f'STR(?{v}) = "{full}" || STRSTARTS(STR(?{v}), "{full}/")')
            return "(" + " || ".join(tests) + ")" if tests else "false"

        keyed = f'(STRSTARTS(STR(?{var}), "urn:point:") || STRSTARTS(STR(?{var}), "urn:equip:"))'
        # a nested Dict value is only reachable from a record already in scope
        value_node = f'STRSTARTS(STR(?{var}), "urn:baler:dict:")'
        k = f"{var}_k"
        if self.prefixes is not None:
            if not self.prefixes:
                return "false"
            return (f"{value_node} || ({keyed} && {under(var, self.prefixes)}) || "
                    f'(!{keyed} && STRSTARTS(STR(?{var}), "urn:") && '
                    f"EXISTS {{ ?{var} {_SITE_MEMBER} ?{k} . FILTER({under(k, self.prefixes)}) }})")
        return (f"({keyed} && !{under(var, self.exclude)}) || "
                f"(!{keyed} && NOT EXISTS {{ ?{var} {_SITE_MEMBER} ?{k} . FILTER({under(k, self.exclude)}) }})")

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
    own = {normalize_prefix(p) for _, o, ps in rows if o == org_id for p in (ps or [])}
    others = {normalize_prefix(p) for _, o, ps in rows if o != org_id for p in (ps or [])}
    if wanted:  # a site-restricted login: exactly its sites' prefixes
        mine = {normalize_prefix(p) for sid, o, ps in rows if o == org_id and sid in wanted for p in (ps or [])}
        return Scope(tuple(sorted(mine)))
    if own:
        return Scope(tuple(sorted(own)))
    return Scope(None, tuple(sorted(others)))


def request_scope(conn, headers) -> Scope:
    """The calling request's scope, from its own Authorization header."""
    return scope_for(conn, claims_from_header(headers.get("Authorization")))


# Shared immutable default for "no restriction" (Scope is frozen).
EVERYTHING = Scope.everything()
