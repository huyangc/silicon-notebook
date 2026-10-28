"""按库重写概念描述(换了描述模型或描述提示词之后,由运维择时触发)。

用法:
  scripts/cli.sh kg rewrite-descriptions <notebook_id> [--dry-run] [--limit N]
  scripts/cli.sh kg rewrite-descriptions --all [--dry-run] [--limit N]
  (等价: PYTHONPATH=backend python -m app.scripts.rewrite_concept_descriptions ...)

概念描述按「概念名 + 前 8 条引文」的签名复用,签名里没有模型与提示词,所以换模型或
改提示词之后,名字与引文没变的概念不会被重写。本命令对一个库(或全部库)的多成员
概念强制按**当前**描述模型与提示词重新生成描述;签名口径与 rebuild 的自动复用不变。

* ``--dry-run``:只报告该库多少个概念要重写、预计调用多少次模型,不调模型、不写库。
* ``--limit N``:本次最多调用 N 次模型(``--all`` 时是所有库合计);没轮到的下次续跑。
* 可中断、可续跑:每生成 16 条落一次暂存,按「描述模型 + 提示词指纹」记账;同一来源下
  已生成过的输入不再调模型。中断(Ctrl-C)后重跑同一条命令即可。
* 单条失败只记日志(类名,不记内容)并计数,不中止;重跑会重试失败的那些。
* 可与在线服务同时运行:生成阶段不持任何锁,发布阶段按库持派生代认领几秒钟
  (与「刷新图谱」的单飞同一把);有重建在跑时本库报 busy,稍后重跑即可。

退出码:0 完成(或 dry run);1 未完成(有失败、被 --limit 截断、发布时输入已变化、
有重建在跑),重跑即可;2 参数错误、笔记本不存在、描述阶段关闭或未绑定
``kg_concept_description`` 模型;3 库不在本构建的 schema 版本(本命令从不迁移);
130 被中断。运维手册:``docs/operations_zh.md`` 的「换描述模型或提示词后重写概念描述」。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from app.core.config import get_settings
from app.services.kg.concept_description_rewrite import ConceptDescriptionRewriteUnavailable
from app.services.maintenance_cli import MaintenanceCliError, open_cli_repository

_TOTAL_KEYS = (
    "canonicals", "up_to_date", "to_rewrite", "model_calls", "generated",
    "failed", "remaining", "rewritten", "moved",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rewrite_concept_descriptions",
        description="按当前描述模型与提示词重写一个库（或全部库）的概念描述",
    )
    parser.add_argument("notebook_id", nargs="?", help="要重写的笔记本 id")
    parser.add_argument("--all", action="store_true", help="处理全部笔记本")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告要重写的概念数与预计模型调用次数，不调模型、不写库")
    parser.add_argument("--limit", type=int, default=None,
                        help="本次最多调用多少次模型（--all 时为合计），其余下次续跑")
    return parser


def _progress(notebook_id: str):
    def report(stage: str, done: int, total: int) -> None:
        if stage == "generate" and done != total and done % 16:
            return
        print(f"[rewrite-descriptions] {notebook_id}: {stage} {done}/{total}", flush=True)

    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if bool(args.notebook_id) == bool(args.all):
        parser.error("给出一个 <notebook_id>，或用 --all（二者只能选一）")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须是正整数")
    settings = get_settings()
    totals = {key: 0 for key in _TOTAL_KEYS}
    incomplete: List[str] = []
    try:
        with open_cli_repository(settings) as repository:
            lifecycle = repository._runtime.knowledge_lifecycle
            targets = (repository.maintenance.all_notebook_ids()
                       if args.all else [args.notebook_id])
            budget = args.limit
            for notebook_id in targets:
                try:
                    lifecycle.get_notebook(notebook_id)
                except KeyError:
                    if args.all:
                        continue  # deleted while the run walked the list
                    print(f"error: unknown notebook {notebook_id}", file=sys.stderr)
                    return 2
                report = lifecycle.rewrite_concept_descriptions(
                    notebook_id, dry_run=args.dry_run, limit=budget,
                    progress=None if args.dry_run else _progress(notebook_id))
                if budget is not None:
                    budget = max(0, budget - report.model_calls)
                for key in _TOTAL_KEYS:
                    totals[key] += getattr(report, key)
                if report.status in ("busy", "incomplete"):
                    incomplete.append(notebook_id)
                if report.canonicals or not args.all:
                    line = {k: v for k, v in report.as_dict().items() if k != "version"}
                    print("[rewrite-descriptions] " + json.dumps(line, ensure_ascii=False),
                          flush=True)
    except ConceptDescriptionRewriteUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MaintenanceCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted: finished results are staged; run the same command again "
              "to continue", file=sys.stderr)
        return 130
    label = "dry-run" if args.dry_run else "done"
    print(f"rewrite-descriptions {label}: " + json.dumps(
        {**totals, "incomplete_notebooks": incomplete}, ensure_ascii=False), flush=True)
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
