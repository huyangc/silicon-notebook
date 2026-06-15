# Reasoning 子查询配额重排 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复合问题(如「V3 相比 V2 优化？R1 呢」)的 reasoning 检索,在最终排序时按子查询配额 round-robin 选 top-N,避免整串全局重打分让信息量大的一方(R1)通吃、另一方(V3 vs V2)证据归零。

**Architecture:** 改动集中在 `reasoning_retrieval.py` 的 `run()` 末尾 + 一个新方法 `_quota_rerank`,外加一个 config 开关。`run()` 收集 `used_queries`(plan 子查询 + add_subquery 查询,保序去重);末尾若开关开且子查询≥2,走配额重排,否则走现有全局重排(单查询/开关关→行为不变,向后兼容)。

**Tech Stack:** Python/FastAPI、pytest;复用 `ReasoningRetriever.search`(即 `_retrieve_scored`)检索原语。

**对应 spec:** `docs/superpowers/specs/2026-06-14-reasoning-subquery-quota-design.md`

---

## File Structure

- Modify: `backend/app/core/config.py` — 新增 `reasoning_quota_enabled` 开关
- Modify: `backend/app/services/reasoning_retrieval.py` — 新增 `_quota_rerank` 方法;`run()` 收集 `used_queries` + 末尾接线 + answer step quota detail
- Test: `backend/tests/test_reasoning_retrieval.py` — 追加配额相关测试(沿用现有 `rrepo` fixture / `_SeqLLM` / `_mk_rk` / monkeypatch search 模式)

---

## Task 1: config 开关 reasoning_quota_enabled

**Files:**
- Modify: `backend/app/core/config.py`(在 `reasoning_max_subqueries` 行附近,reasoning 旋钮区)
- Test: `backend/tests/test_reasoning_retrieval.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_reasoning_retrieval.py` 的 `test_reasoning_settings_knobs` 函数后追加:

```python
def test_reasoning_quota_enabled_default():
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is True


def test_reasoning_quota_enabled_env(monkeypatch):
    monkeypatch.setenv("REASONING_QUOTA_ENABLED", "false")
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q -k "quota_enabled" 2>&1 | tail -5
```
Expected: FAIL(`AttributeError: ... reasoning_quota_enabled`)

- [ ] **Step 3: 实现**

`backend/app/core/config.py`,在 `reasoning_max_subqueries: int = Field(5, env="REASONING_MAX_SUBQUERIES")` 行后追加:

```python
    # 复合问题最终排序: 开启后按子查询配额 round-robin 选 top-N(避免整串全局排序让
    # 信息量大的一方通吃); 关闭则回退全局重排。单子查询时自动等价全局。
    reasoning_quota_enabled: bool = Field(True, env="REASONING_QUOTA_ENABLED")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q -k "quota_enabled" 2>&1 | tail -3
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): config 开关 reasoning_quota_enabled(默认开)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: _quota_rerank 配额算法

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(在 `run` 方法前,作为 `ReasoningRetriever` 方法)
- Test: `backend/tests/test_reasoning_retrieval.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_reasoning_retrieval.py` 末尾追加。先加一个带 relevance 的构造助手 + 三个测试:

```python
def _rk(oid, rel, otype="claim"):
    """构造带 relevance 的 RetrievedKnowledge(配额测试用)。"""
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype,
                              payload={"name": oid}, relevance=rel)


def test_quota_rerank_rescues_weak_group(rrepo, monkeypatch):
    """配额核心: 弱势子查询组(分数低)也保底进 top-N, 不被强势组通吃。
    模拟根因: V3 组分低(.5/.45), R1 组分高且多(.95/.9/.85/.8); 全局 top-2 会全 R1,
    配额 round-robin 让 V3 组也分到名额。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qV3": [_rk("A", 0.5), _rk("B", 0.45)],
        "qR1": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85), _rk("F", 0.8)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["A", "B", "C", "D", "E", "F"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qV3", "qR1"], top_n=2)
    ids = [h.object_id for h in hits]
    assert "A" in ids and "C" in ids       # 两组各贡献队首(全局会是 C,D)
    assert counts == [1, 1, 0]               # [qV3, qR1, 兜底组]: 各子查询 1 条、兜底 0


def test_quota_rerank_roundrobin_balance(rrepo, monkeypatch):
    """组大小悬殊(4 vs 2)时 top_n=4 内两组都有名额, 不被大组占满。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qA": [_rk("a1", .9), _rk("a2", .8), _rk("a3", .7), _rk("a4", .6)],
        "qB": [_rk("b1", .95), _rk("b2", .85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["a1","a2","a3","a4","b1","b2"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA", "qB"], top_n=4)
    ids = {h.object_id for h in hits}
    assert "b1" in ids and "b2" in ids       # 小组的 2 条都进(round-robin 保底)
    assert counts == [2, 2, 0]                # [qA, qB, 兜底组]: 4 名额两组均分、兜底 0


def test_quota_rerank_tolerates_subquery_failure(rrepo, monkeypatch):
    """某子查询 search 抛错 → 该组空, 其余组正常出候选, 不崩。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    def fake_search(self, n, q, types=None, prefer="balanced"):
        if q == "boom":
            raise RuntimeError("search blew up")
        return [_rk("C", 0.9), _rk("D", 0.8)]
    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    rr = ReasoningRetriever(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["C", "D"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["boom", "ok"], top_n=2)
    ids = {h.object_id for h in hits}
    assert ids == {"C", "D"}                  # 失败组空, ok 组正常
    assert counts[0] == 0                      # 失败组贡献 0


def test_quota_rerank_fallback_group_last(rrepo, monkeypatch):
    """所有子查询都查不到的候选(relevance 全 0)进兜底组, 优先级最低但仍可入选。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rr = ReasoningRetriever(rrepo, rrepo.settings)
    # X 不在任何子查询结果 → 兜底组
    collected = {"A": _rk("A", 0.0), "X": _rk("X", 0.0)}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA"], top_n=2)
    ids = [h.object_id for h in hits]
    assert ids[0] == "A"                       # 子查询组优先
    assert "X" in ids                          # 兜底组仍入选(名额没满时)
    assert counts[-1] == 1                     # 最后一个 count 是兜底组
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q -k "quota_rerank" 2>&1 | tail -6
```
Expected: FAIL(`AttributeError: ... _quota_rerank`)

- [ ] **Step 3: 实现**

`backend/app/services/reasoning_retrieval.py`,在 `def run(` 方法定义**之前**(作为 `ReasoningRetriever` 的方法,与 `_summarize` 相邻)插入:

```python
    def _quota_rerank(self, notebook_id, collected, used_queries, top_n):
        """复合问题: 按子查询配额 round-robin 选 top_n, 避免整串全局排序让信息量大的
        一方通吃。每个候选归到它 relevance 最高的子查询组, 各组内降序后跨组轮流取队首;
        所有子查询都查不到的候选(relevance 全 0)归兜底组, 最后轮转。
        返回 (top_hits, counts): counts[i]=第 i 个子查询贡献数, counts[-1]=兜底组。"""
        # 1. 每个子查询全库重打分(容错: 抛错则该组空)。
        per_q = []
        for q in used_queries:
            try:
                per_q.append({h.object_id: h for h in self.search(notebook_id, q)})
            except Exception:
                per_q.append({})
        # 2. 每个候选归到 relevance 最高的子查询组; 都查不到 → 兜底组。
        groups = [[] for _ in used_queries]
        fallback = []
        for oid, rk in collected.items():
            best_i, best_h = -1, None
            for i, scored in enumerate(per_q):
                h = scored.get(oid)
                if h is not None and (best_h is None or h.relevance > best_h.relevance):
                    best_i, best_h = i, h
            if best_i >= 0:
                groups[best_i].append(best_h)
            else:
                fallback.append(rk)
        # 3. 组内按 relevance 降序。
        for g in groups:
            g.sort(key=lambda h: h.relevance, reverse=True)
        # 4. round-robin 跨组轮流取队首未选过的; 兜底组放最后。
        queues = groups + [fallback]
        idx = [0] * len(queues)
        result, seen, sources = [], set(), []
        while len(result) < top_n:
            progressed = False
            for qi in range(len(queues)):
                if len(result) >= top_n:
                    break
                while idx[qi] < len(queues[qi]):
                    h = queues[qi][idx[qi]]
                    idx[qi] += 1
                    if h.object_id not in seen:
                        seen.add(h.object_id)
                        result.append(h)
                        sources.append(qi)
                        progressed = True
                        break
            if not progressed:
                break
        counts = [sources.count(i) for i in range(len(queues))]
        return result, counts
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q -k "quota_rerank" 2>&1 | tail -3
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): _quota_rerank 子查询配额 round-robin 重排

按子查询分组(argmax relevance)+ round-robin 跨组轮流取, 弱势子查询组
也保底进 top-N, 不被强势组通吃; 兜底组兜住纯 expand 节点; 子查询检索
抛错该组空容错。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: run() 收集 used_queries + 末尾接线 + quota detail

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(`run` 方法内: 初检索后收集、add_subquery 分支、末尾重排)
- Test: `backend/tests/test_reasoning_retrieval.py`

- [ ] **Step 1: 写失败测试**

末尾追加(run 级端到端,沿用 `_SeqLLM` + monkeypatch search):

```python
def test_run_quota_path_keeps_both_groups(rrepo, monkeypatch):
    """复合(≥2 子查询)+ 开关开 → 走配额, top_hits 同时含两组候选(弱势组不被挤掉)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    rrepo.settings.retrieval_top_n = 2
    per_q = {
        "qV3": [_rk("A", 0.5), _rk("B", 0.45)],
        "qR1": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    # plan 出 2 个子查询; reflect 首步即 answer(聚焦末尾排序)。
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "qV3"}, {"query": "qR1"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "qV3 qR1", "")
    ids = {h.object_id for h in res.top_hits}
    assert "A" in ids and "C" in ids          # 配额救回弱势组 A(全局 top-2 会是 C,D)
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert ans.detail.get("quota") == [1, 1]  # 可观测: 每子查询贡献数


def test_run_single_subquery_uses_global(rrepo, monkeypatch):
    """单子查询 → 不进配额, 走原全局重排(行为不变)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "only"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "only", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 全局路径不带 quota


def test_run_quota_disabled_uses_global(rrepo, monkeypatch):
    """开关关 → 复合问题也走全局重排。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = False
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "q1"}, {"query": "q2"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "q1 q2", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 开关关 → 全局路径
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q -k "run_quota or run_single_subquery" 2>&1 | tail -6
```
Expected: FAIL(`test_run_quota_path_keeps_both_groups`: 当前末尾走全局,A 被挤出 / `quota` detail 不存在)

- [ ] **Step 3: 实现 — 收集 used_queries**

`backend/app/services/reasoning_retrieval.py` `run()` 内,初检索 `record(TraceStep(step_type="retrieve", summary=f"初检索得到 ...` 之后、`steps = 0` 之前,插入:

```python
        # 复合问题最终配额排序用: 记录所有用过的子查询(保序去重)。
        used_queries = list(dict.fromkeys(s.query for s in subqueries))
```

在 `add_subquery` 分支里,`record(TraceStep(step_type="retrieve", summary=f"补充子查询: {sq.query}",` 之前(即 `collected.setdefault` 循环后),插入:

```python
                    if sq.query not in used_queries:
                        used_queries.append(sq.query)
```

- [ ] **Step 4: 实现 — 末尾接线 + quota detail**

把 `run()` 末尾(现 294-301 行附近)的全局重排 + answer record 整段:

```python
        # 统一口径: 用原问题对全库重打分,agent 召回的候选优先用此版本(带原问题 relevance)
        scored_map = {h.object_id: h for h in self.repo._retrieve_scored(notebook_id, question)}
        top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
        top_hits.sort(key=lambda h: h.relevance, reverse=True)
        top_hits = top_hits[: self.settings.retrieval_top_n]
        record(TraceStep(step_type="answer",
                         summary=f"合成: 采用 {len(top_hits)} 个KG候选 + {len(elements)} 段原文",
                         detail={"kg": len(top_hits), "elements": len(elements)}))
        return ReasoningResult(top_hits=top_hits, elements=elements, trace=trace)
```

替换为:

```python
        answer_detail = {"elements": len(elements)}
        if self.settings.reasoning_quota_enabled and len(used_queries) >= 2:
            # 复合问题: 按子查询配额 round-robin, 避免一方通吃。
            top_hits, counts = self._quota_rerank(
                notebook_id, collected, used_queries, self.settings.retrieval_top_n)
            answer_detail["quota"] = counts
        else:
            # 单查询/开关关: 原全局重排(用原问题统一打分), 行为不变。
            scored_map = {h.object_id: h for h in self.repo._retrieve_scored(notebook_id, question)}
            top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
            top_hits.sort(key=lambda h: h.relevance, reverse=True)
            top_hits = top_hits[: self.settings.retrieval_top_n]
        answer_detail["kg"] = len(top_hits)
        record(TraceStep(step_type="answer",
                         summary=f"合成: 采用 {len(top_hits)} 个KG候选 + {len(elements)} 段原文",
                         detail=answer_detail))
        return ReasoningResult(top_hits=top_hits, elements=elements, trace=trace)
```

注意: `_quota_rerank` 的 `counts` 长度是 `len(used_queries)+1`(末位兜底组);`answer_detail["quota"]` 直接存完整 `counts` 列表(测试断言 `[1, 1]` 是两子查询、无兜底贡献的情形 —— 即 `counts[:len(used_queries)]` 全部、兜底为 0 时列表为 `[1, 1]`;若兜底有贡献则为 `[1, 1, k]`)。**为让测试 `assert ans.detail.get("quota") == [1, 1]` 成立,存 `counts[:len(used_queries)]`**:

```python
            answer_detail["quota"] = counts[:len(used_queries)]
```

(即只暴露各子查询贡献数,不含兜底组;兜底组贡献从总数反推即可。)

- [ ] **Step 5: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q 2>&1 | tail -3
```
Expected: 全部 passed(新 run 级测试 + Task1/2 测试 + 现有全部不回归)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "feat(reasoning): run() 收集 used_queries + 末尾配额接线

复合(子查询≥2)且开关开 → _quota_rerank 配额重排, answer step detail
带每子查询贡献数(quota); 单查询/开关关 → 原全局重排(行为不变)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 全量验证 + 提 PR

- [ ] **Step 1: 全量 check.sh**

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh > /tmp/check-quota.log 2>&1
echo "exit=$?"; tail -4 /tmp/check-quota.log
```
Expected: `exit=0`(py_compile + hermetic smoke + tsc 全绿)

- [ ] **Step 2: reasoning 全套确认**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_reasoning_retrieval.py -q 2>&1 | tail -2
```
Expected: 全部 passed(约 50 个)

- [ ] **Step 3: push + PR**

```bash
git push -u origin claude/reasoning-quota 2>&1 | tail -2
gh pr create --base master --title "fix(reasoning): 复合问题子查询配额重排" --body "$(cat <<'EOF'
## 问题
复合问题(如「deepseekv3相比v2优化？r1呢」)的 reasoning 检索,末尾用整串全局重打分取 top-N,导致信息量大的一方(R1论文)通吃 top-12、另一方(V3 vs V2,agent 明明 expand 到了)证据归零 → 答题对 V3 缺料走「(推断)」。铁证: 该问题 top-12 候选 12/12 全来自 R1 论文。

## 修复(方案①子查询配额)
- `run()` 收集 used_queries(plan 子查询 + add_subquery, 保序去重)
- 末尾若开关开且子查询≥2 → `_quota_rerank`: 每子查询重打分 → 候选按 argmax relevance 分组 → round-robin 跨组轮流取 → 弱势子查询组也保底进 top-N
- config 开关 `REASONING_QUOTA_ENABLED`(默认 true), 单查询/关 → 原全局重排(行为不变)
- answer step detail 带 quota(每子查询贡献数)便于观测

## 测试(离线)
config 默认/env、配额救回弱势组、round-robin 均衡、子查询失败容错、兜底组、run 级端到端配额/单查询退化/开关退化。check.sh 全绿。

spec: docs/superpowers/specs/2026-06-14-reasoning-subquery-quota-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -2
```

- [ ] **Step 4: root 切回 master + 告知用户**

```bash
git -C /Users/hzf/workspace/silicon_notebook checkout master 2>&1 | tail -1
```
告知: PR 已提;生效需重启后端(逻辑改动, 后端无 `--reload`)——交用户重启,不由我重启。

---

## 验证基线(贯穿)

- 每个 Task 跑对应 `pytest -k`;Task 3/4 跑全套 `test_reasoning_retrieval.py` + `check.sh`。
- 测试隔离沿用 `rrepo` fixture(已清空 LLM/reasoning key,不打真实网络)。
