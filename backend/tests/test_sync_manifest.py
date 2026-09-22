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
    ScopeKind,
    SyncClass,
    TableScope,
    local_tables,
    scope_chain,
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


def test_local_tables_have_no_optional_refs():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.LOCAL:
            assert not spec.optional_refs, (
                f"{spec.name} 是 LOCAL 但 optional_refs 非空: {spec.optional_refs}"
            )


def test_local_tables_have_no_scope():
    for spec in SYNC_MANIFEST:
        if spec.sync_class is SyncClass.LOCAL:
            assert spec.scope is None, f"{spec.name} 是 LOCAL 但 scope 非 None: {spec.scope}"


def test_synced_layer_tables_have_a_scope():
    for spec in SYNC_MANIFEST:
        if spec.sync_class in (SyncClass.SYNCED, SyncClass.SYNCED_WITH_MAPPING):
            assert spec.scope is not None, f"{spec.name}: 同步层/边界层表必须设置 scope"


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
        "object_schemas",
    }


def test_optional_refs_registry_is_pinned():
    optional_refs = {
        spec.name: spec.optional_refs for spec in SYNC_MANIFEST if spec.optional_refs
    }
    assert optional_refs == {
        "notebook_bases": (("base_notebook_id", "notebooks"),),
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


# ------------------------------------------------------------------- scope


def _synced_layer_specs():
    return [
        spec
        for spec in SYNC_MANIFEST
        if spec.sync_class in (SyncClass.SYNCED, SyncClass.SYNCED_WITH_MAPPING)
    ]


def _parent_scoped_specs():
    return [spec for spec in _synced_layer_specs() if spec.scope.kind is ScopeKind.PARENT]


def test_global_scope_registry_is_pinned():
    """The only three synced-layer tables whose scope is GLOBAL (not scoped
    to a single notebook, see their TableSyncSpec.notes): groups/
    group_members (scoped by the authorization edges of the notebooks being
    exported) and object_schemas (an environment-global registry owned by
    no single notebook -- its only writer hardcodes notebook_id='' and its
    reads never filter by notebook; see migrations/
    0025_notebook_object_schemas.sql, which already moved every non-empty-
    notebook_id row out into notebook_object_schemas). Any other synced/
    synced_with_mapping table landing here by accident (e.g. a NOTEBOOK
    table someone forgot to set scope=... on) must fail this test."""
    global_names = {
        spec.name for spec in _synced_layer_specs() if spec.scope.kind is ScopeKind.GLOBAL
    }
    assert global_names == {"groups", "group_members", "object_schemas"}


def test_notebook_scope_column_is_notebook_id_except_the_notebooks_root():
    for spec in _synced_layer_specs():
        if spec.scope.kind is not ScopeKind.NOTEBOOK:
            continue
        if spec.name == "notebooks":
            assert spec.scope.column == "id", (
                "notebooks is the root of the scope chain: its own primary "
                "key answers 'which notebook', not a notebook_id column"
            )
        else:
            assert spec.scope.column == "notebook_id", spec.name


def test_notebook_scope_column_exists_in_sqlite_schema(sqlite_table_columns):
    for spec in _synced_layer_specs():
        if spec.scope.kind is not ScopeKind.NOTEBOOK:
            continue
        real_columns = sqlite_table_columns.get(spec.name, set())
        assert spec.scope.column in real_columns, (
            f"{spec.name}.{spec.scope.column} (NOTEBOOK scope) 不是真实列"
        )


def test_notebook_scope_column_is_not_null_without_default():
    """NOTEBOOK scope means "this column reliably names the row's
    notebook". A nullable or DEFAULTed notebook_id can't back that promise
    -- a DEFAULT lets rows exist that were never actually given a real
    notebook while still satisfying NOT NULL. object_schemas.notebook_id is
    exactly that shape (``text NOT NULL DEFAULT ''``, see
    0001_initial.sql) and was wrongly scoped NOTEBOOK before this revision
    (it is GLOBAL now, see test_global_scope_registry_is_pinned) -- this
    guard is what would have caught that misclassification: it is the only
    synced-layer table whose notebook_id column has a DEFAULT. notebooks
    itself is exempt: it has no notebook_id column at all, its scope column
    is "id"."""
    definitions = _parse_notebook_id_column_definitions(_MIGRATIONS_DIR)
    problems: list[str] = []
    for spec in _synced_layer_specs():
        if spec.scope.kind is not ScopeKind.NOTEBOOK or spec.name == "notebooks":
            continue
        definition = definitions.get(spec.name)
        if definition is None:
            problems.append(f"{spec.name}: 找不到 notebook_id 列定义")
            continue
        upper = definition.upper()
        if "NOT NULL" not in upper:
            problems.append(f"{spec.name}.notebook_id 不是 NOT NULL: {definition!r}")
        if "DEFAULT" in upper:
            problems.append(
                f"{spec.name}.notebook_id 带 DEFAULT，不能作为可靠的归属键: {definition!r}"
            )
    assert not problems, "; ".join(problems)


def test_parent_scope_column_exists_in_sqlite_schema(sqlite_table_columns):
    for spec in _parent_scoped_specs():
        real_columns = sqlite_table_columns.get(spec.name, set())
        assert spec.scope.column in real_columns, (
            f"{spec.name}.{spec.scope.column} (PARENT scope) 不是真实列"
        )


def test_parent_scope_table_is_non_empty_and_paired_with_a_column():
    for spec in _parent_scoped_specs():
        assert spec.scope.column, f"{spec.name}: PARENT scope 缺列名"
        assert spec.scope.parent_table, f"{spec.name}: PARENT scope 缺父表"


def test_parent_scope_parent_table_is_in_the_synced_layer():
    synced_layer_names = {spec.name for spec in _synced_layer_specs()}
    for spec in _parent_scoped_specs():
        assert spec.scope.parent_table in synced_layer_names, (
            f"{spec.name}: PARENT scope 的父表 {spec.scope.parent_table!r} "
            "不在同步层/边界层里"
        )


def test_parent_scope_edge_is_a_declared_foreign_key():
    """(child, column, parent) for every PARENT-scoped table must be a real
    ``REFERENCES`` edge in the PG migrations -- proves ``scope`` did not just
    invent a relationship, and that it names the right column (not merely
    some column pointing at the right parent). knowhow_changes.table_id and
    knowhow_milestones.table_id both declare
    ``REFERENCES knowhow_tables(id)`` (0008_master_v28_features.sql), so no
    exemption is needed here: every PARENT edge in the current manifest is a
    declared FK."""
    edges = _parse_foreign_key_edges(_MIGRATIONS_DIR)
    missing = [
        (spec.name, spec.scope.column, spec.scope.parent_table)
        for spec in _parent_scoped_specs()
        if (spec.name, spec.scope.column, spec.scope.parent_table) not in edges
    ]
    assert not missing, (
        f"这些 PARENT scope 边在 PG 迁移里没有声明为外键: {missing}"
    )


def test_scope_chain_succeeds_for_every_non_global_synced_table():
    """scope_chain() must resolve to a NOTEBOOK table (no exception) for
    every synced-layer table that isn't itself GLOBAL -- proves there is no
    dangling PARENT edge, no cycle, and no chain over 4 hops anywhere in the
    real manifest, not just in the two constructed cases below."""
    for spec in _synced_layer_specs():
        if spec.scope.kind is ScopeKind.GLOBAL:
            continue
        scope_chain(spec.name)  # must not raise


def test_scope_chain_knowhow_cell_code_walks_to_knowhow_tables():
    assert scope_chain("knowhow_cell_code") == (
        ("knowhow_cell_code", "row_id", "knowhow_rows"),
        ("knowhow_rows", "table_id", "knowhow_tables"),
    )


def test_scope_chain_notebook_scoped_table_is_empty():
    assert scope_chain("chunks") == ()


def test_scope_chain_global_table_raises():
    with pytest.raises(ValueError):
        scope_chain("groups")


class _FakeSpec:
    def __init__(self, scope):
        self.scope = scope


def _patch_by_name(monkeypatch, fake_by_name: dict) -> None:
    monkeypatch.setattr(
        sync_manifest_module,
        "_BY_NAME",
        {**sync_manifest_module._BY_NAME, **fake_by_name},
    )


def test_scope_chain_cycle_raises(monkeypatch):
    fake_by_name = {
        "a": _FakeSpec(TableScope(ScopeKind.PARENT, column="b_id", parent_table="b")),
        "b": _FakeSpec(TableScope(ScopeKind.PARENT, column="a_id", parent_table="a")),
    }
    _patch_by_name(monkeypatch, fake_by_name)
    with pytest.raises(ValueError, match="cycles back"):
        scope_chain("a")


def test_scope_chain_too_long_raises(monkeypatch):
    fake_by_name = {
        "p0": _FakeSpec(TableScope(ScopeKind.PARENT, column="p1_id", parent_table="p1")),
        "p1": _FakeSpec(TableScope(ScopeKind.PARENT, column="p2_id", parent_table="p2")),
        "p2": _FakeSpec(TableScope(ScopeKind.PARENT, column="p3_id", parent_table="p3")),
        "p3": _FakeSpec(TableScope(ScopeKind.PARENT, column="p4_id", parent_table="p4")),
        "p4": _FakeSpec(TableScope(ScopeKind.PARENT, column="p5_id", parent_table="p5")),
        "p5": _FakeSpec(TableScope(ScopeKind.NOTEBOOK, column="notebook_id")),
    }
    _patch_by_name(monkeypatch, fake_by_name)
    with pytest.raises(ValueError, match="exceeds 4 hops"):
        scope_chain("p0")


def test_scope_chain_unknown_parent_table_raises_valueerror_not_keyerror(monkeypatch):
    """spec_for's bare KeyError for a parent_table absent from the manifest
    must come out of scope_chain as a point-named ValueError, not leak
    through unconverted."""
    fake_by_name = {
        "q0": _FakeSpec(
            TableScope(ScopeKind.PARENT, column="ghost_id", parent_table="does_not_exist")
        ),
    }
    _patch_by_name(monkeypatch, fake_by_name)
    with pytest.raises(ValueError, match="unknown table"):
        scope_chain("q0")


def test_scope_chain_local_table_raises(monkeypatch):
    fake_by_name = {"local0": _FakeSpec(None)}
    _patch_by_name(monkeypatch, fake_by_name)
    with pytest.raises(ValueError, match="LOCAL"):
        scope_chain("local0")


def test_new_sync_control_tables_are_registered_local():
    """sync_export_state/sync_imports/sync_import_progress: PR-2 同步控制表，
    由并行任务落地 migrations/schema_manifest/shadow manifest/fixtures；这里
    预先把它们登记为 LOCAL，钉死不能被误分类为同步层。"""
    for name in ("sync_export_state", "sync_imports", "sync_import_progress"):
        assert spec_for(name).sync_class is SyncClass.LOCAL, name


def test_parent_table_sorts_before_child_in_synced_tables_order():
    order = {name: index for index, name in enumerate(synced_tables())}
    violations = [
        (spec.name, spec.scope.parent_table)
        for spec in _parent_scoped_specs()
        if spec.name in order
        and spec.scope.parent_table in order
        and order[spec.scope.parent_table] >= order[spec.name]
    ]
    assert not violations, (
        "这些 PARENT scope 表的父表在 synced_tables() 里没有排在它前面 "
        f"(child, parent): {violations}"
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


# ----------------------------------------- key <-> 目标唯一面 (SQLite 模板侧)


def _sqlite_catalog_primary_key(
    conn: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    keyed = sorted((row[5], row[1]) for row in rows if row[5] > 0)
    return tuple(name for _position, name in keyed)


def _sqlite_unique_column_sets(
    conn: sqlite3.Connection, table: str
) -> list[frozenset[str]]:
    """Every column set the SQLite catalog enforces as unique for ``table``:
    the primary key (if any) plus every non-partial UNIQUE index. Mirrors
    ``app.migration.sync.export._Source._has_unique_surface`` at test time,
    over the SESSION'S SCHEMA TEMPLATE rather than a live export/import run
    -- see ``sqlite_table_columns`` above for why this fixture reads the
    template instead of running the migration ladder itself."""
    sets: list[frozenset[str]] = []
    pk = _sqlite_catalog_primary_key(conn, table)
    if pk:
        sets.append(frozenset(pk))
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        # index_list columns: seq, name, unique, origin, partial.
        if not index[2] or (len(index) > 4 and index[4]):
            continue
        columns = frozenset(
            row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
        )
        sets.append(columns)
    return sets


def test_registered_sync_key_has_a_covering_unique_surface_in_sqlite(
    _sqlite_schema_template,
):
    """A registered ``TableSyncSpec.key`` is only a usable row identity if the
    LIVE SQLite schema actually enforces it is unique --
    ``app.migration.sync.export._Source.sync_key`` re-checks exactly this at
    runtime (SQLite cannot add a primary key to
    knowledge_object_sources/community_members in place, so v84 backs their
    registered key with a UNIQUE index instead), and this guard catches the
    same drift at test time rather than only when an export/import runs.
    A table with NO registered key must instead have a real catalog primary
    key -- the manifest's ``key`` fallback exists only for the tables that
    declare one.
    """
    conn = sqlite3.connect(f"file:{_sqlite_schema_template}?mode=ro", uri=True)
    try:
        problems: list[str] = []
        for table in synced_tables():
            spec = spec_for(table)
            unique_sets = _sqlite_unique_column_sets(conn, table)
            if spec.key:
                if frozenset(spec.key) not in unique_sets:
                    problems.append(
                        f"{table}: TableSyncSpec.key {spec.key} has no covering "
                        "unique index/constraint in the SQLite template (found "
                        f"{[sorted(s) for s in unique_sets]})"
                    )
            elif not _sqlite_catalog_primary_key(conn, table):
                problems.append(
                    f"{table}: no TableSyncSpec.key and no catalog primary key "
                    "in the SQLite template -- the sync layer has no row "
                    "identity for it"
                )
    finally:
        conn.close()
    assert not problems, "; ".join(problems)


def test_the_sqlite_primary_key_matches_the_row_key_column_for_column(
    _sqlite_schema_template,
):
    """两端主键平价, **含顺序**。

    ``app.migration.sync.capture.key_columns`` 解析的是 PostgreSQL 迁移 DDL 里
    的主键(没登记 ``TableSyncSpec.key`` 时), 捕获触发器按那个顺序拼 ``key_json``
    —— 两端各自建库, 所以「只改了一端主键」的迁移在别处没有任何东西会红: PG 侧
    照常跑, SQLite 侧照常跑, 只有跨环境同步时目标端按另一种身份找行、一行也匹配
    不上。顺序同理: 同一列集换个次序就是另一个 JSON 对象。

    只覆盖**未登记 key** 的表 —— 登记了 key 的两张表在 SQLite 上根本没有主键
    (v84 用唯一索引承担), 它们的平价由上一条守卫按列集比对。
    """
    from app.migration.sync.capture import key_columns

    conn = sqlite3.connect(f"file:{_sqlite_schema_template}?mode=ro", uri=True)
    try:
        problems: list[str] = []
        for table in synced_tables():
            if spec_for(table).key:
                continue
            sqlite_key = _sqlite_catalog_primary_key(conn, table)
            row_key = key_columns(table)
            if sqlite_key != row_key:
                problems.append(
                    f"{table}: SQLite primary key {sqlite_key} != sync row key "
                    f"{row_key} (parsed from the PostgreSQL migration DDL)"
                )
    finally:
        conn.close()
    assert not problems, "; ".join(problems)


def test_registered_sync_key_guard_actually_detects_a_missing_unique_surface():
    """Mutation verification for the guard above: build an in-memory SQLite
    table shaped like a registered-key table but WITHOUT the unique index
    that is supposed to back it, and confirm ``_sqlite_unique_column_sets``
    does not manufacture a match -- i.e. that the guard would actually catch
    a real drift rather than passing vacuously no matter what the catalog
    says."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a TEXT NOT NULL, b TEXT NOT NULL)")
        # No unique index at all yet: the declared key must not be reported
        # as covered.
        assert frozenset(("a", "b")) not in _sqlite_unique_column_sets(conn, "t")
        # A unique index over a DIFFERENT column set must not count either.
        conn.execute("CREATE UNIQUE INDEX uq_t_a ON t(a)")
        assert frozenset(("a", "b")) not in _sqlite_unique_column_sets(conn, "t")
        # Only the matching unique index makes it pass.
        conn.execute("CREATE UNIQUE INDEX uq_t_ab ON t(a, b)")
        assert frozenset(("a", "b")) in _sqlite_unique_column_sets(conn, "t")
    finally:
        conn.close()


# --------------------------------------------------- 外键父表先于子表 (copy_rank)


_MIGRATIONS_DIR = (
    pathlib.Path(sync_manifest_module.__file__).resolve().parents[2]
    / "repositories"
    / "postgres"
    / "migrations"
)


def _strip_sql_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


def _parse_foreign_key_edges(migrations_dir: pathlib.Path) -> set[tuple[str, str, str]]:
    """Return {(child_table, column, parent_table)} for every declared
    foreign key in every ``*.sql`` migration: both the table-level
    ``FOREIGN KEY (col) REFERENCES parent`` form (inline in ``CREATE TABLE``
    or in a later ``ALTER TABLE ... ADD CONSTRAINT``) and the column-level
    ``col ... REFERENCES parent(id)`` shorthand. Self-references are
    dropped."""
    edges: set[tuple[str, str, str]] = set()
    fk_form = re.compile(r"FOREIGN KEY\s*\(\s*(\w+)\s*\)\s*REFERENCES\s+(\w+)")
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
            for column, parent in fk_form.findall(stripped):
                if parent != child:
                    edges.add((child, column, parent))
            # Column-level shorthand: "col type ... REFERENCES parent(...)"
            # all on one physical line, with no "FOREIGN KEY" keyword (which
            # would instead be caught -- possibly wrapped onto the next
            # line -- by fk_form above; skipping any line containing it
            # here avoids misreading "FOREIGN" itself as a column name).
            for line in stripped.splitlines():
                if "FOREIGN KEY" in line:
                    continue
                shorthand = re.match(r"\s*(\w+)\s+\S.*\bREFERENCES\s+(\w+)\s*\(", line)
                if shorthand:
                    column, parent = shorthand.group(1), shorthand.group(2)
                    if parent != child:
                        edges.add((child, column, parent))
    return edges


def _parse_notebook_id_column_definitions(migrations_dir: pathlib.Path) -> dict[str, str]:
    """Return {table: raw column-definition text} for every table's own
    ``notebook_id`` column, read straight out of its ``CREATE TABLE`` body.
    Used by test_notebook_scope_column_is_not_null_without_default to prove
    a NOTEBOOK-scoped table's notebook_id can't silently mean "no real
    notebook" (NOT NULL with no DEFAULT)."""
    definitions: dict[str, str] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        text = _strip_sql_comments(path.read_text())
        for statement in text.split(";"):
            stripped = statement.strip()
            create_match = re.match(r"CREATE TABLE (\w+)\s*\(", stripped)
            if not create_match:
                continue
            table = create_match.group(1)
            for line in stripped.splitlines():
                m = re.match(r"\s*notebook_id\s+(.*)$", line)
                if m:
                    definitions[table] = m.group(1)
    return definitions


def test_foreign_key_parents_have_lower_copy_rank_than_children_when_both_synced():
    assert _MIGRATIONS_DIR.is_dir(), f"未找到迁移目录: {_MIGRATIONS_DIR}"

    synced = set(synced_tables())
    shadow_ranks = {spec.name: spec.copy_rank for spec in SHADOW_MANIFEST.tables}
    edges = _parse_foreign_key_edges(_MIGRATIONS_DIR)

    assert edges, "外键解析结果为空，说明解析逻辑本身坏了（应有一百多条边）"

    violations = []
    for child, _column, parent in sorted(edges):
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
    # PR-2 (app.migration.sync.export): an export runs as an offline tool
    # against a quiesced database, so it composes the two Database classes
    # itself instead of a repository facade. These five are the entire seam
    # that needs -- backend selection, the two connection sources, the
    # PG->SQLite raw-row projection every other PG raw-row port already uses,
    # and Settings for typing. Services and repository facades stay out.
    "app.core.config",
    "app.core.database_url",
    "app.migration.shadow.postgres_catalog",
    "app.repositories.postgres.database",
    "app.repositories.sqlite.database",
    # PR-2 (app.migration.sync.import_): the importer converts the package's
    # SQLite-shaped values into typed PostgreSQL parameters with the shadow
    # transform, and marks imported notebooks as mirrors through the two
    # backends' sharing stores (the only writers of notebooks.sync_origin).
    "app.migration.shadow.transform",
    "app.repositories.postgres.sharing_store",
    "app.repositories.sqlite.sharing_store",
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
