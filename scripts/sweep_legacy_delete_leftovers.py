#!/usr/bin/env python3
"""存量删除残渣离线清扫 CLI(薄包装)。逻辑见 app.migration.legacy_leftover_sweep。

删除作业化(PR #659)之前的同步删除路径崩溃留下的两类存量垃圾,没有任何在线
路径会再清:5 张无外键表的孤儿行、5 棵存储根下的孤儿目录(含 scale 产物的
.old/.tmp/.tmp-<token> scratch 兄弟)。默认 inspect 只报数与 id;--apply 才
动手。scale 三根删前逐本取跨进程排它 claim(PostgreSQL advisory lock),
被占/无法评估一律跳过留声。支持 SQLite 与 PostgreSQL 两种部署。

用法(与在役服务同一份生产 `.env` 运行;数据库 URL 从环境读取且绝不打印):
  PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py
  PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py --apply
  PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py --apply --rows-only
  PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py --apply --disk-only

退出码:0 完成;1 apply 有跳过(锁被占)、删不掉的路径或残余孤儿行;2 参数拒绝。
运维手册:`docs/operations_zh.md` 的「存量删除残渣清扫」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.migration.legacy_leftover_sweep import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
