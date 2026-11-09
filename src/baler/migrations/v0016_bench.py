"""
0016 bench-test records on assets (#211, epic #221).

- lc_bench_procedures: bench procedures as data (the Cx 6 pattern): steps,
  each with what to measure and its acceptance criterion.
- lc_bench_tests: one bench test of one asset: the procedure as it was run
  (a snapshot, so later edits to the procedure don't rewrite history), each
  step's measurement and verdict, the overall verdict, who tested it, with
  what instruments, and when. Append-only. The earliest passing test before
  the asset's first install is its first baseline.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 16
NAME = "lifecycle bench tests"

PROCEDURES = """
CREATE TABLE IF NOT EXISTS lc_bench_procedures (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    name        TEXT NOT NULL,
    asset_type  TEXT,
    description TEXT,
    steps       JSONB NOT NULL,
    template    TEXT,
    status      TEXT NOT NULL,            -- active | archived
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
)"""

TESTS = """
CREATE TABLE IF NOT EXISTS lc_bench_tests (
    id             TEXT PRIMARY KEY,
    org_id         TEXT NOT NULL REFERENCES orgs(id),
    asset_id       TEXT NOT NULL REFERENCES lc_assets(id),
    procedure_id   TEXT REFERENCES lc_bench_procedures(id),
    procedure      JSONB NOT NULL,
    results        JSONB NOT NULL,
    verdict        TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'inconclusive')),
    tested_at      TIMESTAMPTZ NOT NULL,
    technician     TEXT,
    instruments    TEXT,
    notes          TEXT,
    before_install BOOLEAN NOT NULL,
    document_id    TEXT,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL
)"""


def plan(conn) -> list[Step]:
    return [
        Step(PROCEDURES, "lc_bench_procedures: bench procedures as data"),
        Step(TESTS, "lc_bench_tests: bench results per asset, append-only"),
        Step("CREATE INDEX IF NOT EXISTS lc_bench_tests_asset ON lc_bench_tests (asset_id, tested_at)", "by asset"),
    ]
