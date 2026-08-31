#!/usr/bin/env python3
"""离线 / 异机 scale 索引构建 CLI(薄包装)。逻辑见 app.services.scale_build_cli。

与 `batch_ingest.py index` 的区别:那条是**停服**通道(全局 advisory lock),这条
可以与运行中的服务并存——取 per-notebook 跨进程锁、`.tmp` + 原子 swap,服务进程
按既有逐请求探测自动换代,无需重启。仅支持 PostgreSQL。

用法(必须用**生产 `.env`** 运行,storage/embed 维度/管线都要与在役服务一致):
  PYTHONPATH=backend python scripts/build_scale_index.py inspect --notebook nb-xxx
  PYTHONPATH=backend python scripts/build_scale_index.py build   --notebook nb-xxx [--full|--fold]
  PYTHONPATH=backend python scripts/build_scale_index.py export  --notebook nb-xxx --to DIR
  PYTHONPATH=backend python scripts/build_scale_index.py import  --notebook nb-xxx --from DIR

退出码:0 成功;1 已开始但失败(锁被占、构建失败、swap 前复验失败);
2 未动手就拒绝(SQLite 后端、未知 notebook、迁移账本不一致、包校验不通过);
130 Ctrl-C。数据库 URL 从环境读取且绝不打印。

完整运维步骤(异机三步、两机 pin 清单、连接预算、PgBouncer 前提、`.old` 恢复、
allow_pickle 来源约束)见 `docs/operations_zh.md` 的「离线 / 异机 scale 构建」。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.scale_build_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
