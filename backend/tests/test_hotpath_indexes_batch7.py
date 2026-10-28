"""Unit tests (fake-connection only, no live PG — G1-tier, same placement
rule as the batch-1..6 siblings) for batch 7's one addition to
``HOTPATH_INDEX_SPECS`` (migration ``0084_source_paper_meta_source_nb.sql``):
the INCLUDE-carrying covering index ``idx_source_paper_meta_source_nb`` for the
own-notebook paper-metadata probe.

Contract under test (anti-drift, same rationale as the earlier batches: the
migration file, its DO-block validation values and the spec tuple are three
hand-authored copies of one index shape), plus the SQLite twin: v103 builds
the same index name with the include columns as trailing keys, and the
SQLite readers that name it with ``INDEXED BY`` spell the same name.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.repositories.postgres.hotpath_indexes import HOTPATH_INDEX_SPECS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = (
    _REPO_ROOT / "backend" / "app" / "repositories" / "postgres"
    / "migrations" / "0084_source_paper_meta_source_nb.sql"
)
_NAME = "idx_source_paper_meta_source_nb"

_STATEMENT_PATTERN = re.compile(
    r"CREATE (?P<unique>UNIQUE )?INDEX IF NOT EXISTS\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)"
    r"\s*\(\s*(?P<columns>[\s\S]*?)\s*\)"
    r"(?:\s*INCLUDE\s*\(\s*(?P<include>[\s\S]*?)\s*\))?;",
    re.MULTILINE,
)
_DO_ROW_PATTERN = re.compile(
    r"\('(?P<name>idx_\w+)',\s*\n\s*'(?P<table>\w+)', '(?P<am>\w+)', (?P<unique>true|false),\s*\n"
    r"\s*ARRAY\[(?P<keys>[^\]]*)\],\s*\n"
    r"\s*ARRAY\[(?P<includes>[^\]]*)\],\s*\n"
    r"\s*ARRAY\[(?P<opclasses>[^\]]*)\],\s*\n"
    r"\s*ARRAY\[(?P<collations>[^\]]*)\]\)",
)


def _ddl_only() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def _spec():
    (spec,) = [spec for spec in HOTPATH_INDEX_SPECS if spec.name == _NAME]
    return spec


def _items(raw: str) -> list[str]:
    return [piece.strip()[1:-1] for piece in raw.split(",") if piece.strip()]


def test_migration_declares_exactly_the_batch7_statement():
    assert _MIGRATION.is_file()
    parsed = {m.group("name"): m for m in _STATEMENT_PATTERN.finditer(_ddl_only())}
    assert set(parsed) == {_NAME}, sorted(parsed)
    match, spec = parsed[_NAME], _spec()
    assert match.group("table") == spec.table == "source_paper_meta"
    assert not match.group("unique") and spec.unique is False
    columns = tuple(column.strip() for column in match.group("columns").split(","))
    assert columns == spec.columns == ("source_id",)
    include = tuple(
        column.strip() for column in (match.group("include") or "").split(",")
        if column.strip()
    )
    assert include == spec.include == ("notebook_id", "is_paper")
    assert not re.search(r"^\s*DROP INDEX", _ddl_only(), re.MULTILINE)


def test_do_block_expected_values_reconcile_with_the_spec():
    rows = {m.group("name"): m for m in _DO_ROW_PATTERN.finditer(_MIGRATION.read_text())}
    assert set(rows) == {_NAME}, sorted(rows)
    row, spec = rows[_NAME], _spec()
    assert row.group("table") == spec.table
    assert row.group("am") == (spec.using or "btree") == "btree"
    assert (row.group("unique") == "true") is spec.unique
    assert _items(row.group("keys")) == list(spec.columns)
    assert _items(row.group("includes")) == list(spec.include)
    assert _items(row.group("opclasses")) == list(spec.opclasses)
    assert _items(row.group("collations")) == list(spec.collations)


def test_spec_renders_the_include_ddl():
    text = _spec().ddl("public", concurrently=True).as_string(None)
    assert text.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS"), text
    assert text.endswith("INCLUDE (notebook_id, is_paper)"), text


def test_sqlite_twin_builds_the_same_name_with_the_includes_as_trailing_keys():
    from app.repositories.sqlite import migrations as sqlite_migrations
    from app.repositories.sqlite import source_store as sqlite_source_store

    source = Path(sqlite_migrations.__file__).read_text(encoding="utf-8")
    assert (
        f'"CREATE INDEX IF NOT EXISTS {_NAME} "\n'
        '                "ON source_paper_meta(source_id, notebook_id, is_paper)"'
    ) in source
    assert sqlite_source_store.PAPER_META_SOURCE_NB_INDEX == _NAME
    assert "INDEXED BY {PAPER_META_SOURCE_NB_INDEX}" in Path(
        sqlite_source_store.__file__).read_text(encoding="utf-8")
