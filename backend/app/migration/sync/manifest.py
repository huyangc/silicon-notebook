"""Sync-scope manifest: the single source of truth for which business tables
cross-environment sync touches, and how (docs/incremental-sync-design.md §3).

``SYNC_MANIFEST`` classifies every table in
``app.repositories.postgres.schema_manifest.POSTGRES_BUSINESS_TABLES`` into
exactly one of three classes:

- ``SYNCED``: copied verbatim, no row content needs identity remapping.
- ``SYNCED_WITH_MAPPING``: copied, but one or more columns reference a user,
  group, or polymorphic principal id that must be rewritten through the
  target environment's identity mapping on import (see
  ``app.migration.sync.identity``). ``mapped_columns`` is non-empty exactly
  when a table is in this class.
- ``LOCAL``: never leaves its environment (interactive/runtime/system data).

More axes cut across those three classes:

- ``severed_columns``: columns that reference a row in a LOCAL table (so the
  reference cannot survive the trip) and must be set to NULL/empty on
  import, independent of ``sync_class``. Never non-empty for LOCAL tables,
  since LOCAL tables are never imported at all.
- ``optional_refs``: columns that reference a row in another synced table,
  but where that reference is allowed to dangle -- if the referenced row is
  missing at the target, import skips the row and logs it instead of
  failing. ``(column, referenced_table)`` pairs. Empty for LOCAL tables.
- ``seed_only``: for a target-side row that already exists (matched by
  primary key), import never overwrites or deletes it; a row that does not
  exist yet is inserted. For the notebook/group membership tables this
  means "only written the first time the parent notebook/group is created
  at the target, never touched again by a re-sync" (the target owns
  membership/sharing decisions made after that first import); for a
  table like ``object_schemas`` that is not owned by any one notebook, it
  is ordinary idempotent-by-primary-key insert. Always False for LOCAL
  tables.
- ``scope``: how a table's rows are attributed to a notebook -- see
  ``TableScope`` and ``scope_chain`` below. ``None`` for LOCAL tables
  (never exported, so the question does not apply); every SYNCED/
  SYNCED_WITH_MAPPING table must set a real one.
- ``key``: the row-identity columns for the two synced tables that carry no
  PostgreSQL primary key of their own. Empty everywhere else, meaning "the
  catalog primary key is the row identity" -- see
  ``app.migration.sync.capture.key_columns``.

A guard test (``tests/test_sync_manifest.py``) asserts this manifest is total
over ``POSTGRES_BUSINESS_TABLES``: every business table must have exactly one
``TableSyncSpec`` here, and a newly added business table that has not been
classified fails that test loudly rather than silently defaulting to any one
class. When you add a business table, add its ``TableSyncSpec`` here in the
same change and update docs/incremental-sync-design.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.migration.shadow.manifest import MANIFEST as _SHADOW_MANIFEST
from app.repositories.postgres.schema_manifest import POSTGRES_BUSINESS_TABLES


class SyncClass(StrEnum):
    SYNCED = "synced"
    SYNCED_WITH_MAPPING = "synced_with_mapping"
    LOCAL = "local"


class ScopeKind(StrEnum):
    # The table carries its own ``notebook_id`` column -- the row's notebook
    # is read straight off the row.
    NOTEBOOK = "notebook"
    # The table has no ``notebook_id`` column of its own; its notebook is
    # found by following ``column`` to a row in ``parent_table``. The parent
    # may itself be PARENT-scoped (e.g. knowhow_cells -> knowhow_rows ->
    # knowhow_tables), so resolving a row's notebook can take more than one
    # hop -- see ``scope_chain``.
    PARENT = "parent"
    # The table's rows are not scoped to a single notebook at all; which
    # rows an export needs is decided some other way (see each GLOBAL
    # table's ``notes``). LOCAL tables use ``scope=None`` instead of GLOBAL
    # -- see ``TableSyncSpec.scope`` below.
    GLOBAL = "global"


class MappingKind(StrEnum):
    # Column holds a users.id; rewritten through app.migration.sync.identity
    # username-based UserMapping.
    USER = "user"
    # Column holds a groups.id; rewritten through a name-based group mapping
    # (group matched by name, created if absent).
    GROUP = "group"
    # Column is polymorphic (its meaning depends on a sibling column in the
    # same row). Currently only notebook_grants.principal_id, whose target
    # table is chosen by that row's principal_type.
    PRINCIPAL = "principal"


@dataclass(frozen=True)
class MappedColumn:
    name: str
    kind: MappingKind


@dataclass(frozen=True)
class TableScope:
    kind: ScopeKind
    # NOTEBOOK: the table's own notebook-id column (always "notebook_id",
    # except for ``notebooks`` itself, whose row *is* the notebook and whose
    # column is "id"). PARENT: the column on this table that points at
    # ``parent_table``'s primary key. Empty for GLOBAL.
    column: str = ""
    # PARENT only: the table ``column`` points into. Empty otherwise.
    parent_table: str = ""

    def __post_init__(self) -> None:
        """Shape validation per ``kind`` -- catches a scope that sets the
        wrong fields for its own kind at construction time, rather than
        letting it silently pass through as inert data until some guard
        test happens to notice."""
        if self.kind is ScopeKind.PARENT:
            if not self.column or not self.parent_table:
                raise ValueError(
                    "PARENT TableScope requires both column and parent_table, "
                    f"got column={self.column!r} parent_table={self.parent_table!r}"
                )
        elif self.kind is ScopeKind.NOTEBOOK:
            if not self.column:
                raise ValueError("NOTEBOOK TableScope requires column")
            if self.parent_table:
                raise ValueError(
                    "NOTEBOOK TableScope must not set parent_table, "
                    f"got parent_table={self.parent_table!r}"
                )
        elif self.kind is ScopeKind.GLOBAL:
            if self.column or self.parent_table:
                raise ValueError(
                    "GLOBAL TableScope must not set column or parent_table, "
                    f"got column={self.column!r} parent_table={self.parent_table!r}"
                )


# The common case: a table with its own "notebook_id" column.
_NOTEBOOK_SCOPE = TableScope(ScopeKind.NOTEBOOK, column="notebook_id")

# GLOBAL for the two tables whose rows are not scoped to a single notebook:
# groups/group_members are scoped by the *set of groups* referenced by the
# notebook_grants rows of the notebook(s) being exported, not by a column on
# groups/group_members themselves -- see their TableSyncSpec.notes.
_GLOBAL_SCOPE = TableScope(ScopeKind.GLOBAL)


@dataclass(frozen=True)
class TableSyncSpec:
    name: str
    sync_class: SyncClass
    # Columns that hold a user/group/principal id and must be rewritten
    # through the identity mapping on import. Non-empty exactly for
    # SYNCED_WITH_MAPPING tables.
    mapped_columns: tuple[MappedColumn, ...] = ()
    # Columns that reference a row in a LOCAL table; import must set them to
    # NULL/empty rather than carry the source-side id across. Independent of
    # sync_class (a SYNCED table can have one); always empty for LOCAL
    # tables, which are never imported.
    severed_columns: tuple[str, ...] = ()
    # Columns that reference a row in another SYNCED/SYNCED_WITH_MAPPING
    # table, but where that reference is allowed to dangle: if the
    # referenced row does not exist at the target after import, the row
    # carrying this column is skipped and logged rather than failing the
    # whole import (unlike an ordinary reference, which the importer expects
    # to resolve). Each entry is ``(column, referenced_table)``. Currently
    # only notebook_bases.base_notebook_id -> notebooks: a notebook can be
    # based on another notebook that was never itself synced to this
    # target. Empty for LOCAL tables (never imported).
    optional_refs: tuple[tuple[str, str], ...] = ()
    # True: for a row that already exists at the target (matched by primary
    # key), import never overwrites or deletes it; a row that does not
    # exist there yet is inserted. For notebook_members/notebook_grants/
    # groups/group_members this means "only written the first time the
    # parent notebook/group is created at the target, never touched again
    # by a re-sync" (the target owns membership/sharing decisions made
    # after that first import). For object_schemas -- an environment-global
    # registry owned by no single notebook -- it means ordinary idempotent-
    # by-primary-key insert: importing a definition the target already has
    # (by object_type) is a no-op. Always False for LOCAL tables.
    seed_only: bool = False
    # Columns that, for a row that already exists at the target (a mirrored
    # notebook being re-synced), are never overwritten by import -- the
    # target environment owns their value. Non-empty only for SYNCED and
    # SYNCED_WITH_MAPPING tables; LOCAL tables are never imported at all, so
    # this must be empty for them.
    target_owned_columns: tuple[str, ...] = ()
    # How this table's rows are attributed to a notebook (docs/incremental-
    # sync-design.md §3 "scope"). The exporter uses it to build each table's
    # notebook-scoped SELECT; the source-side change-capture trigger uses it
    # to resolve the notebook_id to stamp on a change-log row. ``None`` for
    # LOCAL tables (never exported, so the question does not apply); every
    # SYNCED/SYNCED_WITH_MAPPING table must set a real ``TableScope`` --
    # enforced by tests/test_sync_manifest.py, not by this dataclass.
    scope: TableScope | None = None
    # The columns that identify one row of this table, for the tables that
    # have no PostgreSQL primary key of their own. Empty (the normal case)
    # means "use the primary key parsed out of the packaged PostgreSQL
    # migration DDL" -- see app.migration.sync.capture.key_columns, which
    # also refuses a registered key that disagrees with a primary key the
    # catalog does have. SQLite v84 / PostgreSQL 0064 back every registered
    # key with a real unique surface on both backends (a UNIQUE INDEX on
    # SQLite, which cannot add a primary key to an existing table; a PRIMARY
    # KEY on PostgreSQL).
    key: tuple[str, ...] = ()
    notes: str = ""


# --- 3.1 同步层 (SYNCED) -----------------------------------------------

_SYNCED: tuple[TableSyncSpec, ...] = (
    # 笔记本本体
    TableSyncSpec(
        "unified_kg_state",
        SyncClass.SYNCED,
        notes=(
            "kg_mutation_seq 不按普通列原样搬：它是 knowledge_lifecycle.py "
            "_unified_graph_version 缓存版本四元组的一支，原样搬会让目标端凑出"
            "一个自己缓存过的版本、旧图永远命中缓存（codex #772 round 17 "
            "P1）。import_.py::_upsert_statement 对这一列特例：目标端严格推进"
            "到 max(现值, 包内值)+1（首插时是 包内值+1），永不倒退、永不原样"
            "覆盖。同表其它列（含 kg_reset_epoch）仍原样搬。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    # 材料
    TableSyncSpec(
        "source_elements",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="source_id", parent_table="sources"),
    ),
    TableSyncSpec("source_authors", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("source_paper_meta", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("chunks", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("chunk_elements", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("chunk_questions", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("knowledge_source_facts", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec(
        "knowledge_source_fact_elements", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE
    ),
    # 向量
    TableSyncSpec("chunk_embeddings", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("element_embeddings", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("knowledge_embeddings", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("relation_embeddings", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec(
        "memory_embeddings",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="memory_id", parent_table="memory_items"),
    ),
    # KG（knowledge_objects.owner 是治理面板可编辑的自由文本标签，不是用户 id，
    # 不映射；source_candidate_id 指向 LOCAL 的 catalog_candidates，导入时置空）
    TableSyncSpec(
        "knowledge_objects",
        SyncClass.SYNCED,
        severed_columns=("source_candidate_id",),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec("knowledge_relations", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec(
        "knowledge_object_sources",
        SyncClass.SYNCED,
        scope=_NOTEBOOK_SCOPE,
        key=("object_id", "source_id"),
        notes=(
            "0001_initial.sql 没给这张表主键。(object_id, source_id) 就是它的"
            "行身份——notebook_id 由 object_id 决定，不参与identity。v84/0064 "
            "去重后补上唯一面（SQLite 唯一索引 / PG 主键）。"
        ),
    ),
    TableSyncSpec("concept_clusters", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("communities", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec(
        "community_members",
        SyncClass.SYNCED,
        scope=_NOTEBOOK_SCOPE,
        key=("community_id", "canonical_id"),
        notes=(
            "0001_initial.sql 没给这张表主键。行身份取 (community_id, "
            "canonical_id)，与 reap_derived_generations_page 的删除口径一致："
            "community_id 每一代重铸、全库唯一，所以它已经蕴含 notebook_id/"
            "level/generation。shadow manifest 那条三列复制键 (notebook_id, "
            "level, canonical_id) 自 v71 加 generation 后不再唯一，不沿用。"
            "v84/0064 去重后补上唯一面（SQLite 唯一索引 / PG 主键）。"
        ),
    ),
    TableSyncSpec("canonical_relations", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("mention_edges", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("concept_comentions", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("kg_source_profiles", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("kg_community_edges", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    TableSyncSpec("kg_analysis_artifacts", SyncClass.SYNCED, scope=_NOTEBOOK_SCOPE),
    # Knowhow
    TableSyncSpec(
        "knowhow_columns",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="table_id", parent_table="knowhow_tables"),
    ),
    TableSyncSpec(
        "knowhow_rows",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="table_id", parent_table="knowhow_tables"),
    ),
    # row_id, not column_id: every place in the repo that scopes a cell to
    # its table/notebook (knowhow_transfer_store.py, sharing_store.py, both
    # the sqlite/ and postgres/ variants) does
    # "JOIN knowhow_rows r ON r.id = c.row_id" and reads r.table_id off the
    # row side -- row is the path to the parent table, column never is. A
    # cell's row and column belonging to the same knowhow_tables row is
    # guaranteed by how the write path constructs them, not by a DB
    # constraint (no FK ties column_id's table to row_id's table).
    TableSyncSpec(
        "knowhow_cells",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="row_id", parent_table="knowhow_rows"),
    ),
    # 记忆
    TableSyncSpec(
        "memory_provenance",
        SyncClass.SYNCED,
        scope=TableScope(ScopeKind.PARENT, column="memory_id", parent_table="memory_items"),
    ),
    # 环境级自定义类型登记表（services/schema_registry.py）。
    # migrations/0025_notebook_object_schemas.sql 已经把非空 notebook_id 的
    # 行搬进 notebook_object_schemas 并从这张表删除；此后唯一写入方
    # （PostgresKnowledgeStore.insert_custom_schema）硬写 notebook_id=''，
    # 读路径（list_object_schemas 等）也不按 notebook 过滤——它是管理员维护
    # 的、不属于任何单一笔记本的基线，GLOBAL。无用户/组引用列。
    TableSyncSpec(
        "object_schemas",
        SyncClass.SYNCED,
        seed_only=True,
        notes=(
            "导出被导出笔记本的 knowledge_objects.object_type ∪ "
            "notebook_object_schemas.object_type 引用到的行；目标端按主键"
            "（object_type）幂等，已存在不覆盖。"
        ),
        scope=_GLOBAL_SCOPE,
    ),
)

# --- 3.2 边界层 (SYNCED_WITH_MAPPING) -----------------------------------

_SYNCED_WITH_MAPPING: tuple[TableSyncSpec, ...] = (
    TableSyncSpec(
        "notebooks",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        target_owned_columns=("status", "is_shared", "share_token", "sync_origin"),
        notes="created_by 映射不到时硬失败，整个笔记本不导入。",
        # notebooks 自己是根：它没有指向自己的 notebook_id 列，它的主键 id
        # 就是「这一行属于哪个笔记本」的答案。scope_chain() 对 NOTEBOOK 表
        # 返回空元组，不要求 column == "notebook_id"（守卫对 notebooks 单独
        # 放行这一例外）。
        scope=TableScope(ScopeKind.NOTEBOOK, column="id"),
    ),
    TableSyncSpec(
        "notebook_members",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("user_id", MappingKind.USER),),
        seed_only=True,
        notes="映射不到时跳过该行并记日志。种子写入：首次创建笔记本后不再由导入更新/删除。",
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "notebook_grants",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(
            MappedColumn("principal_id", MappingKind.PRINCIPAL),
            MappedColumn("created_by", MappingKind.USER),
        ),
        seed_only=True,
        notes=(
            "principal_id 按行内 principal_type=user/group 选映射表；映射不到"
            "跳过该行并记日志；everyone 不需要映射。created_by 映射不到置为"
            "导入执行者。种子写入：首次创建笔记本后不再由导入更新/删除。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "notebook_bases",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        optional_refs=(("base_notebook_id", "notebooks"),),
        notes=(
            "挂载关系随笔记本同步；created_by 可为空，映射不到置为导入执行者。"
            "base_notebook_id 指向另一本笔记本（0001_initial.sql 声明了硬 FK "
            "fk_notebook_bases_base_notebook_id__notebooks），但那本笔记本"
            "未必也被导出/同步到目标端；导入时目标端不存在该笔记本就跳过"
            "这行并记日志，不算失败。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "notebook_object_schemas",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "notebook_assets",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes=(
            "附件元数据；文件本体在 storage/assets/<notebook_id>/（不是"
            "storage/notebooks/<id>/——那是 sources 上传件的目录，两者由"
            "notebook_catalog.py 删除笔记本时分别清理），导出器单独复制该"
            "目录。映射不到置为导入执行者。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "sources",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("uploaded_by", MappingKind.USER),),
        severed_columns=("agent_profile_id",),
        notes=(
            "uploaded_by 可为空（memory/knowhow 来源的隐藏合成投影本就没有上传"
            "者）；映射不到置空并记日志。agent_profile_id 指向 LOCAL 的"
            "agent_profiles，导入时置空。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "knowhow_tables",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
        scope=_NOTEBOOK_SCOPE,
    ),
    # row_id, not column_id -- same rationale as knowhow_cells above.
    TableSyncSpec(
        "knowhow_cell_code",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("updated_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
        scope=TableScope(ScopeKind.PARENT, column="row_id", parent_table="knowhow_rows"),
    ),
    TableSyncSpec(
        "groups",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(
            MappedColumn("created_by", MappingKind.USER),
            MappedColumn("invite_created_by", MappingKind.USER),
            MappedColumn("owner_id", MappingKind.USER),
        ),
        seed_only=True,
        notes=(
            "组按 name 匹配已有组，匹配不到则创建。invite_created_by 可为空"
            "（无活跃邀请链接时为 NULL），映射不到置空并记日志。owner_id 是组"
            "的现行所有权（0034 迁移，刻意无 FK），映射不到取该组目标端 admin"
            "成员，仍无则置为导入执行者。种子写入：首次创建组后不再由导入"
            "更新/删除。GLOBAL：groups 没有 notebook_id 列，也不经任何单一父"
            "表归属到一个笔记本——一个组可以被多个笔记本的 notebook_grants"
            "引用。导出器按「被导出笔记本集合的 notebook_grants.principal_id"
            "（principal_type=group）引用到的组」这条授权边圈定要带走哪些组，"
            "而不是靠 groups 表自身的某一列。"
        ),
        scope=_GLOBAL_SCOPE,
    ),
    TableSyncSpec(
        "group_members",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(
            MappedColumn("user_id", MappingKind.USER),
            MappedColumn("added_by", MappingKind.USER),
            MappedColumn("group_id", MappingKind.GROUP),
        ),
        seed_only=True,
        notes=(
            "user_id 映射不到跳过该行并记日志；group_id 按组映射（按 name 匹配"
            "/创建）解析所属组。种子写入：首次创建组后不再由导入更新/删除。"
            "GLOBAL：同 groups——按授权边圈定到的组集合决定带走哪些成员行，"
            "不是靠本表自身的列。"
        ),
        scope=_GLOBAL_SCOPE,
    ),
    TableSyncSpec(
        "memory_items",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(
            MappedColumn("created_by", MappingKind.USER),
            MappedColumn("confirmed_by", MappingKind.USER),
        ),
        severed_columns=("agent_profile_id",),
        notes=(
            "created_by 映射不到硬失败；confirmed_by 映射不到置空并记日志。"
            "agent_profile_id 指向 LOCAL 的 agent_profiles，导入时置空。"
            "source_answer_id 指向不同步的 answers，刻意不登记为 severed——"
            "保留这个文本指针供人工排查，是否需要标注「来源在源环境」是设计"
            "文档 §11 的未决问题，不在这里预先决定。"
        ),
        scope=_NOTEBOOK_SCOPE,
    ),
    TableSyncSpec(
        "memory_revisions",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("changed_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
        scope=TableScope(ScopeKind.PARENT, column="memory_id", parent_table="memory_items"),
    ),
    TableSyncSpec(
        "knowhow_milestones",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="编辑历史表，可随开关关闭只同步当前态；映射不到置为导入执行者。",
        scope=TableScope(ScopeKind.PARENT, column="table_id", parent_table="knowhow_tables"),
    ),
    TableSyncSpec(
        "knowhow_changes",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("actor", MappingKind.USER),),
        notes=(
            "编辑历史表，可随开关关闭只同步当前态；用户引用列是 actor（不是"
            "created_by，knowhow_store.py 全程用这个字段名），映射不到置为"
            "导入执行者。"
        ),
        scope=TableScope(ScopeKind.PARENT, column="table_id", parent_table="knowhow_tables"),
    ),
)

# --- 3.3 不同步层 (LOCAL) ------------------------------------------------

_LOCAL: tuple[TableSyncSpec, ...] = (
    # 用户交互
    TableSyncSpec("conversations", SyncClass.LOCAL),
    TableSyncSpec("answers", SyncClass.LOCAL),
    TableSyncSpec("ask_jobs", SyncClass.LOCAL),
    TableSyncSpec("ask_trace_steps", SyncClass.LOCAL),
    TableSyncSpec("feedback", SyncClass.LOCAL),
    TableSyncSpec("reports", SyncClass.LOCAL),
    TableSyncSpec("global_ask_conversations", SyncClass.LOCAL),
    TableSyncSpec("global_ask_jobs", SyncClass.LOCAL),
    TableSyncSpec("retrieval_experiences", SyncClass.LOCAL),
    TableSyncSpec("agent_observations", SyncClass.LOCAL),
    TableSyncSpec("agent_notebook_profile", SyncClass.LOCAL),
    TableSyncSpec("retained_user_activity", SyncClass.LOCAL),
    TableSyncSpec("wishes", SyncClass.LOCAL),
    TableSyncSpec("wish_votes", SyncClass.LOCAL),
    # 运行态与作业
    TableSyncSpec("kg_build_jobs", SyncClass.LOCAL),
    TableSyncSpec("kg_rebuild_checkpoint", SyncClass.LOCAL),
    TableSyncSpec("kg_cluster_scratch", SyncClass.LOCAL),
    TableSyncSpec("kg_canonical_scratch", SyncClass.LOCAL),
    TableSyncSpec("kg_relation_completion_state", SyncClass.LOCAL),
    TableSyncSpec("merge_review_jobs", SyncClass.LOCAL),
    TableSyncSpec("extraction_runs", SyncClass.LOCAL),
    TableSyncSpec("catalog_jobs", SyncClass.LOCAL),
    TableSyncSpec("catalog_candidates", SyncClass.LOCAL),
    TableSyncSpec("promotion_candidates", SyncClass.LOCAL),
    TableSyncSpec("concept_merge_candidates", SyncClass.LOCAL),
    TableSyncSpec("kg_conflict_candidates", SyncClass.LOCAL),
    TableSyncSpec("concept_whitelist", SyncClass.LOCAL),
    TableSyncSpec("indexing_pipeline_stages", SyncClass.LOCAL),
    TableSyncSpec("indexing_pipeline_stage_sources", SyncClass.LOCAL),
    TableSyncSpec("source_index_backfills", SyncClass.LOCAL),
    TableSyncSpec("knowledge_source_fact_backfills", SyncClass.LOCAL),
    TableSyncSpec("chunk_element_backfills", SyncClass.LOCAL),
    TableSyncSpec("notebook_delete_jobs", SyncClass.LOCAL),
    TableSyncSpec("notebook_delete_files", SyncClass.LOCAL),
    TableSyncSpec("agent_profile_jobs", SyncClass.LOCAL),
    TableSyncSpec("notebook_share_requests", SyncClass.LOCAL),
    # 身份与系统
    TableSyncSpec("users", SyncClass.LOCAL),
    TableSyncSpec("user_profiles", SyncClass.LOCAL),
    TableSyncSpec("external_identities", SyncClass.LOCAL),
    TableSyncSpec("auth_sessions", SyncClass.LOCAL),
    TableSyncSpec("auth_policy", SyncClass.LOCAL),
    TableSyncSpec("auth_policy_audit", SyncClass.LOCAL),
    TableSyncSpec("auth_identity_audit", SyncClass.LOCAL),
    TableSyncSpec("auth_transactions", SyncClass.LOCAL),
    TableSyncSpec("agent_profiles", SyncClass.LOCAL),
    TableSyncSpec("agent_access_tokens", SyncClass.LOCAL),
    TableSyncSpec("agent_token_notebooks", SyncClass.LOCAL),
    TableSyncSpec("model_service_status", SyncClass.LOCAL),
    TableSyncSpec("system_model_service_status", SyncClass.LOCAL),
    TableSyncSpec("app_settings", SyncClass.LOCAL),
    TableSyncSpec("extension_runtime_toggles", SyncClass.LOCAL),
    # PR-2 同步控制表：导出水位、导入执行记录、导入进度，环境本地，不随笔记本
    # 同步（预先登记，落地见并行任务：migrations/schema_manifest/shadow
    # manifest/fixtures）。
    TableSyncSpec("sync_export_state", SyncClass.LOCAL, notes="PR-2 同步控制表"),
    TableSyncSpec("sync_imports", SyncClass.LOCAL, notes="PR-2 同步控制表"),
    TableSyncSpec("sync_import_progress", SyncClass.LOCAL, notes="PR-2 同步控制表"),
    # PR-3a 源端变更捕获（SQLite v84 / PostgreSQL 0064）：捕获开关与变更日志。
    # 两张都是本环境自己的记账，绝不随笔记本同步——把源环境的日志搬到目标端，
    # 目标端就会把别人的行变更当成自己的待导出增量。
    TableSyncSpec("sync_capture_control", SyncClass.LOCAL, notes="PR-3a 捕获开关"),
    TableSyncSpec("sync_change_log", SyncClass.LOCAL, notes="PR-3a 变更日志"),
)

SYNC_MANIFEST: tuple[TableSyncSpec, ...] = _SYNCED + _SYNCED_WITH_MAPPING + _LOCAL


def _index_by_name(
    specs: tuple[TableSyncSpec, ...],
) -> dict[str, TableSyncSpec]:
    index: dict[str, TableSyncSpec] = {}
    for spec in specs:
        if spec.name in index:
            raise ValueError(f"duplicate table in SYNC_MANIFEST: {spec.name!r}")
        index[spec.name] = spec
    return index


_BY_NAME: dict[str, TableSyncSpec] = _index_by_name(SYNC_MANIFEST)

# copy_rank per table name, taken from the shadow migration's manifest
# (app.migration.shadow.manifest). Computed once; synced_tables() sorts by
# it and reports a clear error for a synced table missing from that manifest
# instead of letting a bare KeyError surface.
_SHADOW_COPY_RANKS: dict[str, int] = {
    spec.name: spec.copy_rank for spec in _SHADOW_MANIFEST.tables
}


def spec_for(name: str) -> TableSyncSpec:
    """Return the ``TableSyncSpec`` for ``name``. Raises ``KeyError`` for an
    unknown table -- callers must not silently treat an unclassified table as
    any particular sync class."""
    return _BY_NAME[name]


def scope_chain(name: str) -> tuple[tuple[str, str, str], ...]:
    """Walk ``name``'s ``TableScope`` from PARENT to PARENT until it reaches
    a NOTEBOOK table, returning the ``(child_table, child_column,
    parent_table)`` hops in the order the exporter would join them (e.g.
    ``knowhow_cell_code`` -> ``knowhow_rows`` -> ``knowhow_tables``).

    Returns ``()`` for a table whose own scope is already NOTEBOOK (``name``
    itself, e.g. ``chunks``). Raises ``ValueError`` if:

    - ``name`` (or a table the chain walks through) is LOCAL, i.e.
      ``scope is None`` -- a LOCAL table is never exported, so asking for
      its scope chain is a caller bug;
    - the chain ever hits a GLOBAL table -- a GLOBAL table has no single
      notebook to resolve to;
    - it revisits a table (a cycle), checked *before* the hop-count check
      below so a 2-table cycle is reported as a cycle, not as "exceeds 4
      hops" once it has looped around enough times;
    - it exceeds 4 hops.

    The last two indicate a bug in the manifest itself (static data), not a
    caller error. A ``parent_table`` that names a table absent from
    ``SYNC_MANIFEST`` altogether is also a manifest bug -- ``TableScope``'s
    ``__post_init__`` and the guard tests in tests/test_sync_manifest.py are
    the first lines of defense against that, but ``spec_for``'s ``KeyError``
    is converted to a point-named ``ValueError`` here too, so a caller never
    sees a bare, unattributed ``KeyError`` out of this function."""
    chain: list[tuple[str, str, str]] = []
    seen = {name}
    current = name
    while True:
        try:
            spec = spec_for(current)
        except KeyError:
            raise ValueError(
                f"{name}: scope chain references unknown table {current!r}"
            ) from None
        if spec.scope is None:
            raise ValueError(
                f"{name}: table {current!r} is LOCAL (scope=None); it is "
                "never exported, so scope_chain must not be called on or "
                "through it"
            )
        if spec.scope.kind is ScopeKind.NOTEBOOK:
            return tuple(chain)
        if spec.scope.kind is ScopeKind.GLOBAL:
            raise ValueError(
                f"{name}: scope chain reaches GLOBAL table {current!r}, "
                "which has no single notebook"
            )
        parent = spec.scope.parent_table
        if parent in seen:
            raise ValueError(f"{name}: scope chain cycles back to {parent!r}")
        if len(chain) >= 4:
            raise ValueError(f"{name}: scope chain exceeds 4 hops")
        chain.append((current, spec.scope.column, parent))
        seen.add(parent)
        current = parent


def synced_tables() -> tuple[str, ...]:
    """SYNCED ∪ SYNCED_WITH_MAPPING table names, ordered by the shadow
    manifest's ``copy_rank``. Declared foreign-key parents are guaranteed to
    sort before their children -- tests/test_sync_manifest.py parses every
    FOREIGN KEY in the PostgreSQL migrations and asserts that ordering holds
    for every edge inside the synced set; this function only applies the
    ordering, it does not re-derive or re-check it."""
    names = [
        spec.name
        for spec in SYNC_MANIFEST
        if spec.sync_class in (SyncClass.SYNCED, SyncClass.SYNCED_WITH_MAPPING)
    ]
    missing = sorted(name for name in names if name not in _SHADOW_COPY_RANKS)
    if missing:
        raise ValueError(
            "synced table(s) have no same-name TableSpec in the shadow "
            f"manifest (app.migration.shadow.manifest): {missing}. Every "
            "SYNCED/SYNCED_WITH_MAPPING table needs one there so its copy "
            "order is defined."
        )
    return tuple(sorted(names, key=lambda name: _SHADOW_COPY_RANKS[name]))


def local_tables() -> tuple[str, ...]:
    """LOCAL table names, in manifest declaration order. Derived from
    SYNC_MANIFEST the same way synced_tables() is, so the two stay in sync
    with each other even if the manifest is restructured."""
    return tuple(spec.name for spec in SYNC_MANIFEST if spec.sync_class is SyncClass.LOCAL)


_business_names = set(POSTGRES_BUSINESS_TABLES)
_manifest_names = set(_BY_NAME)
if _business_names != _manifest_names:
    _missing = sorted(_business_names - _manifest_names)
    _extra = sorted(_manifest_names - _business_names)
    raise ValueError(
        "SYNC_MANIFEST must classify exactly POSTGRES_BUSINESS_TABLES: "
        f"missing from SYNC_MANIFEST={_missing}, extra in SYNC_MANIFEST={_extra}. "
        "See tests/test_sync_manifest.py for the guard that also enforces this."
    )
