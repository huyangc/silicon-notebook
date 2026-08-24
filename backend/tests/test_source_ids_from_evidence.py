"""Pure-function unit tests for KnowledgeStore.source_ids_from_evidence
(SQLite adapter) — perf-audit 双评审修复批 item C4.

`source_ids_from_evidence` moved from a blanket
`except (json.JSONDecodeError, TypeError)` to an explicit type whitelist:
list is a fast path, str/bytes/None go through json.loads (only a malformed
JSON *string* is swallowed into "no source ids"), and anything else is a
caller bug that must raise loudly. This file is new — none of the touched
files in this batch (`backend/tests/kg/test_relink.py`) are a natural home for a knowledge_store-level
pure-function test, so a small dedicated module was added instead of bolting
an unrelated test onto the relink test files.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.sqlite.knowledge_store import KnowledgeStore


def test_list_shape_is_the_fast_path_and_skips_json_parsing():
    evidence = [{"source_id": "s1"}, {"source_id": "s2"}, {"source_id": "s1"}]
    assert KnowledgeStore.source_ids_from_evidence(evidence) == {"s1", "s2"}


def test_str_shape_parses_json():
    evidence_json = json.dumps([{"source_id": "s1"}, {"source_id": "s2"}])
    assert KnowledgeStore.source_ids_from_evidence(evidence_json) == {"s1", "s2"}


def test_none_and_empty_string_yield_empty_set():
    assert KnowledgeStore.source_ids_from_evidence(None) == set()
    assert KnowledgeStore.source_ids_from_evidence("") == set()


def test_malformed_json_string_yields_empty_set_not_a_raise():
    # A malformed JSON *string* is a real "no evidence"-ish shape existing
    # callers tolerate (e.g. a corrupt legacy row) — still swallowed.
    assert KnowledgeStore.source_ids_from_evidence("{not json") == set()


def test_items_missing_source_id_or_non_dict_items_are_ignored():
    evidence = [{"source_id": "s1"}, {"no_source_id": True}, "not-a-dict", 42]
    assert KnowledgeStore.source_ids_from_evidence(evidence) == {"s1"}


def test_tuple_input_raises_type_error():
    # A tuple is neither the fast-path list shape nor a serialized str/bytes/
    # None — it must raise loudly (caller bug), not be silently swallowed
    # into an empty set the way the old blanket `except (..., TypeError)`
    # did. This is the guard C4 exists to pin: reverting the explicit
    # whitelist back to a bare `except (json.JSONDecodeError, TypeError)`
    # must turn this red.
    with pytest.raises(TypeError):
        KnowledgeStore.source_ids_from_evidence(({"source_id": "s1"},))


def test_int_input_raises_type_error():
    with pytest.raises(TypeError):
        KnowledgeStore.source_ids_from_evidence(42)  # type: ignore[arg-type]
