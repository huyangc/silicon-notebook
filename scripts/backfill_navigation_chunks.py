#!/usr/bin/env python3
"""判定存量来源的导航页并写 chunks.navigation(薄包装)。逻辑见
app.services.navigation_backfill(命令行入口 app.services.navigation_backfill_cli)。

SQLite v101 / PostgreSQL 0082 起,建块时会判定目录、图表清单这类「导航页」
(正文主要由本文档其他章节的标题按文档顺序、成段地组成,且不在它所列的范围之内),写 `chunks.navigation`,并在同一事务里
置 `sources.navigation_scanned_at`。升级前已分块的来源 `navigation_scanned_at` 为空:服务
就绪后会在后台自动补判一遍。本工具用于:

  --dry-run                 只判定、逐来源打印块数/命中数/将改变的块数,不写库
  --dry-run --show-sections 同上,并列出被判为导航页的块的章节标签
  (无选项)                  手动重跑:只判未判定或已过期的来源
  --rescan                  重判范围内全部已分块来源
  --clear                   数据级回滚:导航标记全部清 0、判定凭证清空(要保持清空,
                            先把 NAVIGATION_DETECTION_ENABLED 设为 false 再启动服务)

范围必须显式给出:--notebook-id <id> 或 --all-notebooks。写库按 compare-and-set 进行
(来源在读与写之间被重新分块则放弃该来源,下次再判);PostgreSQL 上写库(重跑或 --clear)
要求 --confirm-service-stopped,dry run 不需要。不迁移、不 seed,库不在本构建的 schema
版本即拒绝。

用法(与在役服务同一份生产 `.env` 运行;数据库 URL 从环境读取且绝不打印):
  PYTHONPATH=backend python scripts/backfill_navigation_chunks.py --all-notebooks --dry-run
  PYTHONPATH=backend python scripts/backfill_navigation_chunks.py --notebook-id nb-... --dry-run --show-sections
  PYTHONPATH=backend python scripts/backfill_navigation_chunks.py --all-notebooks --confirm-service-stopped
  PYTHONPATH=backend python scripts/backfill_navigation_chunks.py --all-notebooks --clear --confirm-service-stopped

退出码:0 完成;1 有来源未判定(读写之间被重新分块、来源锁被占或单个来源失败,重跑即可);
2 参数错误(含 PostgreSQL 写库缺 --confirm-service-stopped、判定关闭时写库);
3 库不在本构建的 schema 版本(本工具从不迁移)。
运维手册:`docs/operations_zh.md` 的「导航页标记回填」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.navigation_backfill_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
