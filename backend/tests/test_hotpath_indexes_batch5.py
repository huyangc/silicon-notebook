"""Unit tests (fake-connection only, no live PG -- G1-tier, same placement
rule as ``test_hotpath_indexes_batch3.py``) for hot-path fix batch 5's three
additions to ``HOTPATH_INDEX_SPECS`` (batch 3 · W1 · PR-3 Phase A):
``idx_agent_tokens_default_notebook``, ``idx_knowhow_cell_code_column`` and
``idx_conversations_notebook`` -- three plain (non-partial, non-GIN) btree
indexes design doc Sec 1.4 registers as prerequisites (not "optimizations")
for the delete-jobization work.

Contract under test:

  1. Anti-drift -- the index definitions live in
     ``migrations/0049_notebook_delete_jobs.sql`` AND in
     ``HOTPATH_INDEX_SPECS``, two independent hand-authored copies (a
     migration file cannot import Python at apply time). This module parses
     the migration file's three ``CREATE INDEX IF NOT EXISTS`` statements
     and cross-checks each against its batch-5 spec.
  2. ``HOTPATH_INDEX_SPECS`` totals seventeen entries (eight batch-1 + two
     batch-2 + one batch-3 + three batch-4 + three batch-5) and batches 1-4
     are untouched by this addition.

(Renumbered from batch 4 when hot-path batch 4's trgm migration 0048 landed
on master first; this batch's migration is 0049. These three plain btree
indexes carry no plan-shape claim needing a live-EXPLAIN twin -- the schema
tests assert their presence after a live migrate.)
"""
from __future__ import annotations

import re
from pathlib import Path

from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT
    / "backend"
    / "app"
    / "repositories"
    / "postgres"
    / "migrations"
    / "0049_notebook_delete_jobs.sql"
)

_BATCH5_NAMES = {
    "idx_agent_tokens_default_notebook",
    "idx_knowhow_cell_code_column",
    "idx_conversations_notebook",
}

# Same shape as test_hotpath_indexes_batch3.py's _STATEMENT_PATTERN: one
# statement, optional "USING <access method>", a parenthesized column list,
# and an optional WHERE predicate. This batch uses neither USING nor WHERE.
# The migration also declares two other CREATE statements (the job/side
# tables) and one CREATE UNIQUE INDEX / one plain CREATE INDEX for
# notebook_delete_jobs -- this pattern only matches "CREATE INDEX IF NOT
# EXISTS" (no UNIQUE, no bare CREATE INDEX without "IF NOT EXISTS"), so it
# naturally finds only the three hot-path-index statements.
_STATEMENT_PATTERN = re.compile(
    r"CREATE INDEX IF NOT EXISTS\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)"
    r"(?:\s+USING\s+(?P<using>\w+))?\s*\(\s*"
    r"(?P<columns>[\s\S]*?)\s*\)"
    r"(?:\s*WHERE\s+(?P<predicate>[\s\S]*?))?;",
    re.MULTILINE,
)


def _migration_text() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _ddl_only(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def _parse_migration_statements() -> list[dict[str, object]]:
    text = _ddl_only(_migration_text())
    out = []
    for match in _STATEMENT_PATTERN.finditer(text):
        out.append(
            {
                "name": match.group("name"),
                "table": match.group("table"),
                "using": (match.group("using") or "").lower(),
                "columns": match.group("columns").strip(),
                "predicate": (match.group("predicate") or "").strip(),
            }
        )
    return out


def test_migration_file_exists_and_declares_exactly_the_three_batch5_statements():
    assert _MIGRATION.is_file()
    parsed = _parse_migration_statements()
    assert {entry["name"] for entry in parsed} == _BATCH5_NAMES, (
        f"expected exactly {_BATCH5_NAMES!r} in {_MIGRATION.name}, "
        f"parsed {[entry['name'] for entry in parsed]}"
    )


def test_batch5_specs_are_present_and_batches_1_to_3_are_untouched():
    names = {spec.name for spec in HOTPATH_INDEX_SPECS}
    assert _BATCH5_NAMES <= names
    assert len(HOTPATH_INDEX_SPECS) == 17, (
        "expected eight batch-1 plus two batch-2 plus one batch-3 plus three "
        f"batch-5 entries in HOTPATH_INDEX_SPECS, found "
        f"{len(HOTPATH_INDEX_SPECS)}: {sorted(names)}"
    )
    batch1_names = {
        "idx_clusters_nb_canonical",
        "idx_clusters_nb_canonical_name_lower",
        "idx_extraction_runs_notebook",
        "idx_knowledge_source_fact_elements_notebook",
        "idx_memory_items_notebook",
        "idx_knowledge_relations_nb_source_target_edge",
        "idx_chunks_source_ordinal",
        "idx_sources_nb_hidden_type",
    }
    batch2_names = {"idx_knowledge_objects_nb_payload_trgm", "idx_source_elements_nonblank"}
    batch3_names = {"idx_clusters_nb_canonical_member"}
    assert batch1_names <= names
    assert batch2_names <= names
    assert batch3_names <= names


def test_migration_statements_match_their_specs_verbatim():
    parsed = {entry["name"]: entry for entry in _parse_migration_statements()}
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}

    expected_tables = {
        "idx_agent_tokens_default_notebook": "agent_access_tokens",
        "idx_knowhow_cell_code_column": "knowhow_cell_code",
        "idx_conversations_notebook": "conversations",
    }
    expected_columns = {
        "idx_agent_tokens_default_notebook": "default_notebook_id",
        "idx_knowhow_cell_code_column": "column_id",
        "idx_conversations_notebook": "notebook_id, id",
    }
    for name in _BATCH5_NAMES:
        entry = parsed[name]
        spec = by_name[name]
        assert entry["table"] == spec.table == expected_tables[name]
        assert entry["using"] == spec.using == ""
        assert entry["columns"] == spec.column_list_sql() == expected_columns[name]
        assert entry["predicate"] == spec.predicate == ""


def test_delete_job_carrier_tables_are_declared():
    """Sanity check (not the anti-drift judge -- there is no Python-side spec
    for table DDL the way there is for indexes): the migration also declares
    the two new job-carrier tables and their supporting indexes."""
    text = _migration_text()
    assert "CREATE TABLE notebook_delete_jobs" in text
    assert "CREATE TABLE notebook_delete_files" in text
    assert (
        "CREATE UNIQUE INDEX idx_notebook_delete_jobs_one_active" in text
    )
    assert "WHERE status IN ('queued', 'running', 'waiting')" in text
