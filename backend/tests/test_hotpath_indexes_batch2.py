"""Unit tests (fake-connection only, no live PG — G1-tier, same placement rule
as ``test_hotpath_indexes.py``) for hot-path fix batch 2 (R6)'s two additions
to ``HOTPATH_INDEX_SPECS``:

  1. ``idx_knowledge_objects_nb_payload_trgm`` (notebook-scoped composite
     partial GIN trigram index; codex #636 R1 P1) — the first non-btree entry
     ``HotpathIndexSpec`` has ever carried, hence this module's own
     cross-check of the ``using``/``ddl_columns`` fields rather than reusing
     batch 1's plain-btree-only assumptions.
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
  3. ``HOTPATH_INDEX_SPECS`` totals fourteen entries (eight batch-1 + two
     batch-2 + one batch-3 + three batch-4), and the two batch-2 entries
     carry the ``using``/``ddl_columns`` fields this batch's DDL genuinely
     needs.

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
    {"idx_knowledge_objects_nb_payload_trgm", "idx_source_elements_nonblank"}
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
    assert len(HOTPATH_INDEX_SPECS) == 18, (
        "expected seven batch-1 (idx_clusters_nb_canonical superseded by batch 6) plus two batch-2 plus zero batch-3 (superseded by batch 6) plus three "
        f"batch-4 plus three batch-5 plus three batch-6 entries in HOTPATH_INDEX_SPECS, found "
        f"{len(HOTPATH_INDEX_SPECS)}: {sorted(names)}"
    )


def test_migration_statements_match_their_specs_verbatim():
    parsed = {entry["name"]: entry for entry in _parse_migration_statements()}
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}

    gin_entry = parsed["idx_knowledge_objects_nb_payload_trgm"]
    gin_spec = by_name["idx_knowledge_objects_nb_payload_trgm"]
    assert gin_entry["table"] == gin_spec.table
    assert gin_entry["using"] == gin_spec.using == "gin"
    # The migration lays the two composite keys out on separate indented
    # lines; the spec's ddl_columns is the same text single-line. Collapse
    # whitespace on both sides — nothing else.
    assert " ".join(str(gin_entry["columns"]).split()) == " ".join(
        gin_spec.column_list_sql().split()
    )
    assert (
        str(gin_entry["predicate"])
        == gin_spec.predicate
        == "status != 'deprecated'"
    )

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
        if spec.name == "idx_knowledge_objects_nb_payload_trgm"
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
        "idx_knowledge_objects_nb_payload_trgm's ddl_columns must track it"
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
    # 精确等值 + 逐站点:>=6 会在任何人多提一次这个名字时凭空多出余量,
    # 单站点回退(逐字恢复旧形态,含无空格变体)就静默通过(质量评审变异实证)。
    assert ddl_and_comments_stripped.count("_NONBLANK_TEXT_SQL") == 6  # 1 定义 + 5 站点
    import inspect as _inspect
    for method_name in (
        "count_missing_element_vectors",
        "missing_element_embedding_ids",
        "missing_element_embedding_rows",
        "missing_element_embedding_page",
        "missing_element_vector_source_ids",
    ):
        body = _inspect.getsource(
            getattr(postgres_maintenance.PostgresMaintenanceAdapter, method_name)
        )
        assert "_NONBLANK_TEXT_SQL" in body, method_name


def test_do_block_expected_values_reconcile_with_the_specs():
    """迁移 0042 的先存索引校验 DO 块与 ``HOTPATH_INDEX_SPECS`` 是同一组语义
    维度的两份手抄(迁移文件不能在 apply 时 import Python)——这里逐维对账:
    DO 块 VALUES 里的 am/keys/opclasses/collations/predicate 必须恰好等于从
    两条批 2 spec 推导出的规范化期望,任何一边单独改动都在此响亮失败。
    (质量评审 P1 把 DO 块从「pg_get_indexdef 全文比对」改成语义维度比对——
    全文比对会被 reloptions/TABLESPACE 这类存储子句误杀完好索引;live 测试
    另有 fastupdate=off 的容忍用例与真目录的 accept/reject 路径。)"""
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
    assert set(parsed) == _BATCH2_NAMES, sorted(parsed)
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}
    for name in sorted(_BATCH2_NAMES):
        spec, match = by_name[name], parsed[name]
        assert match.group("table") == spec.table, name
        assert match.group("am") == (spec.using or "btree"), name
        assert _items(match.group("keys")) == [
            _normalized_expr(column) for column in spec.columns
        ], name
        assert _items(match.group("opclasses")) == list(spec.opclasses), name
        assert _items(match.group("collations")) == list(spec.collations), name
        assert match.group("predicate") == _normalized_expr(spec.predicate_shape), name


def test_same_named_btree_posing_as_the_gin_is_reported_unexpected():
    """codex #636 R1 P2 的钉:形态比对必须含访问方法/opclass——同名 btree(或非
    trgm opclass 的 GIN)即使 keys/predicate 碰巧一致,对 ILIKE 也零加速,三层
    (inspect/apply/迁移 IF NOT EXISTS)都不许把它当「存在」。"""
    from app.repositories.postgres.hotpath_indexes import (
        HOTPATH_INDEX_SPECS,
        _matches_shape,
    )

    spec = next(
        s for s in HOTPATH_INDEX_SPECS
        if s.name == "idx_knowledge_objects_nb_payload_trgm"
    )
    good_row = {
        "keys": list(spec.columns),
        "predicate": spec.predicate_shape,
        "access_method": "gin",
        "opclasses": ["public:text_ops", "public:gin_trgm_ops"],
        "collations": ["pg_catalog:C", "pg_catalog:C"],
        "indisunique": False,
        "indnkeyatts": 2,
        "indnatts": 2,
    }
    assert _matches_shape(good_row, spec)
    assert not _matches_shape({**good_row, "access_method": "btree"}, spec)
    # codex #636 R2 P2:声明 UNIQUE、或带 INCLUDE 附加列(indnatts > indnkeyatts)
    # 的同名索引,keys/谓词全同也不许——否则 inspect 报就绪而迁移 DO 块按同维拒绝,
    # 两个校验器对同一目录行给出相反结论。
    assert not _matches_shape({**good_row, "indisunique": True}, spec)
    assert not _matches_shape({**good_row, "indnatts": 3}, spec)
    assert not _matches_shape(
        {**good_row, "opclasses": ["public:text_ops", "public:jsonb_ops"]}, spec
    )
    # 质量评审 P1 的实证场景:手建时少写 COLLATE "C"——keys/predicate/am/opclass
    # 全对,唯表达式键 collation 落回默认,planner 因 exprCollation 不匹配拒用。
    assert not _matches_shape(
        {**good_row, "collations": ["pg_catalog:C", "pg_catalog:default"]}, spec
    )
    # codex #636 R1 P1 的钉:少了 notebook_id 前置键的旧单表达式形(全局位图
    # 跨 notebook 退化,docs/operations.md 已记录的教训)不许被认作就绪。
    assert not _matches_shape(
        {
            **good_row,
            "keys": ["(payload::text)"],
            "opclasses": ["public:gin_trgm_ops"],
            "collations": ["pg_catalog:C"],
        },
        spec,
    )
    # 同形但缺 partial 谓词(全表 GIN):也不许。
    assert not _matches_shape({**good_row, "predicate": ""}, spec)
    # 批 1 的普通 btree 条目:期望 access_method 恒为 btree。
    plain = next(s for s in HOTPATH_INDEX_SPECS if s.using == "")
    plain_row = {
        "keys": list(plain.columns),
        "predicate": plain.predicate_shape,
        "access_method": "btree",
        "opclasses": list(plain.opclasses),
        "collations": list(plain.collations),
        "indisunique": False,
        "indnkeyatts": len(plain.columns),
        "indnatts": len(plain.columns),
    }
    assert _matches_shape(plain_row, plain)
    assert not _matches_shape({**plain_row, "access_method": "gin"}, plain)
