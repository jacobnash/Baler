"""
0015 deferred maintenance, service-life references and FCI (#200, epic #221).

- lc_deferred_items: deferred work with its cost, risk and the asset or
  position it affects. Closed (done/cancelled), never deleted.
- lc_service_life: an org's expected service life per asset type, entered
  by a person with the reference it came from (title, URL, the date it was
  read). Baler ships no values: published tables are cited, not copied.
- lc_asset_values: append-only replacement-value estimates per asset, each
  dated with its basis; the latest is the asset's current replacement
  value, the denominator of FCI.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 15
NAME = "lifecycle deferred maintenance and service life"

DEFERRED = """
CREATE TABLE IF NOT EXISTS lc_deferred_items (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    asset_id      TEXT REFERENCES lc_assets(id),
    position_uri  TEXT,
    title         TEXT NOT NULL,
    description   TEXT,
    category      TEXT,
    cost          NUMERIC(14, 2) NOT NULL CHECK (cost >= 0),
    currency      TEXT,
    risk          TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    identified_on DATE NOT NULL,
    target_year   INT,
    status        TEXT NOT NULL CHECK (status IN ('open', 'planned', 'done', 'cancelled')),
    resolved_at   TIMESTAMPTZ,
    resolution    TEXT,
    source        JSONB,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    CHECK (asset_id IS NOT NULL OR position_uri IS NOT NULL)
)"""

SERVICE_LIFE = """
CREATE TABLE IF NOT EXISTS lc_service_life (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(id),
    asset_type   TEXT NOT NULL,
    median_years NUMERIC(5, 1) NOT NULL CHECK (median_years > 0),
    source_title TEXT NOT NULL,
    source_url   TEXT,
    retrieved_on DATE NOT NULL,
    note         TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
)"""

VALUES = """
CREATE TABLE IF NOT EXISTS lc_asset_values (
    id                TEXT PRIMARY KEY,
    org_id            TEXT NOT NULL REFERENCES orgs(id),
    asset_id          TEXT NOT NULL REFERENCES lc_assets(id),
    replacement_value NUMERIC(14, 2) NOT NULL CHECK (replacement_value >= 0),
    currency          TEXT,
    as_of             DATE NOT NULL,
    basis             TEXT,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(DEFERRED, "lc_deferred_items: the deferred-maintenance register"),
        Step("CREATE INDEX IF NOT EXISTS lc_deferred_items_org ON lc_deferred_items (org_id, status)", "by org"),
        Step(SERVICE_LIFE, "lc_service_life: expected life per asset type, with its reference"),
        Step("CREATE UNIQUE INDEX IF NOT EXISTS lc_service_life_type ON lc_service_life (org_id, lower(asset_type))",
             "one reference per type per org"),
        Step(VALUES, "lc_asset_values: dated replacement-value estimates"),
        Step("CREATE INDEX IF NOT EXISTS lc_asset_values_asset ON lc_asset_values (asset_id, as_of)", "by asset"),
    ]
