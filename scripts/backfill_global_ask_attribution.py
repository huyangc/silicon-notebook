#!/usr/bin/env python3
"""为升级前已完成的全局问答补写归因行(薄包装)。逻辑见
app.services.global_ask_attribution_backfill。

SQLite v98 / PostgreSQL 0078 起,全局问答作业完成时把归入的笔记本写进
`global_ask_job_notebooks`(检索到 ∪ 引用到;两者皆空时为参与库减被跳过的库)。
升级前已完成的作业 `attributed = 0`、没有归因行:服务就绪后会在后台自动补写一遍,
补完前知识分析看板对它们走旧的 payload 判据(更慢)。本工具用于手动补跑/dry run:
可续跑、幂等,可在服务运行时执行;不迁移、不 seed(不会改写 admin 密码),库未迁移即拒绝。

用法(与在役服务同一份生产 `.env` 运行;数据库 URL 从环境读取且绝不打印):
  PYTHONPATH=backend python scripts/backfill_global_ask_attribution.py --dry-run
  PYTHONPATH=backend python scripts/backfill_global_ask_attribution.py
  PYTHONPATH=backend python scripts/backfill_global_ask_attribution.py --after-id gask-... --report out.jsonl

退出码:0 完成;1 有读不回的作业(已列入 --report,仍按旧判据计数);2 参数错误;
3 库尚未迁移到 SQLite v98 / PostgreSQL 0078(本工具从不迁移)。
运维手册:`docs/operations_zh.md` 的「全局问答归因回填」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.global_ask_attribution_backfill import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
