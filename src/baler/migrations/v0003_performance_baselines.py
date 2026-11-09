"""
0003 performance baselines (#215, epic #221 §8.2): a regression fitted on a
known-good period (chiller kW/ton vs load and entering condenser water
first), read and written by baler.commissioning.baselines_api.

cx_performance_baselines: the position (equip_uri), the asset when the
asset registry knows it (asset_id, #197), the points the model reads, the
fit period, and the model (coefficients, fitted ranges, CV(RMSE), NMBE).
A refit supersedes the previous active baseline for the same equipment,
kind and asset; nothing is deleted.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 3
NAME = "performance baselines"

BASELINES = """
CREATE TABLE IF NOT EXISTS cx_performance_baselines (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    equip_uri   TEXT NOT NULL,
    asset_id    TEXT,
    kind        TEXT NOT NULL,              -- chiller_kw_per_ton
    points      JSONB NOT NULL,
    fit_start   TIMESTAMPTZ NOT NULL,
    fit_end     TIMESTAMPTZ NOT NULL,
    model       JSONB NOT NULL,
    status      TEXT NOT NULL,              -- active | superseded
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(BASELINES, "cx_performance_baselines: fitted equipment performance models"),
        Step("CREATE INDEX IF NOT EXISTS cx_performance_baselines_equip ON cx_performance_baselines (project_id, equip_uri)",
             "baselines by equipment"),
    ]
