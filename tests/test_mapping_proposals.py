"""
Mapping proposals (#182): the rows the review queue reads. The memory
store is what the eval harness uses; the Postgres one is what autotag
writes, checked against a live database.
"""

import uuid

import pytest

from baler import mapping_proposals


def _propose(store, uri, why="asked twice", cands=("Fan_Status",)):
    store.propose(entity_uri=uri, pattern="point|fbf/ef-{n}/FAN-STA-{n}||", refused_because=why,
                  candidates=[{"brick_class": c, "reason": "r", "source": "model"} for c in cands],
                  evidence={"label": uri}, asks=[])


def _exercise(store, uri):
    _propose(store, uri)
    _propose(store, uri, why="no model configured", cands=("Fan_Status", "Fan_Command"))
    rows = [r for r in store.list() if r["entity_uri"] == uri]
    assert len(rows) == 1 and rows[0]["refused_because"] == "no model configured"
    assert [c["brick_class"] for c in rows[0]["candidates"]] == ["Fan_Status", "Fan_Command"]

    store.resolve(uri, "accepted", "Fan_Status", by="someone@example.com")
    assert not [r for r in store.list() if r["entity_uri"] == uri]
    assert [r["decided_class"] for r in store.list("accepted") if r["entity_uri"] == uri] == ["Fan_Status"]

    _propose(store, uri)  # a later sweep can open a fresh one; the decided row stays
    assert len([r for r in store.list() if r["entity_uri"] == uri]) == 1
    assert len([r for r in store.list("accepted") if r["entity_uri"] == uri]) == 1


def test_memory_proposals_keep_one_open_row_per_entity():
    _exercise(mapping_proposals.MemoryProposals(), "urn:point:fbf/ef-1/FAN-STA-3")


@pytest.mark.integration
def test_pg_proposals_keep_one_open_row_per_entity():
    from baler import timeseries

    conn = timeseries.connect()
    try:
        store = mapping_proposals.PgProposals(conn)
        store.ensure_schema()
        uri = f"urn:point:test-proposals/{uuid.uuid4().hex}"
        try:
            _exercise(store, uri)
        finally:
            conn.execute("DELETE FROM mapping_proposals WHERE entity_uri = %s", (uri,))
    finally:
        conn.close()
