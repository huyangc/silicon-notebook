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

Two more axes cut across those three classes:

- ``severed_columns``: columns that reference a row in a LOCAL table (so the
  reference cannot survive the trip) and must be set to NULL/empty on
  import, independent of ``sync_class``. Never non-empty for LOCAL tables,
  since LOCAL tables are never imported at all.
- ``seed_only``: import only writes this table's rows the first time a
  notebook/group is created at the target; a re-sync neither updates nor
  deletes rows the target already has, because the target environment owns
  membership/sharing decisions made after that first import. Always False
  for LOCAL tables.

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
    # True: import only writes this table's rows the first time the parent
    # notebook/group is created at the target; a re-sync never updates or
    # deletes rows the target already has. Always False for LOCAL tables.
    seed_only: bool = False
    # Columns that, for a row that already exists at the target (a mirrored
    # notebook being re-synced), are never overwritten by import -- the
    # target environment owns their value. Non-empty only for SYNCED and
    # SYNCED_WITH_MAPPING tables; LOCAL tables are never imported at all, so
    # this must be empty for them.
    target_owned_columns: tuple[str, ...] = ()
    notes: str = ""


# --- 3.1 同步层 (SYNCED) -----------------------------------------------

_SYNCED: tuple[TableSyncSpec, ...] = (
    # 笔记本本体
    TableSyncSpec("unified_kg_state", SyncClass.SYNCED),
    # 材料
    TableSyncSpec("source_elements", SyncClass.SYNCED),
    TableSyncSpec("source_authors", SyncClass.SYNCED),
    TableSyncSpec("source_paper_meta", SyncClass.SYNCED),
    TableSyncSpec("chunks", SyncClass.SYNCED),
    TableSyncSpec("chunk_elements", SyncClass.SYNCED),
    TableSyncSpec("chunk_questions", SyncClass.SYNCED),
    TableSyncSpec("knowledge_source_facts", SyncClass.SYNCED),
    TableSyncSpec("knowledge_source_fact_elements", SyncClass.SYNCED),
    # 向量
    TableSyncSpec("chunk_embeddings", SyncClass.SYNCED),
    TableSyncSpec("element_embeddings", SyncClass.SYNCED),
    TableSyncSpec("knowledge_embeddings", SyncClass.SYNCED),
    TableSyncSpec("relation_embeddings", SyncClass.SYNCED),
    TableSyncSpec("memory_embeddings", SyncClass.SYNCED),
    # KG（knowledge_objects.owner 是治理面板可编辑的自由文本标签，不是用户 id，
    # 不映射；source_candidate_id 指向 LOCAL 的 catalog_candidates，导入时置空）
    TableSyncSpec(
        "knowledge_objects",
        SyncClass.SYNCED,
        severed_columns=("source_candidate_id",),
    ),
    TableSyncSpec("knowledge_relations", SyncClass.SYNCED),
    TableSyncSpec("knowledge_object_sources", SyncClass.SYNCED),
    TableSyncSpec("concept_clusters", SyncClass.SYNCED),
    TableSyncSpec("communities", SyncClass.SYNCED),
    TableSyncSpec("community_members", SyncClass.SYNCED),
    TableSyncSpec("canonical_relations", SyncClass.SYNCED),
    TableSyncSpec("mention_edges", SyncClass.SYNCED),
    TableSyncSpec("concept_comentions", SyncClass.SYNCED),
    TableSyncSpec("kg_source_profiles", SyncClass.SYNCED),
    TableSyncSpec("kg_community_edges", SyncClass.SYNCED),
    TableSyncSpec("kg_analysis_artifacts", SyncClass.SYNCED),
    # Knowhow
    TableSyncSpec("knowhow_columns", SyncClass.SYNCED),
    TableSyncSpec("knowhow_rows", SyncClass.SYNCED),
    TableSyncSpec("knowhow_cells", SyncClass.SYNCED),
    # 记忆
    TableSyncSpec("memory_provenance", SyncClass.SYNCED),
    # 笔记本自定义提取 schema 登记表（services/schema_registry.py）；
    # notebook_id 非空的行是笔记本内容，随笔记本同步；notebook_id='' 的
    # builtin 行幂等 upsert 后与目标端已有内置行一致，无害。无用户/组引用列。
    TableSyncSpec("object_schemas", SyncClass.SYNCED),
)

# --- 3.2 边界层 (SYNCED_WITH_MAPPING) -----------------------------------

_SYNCED_WITH_MAPPING: tuple[TableSyncSpec, ...] = (
    TableSyncSpec(
        "notebooks",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        target_owned_columns=("status", "is_shared", "share_token", "sync_origin"),
        notes="created_by 映射不到时硬失败，整个笔记本不导入。",
    ),
    TableSyncSpec(
        "notebook_members",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("user_id", MappingKind.USER),),
        seed_only=True,
        notes="映射不到时跳过该行并记日志。种子写入：首次创建笔记本后不再由导入更新/删除。",
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
    ),
    TableSyncSpec(
        "notebook_bases",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="挂载关系随笔记本同步；created_by 可为空，映射不到置为导入执行者。",
    ),
    TableSyncSpec(
        "notebook_object_schemas",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
    ),
    TableSyncSpec(
        "notebook_assets",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="附件元数据；文件本体随 storage/notebooks/<id>/ 目录同步。映射不到置为导入执行者。",
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
    ),
    TableSyncSpec(
        "knowhow_tables",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
    ),
    TableSyncSpec(
        "knowhow_cell_code",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("updated_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
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
            "更新/删除。"
        ),
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
        ),
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
    ),
    TableSyncSpec(
        "memory_revisions",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("changed_by", MappingKind.USER),),
        notes="映射不到置为导入执行者。",
    ),
    TableSyncSpec(
        "knowhow_milestones",
        SyncClass.SYNCED_WITH_MAPPING,
        mapped_columns=(MappedColumn("created_by", MappingKind.USER),),
        notes="编辑历史表，可随开关关闭只同步当前态；映射不到置为导入执行者。",
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
