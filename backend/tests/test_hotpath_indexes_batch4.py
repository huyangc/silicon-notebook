"""Unit tests (fake-connection only, no live PG -- G1-tier, same placement rule
as ``test_hotpath_indexes.py`` / ``test_hotpath_indexes_batch2.py`` /
``test_hotpath_indexes_batch3.py``) for hot-path fix batch 4's three additions
to ``HOTPATH_INDEX_SPECS``:

  1. ``idx_sources_nb_title_file_trgm`` -- a notebook-scoped composite PARTIAL
     GIN with TWO trigram keys (``lower(title)`` and ``lower(file_name)``), the
     first spec in this module to carry more than one expression key. The two
     keys exist so the query's ``LIKE … OR LIKE …`` can BitmapOr two scans of
     one index; see ``test_hotpath_indexes_batch4_live.py`` for the EXPLAIN
     proof that the planner actually does that.
  2. ``idx_source_authors_nb_name_trgm``
  3. ``idx_source_paper_meta_nb_ptitle_trgm``

Contract under test:

  1. Anti-drift -- the three index definitions live in
     ``migrations/0048_source_search_trgm_indexes.sql`` AND in
     ``HOTPATH_INDEX_SPECS``, two independent hand-authored copies (a
     migration file cannot import Python at apply time). This module parses
     the migration file and cross-checks it statement-by-statement against
     the three batch-4 specs, reusing batch 2/3's statement regex.
  2. VISIBLE_SOURCE_TYPES_PREDICATE reconciliation -- the easiest way to get
     this batch wrong. The sources index's partial predicate, the query's own
     visible filter in ``list_sources_page``, and the migration file's DDL
     must all trace back to the SAME module constant byte-for-byte: if the
     index's predicate and the query's filter ever drift apart, the partial
     index silently stops covering the query.
  3. The DO block's expected values reconcile with the specs, dimension by
     dimension (the same check ``test_hotpath_indexes_batch2.py`` performs for
     its own migration).
  4. ``HOTPATH_INDEX_SPECS`` totals fourteen entries (eight batch-1 + two
     batch-2 + one batch-3 + three batch-4) and the earlier batches are
     untouched.
  5. Each spec's key columns stay byte-identical to the leg of
     ``list_sources_page``'s rewritten UNION that it exists to serve, on both
     backends -- an index keyed on an expression the query does not spell is
     an index the planner cannot use.

See ``backend/tests/postgres/test_hotpath_indexes_batch4_live.py`` for the
live-PostgreSQL half (real catalog rendering, real EXPLAIN plan proof, real
UNION-shape spy on the SQL the adapter issues) a fake connection cannot
exercise.
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
    / "0048_source_search_trgm_indexes.sql"
)

_BATCH4_NAMES = (
    "idx_sources_nb_title_file_trgm",
    "idx_source_authors_nb_name_trgm",
    "idx_source_paper_meta_nb_ptitle_trgm",
)

# Same shape as test_hotpath_indexes_batch2.py's / batch3's _STATEMENT_PATTERN:
# one statement, optional "USING <access method>", a parenthesized column list,
# and an optional WHERE predicate. The column lists here contain their own
# parentheses (``lower(title)``), which the non-greedy body handles by
# backtracking to the last close paren that still lets the tail match.
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


def _spec(name: str):
    return next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == name)


# ---------------------------------------------------------------------------
# 1. Migration file exists, is parseable, and declares exactly the three names.
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_declares_exactly_three_statements():
    assert _MIGRATION.is_file()
    parsed = _parse_migration_statements()
    assert [entry["name"] for entry in parsed] == list(_BATCH4_NAMES), (
        f"expected exactly {list(_BATCH4_NAMES)} in {_MIGRATION.name}, "
        f"parsed {[entry['name'] for entry in parsed]}"
    )


def test_batch4_specs_are_present_and_earlier_batches_are_untouched():
    names = {spec.name for spec in HOTPATH_INDEX_SPECS}
    assert set(_BATCH4_NAMES) <= names
    assert len(HOTPATH_INDEX_SPECS) == 14, (
        "expected eight batch-1 plus two batch-2 plus one batch-3 plus three "
        f"batch-4 entries in HOTPATH_INDEX_SPECS, found "
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
    batch2_names = {
        "idx_knowledge_objects_nb_payload_trgm", "idx_source_elements_nonblank"
    }
    batch3_names = {"idx_clusters_nb_canonical_member"}
    assert batch1_names <= names
    assert batch2_names <= names
    assert batch3_names <= names


def test_migration_statements_match_their_specs_verbatim():
    parsed = {entry["name"]: entry for entry in _parse_migration_statements()}

    for name in _BATCH4_NAMES:
        entry, spec = parsed[name], _spec(name)
        assert entry["table"] == spec.table, name
        assert entry["using"] == spec.using == "gin", name
        # The migration lays composite keys out on separate indented lines;
        # the spec's ddl_columns is the same text single-line. Collapse
        # whitespace on both sides — nothing else.
        assert " ".join(str(entry["columns"]).split()) == " ".join(
            spec.column_list_sql().split()
        ), name
        assert str(entry["predicate"]) == spec.predicate, name

    assert (
        parsed["idx_sources_nb_title_file_trgm"]["predicate"]
        == "source_type NOT IN ('memory','knowhow')"
    )
    assert parsed["idx_source_authors_nb_name_trgm"]["predicate"] == ""
    assert parsed["idx_source_paper_meta_nb_ptitle_trgm"]["predicate"] == ""


# ---------------------------------------------------------------------------
# 2. VISIBLE_SOURCE_TYPES_PREDICATE reconciliation — the load-bearing pin.
# ---------------------------------------------------------------------------


def test_partial_predicate_is_byte_identical_to_the_visible_types_constant():
    """The index's partial predicate and the query's own visible filter are the
    SAME module constant, spelled twice (a migration file cannot import
    Python). If they ever drift, the partial index quietly stops covering the
    query it was built for — the planner would simply not use it, with no
    error anywhere."""
    from app.repositories.postgres.source_store import (
        VISIBLE_SOURCE_TYPES_PREDICATE as PG_VISIBLE,
    )
    from app.repositories.sqlite.source_store import (
        VISIBLE_SOURCE_TYPES_PREDICATE as SQLITE_VISIBLE,
    )

    # The index's predicate must track the POSTGRES constant byte-for-byte —
    # that is the text list_sources_page interpolates into the SQL the planner
    # has to prove the partial-index implication against.
    assert PG_VISIBLE == "source_type NOT IN ('memory','knowhow')"
    assert _spec("idx_sources_nb_title_file_trgm").predicate == PG_VISIBLE
    # The SQLite twin differs by one cosmetic space after the comma
    # (pre-existing, and semantically irrelevant — SQLite gets no index from
    # this batch at all). Pin the two as whitespace-insensitively equal so a
    # real divergence in WHICH types are hidden still fails here, without this
    # test pretending the two spellings are byte-identical.
    assert PG_VISIBLE.replace(" ", "") == SQLITE_VISIBLE.replace(" ", "")
    migration_text = _ddl_only(_migration_text())
    assert f"WHERE {PG_VISIBLE}" in migration_text
    assert migration_text.count(PG_VISIBLE) == 1


def test_predicate_shape_is_the_not_in_mirror_of_batch1s_in_shape():
    """PostgreSQL canonicalizes ``NOT IN (…)`` to ``<> ALL (ARRAY[…])`` on
    store, exactly mirroring how batch 1's idx_sources_nb_hidden_type — the
    complementary ``IN (…)`` predicate over the very same column — is stored as
    ``= ANY (ARRAY[…])``. Pinning the two side by side means a future deparser
    change fails loudly here rather than silently under-matching in
    production."""
    hidden = _spec("idx_sources_nb_hidden_type")
    visible = _spec("idx_sources_nb_title_file_trgm")
    assert hidden.predicate_shape == (
        "source_type = ANY (ARRAY['memory'::text, 'knowhow'::text])"
    )
    assert visible.predicate_shape == (
        "source_type <> ALL (ARRAY['memory'::text, 'knowhow'::text])"
    )


# ---------------------------------------------------------------------------
# 3. The DO block's expected values reconcile with the specs.
# ---------------------------------------------------------------------------


def test_do_block_expected_values_reconcile_with_the_specs():
    """Migration 0048's pre-existing-index validation DO block and
    ``HOTPATH_INDEX_SPECS`` are two hand-authored copies of the same semantic
    dimensions — here they are reconciled one dimension at a time, so an edit
    to either side alone fails loudly."""
    from app.repositories.postgres.hotpath_indexes import _normalized_expr

    text = _migration_text()
    pattern = re.compile(
        r"\('(?P<name>idx_\w+)',\s*\n\s*'(?P<table>\w+)',\s*\n\s*'(?P<am>\w+)',\s*\n"
        r"\s*ARRAY\[(?P<keys>[^\]]*)\],\s*\n"
        r"\s*ARRAY\[(?P<opclasses>[^\]]*)\],\s*\n"
        r"\s*ARRAY\[(?P<collations>[^\]]*)\],\s*\n"
        r"\s*\$pred\$(?P<predicate>.*?)\$pred\$\)",
        re.S,
    )

    def _items(raw: str) -> list[str]:
        return [piece.strip()[1:-1] for piece in raw.split(",")]

    parsed = {m.group("name"): m for m in pattern.finditer(text)}
    assert set(parsed) == set(_BATCH4_NAMES), sorted(parsed)
    for name in _BATCH4_NAMES:
        spec, match = _spec(name), parsed[name]
        assert match.group("table") == spec.table, name
        assert match.group("am") == (spec.using or "btree") == "gin", name
        assert _items(match.group("keys")) == [
            _normalized_expr(column) for column in spec.columns
        ], name
        assert _items(match.group("opclasses")) == list(spec.opclasses), name
        assert _items(match.group("collations")) == list(spec.collations), name
        assert match.group("predicate") == _normalized_expr(spec.predicate_shape), name


# ---------------------------------------------------------------------------
# 4. Each key column is spelled by the query leg it serves.
# ---------------------------------------------------------------------------


def test_spec_key_columns_match_the_rewritten_search_legs_on_both_backends():
    """An index keyed on an expression the query does not spell is an index the
    planner cannot use. The three legs are pinned against BOTH stores' source
    text so a one-sided edit (e.g. dropping ``LOWER()`` on one backend) fails
    here instead of quietly costing the index."""
    from app.repositories.postgres import source_store as pg_source_store
    from app.repositories.sqlite import source_store as sqlite_source_store

    assert _spec("idx_sources_nb_title_file_trgm").columns == (
        "notebook_id", "lower(title)", "lower(file_name)"
    )
    assert _spec("idx_source_authors_nb_name_trgm").columns == (
        "notebook_id", "lower(name)"
    )
    assert _spec("idx_source_paper_meta_nb_ptitle_trgm").columns == (
        "notebook_id", "lower(paper_title)"
    )

    pg_source = Path(pg_source_store.__file__).read_text(encoding="utf-8")
    assert "AND (LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s) " in pg_source
    assert "WHERE a.notebook_id=%s AND LOWER(a.name) LIKE %s " in pg_source
    assert "WHERE m.notebook_id=%s AND LOWER(m.paper_title) LIKE %s)" in pg_source

    sqlite_source = Path(sqlite_source_store.__file__).read_text(encoding="utf-8")
    assert " AND (LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?)" in sqlite_source
    assert " WHERE a.notebook_id = ? AND LOWER(a.name) LIKE ?" in sqlite_source
    assert " WHERE m.notebook_id = ? AND LOWER(m.paper_title) LIKE ?)" in sqlite_source


def test_no_backend_regresses_to_the_cross_table_or_exists_predicate():
    """站点级防回退钉:两端的 q 过滤都必须是 id 半连接的三腿 UNION,不能退回
    跨表 OR-EXISTS —— 那个形态让 planner 选 hashed subplan 并整表扫两张子表
    (迁移 0048 头注释里的 363ms 生产实测)。上面的按腿字节钉管不到「谁把三条腿
    重新拼回一个 OR」这一半,这条管。"""
    from app.repositories.postgres import source_store as pg_source_store
    from app.repositories.sqlite import source_store as sqlite_source_store

    for module, marker in (
        (pg_source_store, "AND sources.id IN ("),
        (sqlite_source_store, " AND sources.id IN ("),
    ):
        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert marker in code, module.__name__
        assert code.count("UNION SELECT a.source_id FROM source_authors a") == 1, (
            module.__name__
        )
        assert code.count("UNION SELECT m.source_id FROM source_paper_meta m") == 1, (
            module.__name__
        )
        # The retired shape, in either dialect's spelling.
        assert "EXISTS(SELECT 1 FROM source_authors a" not in code, module.__name__
        assert "EXISTS(SELECT 1 FROM source_paper_meta m" not in code.replace(
            # PAPER_META_NO_META_SQL legitimately keeps its own EXISTS probe on
            # source_paper_meta; it is a different predicate (row presence, no
            # LIKE) and must not be caught by this guard.
            "NOT EXISTS(SELECT 1 FROM source_paper_meta m WHERE m.source_id=s.id)", ""
        ).replace(
            "NOT EXISTS(SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)", ""
        ), module.__name__
