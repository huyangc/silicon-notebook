"""Unit tests (fake-connection only, no live PG — G1-tier, same placement
rule as the batch-1..4 siblings) for batch 6's three additions to
``HOTPATH_INDEX_SPECS`` (batch 3 · W2 · PR-1, migration
``0051_derived_generation.sql``): the four-column UNIQUE
``uq_clusters_nb_type_member_generation`` and the two INCLUDE-carrying
covering replacements ``idx_clusters_nb_canonical_member_gen`` /
``idx_clusters_nb_created_gen``.

Contract under test (anti-drift, same rationale as the earlier batches: the
migration file and the spec tuple are two independent hand-authored copies):

  1. the migration declares exactly the three batch-6 statements, with
     UNIQUE / INCLUDE shapes matching each spec byte-for-byte;
  2. the migration DROPs exactly the three superseded indexes
     (``uq_clusters_notebook_type_member`` — the three-column unique that
     physically forbids dual generations, PR-2's whole mechanism —
     ``idx_clusters_nb_canonical_member`` and ``idx_clusters_nb_created``);
  3. spec ``unique``/``include`` fields render the intended DDL and default
     to inert values on every earlier batch's entry.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT / "backend" / "app" / "repositories" / "postgres"
    / "migrations" / "0051_derived_generation.sql"
)

_BATCH6_NAMES = (
    "uq_clusters_nb_type_member_generation",
    "idx_clusters_nb_canonical_member_gen",
    "idx_clusters_nb_created_gen",
)
_DROPPED_NAMES = (
    "uq_clusters_notebook_type_member",
    "idx_clusters_nb_canonical_member",
    "idx_clusters_nb_created",
    # 0004 的裸 notebook_id 前缀索引:留着会劫走聚合读者的计划(窄索引 +
    # 回表过滤 generation),裸前缀扫描由 _created_gen 前导列等价服务。
    "idx_clusters_nb",
)

_STATEMENT_PATTERN = re.compile(
    r"CREATE (?P<unique>UNIQUE )?INDEX IF NOT EXISTS\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)"
    r"\s*\(\s*(?P<columns>[\s\S]*?)\s*\)"
    r"(?:\s*INCLUDE\s*\(\s*(?P<include>[\s\S]*?)\s*\))?;",
    re.MULTILINE,
)
_DROP_PATTERN = re.compile(r"DROP INDEX IF EXISTS\s+(?P<name>\w+);")


def _ddl_only() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def test_migration_declares_exactly_the_three_batch6_statements():
    assert _MIGRATION.is_file()
    parsed = {
        m.group("name"): m for m in _STATEMENT_PATTERN.finditer(_ddl_only())
    }
    assert set(parsed) == set(_BATCH6_NAMES), sorted(parsed)
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}
    for name in _BATCH6_NAMES:
        match, spec = parsed[name], by_name[name]
        assert match.group("table") == spec.table, name
        assert bool(match.group("unique")) == spec.unique, name
        columns = tuple(
            column.strip() for column in match.group("columns").split(",")
        )
        assert columns == spec.columns, name
        include = tuple(
            column.strip()
            for column in (match.group("include") or "").split(",")
            if column.strip()
        )
        assert include == spec.include, name


def test_migration_drops_exactly_the_three_superseded_indexes():
    dropped = [m.group("name") for m in _DROP_PATTERN.finditer(_ddl_only())]
    assert sorted(dropped) == sorted(_DROPPED_NAMES), dropped


def test_unique_and_include_fields_render_ddl_and_stay_inert_elsewhere():
    by_name = {spec.name: spec for spec in HOTPATH_INDEX_SPECS}
    unique_spec = by_name["uq_clusters_nb_type_member_generation"]
    text = unique_spec.ddl("public", concurrently=True).as_string(None)
    assert text.startswith("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS")
    include_spec = by_name["idx_clusters_nb_canonical_member_gen"]
    text = include_spec.ddl("public", concurrently=False).as_string(None)
    assert text.endswith("INCLUDE (generation)"), text
    for spec in HOTPATH_INDEX_SPECS:
        if spec.name in _BATCH6_NAMES:
            continue
        assert spec.unique is False and spec.include == (), spec.name


def test_prerequisites_are_limited_to_idempotent_add_column_form():
    """守卫:spec.prerequisites 只许 ALTER TABLE … ADD COLUMN IF NOT EXISTS
    形态(元数据级、幂等、在线安全)——builder 不许经此演化成第二个迁移器。"""
    pattern = re.compile(
        r"^ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS \w+ "
        r"(bigint|integer|text|timestamp with time zone)( NOT NULL DEFAULT \S+)?$"
    )
    for spec in HOTPATH_INDEX_SPECS:
        for prerequisite in spec.prerequisites:
            assert pattern.fullmatch(prerequisite), (spec.name, prerequisite)
        if spec.name not in _BATCH6_NAMES:
            assert spec.prerequisites == (), spec.name
