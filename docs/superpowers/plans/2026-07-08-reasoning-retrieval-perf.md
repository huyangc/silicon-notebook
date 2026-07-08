# Reasoning 检索性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 300 万 KG 节点 / 50 万 chunk 部署规模下，把 reasoning 模式（规划→检索→漫游→反思→合成）前三步各 20-30s 的耗时压下来，且除 PPR float32/tol 项（用户已接受数值波动）外**检索结果逐位不变**。

**Architecture:** 五个独立优化 + 观测配套：(1) ask_stage 埋点 + 检索回放对照脚本（验证"不变"的证据工具）；(2) P0-A 版本探针 O(1) 化——新增 `cluster_mutation_seq` DB 计数器替代每次查询对百万行 `concept_clusters` 跑 COUNT/MAX，`_keyword_token_sets` 的 ANN 有界路径跳过 COUNT；(3) P0-C PPR seed pass 与 plan LLM 并行（纯调度）；(4) P1-A per-ask embed 缓存（ContextVar，单点改 `_embed_query`）；(5) P1-B `_quota_rerank` 复用初检索打分（省 5+ 次全量重检索）；(6) P0-B PPR float32 + tol 放宽（唯一动数值项）。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / scipy CSR / hnswlib / pytest。

## Global Constraints

- **效果不变**：除 Task 7（P0-B，用户已接受波动）外，所有任务必须保证检索结果逐位一致；每个任务的测试必须包含等价性断言（on/off 对比或调用计数）。
- **效率第一**（用户强制）：不新增任何 LLM/embed/DB 每查询调用；埋点本身必须是 O(1) 廉价操作。
- **schema 迁移约定**：改 `unified_kg_state` 表必须新增 `_migration_5` + bump `SCHEMA_VERSION = 5`（backend/app/services/sqlite_repository.py:245，当前=4），绝不能只改 `_migration_1` baseline；已部署库靠 `_add_column_if_missing` 补列（先例：sqlite_repository.py:1129 的 kg_mutation_seq）。
- **pydantic 坑**：`app/core/config.py` 是 pydantic-settings v2，新配置项的环境变量映射必须用 `validation_alias=`（`Field(env=...)` 失效）。
- **测试命令**：`cd backend && PYTHONPYCACHEPREFIX=../.local/pycache ${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python} -m pytest tests/ -q`（单文件加路径）。全量必须绿。
- **git**：所有任务在当前 worktree 分支 `claude/clever-merkle-631d2e` 上按任务顺序提交；最终一个 PR（各优化有独立 env 逃生口：`REASONING_PPR_PREFETCH` / `REASONING_QUOTA_REUSE` / `PPR_FLOAT32` / `PPR_TOL`，回滚粒度靠 flag）。
- 中文注释风格与现有代码一致；提交信息格式仿现有历史（`feat(retrieval): ...` / `perf(...): ...`），结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 大文件警告：`sqlite_repository.py` 有 1.2 万行，编辑前先 grep 定位、Read 目标片段，勿通读。

---

### Task 1: ask_stage 埋点（检索步内分解 + PPR 迭代统计）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_retrieve_scored` ~10657-10772；`scale_ppr` ~10011-10179）
- Modify: `backend/app/services/kg/scale_index.py`（`personalized_ppr` 110-137）
- Test: `backend/tests/test_ask_stage_events.py`（新建）

**Interfaces:**
- Produces: `personalized_ppr(transition, reset, damping=0.5, tol=1e-8, max_iter=100, stats=None)` —— 新增可选 `stats: dict`，函数结束时写入 `stats["iters"] = <实际迭代轮数>`。传 None（默认）行为完全不变。Task 7 复用此签名。
- Produces: events.jsonl 中两类新事件：`kind="ask_stage"`（site="_retrieve_scored"，含各阶段 ms）与 `kind="scale_ppr_done"`（含 iters/各阶段 ms）。回放脚本（Task 2）不依赖这些事件（自己计时），它们服务生产诊断。

**等价性论证：** 纯观测。`time.perf_counter()` 差值 + 每次调用一条 `event_log.emit`（一次 jsonl append，~10μs 级），无任何检索逻辑变化。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ask_stage_events.py
"""ask_stage 埋点:_retrieve_scored 每次调用 emit 一条阶段耗时事件;
personalized_ppr 的 stats 出参回报迭代轮数。纯观测,不改检索结果。"""
import numpy as np
import scipy.sparse as sp

from app.services.kg import scale_index as si


def test_personalized_ppr_stats_reports_iters():
    # 3 节点环图,列随机;stats 出参收集迭代轮数
    A = sp.csr_matrix(np.array([[0, 0, 1.0], [1.0, 0, 0], [0, 1.0, 0]]))
    reset = np.array([1.0, 0.0, 0.0])
    stats = {}
    x = si.personalized_ppr(A, reset, damping=0.5, stats=stats)
    assert x.shape == (3,)
    assert stats.get("iters", 0) >= 1


def test_personalized_ppr_stats_none_unchanged():
    # 默认 stats=None 路径:结果与传 dict 完全一致(纯观测不改数值)
    A = sp.csr_matrix(np.array([[0, 0, 1.0], [1.0, 0, 0], [0, 1.0, 0]]))
    reset = np.array([1.0, 0.0, 0.0])
    x1 = si.personalized_ppr(A, reset, damping=0.5)
    x2 = si.personalized_ppr(A, reset, damping=0.5, stats={})
    assert np.array_equal(x1, x2)


def test_retrieve_scored_emits_ask_stage(repo_factory):
    repo, nb = repo_factory()
    repo._retrieve_scored(nb, "什么是带隙基准")
    kinds = [e.get("kind") for e in repo.event_log.events]
    assert "ask_stage" in kinds
    ev = next(e for e in repo.event_log.events if e.get("kind") == "ask_stage")
    assert ev.get("site") == "_retrieve_scored"
    assert "total_ms" in ev and "embed_ms" in ev and "score_ms" in ev
```

关于 `repo_factory` fixture：先查 `backend/tests/conftest.py` 已有的 repo 构造 fixture（grep `def repo` / `SQLiteRepository(`），复用现有模式（临时目录 + 无 embed 配置的 Settings + 建一个 notebook + 塞 1-2 个 knowledge object）。若 conftest 已有等价 fixture 直接用其名字；若没有，在本测试文件内定义局部 fixture（参考 `backend/tests/test_ask_modes.py` 等现有测试如何构造 repo——先读一个现有测试文件再写）。`repo.event_log.events` 若不存在（event_log 只写文件），改为 monkeypatch `repo.event_log.emit` 收集到 list：

```python
def _capture_events(repo):
    captured = []
    orig = repo.event_log.emit
    repo.event_log.emit = lambda e: (captured.append(e), orig(e))[1]
    return captured
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python} -m pytest tests/test_ask_stage_events.py -v`
Expected: FAIL（`personalized_ppr() got an unexpected keyword argument 'stats'` / ask_stage 事件缺失）

- [ ] **Step 3: 实现**

3a. `backend/app/services/kg/scale_index.py` 的 `personalized_ppr`（110-137 行）加 `stats: dict = None` 形参，循环内计数：

```python
def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
    stats: dict = None,
) -> "np.ndarray":
    """... 原 docstring 保留,追加一行:
    stats: 可选出参 dict,写入 {"iters": 实际迭代轮数}(纯观测,不影响数值)。
    """
    s = float(reset.sum())
    if s <= 0:
        if stats is not None:
            stats["iters"] = 0
        return np.zeros(transition.shape[0], dtype=np.float64)
    p = (reset.astype(np.float64) / s)
    x = p.copy()
    d = float(damping)
    iters = 0
    for _ in range(max_iter):
        iters += 1
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - x_new.sum()) * p
        if np.abs(x_new - x).sum() < tol:
            x = x_new
            break
        x = x_new
    if stats is not None:
        stats["iters"] = iters
    total = x.sum()
    return x / total if total > 0 else x
```

3b. `sqlite_repository.py` `_retrieve_scored`：在函数开头 `t0 = time.perf_counter()`，在 embed 后、ANN 候选后、DB hydrate+边查询后、打分后各记一次 `time.perf_counter()`，函数 return 前 emit（`time` 已在文件顶部 import，确认无则加）：

```python
        # ask_stage 埋点(纯观测):阶段墙钟拆解,生产诊断 20-30s 级检索用。
        self.event_log.emit({
            "kind": "ask_stage", "site": "_retrieve_scored",
            "notebook_id": notebook_id,
            "embed_ms": round((t_embed - t0) * 1000),
            "ann_ms": round((t_ann - t_embed) * 1000),
            "hydrate_ms": round((t_hydrate - t_ann) * 1000),
            "score_ms": round((t_score - t_hydrate) * 1000),
            "total_ms": round((t_score - t0) * 1000),
            "candidates": len(all_kg_objs),
            "ann_gated": cand_sims is not None,
        })
```

计时点插入位置：`t0` 在 `type_list = ...` 前；`t_embed` 在 `query_vector = self._embed_query(query)` 后；`t_ann` 在 ANN 候选块（`if query_vector is not None: ... cand_sims = ...`，含 FTS 兜底块）整体之后；`t_hydrate` 在 `with self._connect() as db:` 块结束后（isolated_ids 计算完）；`t_score` 在 `scored.sort(...)` 之后、canonical fold 之前。emit 放 fold 之后 return 之前（fold 耗时并入观察：再加 `fold_ms": round((time.perf_counter() - t_score) * 1000)`）。

3c. `scale_ppr`（~10150）：调用处传 stats 并 emit 完成事件：

```python
        t_ppr0 = time.perf_counter()
        _ppr_stats: dict = {}
        x = si.personalized_ppr(combined_A, reset, damping=self.settings.ppr_damping,
                                stats=_ppr_stats)
        # ... 现有 zero_ppr_mass bail 保持不动 ...
```

在函数末尾 `norm.sort(...)` 后、`return norm` 前：

```python
        self.event_log.emit({
            "kind": "scale_ppr_done", "notebook_id": notebook_id,
            "iters": _ppr_stats.get("iters", -1),
            "ppr_ms": round((time.perf_counter() - t_ppr0) * 1000),
            "nodes": len(combined_ids), "seeds": ann_seeds + active_seeds + chunk_seeds,
            "chunks_ranked": len(norm),
        })
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_ask_stage_events.py -v`
Expected: PASS

- [ ] **Step 5: 跑全量测试**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/ -q`
Expected: 全绿（现有 personalized_ppr 调用者不受影响——新参有默认值）

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_ask_stage_events.py backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py
git commit -m "feat(observability): reasoning 检索 ask_stage 埋点+PPR 迭代统计(纯观测)"
```

---

### Task 2: 检索回放对照脚本 scripts/replay_retrieval.py

**Files:**
- Create: `scripts/replay_retrieval.py`
- Modify: `README.md`、`README_zh.md`（新增 CLI 用法小节——用户约定：新 CLI 必须进两个 README）
- Test: `backend/tests/test_replay_retrieval.py`（新建）

**Interfaces:**
- Consumes: `repo.retrieval.federated_retrieve(nb, q)`、`repo.retrieval.ppr_retrieve(nb, q)`（现有原语）；`ReasoningRetriever`（--full 层）。
- Produces: CLI：`python scripts/replay_retrieval.py --notebook <id> --questions <file> --out a.json [--full --plan-file plan.json]` 与 `--compare a.json b.json [--mode exact|topk --k 30]`。输出 JSON 结构见 Step 3。上线验收工具：改动前后各跑一次，`--compare` 全 PASS = 效果不变的证据。

**设计要点（实现者必读）：**
- 脚本必须从**主 checkout 根**运行（`.env` 按 CWD 加载——参照 `scripts/batch_ingest.py` 开头的 sys.path/env 处理方式，直接复制其引导段）。
- 需要 embed 端点可用（回放要真实向量化查询）；LLM **不需要**——默认层只跑检索原语；`--full` 层用 `--plan-file` 里的固定子查询 + reflect 直接 answer 的 stub，绕过所有 LLM。
- owner：复用 `batch_ingest.py` 的 `--owner`（默认 admin）+ `set_request_user` 模式（先读 batch_ingest.py 怎么做的，逐行模仿）。
- 分数序列化 `round(x, 6)`：float32 化（Task 7）后分数会变，`--mode exact` 比较 id 序列 + 分数；`--mode topk` 只比较前 k 个 id 的集合重叠率与序（给 Task 7 用）。

- [ ] **Step 1: 写失败测试（compare 逻辑纯函数）**

```python
# backend/tests/test_replay_retrieval.py
"""回放对照的 compare 纯函数:exact 逐位比较与 topk 集合重叠。"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "replay_retrieval",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "replay_retrieval.py")
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def _rec(ids_scores):
    return {"kg": [{"id": i, "relevance": s} for i, s in ids_scores],
            "ppr_chunks": [{"id": i, "relevance": s} for i, s in ids_scores]}


def test_compare_exact_pass():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["q1"]["kg"]["pass"] is True and rep["_summary"]["all_pass"] is True


def test_compare_exact_fail_on_reorder():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("y", 0.5), ("x", 0.9)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["_summary"]["all_pass"] is False


def test_compare_topk_overlap():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5), ("z", 0.1)])}
    b = {"q1": _rec([("x", 0.8), ("z", 0.6), ("y", 0.2)])}
    rep = replay.compare_runs(a, b, mode="topk", k=2)
    # top-2: {x,y} vs {x,z} → overlap 0.5
    assert abs(rep["q1"]["kg"]["overlap"] - 0.5) < 1e-9
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_replay_retrieval.py -v`
Expected: FAIL（文件不存在）

- [ ] **Step 3: 实现脚本**

```python
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
    # 引导段:逐行模仿 scripts/batch_ingest.py 的 Settings/repo/set_request_user 初始化
    # (读 batch_ingest.py 后复制其模式,包括 --owner 解析与用户上下文设置)。
    from app.core.config import get_settings
    from app.services.repository import get_repository  # 以 batch_ingest.py 实际 import 为准
    settings = get_settings()
    if not settings.embedder_configured:
        print("ERROR: embed 未配置,回放需要真实查询向量;拒绝静默降级", file=sys.stderr)
        sys.exit(2)
    repo = get_repository()
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


def _run_full(repo, notebook_id: str, question: str, sub_queries: list) -> dict:
    """--full 层:固定子查询 + reflect 立即 answer,复现 run() 的确定性部分
    (初检索/seed pass/quota 收尾),验证编排层改动(P0-C/P1-B)等价。"""
    from app.core.config import get_settings
    from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision

    class _FixedPlanRetriever(ReasoningRetriever):
        def plan(self, question, history=""):
            return [SubQuery(query=s) for s in sub_queries]

        def reflect(self, question, candidates_summary):
            return ReflectDecision(sufficient=True, next_action="answer")

    t0 = time.perf_counter()
    result = _FixedPlanRetriever(repo, get_settings()).run(notebook_id, question)
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
    ap.add_argument("--owner", default="admin")
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
```

实现时必做的两个核对：(1) 读 `scripts/batch_ingest.py` 开头，把 repo 初始化 + `--owner` + `set_request_user` 的真实写法搬进 `record_run`（上面 `get_repository` 是占位，以 batch_ingest 实际用法为准）；(2) `ppr_retrieve` 原语在 `repo.retrieval` 上的确切方法名（reasoning_retrieval.py:119 是 `self.repo.retrieval.ppr_retrieve`，直接可用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_replay_retrieval.py -v`
Expected: PASS

- [ ] **Step 5: README 补 CLI 用法**

`README.md` 与 `README_zh.md` 的 CLI/脚本小节（找到 batch_ingest 的段落，紧随其后）各加一段：脚本名、三种用法（记录/全流程/对照）、"需 embed 端点、不调 LLM"、退出码（compare 不一致时非 0）。保持通用口径（不写机器路径）。

- [ ] **Step 6: Commit**

```bash
git add scripts/replay_retrieval.py backend/tests/test_replay_retrieval.py README.md README_zh.md
git commit -m "feat(scripts): 检索回放对照 CLI(效果不变验收工具)"
```

---

### Task 3: P0-A 版本探针 O(1) 化（cluster_mutation_seq + kwtok bounded）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`：
  - `SCHEMA_VERSION`（245）4→5；新增 `_migration_5`（放在 `_migration_4` 之后，~1226 之后）
  - `unified_kg_state` CREATE TABLE（~889-897）加列
  - 新增 helper `_bump_cluster_mutation_seq`（放 `_mark_unified_kg_dirty` 旁，~5964）
  - 4 个写点：`write_clusters`（~4743）、`append_clusters`（~4762）、`incremental_fuse_source` orphan 清理（~4798）、`_persist_clusters_streaming`（~6395）
  - `_probe_scale_version_signal`（~8196-8207）、`_scale_index_version`（~8298-8329）、`_compute_scale_version_cold`（~8209-8235）
  - `_keyword_token_sets`（7946-7967）加 `bounded` 参数；调用点 `_retrieve_scored`（10705）
- Test: `backend/tests/test_scale_version_probe.py`（新建）

**Interfaces:**
- Consumes: `_mark_unified_kg_dirty` 的 UPSERT 模式（5972-5984）；`_add_column_if_missing`（迁移先例 1129）。
- Produces: `unified_kg_state.cluster_mutation_seq INTEGER NOT NULL DEFAULT 0` 列；`_bump_cluster_mutation_seq(db, notebook_id)`（**注意：接收已打开的写事务 db，在调用方事务内执行**，与 `_mark_unified_kg_dirty` 自开事务不同——4 个写点都已持有 `self._write()` 块）；`_probe_scale_version_signal` 返回值从 `(seq, clu_key, settings_tail)` 变为 `(seq, cseq, settings_tail)`（clu_key 改在冷路径算）。

**等价性论证（写进 PR 描述）：** 磁盘 manifest.version 的**格式与内容都不变**（冷路径仍算 concept_clusters COUNT/MAX 塞进 version list）；变的只是热路径 memo 的失效信号：`(seq, COUNT, MAX)` → `(seq, cseq)`。「clusters 内容变 ⇒ 必经 4 写点之一 ⇒ 同事务 bump cseq ⇒ memo miss ⇒ 冷路径重算真实 COUNT/MAX」。cseq 是 DB 行，跨进程可见（CLI rebuild 后端进程读得到——这是不能用纯内存失效的原因）。反向（cseq 变但内容没变）只多付一次冷聚合，无正确性影响。相比现状还**修复**一个盲区：同秒等基数重写 clusters（COUNT/MAX 都不变）现状探测不到，cseq 必变。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_scale_version_probe.py
"""P0-A:cluster_mutation_seq 列 + 探针 O(1) 化的行为契约。
核心不变量:任何 concept_clusters 写路径之后,_scale_index_version 必须反映变化
(经 cseq bump→memo miss→冷路径重算);热路径不再每次跑 concept_clusters COUNT/MAX。"""


def _cseq(repo, nb):
    with repo._connect() as db:
        row = db.execute(
            "SELECT cluster_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb,)).fetchone()
    return int(row["cluster_mutation_seq"]) if row else 0


def test_migration_adds_cluster_seq_column(repo_factory):
    repo, nb = repo_factory()
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(unified_kg_state)")}
    assert "cluster_mutation_seq" in cols


def test_write_clusters_bumps_cseq(repo_factory):
    repo, nb = repo_factory()
    before = _cseq(repo, nb)
    repo.write_clusters(nb, [{"canonical_id": "c1", "member_object_id": "o1",
                              "canonical_name": "N"}])
    assert _cseq(repo, nb) > before


def test_append_clusters_bumps_cseq(repo_factory):
    repo, nb = repo_factory()
    before = _cseq(repo, nb)
    repo.append_clusters(nb, [{"canonical_id": "c2", "member_object_id": "o2",
                               "canonical_name": "M"}])
    assert _cseq(repo, nb) > before


def test_version_memo_hit_skips_cluster_aggregates(repo_factory, monkeypatch):
    """热路径(memo 命中)不得对 concept_clusters 跑 COUNT/MAX——探针只读
    unified_kg_state 单行。通过统计 SQL 文本断言。"""
    repo, nb = repo_factory()
    repo._scale_index_version(nb)          # 冷路径,填 memo
    seen_sql = []
    real_connect = repo._connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner
        def execute(self, sql, *a):
            seen_sql.append(sql)
            return self._inner.execute(sql, *a)
        def __enter__(self):
            self._inner.__enter__()
            return self
        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(repo, "_connect", lambda: _SpyConn(real_connect()))
    repo._scale_index_version(nb)          # 热路径
    cluster_aggr = [s for s in seen_sql
                    if "concept_clusters" in s and ("COUNT" in s or "MAX" in s)]
    assert cluster_aggr == []


def test_version_changes_after_cluster_write(repo_factory):
    """写 clusters 后 version key 必须变化(memo 失效→冷路径重算 COUNT/MAX)。"""
    repo, nb = repo_factory()
    v1 = repo._scale_index_version(nb)
    repo.write_clusters(nb, [{"canonical_id": "c1", "member_object_id": "o1",
                              "canonical_name": "N"}])
    v2 = repo._scale_index_version(nb)
    assert v1 != v2


def test_kwtok_bounded_skips_count_and_matches_live(repo_factory, monkeypatch):
    """bounded=True:不跑 knowledge_objects COUNT,token set 与非缓存构建逐字节等价。"""
    from app.services.retrieval import _tokens, _payload_text
    repo, nb = repo_factory()
    objs = [{"id": "o1", "payload": {"name": "带隙基准", "statement": "PTAT 电流"},
             "evidence": []}]
    with repo._connect() as db:
        seen_sql = []
        orig_exec = db.execute
        db.execute = lambda sql, *a: (seen_sql.append(sql), orig_exec(sql, *a))[1]
        ts = repo._keyword_token_sets(db, nb, objs, bounded=True)
    assert not any("COUNT" in s for s in seen_sql)
    expected = frozenset(_tokens(f"{_payload_text(objs[0]['payload'])} "))
    assert ts["o1"] == expected
```

repo_factory 同 Task 1（若 Task 1 已把 fixture 放 conftest.py 则直接复用；否则本文件自建，需要 `write_clusters`/`append_clusters` 可跑——它们只写表，无外部依赖）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_scale_version_probe.py -v`
Expected: FAIL（列不存在 / bounded 参数不存在 / 热路径仍跑 COUNT）

- [ ] **Step 3: 实现**

3a. schema：CREATE TABLE `unified_kg_state`（~889）列清单里加 `cluster_mutation_seq INTEGER NOT NULL DEFAULT 0,`（放 kg_mutation_seq 旁）。`SCHEMA_VERSION = 5`（245）。新增迁移（仿 `_migration_4` 的结构，注意 `_migrate` 的分发方式——先读 `_migrate` 怎么按版本号调 `_migration_N`，保证 `_migration_5` 会被旧库执行到）：

```python
    def _migration_5(self) -> None:
        """cluster_mutation_seq: concept_clusters 写路径的单调计数器(P0-A 探针
        O(1) 化)。DEFAULT 0 = "从未 bump":首次探针 memo 必 miss→冷路径重算,
        旧库升级后行为无缝。"""
        with self._write() as db:
            self._add_column_if_missing(
                db, "unified_kg_state", "cluster_mutation_seq",
                "INTEGER NOT NULL DEFAULT 0")
```

3b. bump helper（放 `_mark_unified_kg_dirty` 后面）：

```python
    def _bump_cluster_mutation_seq(self, db, notebook_id: str) -> None:
        """concept_clusters 写路径的单调计数器 bump。与 _mark_unified_kg_dirty 不同,
        本 helper 在调用方已持有的写事务 db 内执行(写簇+bump 同 commit,原子——
        不存在"簇写了、seq 没 bump"的窗口)。kg_mutation_seq 不在此处动:rebuild
        刻意保持它稳定(幂等,见 _cluster_input_version),clusters 的变化信号独立成列。"""
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, cluster_mutation_seq, updated_at)
            VALUES (?, 0, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              cluster_mutation_seq=unified_kg_state.cluster_mutation_seq+1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, _now()),
        )
```

注意 INSERT 分支 `dirty=0`（新行不该被标脏——bump cseq 不代表 KG 主体变了）；UPDATE 分支不动 dirty。核对 unified_kg_state 的 NOT NULL 列（读 CREATE TABLE）：若还有其它 NOT NULL 无默认列（如 kg_mutation_seq 有 DEFAULT 0 则无碍），INSERT 需补上。

3c. 4 个写点，在各自 `with self._write() as db:` 块的**末尾**（写完簇行后）加一行 `self._bump_cluster_mutation_seq(db, notebook_id)`：
- `write_clusters`：DELETE+INSERT 循环后。
- `append_clusters`：仅 `added > 0` 时（在循环后、`with` 块内加 `if added: self._bump_cluster_mutation_seq(db, notebook_id)`）。
- `incremental_fuse_source` 的 orphan 清理：`cur = db.execute("DELETE FROM concept_clusters ...")` 后 `if cur.rowcount > 0: self._bump_cluster_mutation_seq(db, notebook_id)`。
- `_persist_clusters_streaming`：`wdb` 事务末尾（buf flush 完）`self._bump_cluster_mutation_seq(wdb, notebook_id)`。

3d. 探针改造。`_probe_scale_version_signal`（8196-8207）：

```python
        with self._connect() as db:
            st = db.execute(
                "SELECT kg_mutation_seq, cluster_mutation_seq FROM unified_kg_state "
                "WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            seq = int(st["kg_mutation_seq"]) if st else 0
            cseq = int(st["cluster_mutation_seq"]) if st else 0
        return seq, cseq, settings_tail
```

（settings_tail 的来源保持原样——先读该函数完整实现确认 settings_tail 在哪算的，只替换 clusters 聚合那一段。）

`_compute_scale_version_cold`：形参 `clu_key` 删除，函数内自己算（保持 version list 内容与格式**逐位不变**）：

```python
    def _compute_scale_version_cold(self, notebook_id: str, seq: int,
                                     settings_tail: tuple) -> list:
        """冷路径:五表聚合(clusters 聚合从热路径移到这里——P0-A 后热路径只读
        unified_kg_state 单行,COUNT/MAX 只在 memo miss 时算)。version list 的
        内容与格式与 P0-A 前逐位一致,磁盘 manifest.version 兼容性不受影响。"""
        with self._connect() as db:
            obj_ver = db.execute(...)   # 原四个聚合保持不动
            rel_ver = db.execute(...)
            chunk_ver = db.execute(...)
            emb_ver = db.execute(...)
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchone()
        return [
            notebook_id,
            int(obj_ver["c"]), obj_ver["ts"],
            int(rel_ver["c"]), rel_ver["ts"],
            int(chunk_ver["c"]), chunk_ver["ts"],
            int(clu_ver["c"]), clu_ver["ts"],
            int(emb_ver["c"]), emb_ver["ts"],
            *settings_tail,
        ]
```

（**核对原函数**：clu_key 在 version list 里的位置必须保持第 8、9 位——对照 8227-8235 原实现逐位核对。）

`_scale_index_version`：memo 元组从 `(seq, clu_key, settings_tail, version)` 改为 `(seq, cseq, settings_tail, version)`；两处对比与两处 `_probe_scale_version_signal` 解包相应改名；`_compute_scale_version_cold` 调用去掉 clu_key 实参。docstring 更新（clusters 现在靠 cluster_mutation_seq，冷路径才聚合）。

3e. `_keyword_token_sets` 加 bounded：

```python
    def _keyword_token_sets(self, db, notebook_id: str, objects: list,
                            bounded: bool = False) -> dict:
        """... 原 docstring 保留,追加:
        bounded=True(ANN 门控的有界候选路径):跳过版本 COUNT 与进程缓存,直接对
        本批 objects 现场构建(与 _load 同构建逻辑,逐字节等价——为 ≤recall 个候选
        付一次百万行 COUNT 是倒挂;该缓存对每查询候选集不同的 ANN 路径也从未命中过)。"""
        from app.services.retrieval import _tokens, _payload_text

        def _build(objs):
            out = {}
            for o in objs:
                ev_text = " ".join(e.quoted_span for e in o.get("evidence", []))
                out[o["id"]] = frozenset(_tokens(f"{_payload_text(o['payload'])} {ev_text}"))
            return out

        if bounded:
            return _build(objects)
        ver = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,)).fetchone()
        version = ("kwtok", ver["c"], ver["ts"])
        return self._vector_cache.get(f"{notebook_id}:kwtok", version, lambda: _build(objects))
```

调用点 10705：`token_sets = self._keyword_token_sets(db, notebook_id, all_kg_objs, bounded=cand_sims is not None)`。

3f. 全仓 grep `_compute_scale_version_cold\|_probe_scale_version_signal` 确认没有其它调用者残留旧签名。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_scale_version_probe.py -v`
Expected: PASS

- [ ] **Step 5: 跑全量测试（重点看 schema/migration/scale 相关）**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/ -q`
Expected: 全绿。若 migration 测试断言列清单/版本号，按 SCHEMA_VERSION 惯例更新（先例：test(admin) migration 测试用 SCHEMA_VERSION 常量非硬编码）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_version_probe.py
git commit -m "perf(retrieval): 版本探针 O(1) 化(cluster_mutation_seq 列+kwtok 有界路径免 COUNT;_migration_5)"
```

---

### Task 4: P0-C PPR seed pass 与 plan 并行

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`（`run()` 230-316 区间）
- Modify: `backend/app/core/config.py`（新 flag）
- Test: `backend/tests/test_reasoning_ppr_prefetch.py`（新建）

**Interfaces:**
- Consumes: `self.ppr_retrieve(notebook_id, query)`（既有薄封装 119 行）；`contextvars.copy_context()` 模式（参照 `app/services/report_engine.py:310`）。
- Produces: `settings.reasoning_ppr_prefetch: bool`（env `REASONING_PPR_PREFETCH`，默认 True）；False 时走原串行路径（逐字节保留）。

**等价性论证：** PPR seed pass 输入 = 原问题 + 只读图状态，一次 run 内无写；提前 submit、在**原位置** `future.result()`，`seen_chunks` 合并时序 / trace record 顺序 / chunks 插入顺序全部不变；`result()` 重抛异常 = 与现状串行抛出同语义。唯一实现要求：`copy_context().run` 传播 `_REQUEST_USER`（per-user 模型解析）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_reasoning_ppr_prefetch.py
"""P0-C:PPR seed pass 预取。等价性=预取 on/off 的 ReasoningResult 逐位一致;
并发正确性=ppr_retrieve 在后台线程仍能读到请求用户 ContextVar。"""
import contextvars
import threading

from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision


class _StubSettings:
    retrieval_top_n = 12
    reasoning_max_steps = 5
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 3
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = False
    graph_ppr_enabled = True
    reasoning_ppr_prefetch = True
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


class _StubRetrieval:
    """检索原语 stub:确定性返回,并记录 ppr 调用发生的线程。"""
    def __init__(self):
        self.ppr_threads = []

    def federated_retrieve(self, nb, q, types=None, w_keyword=0.4, w_semantic=0.6):
        return []

    def retrieve_scored(self, nb, q):
        return []

    def ppr_retrieve(self, nb, q):
        self.ppr_threads.append(threading.current_thread().name)
        from app.services.retrieval import RetrievedChunk
        return [RetrievedChunk(chunk_id=f"ch-{q[:4]}", source_id="s1",
                               source_title="t", section_path="p",
                               text="正文", relevance=0.9, score=0.9)]


class _StubRepo:
    def __init__(self):
        self.retrieval = _StubRetrieval()
        self.reasoning_llm_client = type("C", (), {"configured": False})()


def _mk(settings=None):
    repo = _StubRepo()
    r = ReasoningRetriever(repo, settings or _StubSettings())
    # plan/reflect 固定:1 个子查询,反思立即 answer
    r.plan = lambda question, history="": [SubQuery(query=question)]
    r.reflect = lambda question, s: ReflectDecision(sufficient=True, next_action="answer")
    return repo, r


def test_prefetch_result_identical_to_serial():
    s_on = _StubSettings()
    s_off = _StubSettings()
    s_off.reasoning_ppr_prefetch = False
    _, r_on = _mk(s_on)
    _, r_off = _mk(s_off)
    res_on = r_on.run("nb1", "带隙基准的启动电路?")
    res_off = r_off.run("nb1", "带隙基准的启动电路?")
    assert [c.chunk_id for c in res_on.chunks] == [c.chunk_id for c in res_off.chunks]
    assert [t.step_type for t in res_on.trace] == [t.step_type for t in res_off.trace]


def test_prefetch_propagates_contextvar():
    cv = contextvars.ContextVar("probe", default="unset")
    cv.set("user-42")
    repo, r = _mk()
    seen = {}
    orig = repo.retrieval.ppr_retrieve
    repo.retrieval.ppr_retrieve = lambda nb, q: (seen.setdefault("v", cv.get()), orig(nb, q))[1]
    r.run("nb1", "问题")
    assert seen["v"] == "user-42"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_reasoning_ppr_prefetch.py -v`
Expected: `test_prefetch_propagates_contextvar` FAIL（现无预取，串行路径 cv 可见——两个测试可能都过？不：`reasoning_ppr_prefetch` settings 字段还不存在，stub 有但真 config 没有；先跑通 stub 级 FAIL 取决于实现——若两测试意外全过，加一个断言 `ppr_threads[0] != threading.main_thread().name`（prefetch on 时在后台线程跑）来钉住行为）

- [ ] **Step 3: 实现**

3a. config.py（`graph_ppr_enabled` 附近，246-247）：

```python
    reasoning_ppr_prefetch: bool = Field(True, validation_alias="REASONING_PPR_PREFETCH")  # P0-C:seed pass PPR 与 plan LLM 并行(纯调度,结果逐位等价);False=原串行
```

3b. `reasoning_retrieval.py` `run()`：文件顶部加 `import contextvars`。在 `last_ts = time.perf_counter()` 之后、`subqueries = self.plan(...)` 之前插入：

```python
        # P0-C: seed pass PPR 只依赖原问题与只读图状态,与 plan 的 LLM 时间完全
        # 重叠(copy_context 保住 per-user 模型解析的 ContextVar)。在原 seed pass
        # 位置 join,故 seen_chunks 合并时序/trace 顺序与串行版逐位一致;
        # future.result() 重抛异常=与串行抛出同语义。
        ppr_future = None
        ppr_pool = None
        if self.settings.graph_ppr_enabled and getattr(
                self.settings, "reasoning_ppr_prefetch", True):
            ppr_pool = ThreadPoolExecutor(max_workers=1)
            ppr_future = ppr_pool.submit(
                contextvars.copy_context().run,
                self.ppr_retrieve, notebook_id, question)
```

原 seed pass 块（302-311）改为：

```python
        if self.settings.graph_ppr_enabled:
            raise_if_cancelled(self.cancel_event)
            try:
                ppr_all = (ppr_future.result() if ppr_future is not None
                           else self.ppr_retrieve(notebook_id, question))
            finally:
                if ppr_pool is not None:
                    ppr_pool.shutdown(wait=False)
            seeded = [c for c in ppr_all if c.chunk_id not in seen_chunks]
            ...（其余不动）
```

再给整个 run 主体兜底：plan/初检索若抛异常（含 AskCancelled），后台线程不能泄漏——把 `subqueries = self.plan(...)` 到 seed pass 之间的代码包一层 `try: ... except BaseException: if ppr_pool is not None: ppr_pool.shutdown(wait=False); raise`。最简单实现：在 submit 之后立刻 `try:`，在 seed pass 的 finally 处 shutdown（如上），并在 run 末尾（return 前）不需要再处理（seed pass 必然执行 shutdown；若 graph_ppr_enabled 为 False 则 ppr_pool 也是 None）。**注意**：seed pass 只有在 `graph_ppr_enabled` 时才执行，而 submit 也仅在同条件下发生，两者条件一致、不会有 future 无人 join 的路径；但 plan/初检索抛异常时 seed pass 不会执行——所以 try/except BaseException + shutdown + raise 的包裹是必须的。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_reasoning_ppr_prefetch.py -v`
Expected: PASS

- [ ] **Step 5: 跑全量 + Commit**

```bash
cd backend && ${PYTHON_BIN:-...} -m pytest tests/ -q
git add backend/app/services/reasoning_retrieval.py backend/app/core/config.py backend/tests/test_reasoning_ppr_prefetch.py
git commit -m "perf(reasoning): PPR seed pass 与 plan LLM 并行(REASONING_PPR_PREFETCH,结果逐位等价)"
```

---

### Task 5: P1-A per-ask embed 缓存（ContextVar 单点改 _embed_query）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（模块级 ContextVar ~197 附近；`_embed_query` 7801-7817；`ask_reasoning` 12137-12220 的 try/finally）
- Test: `backend/tests/test_ask_embed_cache.py`（新建）

**Interfaces:**
- Consumes: `_ASK_MODEL_ERRORS` 的 set/reset 模式（12138 与 12220）。
- Produces: 模块级 `_ASK_EMBED_CACHE: ContextVar`（default None）。非 ask 路径（default None）行为逐字节不变。

**等价性论证：** 同一 ask 内同一文本的重复 embed（federated 2 tier × 同 query、seed pass 与 quota 的原问题、scale_ppr 内部 chunk 检索的同问题）改为复用第一次结果。若 embed 服务确定性 → 逐位一致；若有服务端噪声 → 消除现有的"两 tier 各自打分基于两个略不同向量"的不一致源，方向是更一致。embed 失败（None）不缓存——保留每次重试语义。copy_context（Task 4 的并行线程、background_jobs）复制的是 ContextVar 到同一 dict 对象的引用，后台线程读写同一 dict：CPython dict 读写原子，最坏竞态=同文本算两次，无正确性问题。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ask_embed_cache.py
"""P1-A:per-ask embed 缓存。ask 作用域内同文本只打一次 embed 端点;
作用域外(ContextVar 默认 None)行为不变;失败不缓存。"""
from app.services import sqlite_repository as sr


class _CountingEmbedder:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    def embed_query(self, text):
        self.calls.append(text)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("boom")
        return [0.1, 0.2, 0.3]


def _mk_repo(repo_factory):
    repo, nb = repo_factory()
    emb = _CountingEmbedder()
    repo.embedder = emb
    # embedder_configured 必须为真才走 embed;按 repo_factory 的 settings 结构设置
    repo.settings.embedder_configured = True
    return repo, emb


def test_no_cache_outside_ask_scope(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    repo._embed_query("q1")
    repo._embed_query("q1")
    assert len(emb.calls) == 2      # 默认 None:每次都打端点(现状不变)


def test_cache_within_ask_scope(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    tok = sr._ASK_EMBED_CACHE.set({})
    try:
        v1 = repo._embed_query("q1")
        v2 = repo._embed_query("q1")
        repo._embed_query("q2")
    finally:
        sr._ASK_EMBED_CACHE.reset(tok)
    assert len(emb.calls) == 2      # q1 一次 + q2 一次
    assert v1 == v2


def test_failure_not_cached(repo_factory):
    repo, emb = _mk_repo(repo_factory)
    tok = sr._ASK_EMBED_CACHE.set({})
    try:
        emb.fail_next = True
        assert repo._embed_query("q1") is None
        assert repo._embed_query("q1") is not None   # 失败未缓存,重试成功
    finally:
        sr._ASK_EMBED_CACHE.reset(tok)
    assert len(emb.calls) == 2
```

`repo.settings.embedder_configured` 若是只读 property，改用 monkeypatch 或 repo_factory 传参——实现者按 conftest 实际结构调整（先读 `embedder_configured` 在 config.py 里的定义：property 还是字段）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_ask_embed_cache.py -v`
Expected: FAIL（`_ASK_EMBED_CACHE` 不存在）

- [ ] **Step 3: 实现**

3a. 模块级（`_ASK_MODEL_ERRORS` 定义旁）：

```python
# per-ask 查询 embed 缓存(P1-A):同一 ask 内同文本只打一次 embed 端点。
# federated 两 tier 同 query、seed pass 与 quota 收尾的原问题此前各自重复 embed
# (10-20 次网络 RTT/ask)。default None=非 ask 路径逐字节不变;失败(None)不缓存,
# 保留重试语义;copy_context 线程共享同一 dict,CPython 下最坏=同文本算两次,无害。
_ASK_EMBED_CACHE: contextvars.ContextVar = contextvars.ContextVar(
    "ask_embed_cache", default=None)
```

3b. `_embed_query`（7801）开头/结尾：

```python
    def _embed_query(self, query: str) -> Optional[List[float]]:
        """...原 docstring 保留..."""
        if not self.settings.embedder_configured:
            return None
        cache = _ASK_EMBED_CACHE.get()
        key = query[:2000]
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                return hit
        try:
            vec = self.embedder.embed_query(query[:2000])
        except Exception as exc:
            self._note_model_error("embed", self.settings.embed_model, exc)
            return None
        from app.services.vector_index import resolve_runtime_dim, truncate_vec
        rd = resolve_runtime_dim(self.settings)
        if rd and vec is not None and len(vec) > rd:
            import numpy as np
            vec = truncate_vec(np.asarray(vec, dtype=np.float32), rd).tolist()
        if cache is not None and vec is not None:
            cache[key] = vec
        return vec
```

3c. `ask_reasoning`（12137-12138）：与 `_err_token` 同处 set，同一 finally reset：

```python
        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        _emb_token = _ASK_EMBED_CACHE.set({})
        try:
            ...
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
            _ASK_EMBED_CACHE.reset(_emb_token)
```

（scope 决定：本轮只挂 `ask_reasoning`。`ask_graph`/chunk 模式的 ask 同样受益，但等价性验证只做了 reasoning 回放——挂多入口留到回放验证后的 fast-follow，PR 描述注明。）

- [ ] **Step 4: 跑测试确认通过 + 全量 + Commit**

```bash
cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_ask_embed_cache.py tests/ -q
git add backend/app/services/sqlite_repository.py backend/tests/test_ask_embed_cache.py
git commit -m "perf(ask): per-ask embed 缓存(ContextVar 单点,砍 tier×2/收尾重复 RTT)"
```

---

### Task 6: P1-B _quota_rerank 复用初检索打分

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`（`search`/`_run_search`/`add_subquery` 分支/`expand_community` 分支/`_quota_rerank`）
- Modify: `backend/app/core/config.py`（新 flag）
- Test: `backend/tests/test_quota_reuse.py`（新建）

**Interfaces:**
- Consumes: `quota_fuse(collected, per_q, top_n)`（app/services/retrieval.py:655，per_q 元素 = {oid: 有 .relevance 的对象}）；`dataclasses.replace`。
- Produces: `settings.reasoning_quota_reuse_enabled: bool`（env `REASONING_QUOTA_REUSE`，默认 True）；`ReasoningRetriever` 实例态 `self._per_query_scored: Dict[str, Dict[str, tuple]]`（norm_key → {oid: (relevance, score)}）。

**等价性论证（写进 PR）：** 现状收尾对每个 used_query 重跑全量 `federated_retrieve` 得 per_q；一次 run 内图只读、打分函数确定 ⇒ 「执行时留存的全量打分」≡「收尾重跑」（embed 确定性前提，且 Task 5 后同文本向量必然相同——比现状更强）。`quota_fuse` 只用 per_q 查 **collected 里的 oid**（quota_fuse 循环 `for oid, item in collected.items()`），所以留存 map 覆盖"该查询召回过的一切 oid" ⇒ 交集查询结果与重跑逐位一致；expand_graph 拉进的邻居若从未被任何子查询召回，现状重跑也查不到（不在 per_q）→ 同样落兜底组。重建对象 `replace(collected[oid], relevance=rel, score=sc)`：payload/evidence/status/owner/weight 不随查询变（同 DB 状态），与重跑产生的对象字段级相同。防御：某 used_query 无留存（理论不可达）→ 该查询回退重跑（fail-open 等价）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_quota_reuse.py
"""P1-B:quota 收尾复用初检索打分。等价性=reuse on/off 的 top_hits 逐位一致;
效率=reuse on 时收尾不再触发新的 federated_retrieve。"""
from dataclasses import replace

from app.services.reasoning_retrieval import ReasoningRetriever, SubQuery, ReflectDecision
from app.services.retrieval import RetrievedKnowledge


class _StubSettings:
    retrieval_top_n = 4
    reasoning_max_steps = 5
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 3
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = True
    reasoning_quota_reuse_enabled = True
    graph_ppr_enabled = False
    reasoning_ppr_prefetch = False
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


def _hit(oid, rel):
    return RetrievedKnowledge(object_id=oid, object_type="claim",
                              payload={"name": oid}, relevance=rel, score=rel)


class _StubRetrieval:
    """两个子查询各自的确定性全量打分表;记录 federated_retrieve 调用次数。"""
    TABLE = {
        "问题A": [_hit("o1", 0.9), _hit("o2", 0.6), _hit("o3", 0.3)],
        "问题B": [_hit("o4", 0.8), _hit("o2", 0.7)],
    }

    def __init__(self):
        self.calls = []

    def federated_retrieve(self, nb, q, types=None, w_keyword=0.4, w_semantic=0.6):
        self.calls.append(q)
        return [replace(h) for h in self.TABLE.get(q, [])]

    def retrieve_scored(self, nb, q):
        return []

    def ppr_retrieve(self, nb, q):
        return []


class _StubRepo:
    def __init__(self):
        self.retrieval = _StubRetrieval()
        self.reasoning_llm_client = type("C", (), {"configured": False})()


def _run(reuse: bool):
    s = _StubSettings()
    s.reasoning_quota_reuse_enabled = reuse
    repo = _StubRepo()
    r = ReasoningRetriever(repo, s)
    r.plan = lambda question, history="": [SubQuery(query="问题A"), SubQuery(query="问题B")]
    r.reflect = lambda question, sm: ReflectDecision(sufficient=True, next_action="answer")
    res = r.run("nb1", "总问题")
    return res, repo.retrieval.calls


def test_reuse_matches_rerun_bit_for_bit():
    res_on, _ = _run(True)
    res_off, _ = _run(False)
    on = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_on.top_hits]
    off = [(h.object_id, round(h.relevance, 9), round(h.score, 9)) for h in res_off.top_hits]
    assert on == off


def test_reuse_skips_final_rerun():
    _, calls_on = _run(True)
    _, calls_off = _run(False)
    # off:初检索 2 次 + 收尾重跑 2 次;on:只有初检索 2 次
    assert len(calls_off) == 4
    assert len(calls_on) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_quota_reuse.py -v`
Expected: FAIL（reuse flag 不存在 → 两种模式都重跑，`test_reuse_skips_final_rerun` 断言 2 失败）

- [ ] **Step 3: 实现**

3a. config.py（reasoning_quota_enabled 附近 ~303）：

```python
    reasoning_quota_reuse_enabled: bool = Field(True, validation_alias="REASONING_QUOTA_REUSE")  # P1-B:quota 收尾复用初检索留存的全量打分(一次 run 内图只读⇒与重跑逐位等价);False=原收尾重跑
```

3b. `reasoning_retrieval.py`：

`ReasoningRetriever.__init__` 加 `self._per_query_scored: Dict[str, Dict[str, tuple]] = {}`。

`search()`（101-104）改为留存全量后返回（所有调用点都经它，天然覆盖初检索/add_subquery/community）：

```python
    def search(self, notebook_id, query, types=None, prefer="balanced"):
        wk, ws = PREFER_WEIGHTS.get(prefer, PREFER_WEIGHTS["balanced"])
        hits = self.repo.retrieval.federated_retrieve(notebook_id, query, types=types,
                                                      w_keyword=wk, w_semantic=ws)
        # P1-B: 留存本次查询的全量打分(轻量 (relevance,score) map,含未进 collected
        # 的候选)。收尾 _quota_rerank 直接复用——一次 run 内图只读、打分确定,
        # 留存≡收尾重跑。仅 quota 开启时留存(省无谓内存)。
        if self.settings.reasoning_quota_enabled and getattr(
                self.settings, "reasoning_quota_reuse_enabled", True):
            self._per_query_scored[_norm_query(query)] = {
                h.object_id: (h.relevance, h.score) for h in hits}
        return hits
```

**注意**：`search` 带 `types` 过滤——同一查询文本不同 types 的两次调用会互相覆盖留存。核对现状：`_quota_rerank` 重跑用 `self.search(notebook_id, q)`（**无 types**，见 192 行），而初检索用 `sq.types`。所以留存的（带 types）与重跑的（无 types）**本就不同**！处理：留存键加不参与——**在 `_quota_rerank` 语义里必须复现"无 types 全类打分"**。解法：`search()` 只在 `types` 为空/None 时留存（初检索带 types 的查询不留存），`_quota_rerank` 对缺留存的 query 回退重跑（fail-open 分支，见 3c）。这保证逐位等价：有留存 ⇒ 留存时就是无 types 调用 ⇒ 与重跑同参。community peers 的 `self.search(notebook_id, pname)`（494 行，无 types）会被留存 ✔；add_subquery 带 types 的不留存 → 收尾对它重跑（与现状同）✔。修改上面代码：留存条件加 `and not types`。

3c. `_quota_rerank`（183-195）：

```python
    def _quota_rerank(self, notebook_id, collected, used_queries, top_n):
        """复合问题: 按子查询配额 round-robin 选 top_n。
        步骤 1: 每个子查询的全库打分——P1-B 优先复用 run 中留存的 map(一次 run 内
        图只读⇒与重跑逐位等价,见 search() 留存点);无留存(带 types 的子查询/
        flag 关)则原样重跑该查询(fail-open,容错: 抛错则该组空)。
        步骤 2-4: 分组+轮转委托给通用 quota_fuse。
        返回 (top_hits, counts): counts[i]=第 i 个子查询贡献数, counts[-1]=兜底组。"""
        from dataclasses import replace
        from app.services.retrieval import quota_fuse
        reuse = self.settings.reasoning_quota_enabled and getattr(
            self.settings, "reasoning_quota_reuse_enabled", True)
        per_q = []
        for q in used_queries:
            stored = self._per_query_scored.get(_norm_query(q)) if reuse else None
            if stored is not None:
                # quota_fuse 只查 collected 里的 oid,交集重建即可(payload/evidence
                # 不随查询变,replace 版与重跑版字段级相同)。
                per_q.append({oid: replace(collected[oid], relevance=rel, score=sc)
                              for oid, (rel, sc) in stored.items() if oid in collected})
                continue
            try:
                per_q.append({h.object_id: h for h in self.search(notebook_id, q)})
            except Exception:
                per_q.append({})
        return quota_fuse(collected, per_q, top_n)
```

**核对一个细节**：现状重跑版 per_q 的 value 是 federated_retrieve 产物（带 `.notebook_id`/`.tier` 标注），而 `collected[oid]` 同样来自 federated_retrieve（也带标注）——`replace` 保留 collected 的标注，同一 oid 的 tier/notebook_id 由其归属 notebook 决定、与查询无关 ⇒ 相同 ✔。

- [ ] **Step 4: 跑测试确认通过 + 全量 + Commit**

```bash
cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_quota_reuse.py tests/ -q
git add backend/app/services/reasoning_retrieval.py backend/app/core/config.py backend/tests/test_quota_reuse.py
git commit -m "perf(reasoning): quota 收尾复用初检索全量打分(REASONING_QUOTA_REUSE,逐位等价+fail-open 重跑)"
```

---

### Task 7: P0-B PPR float32 + tol 放宽（用户已接受数值波动）

**Files:**
- Modify: `backend/app/core/config.py`（`ppr_damping` 旁，246-247）
- Modify: `backend/app/services/kg/scale_index.py`（`personalized_ppr`）
- Modify: `backend/app/services/sqlite_repository.py`（`_scale_combined_graph` 尾部 ~9950-10009；`scale_ppr` 的 reset dtype 10060 与调用 10150）
- Test: `backend/tests/test_ppr_float32.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `stats` 出参（验证迭代轮数下降）。
- Produces: `settings.ppr_tol: float`（env `PPR_TOL`，默认 1e-6）；`settings.ppr_float32: bool`（env `PPR_FLOAT32`，默认 True）。

**数值论证（写进 PR 与代码注释）：** float32 下 L1 残差的噪声地板 ≈ machine-eps × Σ|x| ≈ 1.2e-7×1 ≈ 1e-7：`tol=1e-6` 有 ~10x 余量能正常收敛；**tol < 1e-6 配 float32 会永远达不到 → 空转满 100 轮反而更慢**，`personalized_ppr` 内做防御 clamp。damping=0.5 下收敛速率 ~0.5^k：1e-6 约 20 轮（vs 1e-8 的 ~27 轮），float32 SpMV 内存带宽减半 ≈ 2x，合计 ~2.7x。residual 求和用 `dtype=np.float64` 累积消掉求和噪声。**验收**：真机用 Task 2 的 `--compare --mode topk --k 30` 对照（top-30 chunk 集合一致即收；分数与长尾允许波动——用户已接受）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_ppr_float32.py
"""P0-B:PPR float32+tol。float32 与 float64 在小图上 top-k 排序一致;
float32 下 tol 被 clamp 到 >=1e-6(防空转满 max_iter);迭代轮数确实下降。"""
import numpy as np
import scipy.sparse as sp

from app.services.kg import scale_index as si


def _chain_graph(n=50, dtype=np.float64):
    # 链式图 0-1-2-...-(n-1),双向,列随机
    edges = []
    for i in range(n - 1):
        edges.append((str(i), str(i + 1), 1.0))
        edges.append((str(i + 1), str(i), 1.0))
    A, idx = si.build_transition([str(i) for i in range(n)], edges)
    return A.astype(dtype), idx


def test_float32_topk_matches_float64():
    A64, _ = _chain_graph(dtype=np.float64)
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50); reset[0] = 1.0; reset[10] = 0.5
    x64 = si.personalized_ppr(A64, reset, damping=0.5, tol=1e-8)
    x32 = si.personalized_ppr(A32, reset.astype(np.float32), damping=0.5, tol=1e-6)
    top64 = list(np.argsort(-x64)[:10])
    top32 = list(np.argsort(-x32)[:10])
    assert top64 == top32


def test_float32_tol_clamped_no_spin():
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50, dtype=np.float32); reset[0] = 1.0
    stats = {}
    si.personalized_ppr(A32, reset, damping=0.5, tol=1e-12, stats=stats)
    # clamp 生效:不会空转满 100 轮
    assert stats["iters"] < 100


def test_looser_tol_fewer_iters():
    A64, _ = _chain_graph(dtype=np.float64)
    reset = np.zeros(50); reset[0] = 1.0
    s_tight, s_loose = {}, {}
    si.personalized_ppr(A64, reset, damping=0.5, tol=1e-8, stats=s_tight)
    si.personalized_ppr(A64, reset, damping=0.5, tol=1e-6, stats=s_loose)
    assert s_loose["iters"] < s_tight["iters"]


def test_output_dtype_follows_transition():
    A32, _ = _chain_graph(dtype=np.float32)
    reset = np.zeros(50, dtype=np.float32); reset[0] = 1.0
    x = si.personalized_ppr(A32, reset, damping=0.5, tol=1e-6)
    assert x.dtype == np.float32
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_ppr_float32.py -v`
Expected: FAIL（`personalized_ppr` 强制 float64 → dtype 断言失败；无 clamp → 空转断言失败）

- [ ] **Step 3: 实现**

3a. config.py：

```python
    ppr_tol: float = Field(1e-6, validation_alias="PPR_TOL")          # PPR 幂迭代收敛阈(L1);float32 下 <1e-6 会低于噪声地板被 clamp
    ppr_float32: bool = Field(True, validation_alias="PPR_FLOAT32")   # scale PPR 的 combined 转移阵/迭代用 float32(带宽减半≈2x;top-k 实践一致,分数长尾有 1e-6 级波动)
```

3b. `personalized_ppr` dtype 跟随 + clamp（在 Task 1 版本基础上改）：

```python
    s = float(reset.sum())
    if s <= 0:
        if stats is not None:
            stats["iters"] = 0
        return np.zeros(transition.shape[0], dtype=np.float64)
    # dtype 跟随转移阵(float32 时迭代全程 float32,SpMV 带宽减半);residual 求和
    # 用 float64 累积消掉求和噪声。float32 的 L1 残差噪声地板≈eps×Σ|x|≈1e-7,
    # tol 低于它会永远达不到→空转满 max_iter 反而更慢,防御 clamp 到 1e-6。
    dt = transition.dtype if transition.dtype in (np.float32, np.float64) else np.float64
    eff_tol = max(float(tol), 1e-6) if dt == np.float32 else float(tol)
    p = (reset.astype(dt) / dt.type(s)) if hasattr(dt, "type") else (reset.astype(dt) / s)
    x = p.copy()
    d = float(damping)
    iters = 0
    for _ in range(max_iter):
        iters += 1
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - float(x_new.sum())) * p
        if float(np.abs(x_new - x).sum(dtype=np.float64)) < eff_tol:
            x = x_new
            break
        x = x_new
    if stats is not None:
        stats["iters"] = iters
    total = float(x.sum())
    return x / total if total > 0 else x
```

（`p = reset.astype(dt) / s` 直接写即可——numpy 标量除法保持数组 dtype；上面 `hasattr` 分支是多余的，实现时删掉，写 `p = reset.astype(dt) / s`。）dtype 说明追加进 docstring。

3c. `_scale_combined_graph`：先读该函数（~9916-10009），在其 `_load` 回调 return 前把 combined_A 转 dtype，并把 flag 掺进缓存 version（flag 翻转必须失效缓存）：

```python
            if self.settings.ppr_float32:
                combined_A = combined_A.astype(np.float32)
```

version key（~9947-9948 的版本元组）追加一项 `("f32" if self.settings.ppr_float32 else "f64")`。

3d. `scale_ppr`：10060 `reset = np.zeros(len(combined_ids), dtype=np.float64)` → `dtype=(np.float32 if self.settings.ppr_float32 else np.float64)`（与 combined_A 一致——注意缓存里的旧 dtype 图：reset dtype 以 `combined_A.dtype` 为准更稳，直接写 `dtype=combined_A.dtype`）；10150 调用加 `tol=self.settings.ppr_tol`。

3e. 小库 rustworkx 路径（run_ppr / rx.pagerank）**不动**——它不在 300 万节点热路径上。

- [ ] **Step 4: 跑测试确认通过 + 全量 + Commit**

```bash
cd backend && ${PYTHON_BIN:-...} -m pytest tests/test_ppr_float32.py tests/ -q
git add backend/app/core/config.py backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_ppr_float32.py
git commit -m "perf(ppr): scale PPR float32+PPR_TOL 可调(≈2.7x;top-k 稳定,长尾分数波动已获接受)"
```

---

### Task 8: 收尾——全量验证 + PR

**Files:** 无新文件；`git` 操作。

- [ ] **Step 1: 全量测试**

Run: `cd backend && ${PYTHON_BIN:-...} -m pytest tests/ -q` 与 `bash scripts/check.sh`
Expected: 全绿

- [ ] **Step 2: rebase 到 master 并推送（保持线性——PR 走 Rebase and merge）**

```bash
git fetch origin master
git rebase origin/master
git push -u origin claude/clever-merkle-631d2e --force-with-lease
```

- [ ] **Step 3: 创建 PR**

```bash
gh pr create --base master --title "perf(reasoning): 大库检索提速(探针O(1)/PPR并行+float32/embed缓存/quota复用)+回放验收工具" --body "$(cat <<'EOF'
## 背景
300 万 KG 节点 / 50 万 chunk 部署下 reasoning 前三步(规划/检索/漫游)各 20-30s。归因:版本探针每查询对百万行表跑 COUNT/MAX(×30 次/检索步)、PPR 幂迭代 float64 tol=1e-8、同 query 重复 embed(tier×2+收尾)、quota 收尾全量重检索、seed pass 与 plan 串行。

## 改动(各自独立逃生口)
- ask_stage 埋点 + scripts/replay_retrieval.py 回放对照 CLI(效果验收工具)
- P0-A 探针 O(1):unified_kg_state.cluster_mutation_seq(_migration_5,SCHEMA_VERSION=5);manifest.version 格式不变;kwtok 有界路径免 COUNT —— 结果逐位不变
- P0-C seed pass PPR 与 plan LLM 并行(REASONING_PPR_PREFETCH,默认开) —— 逐位不变
- P1-A per-ask embed 缓存(ContextVar 单点) —— 消除现有 tier×2 向量不一致源
- P1-B quota 收尾复用初检索全量打分(REASONING_QUOTA_REUSE,默认开;带 types 子查询 fail-open 重跑) —— 逐位不变
- P0-B PPR float32+PPR_TOL=1e-6(PPR_FLOAT32,默认开) —— top-k 稳定,长尾分数 1e-6 级波动(已与部署方确认接受)

## 真机验收步骤
1. 合并前在部署库跑: `python scripts/replay_retrieval.py --notebook <nb> --questions qs.txt --out before.json`(旧代码) / `--out after.json`(新代码)
2. `python scripts/replay_retrieval.py --compare before.json after.json --mode exact`(先设 PPR_FLOAT32=false 验证严格等价项)
3. 开 PPR_FLOAT32 后用 `--mode topk --k 30` 对照
4. events.jsonl 观察 ask_stage/scale_ppr_done 的耗时分解

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: 汇报**

向用户报告:PR 链接、每步预期收益(检索 20-30s→~5s、漫游墙钟并入 plan、PPR 计算 ~2.7x、收尾省 5+ 次全量检索)、真机验收步骤(回放对照两阶段:先 PPR_FLOAT32=false 验证 exact,再开 float32 验证 topk)。

---

## Self-Review 结论

- **覆盖**：六项优化 + 观测/验收工具各有任务；P2(规划步 LLM 侧)按用户"效果不变"要求已划出 scope,不在本计划。
- **类型一致性**：`personalized_ppr(stats=)` Task 1 定义、Task 7 复用同签名；`_bump_cluster_mutation_seq(db, nb)` 仅 Task 3 内使用；`reasoning_ppr_prefetch`/`reasoning_quota_reuse_enabled`/`ppr_tol`/`ppr_float32` 的 settings 名在测试 stub 与 config 定义一致。
- **已知实现风险点已内嵌**：`_migrate` 分发方式需实现时核对(Task 3 Step 3a)；`repo_factory` fixture 依 conftest 实际结构(Task 1/3/5)；batch_ingest 引导段照搬(Task 2)；`search(types=...)` 留存语义陷阱(Task 6 Step 3b 显式处理)；float32 tol 噪声地板 clamp(Task 7)。
