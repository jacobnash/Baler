"""
Numbered schema migrations (#194), applied in order by baler.schema.

One module per migration, `vNNNN_<name>.py`, listed in ALL below (a test
checks every file is listed and the numbers run 1..N with no gaps). Each
module has:

    VERSION: int     its number
    NAME: str        a few words
    plan(conn)       -> list[baler.schema.Step]: what it would run against
                     this database now. Read-only; it may look in the
                     catalog to skip what is already there.

and optionally `adopts(conn) -> bool` (only the baseline has it). A
migration is frozen once released: never edit one, add the next. See
docs-site/pages/schema-migrations.mdx for the rules on hypertables and
compressed chunks (migrations.hypertable has the helper).
"""

from baler.migrations import (
    v0001_baseline,
    v0002_reading_feed,
    v0003_quality_watermark,
    v0004_alerts,
    v0005_password_reset_mfa,
    v0006_override_tests,
    v0007_operator_writes,
    v0008_manual_results,
    v0009_fleet_runs,
    v0010_active_schedules,
    v0011_assets,
    v0012_documents,
    v0013_warranties,
    v0014_pm,
    v0015_deferred,
    v0016_bench,
    v0017_doc_proposals,
    v0018_spares,
    v0019_spaces,
    v0020_requirement_packs,
    v0021_pack_proposals,
    v0022_compliance_log,
    v0023_performance_baselines,
    v0024_savings_cases,
    v0025_schedules,
    v0026_schedule_evidence,
    v0027_history_compression,
    v0028_pack_extraction_rights,
)

ALL = (
    v0001_baseline,
    v0002_reading_feed,
    v0003_quality_watermark,
    v0004_alerts,
    v0005_password_reset_mfa,
    v0006_override_tests,
    v0007_operator_writes,
    v0008_manual_results,
    v0009_fleet_runs,
    v0010_active_schedules,
    v0011_assets,
    v0012_documents,
    v0013_warranties,
    v0014_pm,
    v0015_deferred,
    v0016_bench,
    v0017_doc_proposals,
    v0018_spares,
    v0019_spaces,
    v0020_requirement_packs,
    v0021_pack_proposals,
    v0022_compliance_log,
    v0023_performance_baselines,
    v0024_savings_cases,
    v0025_schedules,
    v0026_schedule_evidence,
    v0027_history_compression,
    v0028_pack_extraction_rights,
)
