"""
0001 baseline: the schema as the last release before versioned migrations
(main at 6946378) built it, at connect time, from each module's
`CREATE ... IF NOT EXISTS` (#194).

Frozen: this is a copy of that DDL, not an import of it, so it means the
same thing forever. Change the schema with a new migration, never here.

On a fresh database it builds everything. On an existing one (the live
install, a restore) it *adopts*: point_history's pieces are checked in the
catalog first and only what is missing runs (CREATE INDEX on a 27 M-row
hypertable takes a SHARE lock on every chunk even when the index exists,
Baler#107), and the other tables' statements are the same idempotent
`IF NOT EXISTS` forms every service already ran on every start, so on an
install that has them they change nothing.

It needs TimescaleDB, as point_history always has.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 1
NAME = "baseline"

# --- point_history (was baler.timeseries) -----------------------------------

POINT_HISTORY = """
CREATE TABLE IF NOT EXISTS point_history (
    point_uri  TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    value      DOUBLE PRECISION,
    value_text TEXT,
    value_bool BOOLEAN,
    unit       TEXT,
    label      TEXT,
    quality    TEXT,
    PRIMARY KEY (point_uri, ts)
)"""
HYPERTABLE = "SELECT create_hypertable('point_history', 'ts', if_not_exists => TRUE, migrate_data => TRUE)"
# read_point_labels' lookup: only derivation_engine sets unit/label, so on a
# raw point "newest row with a unit or label" would otherwise walk its whole
# history to find nothing. Small: it indexes the labelled rows only.
LABELS_INDEX = """
CREATE INDEX IF NOT EXISTS point_history_labelled
ON point_history (point_uri, ts DESC) WHERE unit IS NOT NULL OR label IS NOT NULL"""
# Per-sample quality (#183, baler.quality): NULL is ok. Added in place to
# tables made before the column; cheap on a hypertable (no rewrite for a
# nullable column without a default).
QUALITY_COLUMN = "ALTER TABLE point_history ADD COLUMN IF NOT EXISTS quality TEXT"

# The rollups (#51/#54): 5-minute and 1-hour continuous aggregates keeping
# sum/count/min/max, refreshed over the last 35 days (timeseries.read_rollup).
_V = "COALESCE(value, value_bool::int::float8)"
ROLLUP_5M = f"""CREATE MATERIALIZED VIEW IF NOT EXISTS point_history_5m
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT point_uri, time_bucket(INTERVAL '5 minutes', ts) AS bucket,
           sum({_V}) AS vsum, count({_V}) AS vcount, min({_V}) AS vmin, max({_V}) AS vmax
    FROM point_history GROUP BY point_uri, bucket WITH NO DATA"""
ROLLUP_1H = """CREATE MATERIALIZED VIEW IF NOT EXISTS point_history_1h
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT point_uri, time_bucket(INTERVAL '1 hour', bucket) AS bucket,
           sum(vsum) AS vsum, sum(vcount) AS vcount, min(vmin) AS vmin, max(vmax) AS vmax
    FROM point_history_5m GROUP BY point_uri, time_bucket(INTERVAL '1 hour', bucket) WITH NO DATA"""
POLICY_5M = """SELECT add_continuous_aggregate_policy('point_history_5m', if_not_exists => true,
        start_offset => INTERVAL '35 days', end_offset => INTERVAL '5 minutes',
        schedule_interval => INTERVAL '5 minutes')"""
POLICY_1H = """SELECT add_continuous_aggregate_policy('point_history_1h', if_not_exists => true,
        start_offset => INTERVAL '35 days', end_offset => INTERVAL '1 hour',
        schedule_interval => INTERVAL '30 minutes')"""

# Columnar compression (#192), segmented by point and its constant
# unit/label, ordered by time: the reasons are in timeseries.py.
ENABLE_COMPRESSION = """
ALTER TABLE point_history SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'point_uri, unit, label', timescaledb.compress_orderby = 'ts DESC')"""
# compress_after is a setting, not schema (BALER_HISTORY_COMPRESS_AFTER):
# the policy is added at the setting's value and timeseries keeps it in step.
COMPRESSION_POLICY = "SELECT add_compression_policy('point_history', compress_after => %s::interval, if_not_exists => true)"

# One catalog read: which of point_history's pieces are there. (A refresh
# policy's job names its aggregate's view, or on older TimescaleDB its
# materialization hypertable.)
_STATE = """
SELECT to_regclass('point_history') IS NOT NULL,
       EXISTS (SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'point_history'),
       to_regclass('point_history_labelled') IS NOT NULL,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'point_history' AND column_name = 'quality'),
       to_regclass('point_history_5m') IS NOT NULL,
       to_regclass('point_history_1h') IS NOT NULL,
       EXISTS (SELECT 1 FROM timescaledb_information.jobs j JOIN timescaledb_information.continuous_aggregates c
               ON j.hypertable_name IN (c.view_name, c.materialization_hypertable_name)
               WHERE j.proc_name = 'policy_refresh_continuous_aggregate' AND c.view_name = 'point_history_5m'),
       EXISTS (SELECT 1 FROM timescaledb_information.jobs j JOIN timescaledb_information.continuous_aggregates c
               ON j.hypertable_name IN (c.view_name, c.materialization_hypertable_name)
               WHERE j.proc_name = 'policy_refresh_continuous_aggregate' AND c.view_name = 'point_history_1h'),
       COALESCE((SELECT compression_enabled FROM timescaledb_information.hypertables
                 WHERE hypertable_name = 'point_history'), false),
       EXISTS (SELECT 1 FROM timescaledb_information.jobs
               WHERE proc_name = 'policy_compression' AND hypertable_name = 'point_history')
"""


def _history_steps(conn) -> list[Step]:
    if not conn.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb')").fetchone()[0]:
        raise RuntimeError("point_history needs the TimescaleDB extension in this database "
                           "(the timescale/timescaledb image creates it)")
    (table, hyper, labels, quality, v5m, v1h, p5m, p1h, compressed, policy) = conn.execute(_STATE).fetchone()
    from baler.timeseries import COMPRESS_AFTER

    wanted = [
        (table, POINT_HISTORY, "point_history"),
        (hyper, HYPERTABLE, "point_history as a hypertable (converts rows already there)"),
        (labels, LABELS_INDEX, "index point_history_labelled"),
        (quality, QUALITY_COLUMN, "point_history.quality (#183)"),
        (v5m, ROLLUP_5M, "continuous aggregate point_history_5m"),
        (v1h, ROLLUP_1H, "continuous aggregate point_history_1h"),
        (p5m, POLICY_5M, "refresh policy for point_history_5m"),
        (p1h, POLICY_1H, "refresh policy for point_history_1h"),
        (compressed, ENABLE_COMPRESSION, "columnstore compression on point_history (#192)"),
    ]
    steps = [Step(stmt, note) for present, stmt, note in wanted if not present]
    if not policy:
        steps.append(Step(COMPRESSION_POLICY, f"compression policy, compress_after {COMPRESS_AFTER}", (COMPRESS_AFTER,)))
    return steps


# --- every other table, in the order the services created them --------------

TABLES: tuple[tuple[str, str], ...] = (
    ("baler.auth", """
CREATE TABLE IF NOT EXISTS orgs (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    id         TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL REFERENCES orgs(id),
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,   -- admin | operator | viewer
    created_at    TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS user_sites (
    user_id TEXT NOT NULL REFERENCES users(id),
    site_id TEXT NOT NULL REFERENCES sites(id),
    PRIMARY KEY (user_id, site_id)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            TEXT PRIMARY KEY,
    org_id        TEXT NOT NULL REFERENCES orgs(id),
    site_ids      TEXT[],          -- NULL/empty = whole org
    role          TEXT NOT NULL,   -- admin | operator | viewer | service
    jti           TEXT NOT NULL UNIQUE,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL,
    last_used_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti        TEXT PRIMARY KEY,
    revoked_at TIMESTAMPTZ NOT NULL
);
-- Set for revoked *session* tokens (Haystack close), so the denylist can
-- drop them once they'd have expired anyway. NULL = keep (API keys).
ALTER TABLE revoked_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- Set while a user is disabled (Baler#17): they can't log in, by password
-- or Haystack SCRAM, until an admin enables them again.
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
-- Baler#121: how the person signs ("Jacob Nash"), e.g. on a Cx waiver;
-- NULL when unset (the email stands in).
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT;
"""),
    ("baler.scope", """
ALTER TABLE sites ADD COLUMN IF NOT EXISTS topic_prefixes TEXT[] NOT NULL DEFAULT '{}'
"""),
    ("baler.faults", """
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
-- GET /faults' order (newest first), which it pages through.
CREATE INDEX IF NOT EXISTS faults_started ON faults (started_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS webhook_health (
    webhook_id            TEXT PRIMARY KEY,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    disabled              BOOLEAN NOT NULL DEFAULT FALSE,
    last_error            TEXT
);
"""),
    ("baler.haystack_pull_state", """
CREATE TABLE IF NOT EXISTS haystack_pull_checkpoint (
    point_uri      TEXT NOT NULL PRIMARY KEY,
    last_pulled_ts DOUBLE PRECISION NOT NULL
);
"""),
    ("baler.sql_pull_state", """
CREATE TABLE IF NOT EXISTS sql_pull_checkpoint (
    source_id      TEXT NOT NULL PRIMARY KEY,
    last_pulled_ts DOUBLE PRECISION NOT NULL
);
"""),
    ("baler.sources", """
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
"""),
    ("baler.broker_accounts", """
CREATE TABLE IF NOT EXISTS broker_accounts (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    site_id     TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    username    TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    rotated_at  TIMESTAMPTZ
);
"""),
    ("baler.conventions", """
CREATE TABLE IF NOT EXISTS naming_conventions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    scope_prefix TEXT NOT NULL,          -- the topic prefix it applies under ('' = everywhere visible)
    templates    JSONB NOT NULL,         -- annotation strings, tried in order
    point_codes  JSONB NOT NULL,         -- {code: Brick class}
    equip_codes  JSONB NOT NULL,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
"""),
    ("baler.derivation_health", """
CREATE TABLE IF NOT EXISTS derivation_target_health (
    derivation_id         TEXT NOT NULL,
    target_uri            TEXT NOT NULL,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    disabled              BOOLEAN NOT NULL DEFAULT FALSE,
    last_error            TEXT,
    PRIMARY KEY (derivation_id, target_uri)
);
"""),
    ("baler.point_write", """
CREATE TABLE IF NOT EXISTS point_write_levels (
    point_uri  TEXT NOT NULL,
    level      SMALLINT NOT NULL,
    value_json TEXT NOT NULL,
    who        TEXT NOT NULL,
    user_sub   TEXT,
    written_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (point_uri, level)
);
-- Level 17 is Haystack's default (BACnet relinquish-default); the original
-- table capped levels at 16. expires_at: when a timed level-8 override ends.
ALTER TABLE point_write_levels DROP CONSTRAINT IF EXISTS point_write_levels_level_check;
ALTER TABLE point_write_levels ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
"""),
    ("baler.memory", """
CREATE TABLE IF NOT EXISTS mapping_examples (
    id          BIGSERIAL PRIMARY KEY,
    org_id      TEXT,                 -- NULL on a single-tenant install (no site prefixes)
    site_id     TEXT,
    entity_uri  TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- point | equip
    tokens      TEXT[] NOT NULL,
    evidence    JSONB NOT NULL,
    brick_class TEXT NOT NULL,
    source      TEXT NOT NULL,        -- review (a person in the queue)
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS mapping_examples_org ON mapping_examples (org_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_examples_entity ON mapping_examples (entity_uri, source);
"""),
    ("baler.quality", """
CREATE TABLE IF NOT EXISTS quality_suggestions (
    id         BIGSERIAL PRIMARY KEY,
    point_uri  TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- unit
    current    TEXT,
    suggested  TEXT NOT NULL,
    evidence   JSONB NOT NULL,
    status     TEXT NOT NULL,            -- open | accepted | dismissed
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS quality_suggestions_open ON quality_suggestions (point_uri, kind) WHERE status = 'open';
"""),
    ("baler.heartbeat", """
CREATE TABLE IF NOT EXISTS service_heartbeats (
    service TEXT PRIMARY KEY,
    beat_at TIMESTAMPTZ NOT NULL,
    detail  JSONB
);
"""),
    ("baler.record_edit", """
CREATE TABLE IF NOT EXISTS record_edits (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    entity_uri TEXT NOT NULL,
    action     TEXT NOT NULL,      -- edit | delete
    user_sub   TEXT,
    user_email TEXT,
    detail     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS record_edits_entity ON record_edits (entity_uri, ts DESC);
"""),
    ("baler.commissioning.db", """
CREATE TABLE IF NOT EXISTS cx_projects (
    id          TEXT PRIMARY KEY,
    org_id      TEXT NOT NULL REFERENCES orgs(id),
    site_id     TEXT REFERENCES sites(id),   -- NULL: org-wide project
    name        TEXT NOT NULL,
    settings    JSONB NOT NULL DEFAULT '{}',
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cx_projects_org ON cx_projects (org_id);

CREATE TABLE IF NOT EXISTS cx_phases (
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    key          TEXT NOT NULL,
    name         TEXT NOT NULL,
    position     INT NOT NULL,
    status       TEXT NOT NULL,             -- pending | active | complete | skipped
    started_at   TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (project_id, key)
);

-- Every human and agent action on a project: the audit trail, same shape
-- as record_edits.
CREATE TABLE IF NOT EXISTS cx_events (
    id          BIGSERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    ts          TIMESTAMPTZ NOT NULL,
    action      TEXT NOT NULL,
    user_sub    TEXT,
    user_email  TEXT,
    detail      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_events_project ON cx_events (project_id, ts DESC);

CREATE TABLE IF NOT EXISTS cx_runs (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    phase        TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued | running | completed | failed
    started_at   TIMESTAMPTZ NOT NULL,      -- when it was asked for; the window ends here
    finished_at  TIMESTAMPTZ,               -- NULL until completed or failed
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    triggered_by TEXT,
    scope        JSONB NOT NULL,            -- what was in scope, so a run is reproducible
    summary      JSONB,                     -- NULL until completed
    progress     JSONB,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS cx_runs_project ON cx_runs (project_id, started_at DESC);
-- Runs became asynchronous (#57). Rows from before then were only ever
-- written finished, so they backfill as completed.
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE cx_runs ALTER COLUMN status SET DEFAULT 'queued';
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS progress JSONB;
ALTER TABLE cx_runs ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE cx_runs ALTER COLUMN finished_at DROP NOT NULL;
ALTER TABLE cx_runs ALTER COLUMN summary DROP NOT NULL;
-- One run in flight per project.
CREATE UNIQUE INDEX IF NOT EXISTS cx_runs_one_active ON cx_runs (project_id) WHERE status IN ('queued', 'running');

-- Immutable once written: later slices (issues, re-tests) cite results as evidence.
CREATE TABLE IF NOT EXISTS cx_results (
    run_id      TEXT NOT NULL REFERENCES cx_runs(id),
    equip_uri   TEXT NOT NULL,
    check_id    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    status      TEXT NOT NULL,              -- pass | fail | inconclusive | not_applicable
    severity    TEXT,
    symptom     TEXT,
    candidates  JSONB NOT NULL DEFAULT '[]',
    evidence    JSONB NOT NULL,
    PRIMARY KEY (run_id, equip_uri, check_id, subject)
);
CREATE INDEX IF NOT EXISTS cx_results_equip ON cx_results (run_id, equip_uri);

-- Cx 4 (#40): the issues log. A check-linked issue closes only on
-- evidence (commissioning.issues); manual and imported ones by hand.
CREATE TABLE IF NOT EXISTS cx_issues (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES cx_projects(id),
    equip_uri     TEXT,                     -- real urn:equip: URI, NULL if unmatched
    check_id      TEXT,                     -- NULL: manual or imported
    subject       TEXT,
    status        TEXT NOT NULL,            -- open | in_progress | ready_for_retest | closed | risk_accepted
    title         TEXT NOT NULL,
    severity      TEXT,
    assignee      TEXT,
    detail        JSONB NOT NULL DEFAULT '{}',
    source        TEXT NOT NULL DEFAULT 'baler',  -- the external system an imported item came from
    source_id     TEXT,
    opened_run    TEXT,
    last_seen_run TEXT,
    closed_run    TEXT,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cx_issues_project ON cx_issues (project_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS cx_issues_one_active
    ON cx_issues (project_id, check_id, subject) WHERE status <> 'closed' AND check_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS cx_issues_source
    ON cx_issues (project_id, source, source_id) WHERE source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cx_issue_events (
    id          BIGSERIAL PRIMARY KEY,
    issue_id    TEXT NOT NULL REFERENCES cx_issues(id),
    ts          TIMESTAMPTZ NOT NULL,
    action      TEXT NOT NULL,
    user_sub    TEXT,
    user_email  TEXT,
    detail      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_issue_events_issue ON cx_issue_events (issue_id, ts DESC);
-- Baler#86: evidence resolves (ready_for_retest), a person closes citing a
-- retest judged over a window that starts after resolved_at.
ALTER TABLE cx_issues ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE cx_issues ADD COLUMN IF NOT EXISTS resolved_by JSONB;
ALTER TABLE cx_issues ADD COLUMN IF NOT EXISTS retest JSONB;

-- Accepted risks: not faults, not closed issues. One standing risk per issue.
CREATE TABLE IF NOT EXISTS cx_risks (
    id             TEXT PRIMARY KEY,
    issue_id       TEXT NOT NULL REFERENCES cx_issues(id),
    accepted_by    TEXT,
    accepted_email TEXT,
    reason         TEXT NOT NULL,
    review_by      DATE NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    revoked_at     TIMESTAMPTZ,
    revoked_by     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cx_risks_one_standing ON cx_risks (issue_id) WHERE revoked_at IS NULL;
-- Baler#99: a waiver names who carries it (owner, not necessarily who
-- accepted it) and what retest lifts it, as fields rather than prose.
ALTER TABLE cx_risks ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE cx_risks ADD COLUMN IF NOT EXISTS retest_trigger TEXT;
ALTER TABLE cx_risks ADD COLUMN IF NOT EXISTS accepted_name TEXT;
-- Baler#121: "account" when accepted_name is the accepter's user-record
-- display name, "typed" when typed in the form; NULL on older waivers
-- (read as typed - never labelled the CxA).
ALTER TABLE cx_risks ADD COLUMN IF NOT EXISTS accepted_name_source TEXT;

-- Cx 6 (#42): start-up / power-on / functional test procedures as data,
-- and their passive test runs (commissioning.procedures).
CREATE TABLE IF NOT EXISTS cx_test_procedures (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- power_on | startup | functional | seasonal
    equip_class  TEXT NOT NULL,             -- Brick class, subclass-aware
    equip_uris   JSONB,                     -- optional explicit equipment list
    description  TEXT NOT NULL DEFAULT '',
    steps        JSONB NOT NULL,
    window_hours DOUBLE PRECISION NOT NULL,
    schedule     JSONB,                     -- {every_hours} or NULL (on demand only)
    status       TEXT NOT NULL,             -- draft | active | archived
    template     TEXT,                      -- seed it came from
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_test_procedures_project ON cx_test_procedures (project_id);

CREATE TABLE IF NOT EXISTS cx_test_runs (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    procedure_id TEXT NOT NULL REFERENCES cx_test_procedures(id),
    phase        TEXT,
    status       TEXT NOT NULL,             -- queued | running | completed | failed
    started_at   TIMESTAMPTZ NOT NULL,      -- "powered on / started at": the window starts here
    window_end   TIMESTAMPTZ NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    triggered_by TEXT,                      -- user sub, or 'schedule'
    scope        JSONB NOT NULL,
    procedure    JSONB NOT NULL,            -- the procedure as it was run
    equipment    JSONB,                     -- per equipment, per step (procedures.evaluate)
    summary      JSONB,
    previous_id  TEXT,
    diff         JSONB,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS cx_test_runs_procedure ON cx_test_runs (procedure_id, requested_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS cx_test_runs_one_active ON cx_test_runs (procedure_id) WHERE status IN ('queued', 'running');

-- Cx 7 (#43): spec documents (the file itself is never stored), spec
-- items (proposed by extraction or imported), and spec<->device mappings.
CREATE TABLE IF NOT EXISTS cx_spec_documents (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    filename    TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        INT NOT NULL,
    sha256      TEXT NOT NULL,
    method      TEXT NOT NULL,              -- llm | table
    status      TEXT NOT NULL,              -- queued | running | completed | failed
    items       INT NOT NULL DEFAULT 0,
    info        JSONB,                      -- model, notes, token usage
    error       TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cx_spec_documents_project ON cx_spec_documents (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cx_spec_items (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    document_id TEXT,
    kind        TEXT NOT NULL,              -- equipment | point | sequence
    tag         TEXT NOT NULL,
    name        TEXT NOT NULL,
    equip_tag   TEXT,
    brick_class TEXT,
    properties  JSONB NOT NULL DEFAULT '{}',
    source      JSONB NOT NULL,             -- document, page, quote/row, method
    confidence  DOUBLE PRECISION,
    status      TEXT NOT NULL,              -- proposed | accepted | rejected
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cx_spec_items_project ON cx_spec_items (project_id, kind, status);

CREATE TABLE IF NOT EXISTS cx_mappings (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES cx_projects(id),
    spec_item_id TEXT NOT NULL REFERENCES cx_spec_items(id),
    equip_uri    TEXT,
    confidence   TEXT NOT NULL,             -- confirmed | high | medium | low | unmatched
    basis        JSONB NOT NULL,
    alternatives JSONB NOT NULL,
    status       TEXT NOT NULL,             -- proposed | confirmed | rejected
    decided_by   TEXT,
    decided_at   TIMESTAMPTZ,
    written_back JSONB,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_mappings_project ON cx_mappings (project_id, status);

-- Cx 9 (#45): active tests through point_write (commissioning.active).
CREATE TABLE IF NOT EXISTS cx_active_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES cx_projects(id),
    equip_uri       TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- command_response | custom
    steps           JSONB NOT NULL,
    plan            JSONB NOT NULL,         -- the dry run: every write
    status          TEXT NOT NULL,          -- planned | running | completed | failed | aborted | expired | cancelled
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    confirmed_by    TEXT,
    confirmed_at    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    before          JSONB,                  -- latest values and priority arrays before the first write
    writes          JSONB NOT NULL DEFAULT '[]',  -- every write and relinquish, in order
    results         JSONB NOT NULL DEFAULT '[]',
    error           TEXT,
    abort_requested BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS cx_active_runs_project ON cx_active_runs (project_id, created_at DESC);

-- Cx 10 (#46): the commissioning agent's sessions (the API transcript,
-- replayed unchanged) and its proposals, which a person accepts.
CREATE TABLE IF NOT EXISTS cx_agent_sessions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    created_by  TEXT,
    claims      JSONB NOT NULL,             -- org, sites, role of the starter: the agent's scope
    status      TEXT NOT NULL,              -- idle | working | failed
    messages    JSONB NOT NULL DEFAULT '[]',
    last_reply  TEXT,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_agent_sessions_project ON cx_agent_sessions (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cx_agent_proposals (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES cx_projects(id),
    session_id  TEXT,
    kind        TEXT NOT NULL,              -- issue | procedure | phase | spec_item | mapping | active_test
    payload     JSONB NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,              -- proposed | accepted | rejected | failed
    result      JSONB,
    decided_by  TEXT,
    decided_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS cx_agent_proposals_project ON cx_agent_proposals (project_id, status);
"""),
    ("baler.commissioning.db (#72: widen cx_results' key)", """
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conname = 'cx_results_pkey'
                 AND pg_get_constraintdef(oid) = 'PRIMARY KEY (run_id, check_id, subject)') THEN
        ALTER TABLE cx_results DROP CONSTRAINT cx_results_pkey;
        ALTER TABLE cx_results ADD PRIMARY KEY (run_id, equip_uri, check_id, subject);
    END IF;
END $$;
"""),
    ("baler.mapping_proposals", """
CREATE TABLE IF NOT EXISTS mapping_proposals (
    id              TEXT PRIMARY KEY,
    entity_uri      TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    candidates      JSONB NOT NULL,       -- [{brick_class, reason, source}], best first
    refused_because TEXT NOT NULL,        -- why the ladder didn't auto-accept
    evidence        JSONB NOT NULL,
    asks            JSONB NOT NULL,       -- each model ask: model, variant, candidates, confidence, rejected
    status          TEXT NOT NULL,        -- proposed | escalated | accepted | site_visit | auto-accepted
    decided_class   TEXT,
    decided_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS mapping_proposals_open ON mapping_proposals (entity_uri) WHERE status = 'proposed';
CREATE INDEX IF NOT EXISTS mapping_proposals_pattern ON mapping_proposals (status, pattern);
-- The review queue (#184): who a proposal is escalated to, and the
-- reviewer's note. Added, not in CREATE TABLE, so a table #182 made gets them.
ALTER TABLE mapping_proposals ADD COLUMN IF NOT EXISTS assignee TEXT;
ALTER TABLE mapping_proposals ADD COLUMN IF NOT EXISTS note TEXT;

-- A pattern a person approved as a whole ("41 points like VAV-{n}-DA-T are
-- Discharge_Air_Temperature_Sensor"): the model step applies it to every
-- later entity with that pattern, without asking a model.
CREATE TABLE IF NOT EXISTS mapping_site_rules (
    pattern     TEXT PRIMARY KEY,
    brick_class TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL,
    proposal_id TEXT
);
"""),
    ("baler.equip_inference", """
CREATE TABLE IF NOT EXISTS relationship_proposals (
    id         TEXT PRIMARY KEY,
    subject    TEXT NOT NULL,            -- equip (feeds) or the proposed equip (isPointOf)
    predicate  TEXT NOT NULL,            -- hasPoint | feeds
    objects    JSONB NOT NULL,           -- the points (hasPoint) or the fed equip (feeds)
    evidence   JSONB NOT NULL,
    status     TEXT NOT NULL,            -- proposed | accepted | rejected
    decided_by TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS relationship_proposals_open ON relationship_proposals (subject, predicate) WHERE status = 'proposed';
"""),
)


def adopts(conn) -> bool:
    """True when the database already has Baler tables: this baseline is
    then adopting an install made by the pre-migration setup code."""
    return bool(conn.execute(
        "SELECT to_regclass('point_history') IS NOT NULL OR to_regclass('orgs') IS NOT NULL").fetchone()[0])


def plan(conn) -> list[Step]:
    return _history_steps(conn) + [Step(stmt, f"{who} (idempotent)") for who, stmt in TABLES]
