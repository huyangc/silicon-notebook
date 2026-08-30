"""Unit tests (fake-connection only, no live PG -- G1-tier, same placement rule
as ``test_hotpath_indexes.py`` / ``test_hotpath_indexes_batch2.py``) for hot-path
fix batch 3's one addition to ``HOTPATH_INDEX_SPECS``:
``idx_clusters_nb_canonical_member``, a plain (non-partial, non-GIN)
composite btree keyset-covering index on
``concept_clusters(notebook_id, canonical_id, member_object_id)``.

Contract under test:

  1. Anti-drift -- the index definition lives in
     ``migrations/0043_concept_cluster_keyset_index.sql`` AND in
     ``HOTPATH_INDEX_SPECS``, two independent hand-authored copies (a
     migration file cannot import Python at apply time). This module parses
     the migration file and cross-checks it against the batch-3 spec,
     reusing ``test_hotpath_indexes_batch2.py``'s statement regex (it
     already tolerates an absent ``USING``/``WHERE`` clause, which is this
     batch's whole shape).
  2. ``HOTPATH_INDEX_SPECS`` totals eleven entries (eight batch-1 + two
     batch-2 + one batch-3) and batch 1/2 are untouched by this addition.
  3. The new spec's key columns stay byte-identical to
     ``knowledge_store.py``'s ``concept_cluster_detail_rows``/
     ``concept_cluster_member_total`` query text on both backends (the
     ``notebook_id, canonical_id`` equality prefix plus the
     ``member_object_id`` keyset/sort column).

See ``backend/tests/postgres/test_hotpath_indexes_batch3_live.py`` for the
live-PostgreSQL half (real catalog rendering, real EXPLAIN plan proof) a fake
connection cannot exercise.
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
    / "0043_concept_cluster_keyset_index.sql"
)

_BATCH3_NAME = "idx_clusters_nb_canonical_member"

# Same shape as test_hotpath_indexes_batch2.py's _STATEMENT_PATTERN: one
# statement, optional "USING <access method>", a parenthesized column list,
# and an optional WHERE predicate. This batch uses neither USING nor WHERE,
# but the DO block above the real DDL contains no literal "CREATE INDEX"
# text (it only names the index via a VALUES row and PL/pgSQL variables), so
# this regex still finds exactly one real statement.
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


def test_migration_file_exists_and_declares_exactly_one_statement():
    assert _MIGRATION.is_file()
    parsed = _parse_migration_statements()
    assert {entry["name"] for entry in parsed} == {_BATCH3_NAME}, (
        f"expected exactly {{{_BATCH3_NAME!r}}} in {_MIGRATION.name}, "
        f"parsed {[entry['name'] for entry in parsed]}"
    )


def test_batch3_spec_is_present_and_batch1_batch2_are_untouched():
    names = {spec.name for spec in HOTPATH_INDEX_SPECS}
    assert _BATCH3_NAME in names
    assert len(HOTPATH_INDEX_SPECS) == 11, (
        "expected eight batch-1 plus two batch-2 plus one batch-3 entry in "
        f"HOTPATH_INDEX_SPECS, found {len(HOTPATH_INDEX_SPECS)}: {sorted(names)}"
    )
    # Batch 1/2 names untouched by this addition.
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
    assert batch1_names <= names
    assert batch2_names <= names


def test_migration_statement_matches_its_spec_verbatim():
    parsed = {entry["name"]: entry for entry in _parse_migration_statements()}
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}

    entry = parsed[_BATCH3_NAME]
    spec = by_name[_BATCH3_NAME]
    assert entry["table"] == spec.table == "concept_clusters"
    assert entry["using"] == spec.using == ""
    assert entry["columns"] == spec.column_list_sql() == "notebook_id, canonical_id, member_object_id"
    assert entry["predicate"] == spec.predicate == ""


def test_spec_key_columns_match_concept_cluster_detail_rows_query_text():
    """The index's key list must stay byte-identical to the WHERE-prefix
    equality plus ORDER BY/keyset column both backends' concept_cluster_
    detail_rows implementations use -- otherwise the index cannot serve the
    query it exists for."""
    from app.repositories.postgres import knowledge_store as pg_knowledge_store
    from app.repositories.sqlite import knowledge_store as sqlite_knowledge_store

    spec = next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == _BATCH3_NAME)
    assert spec.columns == ("notebook_id", "canonical_id", "member_object_id")

    pg_source = Path(pg_knowledge_store.__file__).read_text(encoding="utf-8")
    assert "cc.notebook_id=%s AND cc.canonical_id=%s" in pg_source
    assert 'ORDER BY cc.member_object_id COLLATE "C"' in pg_source

    sqlite_source = Path(sqlite_knowledge_store.__file__).read_text(encoding="utf-8")
    assert "cc.notebook_id=? AND cc.canonical_id=?" in sqlite_source
    assert "ORDER BY cc.member_object_id" in sqlite_source
