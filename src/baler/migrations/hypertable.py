"""
For migrations that change a hypertable with compressed chunks.

What TimescaleDB 2.31 (the version our pinned image runs) allows directly
on a hypertable whose chunks are compressed, measured, not assumed (see
docs-site/pages/schema-migrations.mdx and tests/test_schema_upgrade.py):

- fine as they are: ADD COLUMN (nullable, or with a default), RENAME
  COLUMN, DROP COLUMN of a column that is neither segmentby nor orderby,
  ALTER COLUMN SET/DROP DEFAULT and SET/DROP NOT NULL, CREATE INDEX, ADD
  CHECK constraint;
- refused: ALTER COLUMN ... TYPE ("operation not supported on hypertables
  with compressed chunks") and DROP COLUMN of a segmentby/orderby column;
- allowed but only for chunks compressed later: a new compress_segmentby
  or compress_orderby. Existing chunks keep the old layout until each is
  recompressed (`compress_chunk(c, recompress => true)`).

For the refused ones, `with_decompressed` wraps the statements: it
decompresses the compressed chunks, runs them, and compresses the same
chunks again, all inside the migration's one transaction, so a failure
leaves the table exactly as it was. Disabling compression is not needed
(and not possible while any chunk is compressed). It needs the
uncompressed size of those chunks free on disk while it runs; `plan` shows
how many chunks and how much. Never a downsample: every row comes back.
"""

from __future__ import annotations

from psycopg import sql

from baler.schema import Step

_COMPRESSED = """
SELECT format('%%I.%%I', c.chunk_schema, c.chunk_name), COALESCE(s.before_compression_total_bytes, 0)
FROM timescaledb_information.chunks c
LEFT JOIN chunk_compression_stats(%s::regclass) s
       ON s.chunk_schema = c.chunk_schema AND s.chunk_name = c.chunk_name
WHERE c.hypertable_schema = 'public' AND c.hypertable_name = %s AND c.is_compressed
ORDER BY c.range_start
"""


def compressed_chunks(conn, table: str) -> list[tuple[str, int]]:
    """(qualified chunk name, uncompressed bytes) of each compressed chunk."""
    return [(name, int(size)) for name, size in conn.execute(_COMPRESSED, (table, table)).fetchall()]


def with_decompressed(conn, table: str, statements: list[str], note: str = "") -> list[Step]:
    """Steps that run `statements` against `table` with its compressed
    chunks decompressed first and compressed again after."""
    chunks = compressed_chunks(conn, table)
    total = sum(size for _, size in chunks)
    steps = [Step(sql.SQL("SELECT decompress_chunk({}::regclass, if_compressed => true)").format(sql.Literal(c)),
                  f"decompress {c}" + (f" ({size / 1e6:.1f} MB uncompressed)" if size else ""))
             for c, size in chunks]
    steps += [Step(s, note or s.strip().splitlines()[0]) for s in statements]
    steps += [Step(sql.SQL("SELECT compress_chunk({}::regclass, if_not_compressed => true)").format(sql.Literal(c)),
                   f"compress {c} again") for c, _ in chunks]
    if chunks:
        steps[0] = Step(steps[0].sql, f"{steps[0].note}; {len(chunks)} chunk(s), {total / 1e6:.1f} MB "
                                      f"uncompressed in all, need that much free disk")
    return steps
