# 深挖推理重复子查询防重 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 治愈 reasoning 深挖循环「连续多轮补充同一条子查询→熔断收尾」:把已试子查询账目回喂 reflect、执行侧硬跳过重复、静态 prompt 补告诫、修 `_summarize` 前 30 条窗口盲区。

**Architecture:** 全部改动集中在 `backend/app/services/reasoning_retrieval.py`(run 循环 + `_summarize`)与 `backend/app/services/prompts.py`(reflect_prompt 一处文案)。核心是新增 `attempted` 账目(归一化 query → 原文/新增证据数/尝试次数):初检索与 add_subquery 都记账,每轮拼进 reflect summary(镜像既有 visited 回喂,顺带破 LLM 缓存 prompt 不动点),add_subquery 对重复键跳过执行。无 schema/API/前端改动(skip TraceStep 前端已原生渲染)。

**Tech Stack:** Python/pytest。测试全部进 `backend/tests/test_reasoning_retrieval.py`,复用现有 `rrepo` fixture、`_seed_two_nodes`、`_mk_rk`、`_RecordingLLM` 模式(chat_json 按 schema_hint 含 `"sub_queries"` 区分 plan/reflect)、`run(..., on_step=steps.append)` 捕获 trace。

**根因诊断背景**(已对抗验证,详见 memory reasoning-subquery-repeat-diagnosis):`used_queries`(:251/:325)只进末尾 `_quota_rerank`,从不回喂 reflect;add_subquery 是四动作里唯一零防重(expand_graph 有 visited skip+回喂,search_elements/ppr 有上限+seen 去重);重复查询命中全被 `setdefault` 吸收→summary 逐字节不变→`LLM_CACHE_ENABLED=true` 下 prompt 不动点命中缓存,逐字重放同一决策;`_summarize` 只渲染 collected 前 30 条(插入序),新增证据落窗口外时"有进展也重复"。

**验证命令**(所有任务通用,从 worktree 根执行):
```bash
PY=/opt/homebrew/Caskroom/miniconda/base/bin/python
$PY -m pytest backend/tests/test_reasoning_retrieval.py -q      # 单文件
$PY -m pytest backend/tests -q                                  # 全量(Task 4)
bash scripts/check.sh                                           # 全检(Task 4)
```

**提交规范:** 中文 conventional commits;消息末尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。只碰计划指定文件;不重启任何服务;不用 dangerouslyDisableSandbox。

---

### Task 1: attempted 账目——采集、回喂 reflect、add_subquery 硬跳过

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(4 处)
- Test: `backend/tests/test_reasoning_retrieval.py`(追加 2 个用例)

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_reasoning_retrieval.py` 末尾(`json`/`pytest` 已 import;`_seed_two_nodes` 会种 2 个可命中节点且库无 source_elements):

```python
def test_run_duplicate_subquery_skipped_not_rerun(rrepo, monkeypatch):
    """add_subquery 重复已试过的子查询(含与初始 plan 重复、归一化等价)→ 硬跳过:
    不再执行 search,记 skip trace(reason=duplicate_subquery)。治「反复补充同一条
    子查询白烧检索」;跳过属零新增,stale 熔断语义不变。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _RepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                # 与 plan 子查询同文本 → 应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},
                # 仅大小写/空白差异,归一化后仍重复 → 也应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "  rtl到gdsii流程 "}},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    rrepo.llm_client = _RepeatLLM()
    retriever = ReasoningRetriever(rrepo, rrepo.settings)
    calls: list[str] = []
    orig_search = retriever.search

    def _spy(nb_id, query, types=None, prefer="balanced"):
        calls.append(query)
        return orig_search(nb_id, query, types, prefer)

    monkeypatch.setattr(retriever, "search", _spy)
    steps = []
    retriever.run(nb.id, "RTL到GDSII流程", "", on_step=steps.append)

    # search 只在初检索执行 1 次;两次重复 add_subquery 均被跳过、未重跑
    assert calls == ["RTL到GDSII流程"]
    skips = [s for s in steps if s.step_type == "skip"
             and s.detail.get("reason") == "duplicate_subquery"]
    assert len(skips) == 2
    assert "跳过重复子查询" in skips[0].summary


def test_run_feeds_attempted_subqueries_to_reflect(rrepo):
    """已执行过的子查询账目(文本+新增证据数+尝试次数)必须回喂 reflect prompt:
    ①首轮即含初始 plan 的子查询与各自新增数(治「plan 对 reflect 不可见→首轮就
    复述 plan 已跑过的」);②重复被跳过后,下一轮 prompt 含尝试次数(账目变化使
    prompt 非不动点,LLM 缓存不会原样吐回上一轮决策)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    captured: list[str] = []

    class _RecordingRepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},  # 重复 plan → 跳过
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [
                    {"query": "RTL到GDSII流程"}, {"query": "时序收敛方法"}]})
            captured.append(messages[-1]["content"])
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    rrepo.llm_client = _RecordingRepeatLLM()
    ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

    assert len(captured) == 2
    # ① 首轮:初始 plan 两条子查询都在账目里,且带新增数与去重告诫
    assert "已执行过的子查询" in captured[0]
    assert "RTL到GDSII流程" in captured[0] and "时序收敛方法" in captured[0]
    assert "新增" in captured[0] and "勿重复" in captured[0]
    assert "已试" not in captured[0]          # 首轮各 1 次,不显示次数
    # ② 重复被跳过后:该条账目显示已试 2 次 → 两轮 prompt 必不同(破缓存不动点)
    assert "已试2次" in captured[1]
    assert captured[0] != captured[1]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q -k "duplicate_subquery or attempted"`
Expected: 2 FAIL(无 skip 事件 / prompt 无「已执行过的子查询」)

- [ ] **Step 3: 实现(4 处编辑,均在 `backend/app/services/reasoning_retrieval.py`)**

**3a. `NO_NEW_EVIDENCE_NOTE` 定义之后**(约 :43 的右括号后)加模块级 helper 与账目类型(文件已 `from dataclasses import dataclass`,若无则补;`field` 不需要):

```python
def _norm_query(q: str) -> str:
    """子查询防重的归一化键:压空白 + casefold。保守精确匹配、不做语义归一——
    宁可放过真改写的近似查询(由回喂账目提示模型约束),不误杀新角度。"""
    return " ".join(str(q).split()).casefold()


@dataclass
class _QueryAttempt:
    """单条子查询的执行账目:原文、带来的新增证据数、尝试次数(含被跳过的重复)。"""
    query: str
    new: int = 0
    tries: int = 0
```

**3b. 初检索改为按子查询记账**(zip 保序,去重语义与原 setdefault 完全等价)。替换:

```python
        if subqueries:
            with ThreadPoolExecutor(max_workers=min(len(subqueries), 8)) as ex:
                # map 保序:第 i 个结果对应第 i 个子查询,与提交顺序一致。
                for hits in ex.map(_run_search, subqueries):
                    raise_if_cancelled(self.cancel_event)
                    for h in hits:
                        collected.setdefault(h.object_id, h)
```

为:

```python
        # 子查询执行账目(初始 plan 与 add_subquery 后补都记):归一化键 → 账目。
        # 每轮回喂 reflect(模型能看到试过什么、哪条是干的),add_subquery 对
        # 重复键硬跳过 —— 治「反复补充同一条子查询」的两层根源。
        attempted: Dict[str, _QueryAttempt] = {}
        if subqueries:
            with ThreadPoolExecutor(max_workers=min(len(subqueries), 8)) as ex:
                # map 保序:第 i 个结果对应第 i 个子查询,与提交顺序一致。
                for sq, hits in zip(subqueries, ex.map(_run_search, subqueries)):
                    raise_if_cancelled(self.cancel_event)
                    rec = attempted.setdefault(_norm_query(sq.query),
                                               _QueryAttempt(query=sq.query))
                    rec.tries += 1
                    for h in hits:
                        if h.object_id not in collected:
                            collected[h.object_id] = h
                            rec.new += 1
```

**3c. while 循环内 summary 组装处**,在 visited 回喂块(`（已展开过的节点，勿重复 expand_graph 请求它们: {vis}）`)之后追加:

```python
            # 已执行过的子查询账目回喂 reflect(镜像 visited 回喂,治"反复补充同
            # 一条子查询"):模型据此区分"没查过"与"查过但没捞到";账目含尝试次数,
            # 重复被跳过时 prompt 仍变化 → 不再是不动点,LLM 缓存不会逐字重放决策。
            if attempted:
                tried = "、".join(
                    f"「{a.query}」(新增{a.new}条"
                    + (f",已试{a.tries}次" if a.tries > 1 else "") + ")"
                    for a in attempted.values())
                summary = (f"{summary}\n\n（已执行过的子查询及各自新增证据数: {tried}。"
                           "勿重复提交相同子查询;新增为 0 的方向请换明显不同的问法,"
                           "或改用其他动作。）")
```

**3d. add_subquery 分支**整体替换(原 :315-329):

```python
            elif decision.next_action == "add_subquery":
                if not decision.new_sub_query:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 add_subquery(缺少 new_sub_query)",
                                     detail={"reason": "missing_new_sub_query"}))
                else:
                    sq = decision.new_sub_query
                    key = _norm_query(sq.query)
                    if key in attempted:
                        # 重复子查询硬跳过(镜像 expand_graph 的 visited 守卫):
                        # 不重跑检索;tries 递增让回喂账目(与 prompt)随之变化。
                        attempted[key].tries += 1
                        record(TraceStep(step_type="skip",
                                         summary=f"跳过重复子查询: {sq.query}",
                                         detail={"query": sq.query,
                                                 "reason": "duplicate_subquery",
                                                 "tries": attempted[key].tries}))
                    else:
                        added = 0
                        for h in self.search(notebook_id, sq.query,
                                             sq.types, sq.prefer)[:_PER_QUERY_LIMIT]:
                            raise_if_cancelled(self.cancel_event)
                            if h.object_id not in collected:
                                collected[h.object_id] = h
                                added += 1
                        attempted[key] = _QueryAttempt(query=sq.query,
                                                       new=added, tries=1)
                        if sq.query not in used_queries:
                            used_queries.append(sq.query)
                        record(TraceStep(step_type="retrieve",
                                         summary=f"补充子查询: {sq.query}",
                                         detail={"query": sq.query, "new": added}))
```

注意:`used_queries` 语义不变(原文精确串,供末尾 `_quota_rerank`);重复键的第二种原文不进 `used_queries`(近似重复,不该占配额组)。

- [ ] **Step 4: 跑新测试确认通过**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q -k "duplicate_subquery or attempted"`
Expected: 2 PASS

- [ ] **Step 5: 跑整个测试文件防回归**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py backend/tests/test_reasoning_ppr.py backend/tests/test_reasoning_ask.py backend/tests/test_reasoning_stream.py backend/tests/test_cross_tier_reasoning.py -q`
Expected: 全 PASS。⚠ 若有既有用例靠「重复 add_subquery 反复空转」构造 stale 熔断场景:跳过仍是零新增→stale 照样累加→熔断语义不变,但 trace 里 `retrieve` 步会变成 `skip` 步——如断言具体 step_type/次数,按新语义修正断言并在 commit message 里说明,不得放宽被测行为。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "fix(reasoning): 子查询账目回喂 reflect + add_subquery 重复硬跳过

治「反思连续多轮补充同一条子查询」:used_queries 从不回喂 reflect、
add_subquery 是四动作里唯一零防重、重复被 setdefault 吸收后 summary
成 prompt 不动点(LLM_CACHE 下逐字重放决策)。新增 attempted 账目
(归一化 query→原文/新增数/尝试次数):初检索与后补都记账,每轮拼进
reflect summary(镜像 visited 回喂,账目变化亦破缓存不动点),重复键
硬跳过不重跑检索(skip trace, reason=duplicate_subquery)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `_summarize` 头尾窗口(修「前 30 条盲区」)

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`(`_summarize`,原 :177-186)
- Test: `backend/tests/test_reasoning_retrieval.py`(追加 2 个用例)

- [ ] **Step 1: 写失败测试**(`_mk_rk` 已在测试文件中定义)

```python
def test_window_helper_head_tail_split():
    """_window: 超窗保留最早 head 条+最新 tail 条并报省略数;不超窗原样返回。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    head, tail, omitted = ReasoningRetriever._window(list(range(15)), 6, 4)
    assert head == list(range(6))
    assert tail == [11, 12, 13, 14]
    assert omitted == 5
    head, tail, omitted = ReasoningRetriever._window(list(range(10)), 6, 4)
    assert head == list(range(10)) and tail == [] and omitted == 0


def test_summarize_shows_recent_tail_when_over_window(rrepo):
    """collected 超 30 条时,summary 必须含最近加入的尾段(修「新增证据落在
    前 30 条窗口外 → summary 不变 → reflect 误判无进展/重复请求」盲区);
    ≤30 条时输出与旧行为完全一致(无省略标记)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever(rrepo, rrepo.settings)
    big = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(45)}
    out = r._summarize(big, [], [])
    assert "节点0" in out and "节点19" in out        # 头段(最早 20 条)
    assert "节点35" in out and "节点44" in out       # 尾段(最新 10 条)
    assert "节点25" not in out                       # 中间被省略
    assert "省略中间 15 条" in out
    small = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(30)}
    out2 = r._summarize(small, [], [])
    assert "省略" not in out2 and "节点29" in out2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q -k "window or recent_tail"`
Expected: FAIL(`_window` 不存在;节点35 不在 out)

- [ ] **Step 3: 实现——`_summarize` 整体替换为**

```python
    @staticmethod
    def _window(items, head, tail):
        """头+尾窗口:超窗时保留最早 head 条 + 最新 tail 条,返回 (头段, 尾段, 省略数)。
        collected/elements/chunks 都按插入序只增不删,纯前缀窗口会让"最近新增"
        落在窗口外:reflect 看到的 summary 不变,误判无进展、重复请求。"""
        if len(items) <= head + tail:
            return list(items), [], 0
        return list(items[:head]), list(items[-tail:]), len(items) - head - tail

    def _summarize(self, collected, elements, chunks):
        lines = []

        def _kg_line(rk):
            name = str(rk.payload.get("name", "")).strip() or rk.object_id
            return f"- [{rk.object_type}] {name} (id={rk.object_id})"

        def _el_line(el):
            return f"- [element] {el.source_title} · {el.location_label}: {el.text[:80]}"

        def _ch_line(c):
            return f"- [chunk] {c.source_title} · {c.section_path}: {c.text[:80]}"

        for items, render, head_n, tail_n, noun in (
                (list(collected.values()), _kg_line, 20, 10, "条较早候选"),
                (elements, _el_line, 6, 4, "段较早原文"),
                (chunks, _ch_line, 6, 4, "段较早原文")):
            head, tail, omitted = self._window(items, head_n, tail_n)
            lines.extend(render(x) for x in head)
            if omitted:
                lines.append(f"-（省略中间 {omitted} {noun},以下为最近加入）")
            lines.extend(render(x) for x in tail)
        return "\n".join(lines) if lines else "(no candidates yet)"
```

渲染预算不变(KG 30/element 10/chunk 10),≤窗口时输出与旧版逐字节一致。

- [ ] **Step 4: 跑新测试确认通过**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q -k "window or recent_tail"`
Expected: PASS

- [ ] **Step 5: 跑整个测试文件防回归**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_reasoning_retrieval.py
git commit -m "fix(reasoning): _summarize 头尾窗口——最近新增证据对 reflect 恒可见

原实现只渲染 collected 前 30 条(插入序,新增永远追加在尾部),初检索
5 子查询×8 命中即占满窗口,此后新证据全落窗口外:summary 不变→reflect
误判无进展/重复请求(有进展也重复)。改为头 20+尾 10(element/chunk 同理
6+4),超窗标注省略数;渲染预算不变,不超窗时输出与旧版逐字节一致。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: reflect_prompt 静态告诫

**Files:**
- Modify: `backend/app/services/prompts.py`(reflect_prompt,原 :221-222)
- Test: `backend/tests/test_reasoning_retrieval.py`(追加 1 个用例)

- [ ] **Step 1: 写失败测试**

```python
def test_reflect_prompt_warns_against_resubmitting_tried_subqueries():
    """静态指令层也要有勿重复告诫(动态账目回喂之外的第二层):expand_graph
    文案明写可反复展开,add_subquery 原本连'勿重复'都没有——治理不对称。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "Never re-submit" in p
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q -k warns_against`
Expected: FAIL

- [ ] **Step 3: 实现——`prompts.py` 中 add_subquery 一行替换**

原:

```python
        "- add_subquery: an aspect of the question is uncovered; add one "
        "sub-query (set new_sub_query).\n"
```

改:

```python
        "- add_subquery: an aspect of the question is uncovered; add one "
        "sub-query (set new_sub_query). Never re-submit a sub-query already "
        "listed as tried in the context; rephrase it substantially or choose "
        "a different action.\n"
```

- [ ] **Step 4: 跑测试确认通过 + 该文件全绿**

Run: `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prompts.py backend/tests/test_reasoning_retrieval.py
git commit -m "fix(prompts): reflect_prompt 对 add_subquery 补勿重复告诫

静态指令层不对称:expand_graph 明写 may expand repeatedly,add_subquery
连勿重复告诫都没有。补 Never re-submit a tried sub-query(与运行期账目
回喂呼应,tried 列表即 summary 中的已执行子查询账目)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 全量验证 + PR

- [ ] **Step 1: 全量后端测试**

Run: `$PY -m pytest backend/tests -q`
Expected: 全 PASS(基线 66+/reasoning 文件,全套 ~1000)

- [ ] **Step 2: 全检**

Run: `bash scripts/check.sh`
Expected: EXIT=0(含 smoke_backend;若 smoke 有 reasoning 断言受 trace 变化影响,修 smoke 断言非产品代码)

- [ ] **Step 3: PR**(由控制器执行)

```bash
git fetch origin && git rebase origin/master
git push -u origin worktree-subquery-repeat-guard
gh pr create --base master --title "fix(reasoning): 深挖重复子查询防重——账目回喂+硬跳过+summary 窗口盲区" --body "..."
```
