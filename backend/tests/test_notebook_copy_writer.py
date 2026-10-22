"""``SnapshotRowSource``: the deep copy's in-memory row source gives every table as
exactly one batch, the snapshot's own list.

That one-batch-per-table shape is what keeps ``NotebookCopyWriter`` at one
``insert_copy_rows`` per table, the frozen write sequence the copy characterization
tests pin; the batch being the snapshot's own list (not a copy) is what keeps the
writer's in-place remaps landing in the snapshot's dicts.
"""
from __future__ import annotations

import pytest

from app.services.notebook_copy_writer import SnapshotRowSource


def test_each_table_is_exactly_one_batch_and_that_batch_is_the_snapshot_list():
    sources = [{"id": "src-1"}, {"id": "src-2"}]
    chunks = [{"id": "chk-1"}]
    snapshot = {"sources": sources, "chunks": chunks}
    rows = SnapshotRowSource(snapshot)

    for table, expected in snapshot.items():
        batches = list(rows.batches(table))
        assert len(batches) == 1
        assert batches[0] is expected


def test_an_empty_table_is_one_empty_batch():
    """Not zero batches: the writer still issues the table's ``insert_copy_rows``."""
    empty: list[dict] = []
    rows = SnapshotRowSource({"concept_clusters": empty})

    batches = list(rows.batches("concept_clusters"))

    assert batches == [[]]
    assert batches[0] is empty


def test_a_table_can_be_asked_for_again_and_gives_the_same_batch():
    """Some phases read a table twice (the generation pre-scan of the fact tables)."""
    facts = [{"id": "fact-1"}]
    rows = SnapshotRowSource({"knowledge_source_facts": facts})

    first = list(rows.batches("knowledge_source_facts"))
    second = list(rows.batches("knowledge_source_facts"))

    assert len(first) == len(second) == 1
    assert first[0] is second[0] is facts


def test_a_table_missing_from_the_snapshot_raises_key_error_when_asked_for():
    rows = SnapshotRowSource({"sources": []})

    with pytest.raises(KeyError, match="chunks"):
        rows.batches("chunks")
