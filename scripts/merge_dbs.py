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
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# --- 表分类(SCHEMA_VERSION=17) --------------------------------------------
NOTEBOOKS_TABLE = "notebooks"  # 按 id 筛(自身即 notebook 行)

NOTEBOOK_SCOPED_TABLES = [
    "sources", "source_authors", "source_paper_meta", "chunks", "chunk_embeddings",
    "element_embeddings", "knowledge_objects", "knowledge_embeddings",
    "knowledge_relations", "knowledge_object_sources", "object_schemas",
    "concept_clusters", "concept_comentions", "concept_merge_candidates",
    "canonical_relations", "communities", "community_members", "mention_edges",
    "relation_embeddings", "unified_kg_state", "kg_rebuild_checkpoint",
    "kg_cluster_scratch", "kg_conflict_candidates", "merge_review_jobs",
    "promotion_candidates", "derived_rule_candidates", "extraction_runs",
    "extraction_candidates", "articles", "article_claims", "conversations",
    "answers", "feedback", "ask_jobs", "reports", "memory_items",
    "knowhow_tables", "notebook_assets", "notebook_members", "agent_token_notebooks",
]

# 独立内容 FTS(带 notebook_id 列, 无触发器) —— 按 notebook_id 列清单拷行
FTS_NOTEBOOK_TABLES = ["chunks_fts", "kg_objects_fts"]

# 子表: (子表, 父表, 子表FK列, 父表键列) —— 按父行集合筛
CHILD_TABLES = [
    ("source_elements", "sources", "source_id", "id"),
    ("knowhow_columns", "knowhow_tables", "table_id", "id"),
    ("knowhow_rows", "knowhow_tables", "table_id", "id"),
    ("knowhow_cells", "knowhow_rows", "row_id", "id"),
    ("memory_provenance", "memory_items", "memory_id", "id"),
    ("memory_revisions", "memory_items", "memory_id", "id"),
    ("memory_embeddings", "memory_items", "memory_id", "id"),
    ("ask_trace_steps", "ask_jobs", "job_id", "id"),
]

# 全局表: 主库优先取并集
GLOBAL_UNION_TABLES = [
    "users", "user_profiles", "agent_profiles", "agent_access_tokens",
    "concept_whitelist",
]

# 外部内容 FTS —— 导入后 rebuild
EXTERNAL_FTS_TABLES = ["memory_items_fts"]

# 副库不导入(临时登录会话, 用户重登即可; primary 的随整库复制保留)
SKIP_SECONDARY_TABLES = ["auth_sessions"]

# 导入后清空(引用可再生的 kg_index 产物, 逼部署侧干净重建)
KG_STATE_TABLES = ["kg_rebuild_checkpoint", "unified_kg_state", "kg_cluster_scratch"]

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
    """把 db_path 就地迁到 SCHEMA_VERSION。只 migrate(), 不 seed。"""
    # 延迟 import: 让 Task 1 的纯 sqlite 测试无需 app 依赖即可跑。
    from app.core.config import Settings
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=db_path.parent)
    return SqliteMigrator(database, settings).migrate()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - filled in Task 6
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
