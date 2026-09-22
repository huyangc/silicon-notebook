"""守卫: 同步范围 manifest (app.migration.sync.manifest) 与身份映射纯函数
(app.migration.sync.identity)。

设计见 docs/incremental-sync-design.md §3/§4。manifest 必须对
``POSTGRES_BUSINESS_TABLES`` 全覆盖: 新增业务表如果没有在这里分类, 这份守卫必须
响亮失败, 而不是让它悄悄落进某个默认分类。

``test_manifest_covers_every_business_table_exactly`` 故意只从 SYNC_MANIFEST
本身取表名集合、和 POSTGRES_BUSINESS_TABLES 直接比较 -- 不经过 synced_tables()/
local_tables()。这样即使全覆盖性被破坏, 报错的是这条测试给出的清晰双向差集,
而不是 synced_tables() 内部一个不知道该怪哪张表的 ValueError; 因此它被放在本文件
最前面, 排在所有会调用 synced_tables() 的测试之前。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sqlite3
import sys

import pytest

from app.migration.shadow.manifest import MANIFEST as SHADOW_MANIFEST
from app.migration.sync import manifest as sync_manifest_module
from app.migration.sync.identity import UserMapping, UserProjection, build_user_mapping
from app.migration.sync.manifest import (
    SYNC_MANIFEST,
    MappedColumn,
    MappingKind,
    SyncClass,
    local_tables,
    spec_for,
    synced_tables,
)
from app.repositories.postgres.schema_manifest import POSTGRES_BUSINESS_TABLES


# --------------------------------------------------------------- 全覆盖性


def test_manifest_covers_every_business_table_exactly():
    manifest_names = {spec.name for spec in SYNC_MANIFEST}
    business_names = set(POSTGRES_BUSINESS_TABLES)

    missing_from_manifest = business_names - manifest_names
    extra_in_manifest = manifest_names - business_names

    assert not missing_from_manifest, (
        "POSTGRES_BUSINESS_TABLES 中未在 SYNC_MANIFEST 分类的表: "
        f"{sorted(missing_from_manifest)}"
    )
    assert not extra_in_manifest, (
        "SYNC_MANIFEST 中有 POSTGRES_BUSINESS_TABLES 没有的表: "
        f"{sorted(extra_in_manifest)}"
    )


def test_manifest_has_no_duplicate_table_names():
    names = [spec.name for spec in SYNC_MANIFEST]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"SYNC_MANIFEST 里的重复表名: {sorted(duplicates)}"


# --------------------------------------------------------- 字段与类的一致性


def test_mapped_columns_only_on_synced_with_mapping():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.SYNCED_WITH_MAPPING:
            assert spec.mapped_columns, (
                f"{spec.name} 是 SYNCED_WITH_MAPPING 但 mapped_columns 为空"
            )
        else:
            assert not spec.mapped_columns, (
                f"{spec.name} 不是 SYNCED_WITH_MAPPING 但 mapped_columns "
                f"非空: {spec.mapped_columns}"
            )


def test_mapped_columns_are_typed_mapped_column_with_a_mapping_kind():
    for spec in SYNC_MANIFEST:
        for column in spec.mapped_columns:
            assert isinstance(column, MappedColumn), (
                f"{spec.name}: mapped_columns 里的元素必须是 MappedColumn, "
                f"实际是 {type(column)!r}"
            )
            assert isinstance(column.kind, MappingKind)


def test_principal_kind_only_used_by_notebook_grants_principal_id():
    for spec in SYNC_MANIFEST:
        for column in spec.mapped_columns:
            if column.kind is MappingKind.PRINCIPAL:
                assert (spec.name, column.name) == ("notebook_grants", "principal_id"), (
                    "MappingKind.PRINCIPAL 目前只用于 notebook_grants.principal_id, "
                    f"却出现在 {spec.name}.{column.name}"
                )


def test_severed_columns_empty_on_local_tables():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.LOCAL:
            assert not spec.severed_columns, (
                f"{spec.name} 是 LOCAL 但 severed_columns 非空: {spec.severed_columns}"
            )


def test_local_tables_have_no_target_owned_columns():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.LOCAL:
            assert not spec.target_owned_columns, (
                f"{spec.name} 是 LOCAL 但 target_owned_columns 非空: "
                f"{spec.target_owned_columns}"
            )


def test_local_tables_are_never_seed_only():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.LOCAL:
            assert spec.seed_only is False, (
                f"{spec.name} 是 LOCAL 但 seed_only=True"
            )


def test_notebooks_target_owned_columns_are_pinned():
    assert spec_for("notebooks").target_owned_columns == (
        "status",
        "is_shared",
        "share_token",
        "sync_origin",
    )


def test_seed_only_registry_is_pinned():
    seed_only_names = {spec.name for spec in SYNC_MANIFEST if spec.seed_only}
    assert seed_only_names == {
        "notebook_members",
        "notebook_grants",
        "groups",
        "group_members",
    }


def test_severed_columns_registry_is_pinned():
    severed = {
        spec.name: spec.severed_columns
        for spec in SYNC_MANIFEST
        if spec.severed_columns
    }
    assert severed == {
        "memory_items": ("agent_profile_id",),
        "sources": ("agent_profile_id",),
        "knowledge_objects": ("source_candidate_id",),
    }


def test_memory_items_mapped_columns_are_pinned():
    assert spec_for("memory_items").mapped_columns == (
        MappedColumn("created_by", MappingKind.USER),
        MappedColumn("confirmed_by", MappingKind.USER),
    )


# ------------------------------------------------------------- synced_tables


def test_synced_tables_all_present_in_shadow_manifest_with_monotonic_copy_rank():
    shadow_ranks = {spec.name: spec.copy_rank for spec in SHADOW_MANIFEST.tables}
    names = synced_tables()

    missing = [name for name in names if name not in shadow_ranks]
    assert not missing, (
        f"synced_tables() 里这些表在 shadow manifest 找不到同名 TableSpec: {missing}"
    )

    ranks = [shadow_ranks[name] for name in names]
    assert ranks == sorted(ranks), (
        "synced_tables() 的顺序与 shadow manifest 的 copy_rank 不是单调一致: "
        f"{list(zip(names, ranks))}"
    )


def test_local_tables_disjoint_from_synced_tables():
    assert set(local_tables()) & set(synced_tables()) == set()
    assert set(local_tables()) | set(synced_tables()) == set(POSTGRES_BUSINESS_TABLES)


def test_spec_for_unknown_table_raises_key_error():
    with pytest.raises(KeyError):
        spec_for("not_a_real_table")


@pytest.mark.parametrize(
    "name,expected_class",
    [
        ("notebooks", SyncClass.SYNCED_WITH_MAPPING),
        ("notebook_members", SyncClass.SYNCED_WITH_MAPPING),
        ("notebook_grants", SyncClass.SYNCED_WITH_MAPPING),
        ("groups", SyncClass.SYNCED_WITH_MAPPING),
        ("group_members", SyncClass.SYNCED_WITH_MAPPING),
        ("sources", SyncClass.SYNCED_WITH_MAPPING),
        ("knowhow_cell_code", SyncClass.SYNCED_WITH_MAPPING),
        ("knowhow_changes", SyncClass.SYNCED_WITH_MAPPING),
        ("knowhow_milestones", SyncClass.SYNCED_WITH_MAPPING),
        ("memory_items", SyncClass.SYNCED_WITH_MAPPING),
        ("object_schemas", SyncClass.SYNCED),
        ("chunks", SyncClass.SYNCED),
        ("chunk_embeddings", SyncClass.SYNCED),
        ("knowledge_objects", SyncClass.SYNCED),
        ("knowhow_cells", SyncClass.SYNCED),
        ("unified_kg_state", SyncClass.SYNCED),
        ("conversations", SyncClass.LOCAL),
        ("answers", SyncClass.LOCAL),
        ("ask_jobs", SyncClass.LOCAL),
        ("reports", SyncClass.LOCAL),
        ("users", SyncClass.LOCAL),
        ("global_ask_jobs", SyncClass.LOCAL),
    ],
)
def test_key_table_classifications_are_pinned(name, expected_class):
    """防止将来误改这些表的归属; 改动必须同 PR 更新设计文档。"""
    assert spec_for(name).sync_class is expected_class


# ------------------------------------------------ 声明的列在真实 schema 里存在


@pytest.fixture(scope="module")
def sqlite_table_columns(_sqlite_schema_template) -> dict[str, set[str]]:
    """columns[table] = set of column names, read from the session's current
    SQLite schema template (PRAGMA table_info) -- proves every name
    SYNC_MANIFEST declares in mapped_columns/severed_columns/
    target_owned_columns is a real column, not a typo. Reads conftest's
    immutable template instead of running SqliteMigrator itself: the
    migration ladder is a contract owned by _REAL_SQLITE_MIGRATION_MODULES
    (tests/test_test_architecture_policy.py), not by this guard."""
    conn = sqlite3.connect(f"file:{_sqlite_schema_template}?mode=ro", uri=True)
    try:
        columns: dict[str, set[str]] = {}
        for spec in SYNC_MANIFEST:
            rows = conn.execute(f"PRAGMA table_info({spec.name})").fetchall()
            columns[spec.name] = {row[1] for row in rows}
    finally:
        conn.close()
    return columns


def test_declared_columns_exist_in_sqlite_schema(sqlite_table_columns):
    problems: list[str] = []
    for spec in SYNC_MANIFEST:
        real_columns = sqlite_table_columns.get(spec.name)
        if not real_columns:
            problems.append(f"{spec.name}: 表在迁移后的 SQLite schema 里不存在")
            continue
        for mapped in spec.mapped_columns:
            if mapped.name not in real_columns:
                problems.append(
                    f"{spec.name}.{mapped.name} (mapped_columns) 不是真实列"
                )
        for column in spec.severed_columns:
            if column not in real_columns:
                problems.append(
                    f"{spec.name}.{column} (severed_columns) 不是真实列"
                )
        for column in spec.target_owned_columns:
            if column not in real_columns:
                problems.append(
                    f"{spec.name}.{column} (target_owned_columns) 不是真实列"
                )
    assert not problems, "; ".join(problems)


# --------------------------------------------------- 外键父表先于子表 (copy_rank)


_MIGRATIONS_DIR = (
    pathlib.Path(sync_manifest_module.__file__).resolve().parents[2]
    / "repositories"
    / "postgres"
    / "migrations"
)


def _strip_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def _parse_foreign_key_edges(migrations_dir: pathlib.Path) -> set[tuple[str, str]]:
    """Return {(child_table, parent_table)} for every ``REFERENCES`` in every
    ``*.sql`` migration, whether it is a table-level ``FOREIGN KEY (...)
    REFERENCES parent(...)`` (inline in ``CREATE TABLE`` or in a later
    ``ALTER TABLE ... ADD CONSTRAINT``) or a column-level ``col ...
    REFERENCES parent(id)`` shorthand. Self-references are dropped."""
    edges: set[tuple[str, str]] = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        text = _strip_sql_comments(path.read_text())
        for statement in text.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            create_match = re.match(r"CREATE TABLE (\w+)\s*\(", stripped)
            alter_match = re.match(r"ALTER TABLE (\w+)\b", stripped)
            if create_match:
                child = create_match.group(1)
            elif alter_match and "REFERENCES" in stripped.upper():
                child = alter_match.group(1)
            else:
                continue
            for parent_match in re.finditer(r"REFERENCES\s+(\w+)", stripped):
                parent = parent_match.group(1)
                if parent != child:
                    edges.add((child, parent))
    return edges


def test_foreign_key_parents_have_lower_copy_rank_than_children_when_both_synced():
    assert _MIGRATIONS_DIR.is_dir(), f"未找到迁移目录: {_MIGRATIONS_DIR}"

    synced = set(synced_tables())
    shadow_ranks = {spec.name: spec.copy_rank for spec in SHADOW_MANIFEST.tables}
    edges = _parse_foreign_key_edges(_MIGRATIONS_DIR)

    assert edges, "外键解析结果为空，说明解析逻辑本身坏了（应有一百多条边）"

    violations = []
    for child, parent in sorted(edges):
        if child not in synced or parent not in synced:
            continue
        if shadow_ranks[parent] >= shadow_ranks[child]:
            violations.append(
                (child, parent, shadow_ranks[child], shadow_ranks[parent])
            )

    assert not violations, (
        "以下外键的父表 copy_rank 没有先于子表 "
        "(child, parent, rank(child), rank(parent)): "
        f"{violations}"
    )


# --------------------------------------------------------------- import 白名单


_ALLOWED_EXACT_MODULES = {
    "app.repositories.postgres.schema_manifest",
    "app.migration.shadow.manifest",
}


def _import_is_allowed(module: str) -> bool:
    top_level = module.split(".", 1)[0]
    if top_level in sys.stdlib_module_names:
        return True
    if module in _ALLOWED_EXACT_MODULES:
        return True
    if module == "app.migration.sync" or module.startswith("app.migration.sync."):
        return True
    return False


def test_sync_package_only_imports_the_whitelist():
    sync_pkg_dir = pathlib.Path(sync_manifest_module.__file__).resolve().parent
    violations: list[str] = []
    for path in sorted(sync_pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _import_is_allowed(alias.name):
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import inside the package: always fine
                module = node.module or ""
                if not _import_is_allowed(module):
                    violations.append(f"{path.name}: from {module} import ...")
    assert not violations, (
        "backend/app/migration/sync/*.py 只允许标准库 + "
        f"{sorted(_ALLOWED_EXACT_MODULES)} + app.migration.sync.* 的 import; "
        f"违规: {violations}"
    )


# --- identity.build_user_mapping ---------------------------------------


def _user(id_: str, username: str, display_name: str = "", role: str = "member"):
    return UserProjection(
        id=id_, username=username, display_name=display_name or username, role=role
    )


def test_build_user_mapping_all_matched():
    source = [_user("s1", "alice"), _user("s2", "bob")]
    target = [_user("t1", "alice"), _user("t2", "bob")]

    result = build_user_mapping(source, target)

    assert result == UserMapping(matched={"s1": "t1", "s2": "t2"}, unmatched=())


def test_build_user_mapping_partial_match():
    source = [_user("s1", "alice"), _user("s2", "carol")]
    target = [_user("t1", "alice")]

    result = build_user_mapping(source, target)

    assert result.matched == {"s1": "t1"}
    assert result.unmatched == (_user("s2", "carol"),)


def test_build_user_mapping_empty_username_is_always_unmatched():
    source = [_user("s1", ""), _user("s2", "bob")]
    target = [_user("t1", ""), _user("t2", "bob")]

    result = build_user_mapping(source, target)

    assert result.matched == {"s2": "t2"}
    assert result.unmatched == (_user("s1", ""),)


def test_build_user_mapping_source_duplicate_username_raises():
    source = [_user("s1", "alice"), _user("s2", "alice")]
    target = [_user("t1", "alice")]

    with pytest.raises(ValueError, match="alice"):
        build_user_mapping(source, target)


def test_build_user_mapping_target_duplicate_username_raises():
    source = [_user("s1", "alice")]
    target = [_user("t1", "alice"), _user("t2", "alice")]

    with pytest.raises(ValueError, match="alice"):
        build_user_mapping(source, target)


def test_build_user_mapping_username_is_case_sensitive_and_not_stripped():
    source = [_user("s1", "Alice"), _user("s2", " bob")]
    target = [_user("t1", "alice"), _user("t2", "bob")]

    result = build_user_mapping(source, target)

    assert result.matched == {}
    assert {u.username for u in result.unmatched} == {"Alice", " bob"}


def test_build_user_mapping_matched_is_not_a_plain_mutable_dict():
    result = build_user_mapping([_user("s1", "alice")], [_user("t1", "alice")])

    with pytest.raises(TypeError):
        result.matched["s1"] = "somewhere-else"
