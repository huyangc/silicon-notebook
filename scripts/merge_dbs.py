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
from pathlib import Path

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


def base_id(conn: sqlite3.Connection) -> str:
    ids = [r[0] for r in conn.execute("SELECT id FROM notebooks WHERE tier='base'").fetchall()]
    if len(ids) != 1:
        raise SystemExit(f"期望恰好 1 个 base 库, 实得 {len(ids)} 个: {ids}")
    return ids[0]


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
    # 1) 版本一致且为 17
    va, vb = _user_version(conn_a), _user_version(conn_b)
    if not (va == vb == 17):
        raise SystemExit(f"schema 版本必须都为 17, 实得 A={va} B={vb}")
    # 2) 各恰好一个 base 且 id 相同
    ba, bb = base_id(conn_a), base_id(conn_b)
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
        # knowhow_cells 是二级子表(cells->rows->tables.notebook_id), 必须两层下钻,
        # 否则会带入 secondary base 的 cells -> row_id 悬挂 -> FK 失败。
        child_scopes = {
            "source_elements": f"source_id IN (SELECT id FROM sec.sources WHERE notebook_id IN ({ph}))",
            "knowhow_columns": f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_rows":    f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_cells":   (f"row_id IN (SELECT id FROM sec.knowhow_rows WHERE table_id IN "
                                f"(SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph})))"),
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


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - filled in Task 6
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
