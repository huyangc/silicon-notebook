#!/usr/bin/env python3
"""离线合并两个共享同一 base 库的 silicon_notebook SQLite 库。

用法:
  PYTHONPATH=backend python scripts/merge_dbs.py \
    --db-a A.db --storage-a A/storage \
    --db-b B.db --storage-b B/storage \
    --keep-base a --out merged.db --out-storage merged_storage \
    [--assume-same-users] [--dry-run] [--force]

非破坏性: 两个源库只读拷贝, 产出独立 --out / --out-storage。设计见
docs/superpowers/specs/2026-07-16-merge-duplicate-base-dbs-design.md。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# codex #524 R8 P2:经验库上限直接取协议常量单源(此前是镜像值 + 对账测试;
# 评审两轮点名后改为真单源)。ports.py 可独立 import(纯类型/常量,实测不拉
# 数据库驱动),脚本自举 backend 进 sys.path——它本就以仓库内脚本身份运行。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.repositories.ports import (  # noqa: E402
    RETRIEVAL_EXPERIENCE_MAX_ENTRIES,
)

# --- 表分类(SCHEMA_VERSION=67) --------------------------------------------
NOTEBOOKS_TABLE = "notebooks"  # 按 id 筛(自身即 notebook 行)

# object_schemas 是部署级全局基线；notebook_object_schemas 才随 notebook 合并。
NOTEBOOK_SCOPED_TABLES = [
    "sources", "source_authors", "source_paper_meta", "chunks", "chunk_embeddings",
    "chunk_questions",
    "element_embeddings", "knowledge_objects", "knowledge_embeddings",
    "knowledge_relations", "knowledge_object_sources", "knowledge_source_facts",
    "knowledge_source_fact_elements", "knowledge_source_fact_backfills",
    "source_index_backfills", "chunk_elements", "chunk_element_backfills",
    "notebook_object_schemas",
    "concept_clusters", "concept_comentions", "concept_merge_candidates",
    "canonical_relations", "communities", "community_members", "mention_edges",
    "relation_embeddings", "unified_kg_state", "kg_rebuild_checkpoint",
    "kg_relation_completion_state",
    "kg_cluster_scratch", "kg_canonical_scratch", "kg_conflict_candidates",
    "merge_review_jobs",
    "promotion_candidates", "derived_rule_candidates", "extraction_runs",
    "extraction_candidates", "articles", "article_claims", "conversations",
    "answers", "feedback", "ask_jobs", "kg_build_jobs", "reports", "memory_items",
    "knowhow_tables", "notebook_assets", "notebook_members", "agent_token_notebooks",
    "notebook_bases",
    # v49 群组知识共享 P1: notebook_grants 直接带 notebook_id 列，与
    # notebook_members 同一形状——按 notebook_id IN (sec_nb) 筛即可。
    "notebook_grants",
    # v50 群组知识共享 P2: notebook_share_requests 同样直接带 notebook_id 列
    # (申请挂在被申请共享的库上，随库走)，按 notebook_id IN (sec_nb) 筛即可。
    # 与 notebook_grants.principal_id 不同, 这张表的 group_id 是真实外键
    # (无停车方案取舍); GLOBAL_UNION_TABLES 对 groups 的导入是无条件全量
    # `INSERT OR IGNORE`(不按 sec_nb 过滤), 所以两侧不存在同 id 冲突时,
    # secondary 的每个 group 行都会原样进入 main.groups, 引用天然满足。
    # ⚠ 不要以为"id 冲突时 `foreign_key_check` 会 fail-loud 中止合并"——
    # 那不成立: `INSERT OR IGNORE` 撞主键时只是静默丢弃 secondary 那一行,
    # id 本身依然存在于 main.groups(留下的是 primary 那个不相干的组), 引用
    # 该 id 的 notebook_share_requests.group_id 外键照样满足, `foreign_key_
    # check` 看不出任何异常——真正发生的是"申请被悄悄接到了错误的组上"这类
    # 静默语义合并, 不是可侦测的悬挂外键。避免它靠的是 id 生成用完整 128 位
    # uuid4 十六进制, 跨部署随机不撞车(与 group_store.py `create_group` 的
    # 同款裁决 1c 论证一致), 而不是任何运行期兜底; `foreign_key_check` 这道
    # 闸只对"真的引用不到任何行"的悬挂外键有效, 对 id 冲突这类问题无能为力。
    "notebook_share_requests",
    # v52 问答会话公开分享 T1: conversations 新增 share_token/shared_through_at/
    # shared_through_id 三列, 均随行走——不需要新分类, `conversations` 早已在
    # 上面这份清单里。与 reports.share_token/notebooks.share_token 同一先例:
    # token 冲突概率由 256 位随机凭据(new_capability_token)兜底, 本脚本不做
    # 任何专门的 token 冲突检测或清空。
    "agent_observations",
    # v55 Agentic Memory P3, T1: agent_observations. 与下面 SKIP_SECONDARY_
    # TABLES 里的 agent_notebook_profile/agent_profile_jobs **刻意不同**:
    # 那两张表存的是"这本库被用出来的理解"(共享底座 + 每位成员的覆盖层),对
    # 副库那份旧语料的理解在合并后就不成立, 所以整体跳过; 这张表存的是逐条
    # 观察行本身, 每行都直接带 notebook_id 外键, 语义与 sources/knowledge_
    # objects 等其余知识行完全一致——本次合并后, 副库那些观察行仍然是"关于
    # 这个 notebook 的一条真实记录", 没有理由丢弃, 按 notebook_id IN (sec_nb)
    # 筛即可与其余知识一起正常合并。
]
# notebook_bases 是"挂载方"拥有的行(notebook_id=挂载方, base_notebook_id=被挂的公共知识
# 库), 按本类通用规则以 notebook_id IN (sec_nb) 筛——sec_nb 恒不含 shared_base(它并入
# primary 整库拷贝, 不算"被导入的" notebook)。这对普通 personal notebook 没问题: 它自己
# 持有的挂载边会随它一起带过。但如果 shared_base 本身在 secondary 那侧也挂了别的参考库
# (notebook_id=shared_base 的行), 这些边就不在 sec_nb 里, 会被静默排除——最终只保留
# primary 那份 base 自己的挂载边。这不是 notebook_bases 独有的新问题, 而是 --keep-base
# "保留更全一侧的 base"这一既有设计对 base 名下所有 notebook-scoped 数据(sources/chunks/
# knowledge_objects/...)一贯的效果, 只是这里显式点出来, 不让它继续无声无息(见 README
# merge_dbs 一节的对应说明)。

# 独立内容 FTS(带 notebook_id 列, 无触发器) —— 按 notebook_id 列清单拷行
FTS_NOTEBOOK_TABLES = ["chunks_fts", "kg_objects_fts"]

# 子表: (子表, 父表, 子表FK列, 父表键列) —— 按父行集合筛
CHILD_TABLES = [
    ("source_elements", "sources", "source_id", "id"),
    ("knowhow_columns", "knowhow_tables", "table_id", "id"),
    ("knowhow_rows", "knowhow_tables", "table_id", "id"),
    ("knowhow_cells", "knowhow_rows", "row_id", "id"),
    ("knowhow_cell_code", "knowhow_rows", "row_id", "id"),  # _migration_18: 格子代码附件(二级子表)
    ("knowhow_changes", "knowhow_tables", "table_id", "id"),  # _migration_24: 变更流水
    ("knowhow_milestones", "knowhow_tables", "table_id", "id"),  # _migration_24: 命名里程碑
    ("memory_provenance", "memory_items", "memory_id", "id"),
    ("memory_revisions", "memory_items", "memory_id", "id"),
    ("memory_embeddings", "memory_items", "memory_id", "id"),
    ("ask_trace_steps", "ask_jobs", "job_id", "id"),
]

# 全局表: 主库优先取并集
GLOBAL_UNION_TABLES = [
    "users", "user_profiles", "agent_profiles", "agent_access_tokens",
    "concept_whitelist", "object_schemas",
    # v65 删除笔记本活动留存。行在删除事务中从 ask/source/report 冻结出来，已不再
    # 带 notebook 外键，且 (activity_type, record_id) 沿用原记录的稳定身份；因此
    # 与 users/groups 一样做主库优先并集。若一侧仍保留 live notebook、另一侧已删
    # 除并归档，同一活动可能在合并结果里同时以 live/archive 两种形态存在；查询层
    # 会按各自数据生命周期展示，而不是在离线合并时猜测并删除其中一份。
    "retained_user_activity",
    # v49 群组知识共享 P1: groups/group_members 都不带 notebook_id，与
    # agent_profiles/agent_access_tokens 同一先例——group_members 的 FK 挂在
    # groups 上而非 notebooks 上，不适用 CHILD_TABLES 的"按 sec_nb 已导入父行"
    # 筛选语义(groups 本身就是全量并集，没有"被 sec_nb 排除"这回事)。两表主键
    # 均是自然复合/稳定 id(groups.id、group_members 的 (group_id, user_id))，
    # INSERT OR IGNORE 按主键去重即是正确的并集语义，与 users/agent_profiles
    # 同一套"主库优先、副库同 id 冲突即丢弃"处理。
    "groups", "group_members",
    # v54 Agentic Memory P2 的检索策略经验库。它是**部署级全局**表——没有
    # notebook_id、没有 owner 列,所以 NOTEBOOK_SCOPED_TABLES / CHILD_TABLES 那两套
    # 按 notebook 筛的语义对它压根不适用,与 agent_profiles/groups 同一先例。
    # ⚠ 并集语义之所以正确,全靠它的主键是**内容寻址**的(情境指纹+动作的确定性
    #   哈希,见 sqlite/migrations.py 的 _migration_54 第 1 条):两个独立部署对同
    #   一情境+动作算出同一个 id,``INSERT OR IGNORE`` 于是「同一条经验只留一份、
    #   主库优先」,不同经验则各自成行。若哪天有人把 id 改成递增整数或随机 uuid,
    #   这一行必须跟着重新论证——递增整数会让两边的 1 号经验撞主键、静默丢掉副库
    #   那条;随机 uuid 则会让同一条经验在合并后变成两行、support 被拆散。
    # 计数列(support/adopted)按主库那份保留、不相加:两个部署对同一条经验各自的
    # 支持次数不可加和(同一批 run 可能在两边都被蒸馏过),而这张表的用途是排序
    # 提示,宁可低估。
    "retrieval_experiences",
    # v65 global feedback: author/user foreign keys point into the users union;
    # insert the parent wishes before their composite vote children.
    "wishes", "wish_votes",
]

OBJECT_SCHEMA_SEMANTIC_COLUMNS = (
    "plural", "fields", "primary_field", "description", "label",
    "list_fields", "source", "status", "rationale",
)

# 外部内容 FTS —— 导入后 rebuild
EXTERNAL_FTS_TABLES = ["memory_items_fts"]

# 副库不导入(primary 自己的行随整库复制原样保留)。本类混着两种理由, 都是"不导入",
# 所以共用一个分类桶, 但别把它们的理由混为一谈:
#   (a) 属于 primary 部署/本次运行的状态 —— 导入副库那份会把两个部署的身份搅在一起;
#   (b) 派生产物 —— 合并后本来就该由重建重新产出, 拷过来只会带着**源库的版本戳**落地。
SKIP_SECONDARY_TABLES = [
    "auth_sessions",
    # Forward-shadow capture state and event history belong to the primary
    # deployment/run. Importing a secondary ledger would mix run identities.
    "shadow_capture_control",
    "shadow_change_log",
    # Deployment health and the scrubbed legacy table belong to the primary
    # deployment. Importing either from a secondary DB would mix runtimes.
    "model_service_status",
    "system_model_service_status",
    # 全局设置 KV(含每笔记本文档数量上限的全局默认)属于 primary 部署;导入副库的
    # app_settings 会覆盖 primary 的部署级配置,故与部署健康表同款只保 primary。
    "app_settings",
    # 命令目录抽取(command-catalog, v38)的运行期*进程*状态 —— 一次运行的任务进度与
    # 尚未审阅完的候选队列, 不是知识。人已经确认过的内容早就落进普通 knowhow 表(会
    # 随 NOTEBOOK_SCOPED_TABLES 正常合并), 副库这份队列合进来只会带来一个半审阅完的
    # 残局, 谁也接不上。同一理由与措辞见 sharing_store.py 深拷贝快照里对这两张表的
    # 同款排除注释。
    "catalog_jobs", "catalog_candidates",
    # --- 理由 (b): KG 质量分析的预计算产物(跨板块边 / 来源画像 / 产物账本, v34)。---
    # 三张都是 `rebuild_communities` 一次事务整体重写的派生数据,而 `kg_analysis_artifacts`
    # 这本账里每行都钉着它建于哪个 `kg_mutation_seq`。这个版本戳只在**它自己那个库**里
    # 有意义:导入 notebook 的 `unified_kg_state` 紧接着就被 KG_STATE_TABLES 清掉(当前
    # seq 因此合成为 0),账本却带着源库的 seq(生产上是几百)活下来 —— 分析视图一比就得出
    # `seq_behind < 0`,而那一档是刻意不 clamp 的红色 integrity 告警,语义是「库被手工改过、
    # 数字不可信」。一次正常合并稳定造出这个假警报,是纯粹的噪声。
    # 所以这三张跟着 KG 状态一起归零:不导入(留白 = 「从未计算过」的诚实表达),等部署后
    # 那次「刷新图谱 / 重新合并」把它们连同社区图一起重新算出来。
    # ⚠ 反过来做(照拷进来再由 KG_STATE_TABLES 清)语义一样, 但要先把生产 base 库里
    #   数以百万计的跨板块边搬进合并事务再删掉 —— 白付一遍 IO。
    "kg_community_edges", "kg_source_profiles", "kg_analysis_artifacts",
    # Agentic Memory P1(v51)的两张表:理解块与每条链的巡固状态。同一理由与措辞
    # 见 sharing_store.py 深拷贝快照里对这两张表的同款排除注释——块里存的是这本库
    # 被**用出来**的理解(共享底座 + 每位成员各自的覆盖层),不是知识本身;知识随
    # NOTEBOOK_SCOPED_TABLES 正常合并,合并后的语料与合并前不是同一份,导入副库那
    # 份对旧语料的理解只会让底座描述一本已经不存在的库。job 行则与 catalog_jobs
    # 同类:一次运行的进程状态外加阈值计数器,合进来等于把副库的活动记在主库账上。
    # 留白 = 「还没巡固过」的诚实表达,合并后由正常触发重新算出来。
    "agent_notebook_profile", "agent_profile_jobs",
    # v59 notebook indexing rebuild stage: unpublished worker-local payloads
    # are valid only for their exact durable job/generation/source snapshot.
    # A merge cannot resume that authority, so keep only the primary DB's
    # in-flight state just like the other process/job tables above.
    "indexing_pipeline_stages", "indexing_pipeline_stage_sources",
    # v61 部署插件运行时开关 + 审计,与 app_settings 同一理由:这是 primary 部署的
    # 管理员决定(哪个插件被临时关停),导入副库的开关会静默改变 primary 的运行态
    # 行为——副库可能把某个 primary 依赖的插件关掉了。保留 primary、丢副库那份。
    "extension_runtime_toggles",
]

# 导入后清空(引用可再生的 kg_index 产物, 逼部署侧干净重建)
KG_STATE_TABLES = [
    "kg_rebuild_checkpoint", "kg_relation_completion_state",
    "unified_kg_state", "kg_cluster_scratch",
]

FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_docsize", "_config", "_content")


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def discover_tables(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """返回 (业务表, FTS 虚表)。排除 sqlite_* 与 FTS 影子表。"""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    virtual = {n for (n, s) in rows if s and "VIRTUAL TABLE" in s.upper()}
    shadow = {v + suf for v in virtual for suf in FTS_SHADOW_SUFFIXES}
    business = {n for (n, _) in rows if n not in shadow and n not in virtual}
    return business, virtual


def assert_taxonomy_complete(conn: sqlite3.Connection) -> None:
    """守卫: DB 中每张业务表/FTS 虚表都必须被显式归类, 否则 fail-loud(防静默丢数据)。

    分类清单是**超集**: 允许清单里的表在某个库里不存在——schema 随版本演进, 迁移上来
    的老库常残留已废弃功能的遗留表(如 articles/article_claims/derived_rule_candidates/
    extraction_candidates, PR#110 删了功能但不 DROP 表), 全新库则没有。这类"已分类但
    本库缺失"只提示、不致命(merge 时按表存在性跳过)。真正致命的是"本库有、但未分类"
    的表: 那会在合并时静默漏拷该表数据。"""
    business, virtual = discover_tables(conn)
    classified_business = (
        {NOTEBOOKS_TABLE}
        | set(NOTEBOOK_SCOPED_TABLES)
        | {t for (t, *_rest) in CHILD_TABLES}
        | set(GLOBAL_UNION_TABLES)
        | set(SKIP_SECONDARY_TABLES)
    )
    classified_virtual = set(FTS_NOTEBOOK_TABLES) | set(EXTERNAL_FTS_TABLES)
    unclassified_b = business - classified_business
    unclassified_v = virtual - classified_virtual
    absent = classified_business - business  # 已分类但本库没有 —— 容忍
    if absent:
        print(f"[提示] 分类清单含本库不存在的表(容忍, merge 时跳过): {sorted(absent)}",
              file=sys.stderr)
    if unclassified_b or unclassified_v:
        raise SystemExit(
            "发现未分类的表, 拒绝合并(防静默丢数据):\n"
            f"  未分类业务表: {sorted(unclassified_b)}\n"
            f"  未分类 FTS 虚表: {sorted(unclassified_v)}\n"
            "请把它们加进 scripts/merge_dbs.py 的对应分类清单后重跑。"
        )


def migrate_to_current(db_path: Path) -> list[int]:
    """把 db_path 就地迁到 SCHEMA_VERSION。只 migrate(), 不 seed。

    迁移用的是 WAL 模式连接; 迁移写入先落在 -wal sidecar, 不 checkpoint 就返回的话,
    调用方后续对 db_path 做 shutil.copy2 / ATTACH 只读 .db 主文件, 会看不到这些写入
    (静默丢数据)。所以这里必须显式 checkpoint(TRUNCATE) 把 -wal 合并回 .db 并截断,
    再关闭本线程连接, 才能保证 db_path 单文件即完整状态。
    """
    # 延迟 import: 让 Task 1 的纯 sqlite 测试无需 app 依赖即可跑。
    from app.core.config import Settings
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=db_path.parent)
    applied = SqliteMigrator(database, settings).migrate()
    try:  # WAL 落盘: 把 -wal 合并回 .db 并截断, 保证后续 copy/ATTACH 看到完整数据
        database.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        database.close_local()  # 关闭本线程 WAL 连接
    return applied


def notebook_ids(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, tier FROM notebooks").fetchall()}


def _sole_base_id(conn: sqlite3.Connection, side: str) -> str:
    """本工具只支持"两边共享恰好一个公共知识库"的合并场景。多领域基准库下一个库可以
    挂载/持有多个 tier='base' 的公共知识库,这种情况下没有安全的隐式选择(不能像单 base
    时代那样直接取第一个)——必须让用户先看清两边各自的公共知识库集合再决定怎么处理,
    所以 >1 个时直接 fail-loud 报出侧别+全部候选,而不是猜一个。"""
    rows = conn.execute("SELECT id, name FROM notebooks WHERE tier='base'").fetchall()
    if len(rows) > 1:
        names = "、".join(f"{r[0]}({r[1]})" for r in rows)
        raise SystemExit(
            f"{side} 侧存在 {len(rows)} 个公共知识库: {names}。本工具只支持"
            f"「两边共享恰好一个公共知识库」的场景, 多领域库请勿使用 --keep-base 猜测"
            f"——请先手动确认要合并的是哪一对公共知识库, 多出来的公共知识库需要单独处理。"
        )
    if not rows:
        raise SystemExit(f"{side} 侧没有公共知识库(tier='base')")
    return str(rows[0][0])


def base_stats(conn: sqlite3.Connection, nb_id: str) -> dict[str, int]:
    out = {}
    for t in ("sources", "chunks", "knowledge_objects"):
        out[t] = conn.execute(
            f"SELECT count(*) FROM {t} WHERE notebook_id=?", (nb_id,)
        ).fetchone()[0]
    return out


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def preflight(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
              assume_same_users: bool) -> str:
    # 0) 两库都跑分类守卫: 被导入的副库若有未分类表, 会在 merge 时静默漏拷其数据,
    #    所以两侧都要检查(不只 primary)。
    assert_taxonomy_complete(conn_a)
    assert_taxonomy_complete(conn_b)
    # 1) 版本一致且为当前 SCHEMA_VERSION(从 app 读, 不硬编码——schema 会随版本演进)
    from app.repositories.sqlite.migrations import SCHEMA_VERSION
    va, vb = _user_version(conn_a), _user_version(conn_b)
    if not (va == vb == SCHEMA_VERSION):
        raise SystemExit(
            f"schema 版本必须都为当前 SCHEMA_VERSION={SCHEMA_VERSION}, 实得 A={va} B={vb}")
    _assert_global_schema_compatibility(conn_a, conn_b)
    # 2) 各恰好一个 base 且 id 相同
    ba, bb = _sole_base_id(conn_a, "A"), _sole_base_id(conn_b, "B")
    if ba != bb:
        raise SystemExit(f"两库 base id 不同: A={ba} B={bb}; 无法认定为同一 base")
    # 3) notebook id 交集恰好只有 base
    ids_a, ids_b = set(notebook_ids(conn_a)), set(notebook_ids(conn_b))
    overlap = (ids_a & ids_b) - {ba}
    if overlap:
        raise SystemExit(f"除 base 外 notebook id 撞车, 无法安全移植: {sorted(overlap)}")
    # 4) users 交集
    ua = {r[0] for r in conn_a.execute("SELECT id FROM users")}
    ub = {r[0] for r in conn_b.execute("SELECT id FROM users")}
    u_overlap = ua & ub
    if u_overlap and not assume_same_users:
        raise SystemExit(
            f"两库有相同 user id: {sorted(u_overlap)}。若确为同一人, 加 --assume-same-users; "
            "否则请先在源库侧改 id 避免归属错乱。")
    # 5) 打印 base 统计供核对
    print(f"[base 统计] A({ba}): {base_stats(conn_a, ba)}", file=sys.stderr)
    print(f"[base 统计] B({bb}): {base_stats(conn_b, bb)}", file=sys.stderr)
    return ba


def _col_list(conn: sqlite3.Connection, table: str) -> str:
    return ", ".join(table_columns(conn, table))


def _table_exists(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _assert_global_schema_compatibility(
    conn_a: sqlite3.Connection, conn_b: sqlite3.Connection
) -> None:
    """Reject same-name global schemas whose effective meaning differs."""
    if not (
        _table_exists(conn_a, "object_schemas")
        and _table_exists(conn_b, "object_schemas")
    ):
        return
    columns = ", ".join(("object_type", *OBJECT_SCHEMA_SEMANTIC_COLUMNS))
    rows_a = {
        row[0]: tuple(row[1:])
        for row in conn_a.execute(
            f"SELECT {columns} FROM object_schemas WHERE notebook_id=''"
        )
    }
    rows_b = {
        row[0]: tuple(row[1:])
        for row in conn_b.execute(
            f"SELECT {columns} FROM object_schemas WHERE notebook_id=''"
        )
    }
    conflicts = sorted(
        object_type
        for object_type in rows_a.keys() & rows_b.keys()
        if rows_a[object_type] != rows_b[object_type]
    )
    if conflicts:
        raise SystemExit(
            "两库存在同名但定义不同的全局图谱类型，拒绝静默采用主库定义: "
            + ", ".join(conflicts)
        )


def _evict_experiences_to_limit(
    conn: sqlite3.Connection, max_entries: int = RETRIEVAL_EXPERIENCE_MAX_ENTRIES
) -> int:
    """合库后把 `retrieval_experiences` 收回运行时硬上限(协议常量单源;
    淘汰序仍镜像 `sqlite/retrieval_experience_store.py::evict_to_limit`,
    改序必须两侧同改——对账测试另钉常量相等作保险)。"""
    if not _table_exists(conn, "retrieval_experiences", "main"):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM main.retrieval_experiences").fetchone()
    overflow = int(row[0]) - max_entries
    if overflow <= 0:
        return 0
    conn.execute(
        "DELETE FROM main.retrieval_experiences WHERE id IN ("
        "SELECT id FROM main.retrieval_experiences "
        "ORDER BY adopted ASC, support ASC, updated_at ASC, id ASC LIMIT ?)",
        (overflow,),
    )
    return overflow


def sweep_orphan_group_grants(conn: sqlite3.Connection) -> int:
    """清掉指向已不存在群组的 `notebook_grants` 行, 返回清掉的条数。

    **合并是这类孤儿边唯一的来源**(已定裁决 1c 的审计承诺就落在这里)。平时删组走
    的是同一个写事务: 先删指向该组的授权边, 再删组。但合并把两库的 `notebook_grants`
    按 notebook 范围导入、把 `groups` 按 GLOBAL_UNION 并集导入, 两者的取舍口径不同,
    于是「副库里那本笔记本的边导进来了, 而它指向的组因为主库同 id 优先/或压根不在
    并集里而对不上」就成立了。

    为什么必须清而不是留着: 谓词侧确实拦得住(join 不到 group_members 就判假, 不会
    越权), 但库主的共享管理列表会永久挂着一条指向不存在的组的记录; 更糟的是将来
    某个部署里凑巧新建一个同 id 的组, 这条边就会**复活成真授权**。

    刻意**不**依赖外键: `principal_id` 是无 FK 的多态列(v49 迁移里写明的裁决),
    所以下面的 `foreign_key_check` 永远看不见这类行 —— 必须显式扫。判据只认两个
    群组主体, `user` / `everyone` 主体的 `principal_id` 根本不指向 `groups`。
    """
    if not (_table_exists(conn, "notebook_grants", "main")
            and _table_exists(conn, "groups", "main")):
        return 0
    cur = conn.execute(
        "DELETE FROM main.notebook_grants "
        "WHERE principal_type IN ('group','group_admins') "
        "AND principal_id NOT IN (SELECT id FROM main.groups)"
    )
    return cur.rowcount or 0


def merge_core(out_db: Path, primary_db: Path, secondary_db: Path,
               shared_base: str) -> dict:
    if out_db.exists():
        raise SystemExit(f"输出已存在: {out_db}(用 --force 覆盖或换路径)")
    shutil.copy2(primary_db, out_db)

    conn = sqlite3.connect(out_db)
    try:
        assert_taxonomy_complete(conn)
        conn.execute("PRAGMA foreign_keys = OFF")  # 导入期不校验; 结束后统一 foreign_key_check
        conn.execute("ATTACH DATABASE ? AS sec", (str(secondary_db),))

        sec_nb = [r[0] for r in conn.execute(
            "SELECT id FROM sec.notebooks WHERE id != ?", (shared_base,)).fetchall()]
        ph = ",".join("?" for _ in sec_nb) or "NULL"  # sec_nb 为空时 IN (NULL) 匹配 0 行

        # 子表 -> 限定 FK 落在"以 sec_nb 为界的已导入父行"内(每个子句恰含一个 IN ({ph}))。
        # knowhow_cells / knowhow_cell_code 是二级子表(->rows->tables.notebook_id), 必须两层
        # 下钻, 否则会带入 secondary base 的行 -> row_id 悬挂 -> FK 失败。
        _knowhow_row_scope = (f"row_id IN (SELECT id FROM sec.knowhow_rows WHERE table_id IN "
                              f"(SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph})))")
        child_scopes = {
            "source_elements": f"source_id IN (SELECT id FROM sec.sources WHERE notebook_id IN ({ph}))",
            "knowhow_columns": f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_rows":    f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_cells":     _knowhow_row_scope,
            "knowhow_cell_code": _knowhow_row_scope,
            "knowhow_changes":    f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_milestones": f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "memory_provenance": f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "memory_revisions":  f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "memory_embeddings": f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "ask_trace_steps":   f"job_id IN (SELECT id FROM sec.ask_jobs WHERE notebook_id IN ({ph}))",
        }
        missing = {c for (c, *_r) in CHILD_TABLES} - set(child_scopes)
        if missing:  # 新增子表却没定义导入范围 -> fail-loud
            raise SystemExit(f"子表缺少导入范围定义: {sorted(missing)}")

        row_counts: dict[str, int] = {}

        def _run(table: str, where: str) -> None:
            # 两库都得有该表才能跨库 INSERT; 版本演进导致某库缺表则跳过(见守卫容忍逻辑)。
            if not (_table_exists(conn, table, "main") and _table_exists(conn, table, "sec")):
                return
            cols = _col_list(conn, table)
            cur = conn.execute(
                f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM sec.{table} WHERE {where}",
                tuple(sec_nb))  # where 恰含一个 IN ({ph}) -> 一份 sec_nb 参数
            row_counts[table] = row_counts.get(table, 0) + cur.rowcount

        with conn:  # 单事务(FK off 期间, 顺序无关)
            _run(NOTEBOOKS_TABLE, f"id IN ({ph})")                       # notebooks 自身按 id
            for t in NOTEBOOK_SCOPED_TABLES + FTS_NOTEBOOK_TABLES:        # A 类 + 独立 FTS
                _run(t, f"notebook_id IN ({ph})")
            for child, *_rest in CHILD_TABLES:                            # B 类: 显式范围
                _run(child, child_scopes[child])
            for t in GLOBAL_UNION_TABLES:                                # C 类: 主库优先并集
                if not (_table_exists(conn, t, "main") and _table_exists(conn, t, "sec")):
                    continue
                cols = _col_list(conn, t)
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{t} ({cols}) SELECT {cols} FROM sec.{t}")
            for t in KG_STATE_TABLES:                                    # 清导入 notebook 的 KG 状态
                if _table_exists(conn, t, "main"):
                    conn.execute(
                        f"DELETE FROM main.{t} WHERE notebook_id IN ({ph})", tuple(sec_nb))

        # 孤儿群组授权边清扫。必须在 GLOBAL_UNION 合并**之后**(那一步才决定
        # `groups` 的最终并集)、`foreign_key_check` **之前**(它看不见这类行 ——
        # `principal_id` 无外键, 见 sweep_orphan_group_grants 的说明)。
        with conn:
            orphan_grants = sweep_orphan_group_grants(conn)

        # codex #524 R1 P2:经验库并集后立刻按运行时同一淘汰序收容——两个各自
        # 合法 300 行的库并出最多 600 行,而普通读者只按 id 序取前 300、下一次
        # 蒸馏(部署可能根本没配模型)之前不会有人淘汰,等于永久性忽略任意一半。
        # 淘汰序与 evict_to_limit 逐字同款((adopted, support, updated_at, id)
        # 升序删到上限),这里不 import store(离线脚本自己持有连接)但序是同
        # 一份契约,改一处必须同改另一处——两侧注释互相指认。
        with conn:
            evicted = _evict_experiences_to_limit(conn)
        if evicted:
            print(f"[merge] 经验库并集超容,按运行时淘汰序收容删除 {evicted} 条")
        row_counts["retrieval_experiences_evicted"] = evicted
        if orphan_grants:
            print(f"[merge] 清理指向已不存在群组的共享授权 {orphan_grants} 条")
        row_counts["notebook_grants_orphans_removed"] = orphan_grants

        # 外部内容 FTS rebuild(在自己的事务里), 提交后再 DETACH(DETACH 不能在事务中)。
        for t in EXTERNAL_FTS_TABLES:
            if _table_exists(conn, t, "main"):
                conn.execute(f"INSERT INTO main.{t}({t}) VALUES('rebuild')")
        conn.commit()

        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            raise SystemExit(f"合并后存在悬挂外键, 已中止: {dangling[:20]}")

        conn.execute("DETACH DATABASE sec")
        conn.commit()
        # WAL 落盘: 保证 merged.db 是自洽单文件, 运维单独拷 .db 部署时不会漏 -wal 数据。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"imported_notebooks": sec_nb, "row_counts": row_counts}
    finally:
        conn.close()


def merge_storage(out_storage: Path, primary_storage: Path,
                  secondary_storage: Path, imported_notebooks: list[str]) -> None:
    out_nb = out_storage / "notebooks"
    out_nb.mkdir(parents=True, exist_ok=True)
    # primary 的 notebooks/ 整份(不含 kg_index / kg_viz)
    prim_nb = primary_storage / "notebooks"
    if prim_nb.is_dir():
        shutil.copytree(prim_nb, out_nb, dirs_exist_ok=True)
    # secondary 的每个导入 notebook 目录
    for nb_id in imported_notebooks:
        src = secondary_storage / "notebooks" / nb_id
        if not src.is_dir():
            continue
        dst = out_nb / nb_id
        if dst.exists():
            raise SystemExit(f"storage 目录撞车(不应发生): {dst}")
        shutil.copytree(src, dst)


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="离线合并两个共享 base 的 silicon_notebook 库")
    ap.add_argument("--db-a", required=True); ap.add_argument("--storage-a", required=True)
    ap.add_argument("--db-b", required=True); ap.add_argument("--storage-b", required=True)
    ap.add_argument("--keep-base", required=True, choices=["a", "b"],
                    help="保留哪侧的 base(= 该侧为容器/primary)")
    ap.add_argument("--out", required=True); ap.add_argument("--out-storage", required=True)
    ap.add_argument("--assume-same-users", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    out = Path(args.out)
    # dry-run 不写 out, 不该被"输出已存在"挡住(它就是拿来预览计划的); 只在真跑时早退。
    if out.exists() and not args.force and not args.dry_run:
        raise SystemExit(f"输出已存在: {out}(加 --force 覆盖或换路径)")

    # primary = keep-base 那侧
    if args.keep_base == "a":
        prim_db, prim_store = Path(args.db_a), Path(args.storage_a)
        sec_db, sec_store = Path(args.db_b), Path(args.storage_b)
    else:
        prim_db, prim_store = Path(args.db_b), Path(args.storage_b)
        sec_db, sec_store = Path(args.db_a), Path(args.storage_a)

    with tempfile.TemporaryDirectory(prefix="merge_dbs_") as tmp:
        tmp = Path(tmp)
        prim_copy, sec_copy = tmp / "primary.db", tmp / "secondary.db"
        shutil.copy2(prim_db, prim_copy); shutil.copy2(sec_db, sec_copy)

        ap_applied = migrate_to_current(prim_copy)
        bp_applied = migrate_to_current(sec_copy)
        print(f"[迁移] primary applied={ap_applied} secondary applied={bp_applied}", file=sys.stderr)

        conn_p, conn_s = sqlite3.connect(prim_copy), sqlite3.connect(sec_copy)
        try:
            shared_base = preflight(conn_p, conn_s, args.assume_same_users)
            sec_nb = [r[0] for r in conn_s.execute(
                "SELECT id FROM notebooks WHERE id != ?", (shared_base,)).fetchall()]
        finally:
            conn_p.close(); conn_s.close()

        print(f"[计划] 将导入 {len(sec_nb)} 个 notebook: {sec_nb}", file=sys.stderr)
        if args.dry_run:
            print("[dry-run] 未产出任何文件。", file=sys.stderr)
            return 0

        if out.exists() and args.force:
            out.unlink()
        try:
            result = merge_core(out, prim_copy, sec_copy, shared_base)
            merge_storage(Path(args.out_storage), prim_store, sec_store,
                          result["imported_notebooks"])
        except BaseException:
            # merge_core 在 FK 校验失败时会留下已提交的部分 out_db; 失败即删, 不留半成品。
            if out.exists():
                out.unlink()
            raise

    print(f"[完成] 输出库={out}  导入 notebook={result['imported_notebooks']}", file=sys.stderr)
    print(f"[完成] 行数={result['row_counts']}", file=sys.stderr)
    print("[提醒] 部署后在 app 内点一次「重建索引/刷新图谱」以重生成 kg_index/kg_viz/ANN。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
