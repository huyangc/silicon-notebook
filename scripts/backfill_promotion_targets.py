#!/usr/bin/env python3
"""补救存量待批晋升候选缺失的目标公共知识库(target_base_id)。

背景: 多领域基准库(SCHEMA_VERSION=20)给 `晋升候选` 加了 `promotion_candidates
.target_base_id` 列 —— 晋升(propose_promotion / propose_memory_promotion)必须显式
解析要进入本笔记本已挂载的哪个公共知识库。但 `_migration_20` 只 ALTER 新列,默认值是
空串,**不回填**旧候选行。于是在这次迁移之前创建、此时仍处 `proposed`/`under_review`
的候选行,批准时会撞上「晋升候选缺少目标公共知识库」的守卫直接失败 —— 而
target_base_id 只在 propose 时可设,没有任何接口能给存量候选补目标。本工具就是那条
补救通路,升级到 SCHEMA_VERSION>=20 后如果还有这类存量候选,批准前需要先跑一次本工具。

用法:
  PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db PATH list
  PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db PATH apply \\
      [--set NOTEBOOK_ID=BASE_ID ...] [--dry-run]

解析规则与 propose 侧(`KnowledgeGovernanceService._resolve_promotion_target`)完全对齐,
并复用同一个判定函数 `GovernanceStore.mounted_public_base_ids`(挂载有效性谓词只在
`backend/app/repositories/sqlite/mount_sql.py` 定义一次 —— 本文件不手写、不复制这份
判定):
  - 该候选所属 notebook 已挂载(有效)的公共知识库恰好 1 个 -> 自动指定为目标。
  - 挂 0 个 -> 无法解析,列为「阻塞: 待挂载」,需要先在该 notebook 挂一个公共知识库
    (或改为在 app 内拒绝该候选);apply 不写这一行。
  - 挂 >1 个 -> 有歧义,必须用 --set 显式指定,否则 apply 不写这一行。

`apply` 默认直接写库(与 scripts/merge_dbs.py / scripts/mineru_batch_parse.py 一致的
"默认执行,--dry-run 显式预览"约定);--set 给出的目标必须落在该 notebook 的挂载集合内,
否则整次运行在任何写入之前直接报错退出(拒绝静默降级/静默部分写入)。

本文件不直接对 promotion_candidates 拼 SQL: 读写都经
`app.repositories.sqlite.governance_store.GovernanceStore` 的
pending_promotion_targets / set_promotion_target / promotion_target_column_ready ——
"主业务库 SQL 只在 backend/app/repositories/sqlite 下" 是本仓库的架构硬约束
(见 backend/tests/test_repository_callers_static.py), 离线 CLI 也不例外。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_RESOLUTION_LABEL = {
    "auto": "可自动解析(唯一挂载)",
    "explicit": "按 --set 指定",
    "ambiguous": "有歧义, 需要 --set 显式指定",
    "blocked_no_mount": "阻塞: 尚未挂载任何公共知识库",
}


def _governance_store():
    """延迟 import: 让本文件里不碰 app 的部分(纯 argparse 逻辑)在不需要 app 时也能
    被导入/检查, 与 scripts/merge_dbs.py 的懒加载风格一致。"""
    from app.repositories.sqlite.governance_store import GovernanceStore
    return GovernanceStore


def _require_migrated(conn: sqlite3.Connection) -> None:
    """守卫: 本库还没跑到 SCHEMA_VERSION>=20 就别假装"没有待处理行"。"""
    if not _governance_store().promotion_target_column_ready(conn):
        raise SystemExit(
            "本库 promotion_candidates 缺少 target_base_id 列(或该表还不存在)—— 尚未迁移到"
            " SCHEMA_VERSION>=20。请先启动一次后端(或用 scripts/merge_dbs.py 的"
            " migrate_to_current 同款迁移路径)把库升到当前版本, 再用本工具处理存量候选。"
        )


def pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """status IN ('proposed','under_review') 且 target_base_id 为空串的候选行。"""
    _require_migrated(conn)
    return _governance_store().pending_promotion_targets(conn)


def mounted_public_base_ids(conn: sqlite3.Connection, notebook_id: str) -> list[str]:
    """复用挂载有效性谓词的唯一定义点(不重写 SQL)。propose 侧
    (`KnowledgeGovernanceService._resolve_promotion_target`)调用的正是同一个
    `GovernanceStore.mounted_public_base_ids` —— 它是纯 staticmethod, 只需要一个
    `sqlite3.Connection`, 不需要整套 SQLiteRepository/Settings。"""
    return _governance_store().mounted_public_base_ids(conn, notebook_id)


def _reject_invalid_override(nb: str, base_id: str, mounted: list[str]) -> None:
    """override 合法性的唯一判定点: 目标必须落在该 notebook 的挂载集合内, 否则 fail-loud。
    `validate_overrides()`(CLI 在写入前的前置校验)与 `plan()`(纵深防御, 防止有人绕开
    `main()` 里"先 validate 后 plan"的调用顺序直接调 `plan()`)都调用这同一个函数,
    不写第二份规则。"""
    if base_id not in mounted:
        raise SystemExit(
            f"--set {nb}={base_id} 不合法: {base_id} 不在 notebook {nb} 已挂载的公共"
            f"知识库集合内(该 notebook 已挂载: {mounted or '(无)'})。"
        )


def plan(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    overrides: dict[str, str],
) -> list[dict]:
    """按 notebook 分组解析每行候选的目标; 只读, 不写库。

    纵深防御: 对每行命中的 override 都用 `_reject_invalid_override` 校验一次
    (复用已经查好的 `mounted` 列表, 不额外查库)。`plan()` 是模块级公开函数, 不能只靠
    调用方遵守 `main()` 里"先 `validate_overrides()` 后 `plan()`"的顺序 —— 直接拿一个
    非法 override 调用 `plan()` 也必须在这里就被拒绝, 而不是把非法目标当成 `explicit`
    解析写进结果。"""
    mounted_cache: dict[str, list[str]] = {}
    out: list[dict] = []
    for row in rows:
        nb = row["notebook_id"]
        if nb not in mounted_cache:
            mounted_cache[nb] = mounted_public_base_ids(conn, nb)
        mounted = mounted_cache[nb]
        entry = {
            "id": row["id"],
            "notebook_id": nb,
            "object_id": row["object_id"],
            "object_type": row["object_type"],
            "status": row["status"],
            "created_at": row["created_at"],
            "mounted": mounted,
        }
        override = overrides.get(nb)
        if override:
            _reject_invalid_override(nb, override, mounted)
            entry["target_base_id"] = override
            entry["resolution"] = "explicit"
        elif len(mounted) == 1:
            entry["target_base_id"] = mounted[0]
            entry["resolution"] = "auto"
        elif not mounted:
            entry["target_base_id"] = ""
            entry["resolution"] = "blocked_no_mount"
        else:
            entry["target_base_id"] = ""
            entry["resolution"] = "ambiguous"
        out.append(entry)
    return out


def validate_overrides(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], overrides: dict[str, str]
) -> None:
    """--set 的每个 (notebook_id, base_id) 必须落在该 notebook 的挂载集合内; 任何一个
    非法就在写入前整体拒绝(fail-loud, 不做部分写入)。对没有命中任何候选行的 override
    只警告(大概率是 notebook_id 拼错), 不因此中止整次运行。(`plan()` 内部对命中行的
    override 也会用同一个 `_reject_invalid_override` 再校验一次, 这里是 CLI 在写入前
    的前置校验, 两处不是重复规则。)"""
    touched = {row["notebook_id"] for row in rows}
    for nb, base_id in overrides.items():
        if nb not in touched:
            print(
                f"[警告] --set {nb}=... 没有命中任何待处理候选"
                f"(notebook_id 拼错? 或该 notebook 没有待处理候选)",
                file=sys.stderr,
            )
            continue
        allowed = mounted_public_base_ids(conn, nb)
        _reject_invalid_override(nb, base_id, allowed)


def apply_plan(conn: sqlite3.Connection, entries: list[dict], now: str) -> int:
    """写入 resolution 为 auto/explicit 的行, 返回写入行数; 其余行原样不动。"""
    store = _governance_store()
    n = 0
    for e in entries:
        if e["resolution"] not in ("auto", "explicit"):
            continue
        store.set_promotion_target(conn, e["id"], e["target_base_id"], now)
        n += 1
    return n


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _print_report(entries: list[dict]) -> None:
    if not entries:
        print("没有待处理的候选(所有 proposed/under_review 候选都已有 target_base_id)。")
        return
    by_nb: dict[str, list[dict]] = {}
    for e in entries:
        by_nb.setdefault(e["notebook_id"], []).append(e)
    for nb, items in by_nb.items():
        mounted_desc = "、".join(items[0]["mounted"]) if items[0]["mounted"] else "(无)"
        print(f"notebook {nb} — 已挂载公共知识库: [{mounted_desc}]")
        for e in items:
            verdict = _RESOLUTION_LABEL[e["resolution"]]
            target = f" -> {e['target_base_id']}" if e["target_base_id"] else ""
            print(
                f"  候选 {e['id']}  {e['object_type']:<10} object={e['object_id']}  "
                f"status={e['status']}  created_at={e['created_at']}  [{verdict}{target}]"
            )
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["resolution"]] = counts.get(e["resolution"], 0) + 1
    summary = "  ".join(f"{_RESOLUTION_LABEL[k]}={v}" for k, v in counts.items())
    print(f"共 {len(entries)} 条候选, {len(by_nb)} 个 notebook。{summary}")


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set 格式应为 NOTEBOOK_ID=BASE_ID, 实得: {pair!r}")
        nb, base_id = pair.split("=", 1)
        nb, base_id = nb.strip(), base_id.strip()
        if not nb or not base_id:
            raise SystemExit(f"--set 格式应为 NOTEBOOK_ID=BASE_ID, 实得: {pair!r}")
        if nb in out and out[nb] != base_id:
            raise SystemExit(f"--set 对 {nb} 给出了冲突的目标: {out[nb]} vs {base_id}")
        out[nb] = base_id
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="补救缺失 target_base_id 的存量待批晋升候选(SCHEMA_VERSION=20 升级遗留)")
    ap.add_argument("--db", required=True, help="silicon_notebook.db 路径(就地读写, 无 --out)")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="只列出待处理候选与各 notebook 的挂载/解析情况, 不写库")
    p_apply = sub.add_parser("apply", help="按解析规则写入 target_base_id")
    p_apply.add_argument(
        "--set", action="append", default=[], dest="set_", metavar="NOTEBOOK_ID=BASE_ID",
        help="为指定 notebook 显式指定晋升目标(该 notebook 挂载了 >1 个公共知识库时必需);"
             " 可重复")
    p_apply.add_argument("--dry-run", action="store_true", help="只打印计划, 不写库")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"数据库文件不存在: {db_path}")

    conn = sqlite3.connect(db_path)  # 离线维护 CLI 直接就地读写操作员指定的 --db 路径
    conn.row_factory = sqlite3.Row
    try:
        rows = pending_rows(conn)

        if args.command == "list":
            entries = plan(conn, rows, {})
            _print_report(entries)
            return 0

        overrides = _parse_overrides(args.set_)
        validate_overrides(conn, rows, overrides)
        entries = plan(conn, rows, overrides)
        _print_report(entries)

        if args.dry_run:
            print("[dry-run] 未写库。", file=sys.stderr)
            return 0

        n = apply_plan(conn, entries, _now())
        conn.commit()
        remaining = sum(1 for e in entries if e["resolution"] not in ("auto", "explicit"))
        print(
            f"[完成] 写入 {n} 条 target_base_id。剩余待处理"
            f"(需要先挂载公共知识库, 或补 --set): {remaining}",
            file=sys.stderr,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
