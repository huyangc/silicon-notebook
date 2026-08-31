#!/usr/bin/env python3
"""检索回放对照:固定问题集跑 reasoning 检索管线(不跑答案 LLM),输出 JSON;
--compare 两份输出逐问题 diff。用于性能优化前后"检索效果不变"的证据验收。

用法(必须从主 checkout 根运行,.env 按 CWD 加载):
  记录:  python scripts/replay_retrieval.py --notebook nb-xxx --questions qs.txt --out a.json
  报告路径: python scripts/replay_retrieval.py --notebook nb-xxx --questions qs.txt \
              --report-run --out a.json
  全流程: python scripts/replay_retrieval.py --notebook nb-xxx --questions qs.txt \
              --full --plan-file plan.json --out a.json
  对照:  python scripts/replay_retrieval.py --compare a.json b.json [--mode exact|topk --k 30]
  脱敏汇总: python scripts/replay_retrieval.py --compare a.json b.json \
              --mode topk --k 30 --summary-only

qs.txt 每行一个问题;plan.json = {"<问题>": ["子查询1", "子查询2", ...]}。
需要 embed 端点可用;不调用任何 LLM(--full 用固定子查询+reflect 直接 answer 的 stub)。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _round(x):
    return round(float(x), 6)


def record_run(notebook_id: str, questions: list, full: bool, plan_map: dict,
               owner: str, report_run: bool = False) -> dict:
    # 引导段:逐行模仿 scripts/batch_ingest.py(见 app.services.batch_ingest)的
    # Settings/repo/set_request_user 初始化——batch_ingest 用的是
    # `repo = SQLiteRepository(Settings())` 直接构造(没有 get_repository()/
    # get_settings() 这类单例 getter,那是 FastAPI app 层的东西),owner 走
    # `_resolve_owner_profile` + `set_request_user`/`reset_request_user` 包裹一段
    # 操作,notebook 存在性校验走 `repo.get_notebook`(不存在则 KeyError)。
    from app.core.config import Settings
    from app.core.request_context import set_request_user, reset_request_user
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.batch_ingest import _resolve_owner_profile

    repo = SQLiteRepository(Settings())
    if not repo.configured("retrieval_query_embedding"):
        print(
            "ERROR: retrieval_query_embedding workload 未绑定系统模型服务;"
            "回放需要真实查询向量,拒绝静默降级",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        print(f"ERROR: notebook 不存在: {notebook_id}", file=sys.stderr)
        sys.exit(2)

    try:
        profile = _resolve_owner_profile(repo, owner)
    except SystemExit as e:
        # _resolve_owner_profile(与 batch_ingest.py 共用,不改其行为)找不到属主时
        # 自己 sys.exit(默认退出码 1)——与 --compare "两次运行不一致"的退出码 1 撞车。
        # 这里捕获改判为本脚本统一的「前置条件失败」退出码 2,不碰共享函数本身。
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    token = set_request_user(profile)
    try:
        out: dict = {}
        for q in questions:
            # The report-only ANN fast path is keyed on current_retrieval_run().
            # Without this explicit scope, toggling CHUNK_FTS_WITH_ANN_ENABLED
            # does not exercise that branch and produces a misleading no-op A/B.
            from contextlib import nullcontext
            from app.services.retrieval_run import retrieval_run

            run_scope = (
                retrieval_run(
                    run_kind="report_generation",
                    event_log=None,
                    actor_id=str(getattr(profile, "id", "") or ""),
                )
                if report_run
                else nullcontext()
            )
            with run_scope:
                t0 = time.perf_counter()
                kg_hits = repo.retrieval.federated_retrieve(notebook_id, q)
                t1 = time.perf_counter()
                ppr_chunks = repo.retrieval.ppr_retrieve(notebook_id, q)
                t2 = time.perf_counter()
                full_result = (
                    _run_full(repo, notebook_id, q, plan_map.get(q) or [q])
                    if full else None
                )
            rec = {
                "kg": [{"id": h.object_id, "relevance": _round(h.relevance),
                        "score": _round(h.score)} for h in kg_hits],
                "ppr_chunks": [{"id": c.chunk_id, "relevance": _round(c.relevance)}
                               for c in ppr_chunks],
                "timings_ms": {"federated": round((t1 - t0) * 1000),
                               "ppr": round((t2 - t1) * 1000)},
                "retrieval_run_kind": "report_generation" if report_run else "none",
            }
            if full:
                rec["full"] = full_result
            out[q] = rec
        return out
    finally:
        reset_request_user(token)


def _run_full(repo, notebook_id: str, question: str, sub_queries: list) -> dict:
    """--full 层:固定子查询 + reflect 立即 answer,复现 run() 的确定性部分
    (初检索/seed pass/quota 收尾),验证编排层改动(P0-C/P1-B)等价。"""
    from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision

    class _FixedPlanRetriever(ReasoningRetriever):
        def plan(self, question, history=""):
            return [SubQuery(query=s) for s in sub_queries]

        def reflect(self, question, candidates_summary):
            return ReflectDecision(sufficient=True, next_action="answer")

    t0 = time.perf_counter()
    result = _FixedPlanRetriever(repo, repo.settings).run(notebook_id, question)
    return {
        "top_hits": [{"id": h.object_id, "relevance": _round(h.relevance),
                      "score": _round(h.score)} for h in result.top_hits],
        "chunks": [{"id": c.chunk_id, "relevance": _round(c.relevance)}
                   for c in result.chunks],
        "total_ms": round((time.perf_counter() - t0) * 1000),
    }


def _seq(rec: dict, key: str) -> list:
    return [(r["id"], r.get("relevance")) for r in rec.get(key) or []]


def _cmp_section(a: dict, b: dict, key: str, mode: str, k: int) -> dict:
    sa, sb = _seq(a, key), _seq(b, key)
    if mode == "exact":
        return {"pass": sa == sb, "len_a": len(sa), "len_b": len(sb)}
    ta = [i for i, _ in sa[:k]]
    tb = [i for i, _ in sb[:k]]
    inter = len(set(ta) & set(tb))
    denom = max(len(ta), len(tb)) or 1
    return {"pass": inter == denom, "overlap": inter / denom,
            "order_equal": ta == tb}


def compare_runs(a: dict, b: dict, mode: str = "exact", k: int = 30) -> dict:
    report: dict = {}
    all_pass = True
    for q in sorted(set(a) | set(b)):
        ra, rb = a.get(q), b.get(q)
        if ra is None or rb is None:
            report[q] = {"pass": False, "reason": "missing_in_one_run"}
            all_pass = False
            continue
        entry = {}
        for key in ("kg", "ppr_chunks"):
            entry[key] = _cmp_section(ra, rb, key, mode, k)
        if "full" in ra and "full" in rb:
            for key in ("top_hits", "chunks"):
                entry[f"full.{key}"] = _cmp_section(ra["full"], rb["full"], key, mode, k)
        entry_pass = all(v.get("pass") for v in entry.values())
        entry["pass"] = entry_pass
        all_pass = all_pass and entry_pass
        report[q] = entry
    report["_summary"] = {"all_pass": all_pass, "mode": mode, "k": k,
                          "questions": len([x for x in report if x != "_summary"])}
    return report


def _nearest(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def summarize_comparison(report: dict, a: dict, b: dict) -> dict:
    """Return a paste-safe aggregate: no question strings or retrieval ids."""
    sections: dict[str, list[dict]] = {}
    failed = 0
    for question, comparison in report.items():
        if question == "_summary":
            continue
        if not comparison.get("pass"):
            failed += 1
        for section, result in comparison.items():
            if section == "pass" or not isinstance(result, dict):
                continue
            sections.setdefault(section, []).append(result)

    section_summary = {}
    for section, results in sorted(sections.items()):
        entry = {
            "comparisons": len(results),
            "pass": sum(1 for result in results if result.get("pass")),
        }
        overlaps = [
            float(result["overlap"])
            for result in results
            if isinstance(result.get("overlap"), (int, float))
        ]
        if overlaps:
            entry.update({
                "overlap_mean": round(sum(overlaps) / len(overlaps), 6),
                "overlap_min": round(min(overlaps), 6),
                "order_equal": sum(
                    1 for result in results if result.get("order_equal") is True
                ),
            })
        section_summary[section] = entry

    timing_summary = {}
    for label, path in (
        ("federated_ms", ("timings_ms", "federated")),
        ("ppr_ms", ("timings_ms", "ppr")),
        ("full_total_ms", ("full", "total_ms")),
    ):
        sides = []
        for payload in (a, b):
            values = []
            for record in payload.values():
                value = record
                for key in path:
                    value = value.get(key) if isinstance(value, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.append(float(value))
            sides.append(values)
        if sides[0] or sides[1]:
            timing_summary[label] = {
                "a": {
                    "n": len(sides[0]),
                    "p50": _nearest(sides[0], 0.50),
                    "p95": _nearest(sides[0], 0.95),
                },
                "b": {
                    "n": len(sides[1]),
                    "p50": _nearest(sides[1], 0.50),
                    "p95": _nearest(sides[1], 0.95),
                },
            }

    summary = dict(report.get("_summary") or {})
    summary["failed_questions"] = failed
    summary["sections"] = section_summary
    summary["timings"] = timing_summary
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notebook")
    ap.add_argument("--questions")
    ap.add_argument("--out")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--report-run", action="store_true",
        help=(
            "每个问题放进独立 report_generation retrieval run；用于真实触发 "
            "CHUNK_FTS_WITH_ANN_ENABLED 的报告 ANN-only / ANN+FTS A/B"
        ),
    )
    ap.add_argument("--plan-file")
    ap.add_argument("--owner", default="admin",
                    help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--mode", choices=("exact", "topk"), default="exact")
    ap.add_argument("--k", type=int, default=30)
    ap.add_argument(
        "--summary-only", action="store_true",
        help="--compare 时只输出无问题文本/无命中 id 的汇总与两侧耗时分位数",
    )
    args = ap.parse_args()

    if args.summary_only and not args.compare:
        ap.error("--summary-only requires --compare A B")
    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text())
        b = json.loads(Path(args.compare[1]).read_text())
        rep = compare_runs(a, b, mode=args.mode, k=args.k)
        rendered = summarize_comparison(rep, a, b) if args.summary_only else rep
        print(json.dumps(rendered, ensure_ascii=False, indent=2))
        sys.exit(0 if rep["_summary"]["all_pass"] else 1)

    if not (args.notebook and args.questions and args.out):
        ap.error("记录模式需要 --notebook/--questions/--out;或使用 --compare A B")
    questions = [l.strip() for l in Path(args.questions).read_text().splitlines() if l.strip()]
    plan_map = json.loads(Path(args.plan_file).read_text()) if args.plan_file else {}
    out = record_run(
        args.notebook, questions, args.full, plan_map, args.owner, args.report_run
    )
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"recorded {len(questions)} questions -> {args.out}")


if __name__ == "__main__":
    main()
