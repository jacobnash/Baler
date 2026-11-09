"""
0011 positions vs installed assets (#197, epic #221).

A position ("AHU-1 supply fan motor") is the Brick equipment the points
hang on: permanent, it lives in the graph and is referred to here by its
URI. An asset is the physical thing bolted there: make, model, serial,
cost. Assets come and go.

- lc_assets: one row per physical asset. Corrections to its fields are
  audited in record_edits (entity urn:baler:asset:<id>).
- lc_asset_events: append-only install/remove events linking an asset to
  a position, each with when and why. A replacement appends a remove and
  an install; nothing is ever overwritten, so "when were the pumps changed,
  and to what brand" is a query over this table.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 11
NAME = "lifecycle assets and install events"

ASSETS = """
CREATE TABLE IF NOT EXISTS lc_assets (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES orgs(id),
    site_id        TEXT REFERENCES sites(id),
    tag            TEXT,
    asset_type     TEXT,
    brick_class    TEXT,
    manufacturer   TEXT,
    model          TEXT,
    serial         TEXT,
    cost           NUMERIC(14, 2),
    currency       TEXT,
    acquired_on    DATE,
    attributes     JSONB NOT NULL DEFAULT '{}',
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL,
    retired_at     TIMESTAMPTZ,
    retired_reason TEXT
)"""

EVENTS = """
CREATE TABLE IF NOT EXISTS lc_asset_events (
    id           TEXT PRIMARY KEY,
    org_id       TEXT NOT NULL REFERENCES orgs(id),
    asset_id     TEXT NOT NULL REFERENCES lc_assets(id),
    position_uri TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('install', 'remove')),
    at           TIMESTAMPTZ NOT NULL,
    reason       TEXT,
    cost         NUMERIC(14, 2),
    work_ref     TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(ASSETS, "lc_assets: physical assets (make, model, serial, cost)"),
        Step("CREATE INDEX IF NOT EXISTS lc_assets_org ON lc_assets (org_id, manufacturer)", "lc_assets by org"),
        Step("CREATE INDEX IF NOT EXISTS lc_assets_serial ON lc_assets (org_id, serial)", "lc_assets by serial"),
        Step(EVENTS, "lc_asset_events: append-only install/remove history"),
        Step("CREATE INDEX IF NOT EXISTS lc_asset_events_position ON lc_asset_events (position_uri, at)",
             "events by position"),
        Step("CREATE INDEX IF NOT EXISTS lc_asset_events_asset ON lc_asset_events (asset_id, at)", "events by asset"),
    ]
