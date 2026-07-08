#!/usr/bin/env python3
"""检索回放对照:固定问题集跑 reasoning 检索管线(不跑答案 LLM),输出 JSON;
--compare 两份输出逐问题 diff。用于性能优化前后"检索效果不变"的证据验收。

用法(必须从主 checkout 根运行,.env 按 CWD 加载):
  记录:  python scripts/replay_retrieval.py --notebook nb-xxx --questions qs.txt --out a.json
  全流程: python scripts/replay_retrieval.py --notebook nb-xxx --questions qs.txt \
              --full --plan-file plan.json --out a.json
  对照:  python scripts/replay_retrieval.py --compare a.json b.json [--mode exact|topk --k 30]

qs.txt 每行一个问题;plan.json = {"<问题>": ["子查询1", "子查询2", ...]}。
需要 embed 端点可用;不调用任何 LLM(--full 用固定子查询+reflect 直接 answer 的 stub)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def _round(x):
    return round(float(x), 6)


def record_run(notebook_id: str, questions: list, full: bool, plan_map: dict,
               owner: str) -> dict:
    # 引导段:逐行模仿 scripts/batch_ingest.py(见 app.services.batch_ingest)的
    # Settings/repo/set_request_user 初始化——batch_ingest 用的是
    # `repo = SQLiteRepository(Settings())` 直接构造(没有 get_repository()/
    # get_settings() 这类单例 getter,那是 FastAPI app 层的东西),owner 走
    # `_resolve_owner_profile` + `set_request_user`/`reset_request_user` 包裹一段
    # 操作,notebook 存在性校验走 `repo.get_notebook`(不存在则 KeyError)。
    from app.core.config import Settings
    from app.services.sqlite_repository import (
        SQLiteRepository, set_request_user, reset_request_user,
    )
    from app.services.batch_ingest import _resolve_owner_profile

    repo = SQLiteRepository(Settings())
    if not repo.settings.embedder_configured:
        print("ERROR: embed 未配置,回放需要真实查询向量;拒绝静默降级", file=sys.stderr)
        sys.exit(2)

    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        print(f"ERROR: notebook 不存在: {notebook_id}", file=sys.stderr)
        sys.exit(2)

    profile = _resolve_owner_profile(repo, owner)
    token = set_request_user(profile)
    try:
        out: dict = {}
        for q in questions:
            t0 = time.perf_counter()
            kg_hits = repo.retrieval.federated_retrieve(notebook_id, q)
            t1 = time.perf_counter()
            ppr_chunks = repo.retrieval.ppr_retrieve(notebook_id, q)
            t2 = time.perf_counter()
            rec = {
                "kg": [{"id": h.object_id, "relevance": _round(h.relevance),
                        "score": _round(h.score)} for h in kg_hits],
                "ppr_chunks": [{"id": c.chunk_id, "relevance": _round(c.relevance)}
                               for c in ppr_chunks],
                "timings_ms": {"federated": round((t1 - t0) * 1000),
                               "ppr": round((t2 - t1) * 1000)},
            }
            if full:
                rec["full"] = _run_full(repo, notebook_id, q, plan_map.get(q) or [q])
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notebook")
    ap.add_argument("--questions")
    ap.add_argument("--out")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--plan-file")
    ap.add_argument("--owner", default=None,
                    help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--mode", choices=("exact", "topk"), default="exact")
    ap.add_argument("--k", type=int, default=30)
    args = ap.parse_args()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text())
        b = json.loads(Path(args.compare[1]).read_text())
        rep = compare_runs(a, b, mode=args.mode, k=args.k)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        sys.exit(0 if rep["_summary"]["all_pass"] else 1)

    if not (args.notebook and args.questions and args.out):
        ap.error("记录模式需要 --notebook/--questions/--out;或使用 --compare A B")
    questions = [l.strip() for l in Path(args.questions).read_text().splitlines() if l.strip()]
    plan_map = json.loads(Path(args.plan_file).read_text()) if args.plan_file else {}
    out = record_run(args.notebook, questions, args.full, plan_map, args.owner)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"recorded {len(questions)} questions -> {args.out}")


if __name__ == "__main__":
    main()
