"""Unit tests (fake-connection only, no live PG — G1-tier, same placement rule
as ``test_hotpath_indexes.py``) for hot-path fix batch 2 (R6)'s two additions
to ``HOTPATH_INDEX_SPECS``:

  1. ``idx_knowledge_objects_payload_trgm`` (GIN trigram expression index) —
     the first non-btree entry ``HotpathIndexSpec`` has ever carried, hence
     this module's own cross-check of the ``using``/``ddl_columns`` fields
     rather than reusing batch 1's plain-btree-only assumptions.
  2. ``idx_source_elements_nonblank`` (partial btree index keyed on the exact
     ``PY_WHITESPACE`` charset).

Contract under test:

  1. Anti-drift — the two index definitions live in
     ``migrations/0042_hotpath_batch2_search_indexes.sql`` AND in
     ``HOTPATH_INDEX_SPECS``, two independent hand-authored copies (a
     migration file cannot import Python at apply time). This module parses
     the migration file and cross-checks it statement-by-statement against
     the two batch-2 specs, mirroring ``test_hotpath_indexes.py``'s batch-1
     equivalent but with its own regex (batch 1's regex has no ``USING``
     support and assumes a short predicate; this migration needs both).
  2. PY_WHITESPACE reconciliation — the single easiest way to get this batch
     wrong (per the task's own instructions): the migration file's partial
     predicate literal, ``HOTPATH_INDEX_SPECS``'s ``predicate``/
     ``predicate_shape`` fields, and ``postgres/maintenance.py``'s
     ``_NONBLANK_TEXT_SQL`` query-side literal must all trace back to the
     SAME ``PY_WHITESPACE`` constant byte-for-byte. A one-character edit to
     any of these four in isolation must fail one of the assertions here.
  3. ``HOTPATH_INDEX_SPECS`` totals ten entries (eight batch-1 + two
     batch-2), and the two new ones carry the ``using``/``ddl_columns``
     fields this batch's DDL genuinely needs.

See ``backend/tests/postgres/test_hotpath_indexes_batch2_live.py`` for the
live-PostgreSQL half (real catalog rendering, real EXPLAIN plan proof) that a
fake connection cannot exercise.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.repositories.postgres import maintenance as postgres_maintenance
from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS
from app.repositories.text_whitespace import PY_WHITESPACE


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT
    / "backend"
    / "app"
    / "repositories"
    / "postgres"
    / "migrations"
    / "0042_hotpath_batch2_search_indexes.sql"
)

_BATCH2_NAMES = frozenset(
    {"idx_knowledge_objects_payload_trgm", "idx_source_elements_nonblank"}
)

# One statement, optionally "USING <access method>", a parenthesized column
# list (balanced against nested parens via a non-greedy body up to the
# matching close before "WHERE" or ";"), and an optional WHERE predicate.
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


def _flat_nonblank_predicate() -> str:
    """The exact flat ``chr(N) || chr(N) || ...`` DDL text derived from
    ``PY_WHITESPACE`` — the same expression ``HotpathIndexSpec.predicate``
    computes for ``idx_source_elements_nonblank`` and the same one this
    migration's author pasted into the static SQL file by hand at authoring
    time. Recomputed here (not imported) so a stray edit to either copy is
    caught independently of the other."""
    return " || ".join(f"chr({ord(character)})" for character in PY_WHITESPACE)


# ---------------------------------------------------------------------------
# 1. Migration file exists, is parseable, and declares exactly the two names.
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_declares_exactly_two_statements():
    assert _MIGRATION.is_file()
    parsed = _parse_migration_statements()
    assert {entry["name"] for entry in parsed} == _BATCH2_NAMES, (
        f"expected exactly {_BATCH2_NAMES} in {_MIGRATION.name}, "
        f"parsed {[entry['name'] for entry in parsed]}"
    )


def test_batch2_specs_are_present_and_batch1_is_untouched():
    names = {spec.name for spec in HOTPATH_INDEX_SPECS}
    assert _BATCH2_NAMES <= names
    assert len(HOTPATH_INDEX_SPECS) == 10, (
        "expected eight batch-1 plus two batch-2 entries in HOTPATH_INDEX_SPECS, "
        f"found {len(HOTPATH_INDEX_SPECS)}: {sorted(names)}"
    )


def test_migration_statements_match_their_specs_verbatim():
    parsed = {entry["name"]: entry for entry in _parse_migration_statements()}
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}

    gin_entry = parsed["idx_knowledge_objects_payload_trgm"]
    gin_spec = by_name["idx_knowledge_objects_payload_trgm"]
    assert gin_entry["table"] == gin_spec.table
    assert gin_entry["using"] == gin_spec.using == "gin"
    assert gin_entry["columns"] == gin_spec.column_list_sql()
    assert gin_entry["predicate"] == gin_spec.predicate == ""

    partial_entry = parsed["idx_source_elements_nonblank"]
    partial_spec = by_name["idx_source_elements_nonblank"]
    assert partial_entry["table"] == partial_spec.table
    assert partial_entry["using"] == partial_spec.using == ""
    assert partial_entry["columns"] == partial_spec.column_list_sql() == "source_id, id"
    assert partial_entry["predicate"] == partial_spec.predicate


# ---------------------------------------------------------------------------
# 2. PY_WHITESPACE reconciliation — the load-bearing anti-drift pin.
# ---------------------------------------------------------------------------


def test_migration_partial_predicate_is_byte_identical_to_py_whitespace():
    expected_flat = _flat_nonblank_predicate()
    migration_text = _ddl_only(_migration_text())
    expected_full = f"btrim(text, {expected_flat}) != ''"
    assert expected_full in migration_text, (
        "migrations/0042_hotpath_batch2_search_indexes.sql's partial predicate "
        "no longer matches PY_WHITESPACE byte-for-byte"
    )
    # Byte-count sanity: exactly one occurrence, not an accidental partial match.
    assert migration_text.count(expected_flat) == 1


def test_hotpath_spec_predicate_is_byte_identical_to_py_whitespace():
    spec = next(
        spec for spec in HOTPATH_INDEX_SPECS if spec.name == "idx_source_elements_nonblank"
    )
    expected = f"btrim(text, {_flat_nonblank_predicate()}) != ''"
    assert spec.predicate == expected


def test_maintenance_nonblank_literal_is_byte_identical_to_py_whitespace():
    from psycopg import sql

    expected_literal = sql.Literal(PY_WHITESPACE).as_string(None)
    expected = f"btrim(e.text, {expected_literal}) <> ''"
    assert postgres_maintenance._NONBLANK_TEXT_SQL == expected


def test_all_three_nonblank_predicate_copies_agree_on_the_charset():
    """Even though the migration/spec use a flat ``chr()`` DDL form and the
    maintenance-module query uses a single quoted string literal (two
    different PostgreSQL surface syntaxes -- see
    ``test_hotpath_indexes_batch2_live.py`` for proof PostgreSQL's own
    ``pg_get_expr`` unifies them), every one of the three copies must encode
    exactly ``PY_WHITESPACE``'s 29 code points, in the same order, and
    nothing else."""
    migration_predicate = next(
        entry["predicate"]
        for entry in _parse_migration_statements()
        if entry["name"] == "idx_source_elements_nonblank"
    )
    migration_codepoints = [int(n) for n in re.findall(r"chr\((\d+)\)", migration_predicate)]

    spec = next(
        spec for spec in HOTPATH_INDEX_SPECS if spec.name == "idx_source_elements_nonblank"
    )
    spec_codepoints = [int(n) for n in re.findall(r"chr\((\d+)\)", spec.predicate)]

    maintenance_literal = postgres_maintenance._NONBLANK_TEXT_SQL
    # Extract the single-quoted string literal between "btrim(e.text, " and ") <>".
    match = re.search(r"btrim\(e\.text, '(.*)'\) <> ''", maintenance_literal, re.S)
    assert match is not None
    maintenance_codepoints = [ord(character) for character in match.group(1)]

    expected_codepoints = [ord(character) for character in PY_WHITESPACE]
    assert migration_codepoints == expected_codepoints
    assert spec_codepoints == expected_codepoints
    assert maintenance_codepoints == expected_codepoints


# ---------------------------------------------------------------------------
# 3. GIN expression must stay byte-identical to search.py's own ILIKE arm.
# ---------------------------------------------------------------------------


def test_gin_ddl_columns_match_search_py_payload_expression_verbatim():
    from app.repositories.postgres import search as search_module

    spec = next(
        spec for spec in HOTPATH_INDEX_SPECS
        if spec.name == "idx_knowledge_objects_payload_trgm"
    )
    # search.py's query source is a Python string literal, so its file bytes
    # carry the expression with escaped quotes (``\"C\"``); compare against
    # the actual RUNTIME string search.py builds (calling
    # notebook_knowledge_rows needs a live connection, but the expression is
    # a plain module-level literal split across two string-literal pieces on
    # one line, so reconstructing it from source is unambiguous here).
    search_source = Path(search_module.__file__).read_text(encoding="utf-8")
    assert '"(payload::text) COLLATE \\"C\\" ILIKE %s' in search_source, (
        "search.py's payload ILIKE arm expression text changed — "
        "idx_knowledge_objects_payload_trgm's ddl_columns must track it"
    )
    assert '(payload::text) COLLATE "C"' in spec.ddl_columns


def test_no_eligibility_site_regresses_to_a_bound_param_btrim():
    """站点级防回退钉:非空元素资格判定必须全部经 ``_NONBLANK_TEXT_SQL`` 单一定义点。
    任何站点改回 ``btrim(e.text, %s)`` 绑定参数形态都会在 generic plan 下丢掉
    idx_source_elements_nonblank(见 live 测试的 force_generic_plan 对照)——上面的
    常量字节钉管不到「站点没用常量」这一半,这条管。"""
    source = Path(postgres_maintenance.__file__).read_text(encoding="utf-8")
    ddl_and_comments_stripped = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "btrim(e.text, %s)" not in ddl_and_comments_stripped
    assert "btrim(text, %s)" not in ddl_and_comments_stripped
    assert ddl_and_comments_stripped.count("_NONBLANK_TEXT_SQL") >= 6  # 定义 + ≥5 站点
