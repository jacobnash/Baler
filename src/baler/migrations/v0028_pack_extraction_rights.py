"""
0028 pack extraction rights confirmation: every document sent for
requirement extraction carries the uploader's confirmation that they hold
the rights to it and may process it with AI tools. The extraction records
who confirmed and when, next to the document's sha256.

- cx_pack_extractions.rights_confirmed_by / rights_confirmed_at. Nullable:
  extractions made before this migration have no confirmation recorded.
"""

from __future__ import annotations

from baler.schema import Step

VERSION = 28
NAME = "pack extraction rights confirmation"

RIGHTS = """
ALTER TABLE cx_pack_extractions
    ADD COLUMN IF NOT EXISTS rights_confirmed_by TEXT,
    ADD COLUMN IF NOT EXISTS rights_confirmed_at TIMESTAMPTZ"""


def plan(conn) -> list[Step]:
    return [Step(RIGHTS, "cx_pack_extractions: who confirmed the rights to the document, and when")]
