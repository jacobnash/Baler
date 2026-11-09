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
    v0003_requirement_packs,
    v0004_pack_proposals,
)

ALL = (v0001_baseline, v0002_reading_feed, v0003_requirement_packs, v0004_pack_proposals)
