# 深度报告性能与可控性 Implementation Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development / TDD。

**Goal:** 治报告"看着卡住/太慢"(根因已诊断:全 deepseek-v4-pro、每次 40–120s,4 节×完整深挖 ≈12min)。三件事:①智能滑块(每节 reflect 步上限,默认 2)②节内实时进度 ③模型定并发(min(节数, kg_job_concurrency))。

**根因证据(已取证,勿再查):** 报告窗口 38 次 LLM 调用全 deepseek-v4-pro,p50 44s/max 121s,零 429;其中 reflect 25 次是大头。非 bug、非 hang,是"pro 思考模型 × 4 节完整深挖"的固有成本。

**验证命令:**
```bash
PY=/opt/homebrew/Caskroom/miniconda/base/bin/python
$PY -m pytest backend/tests/test_report_engine.py backend/tests/test_report_api.py backend/tests/test_reasoning_retrieval.py -q
$PY -m pytest backend/tests -q
cd frontend && npx tsc --noEmit && npm run lint && npm run test
```
提交:中文 conventional commits,末尾 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。不 push/不启停服务/不用 dangerouslyDisableSandbox。

---

### Task 1: reasoning `run(max_steps=)` 覆盖 + 删 REPORT_SECTION_CONCURRENCY

**Files:** `backend/app/services/reasoning_retrieval.py`、`backend/app/core/config.py`、`.env.example`、`README.md`、`README_zh.md`、`backend/tests/test_reasoning_retrieval.py`、`backend/tests/test_report_engine.py`(改既有断言)

- [ ] **Step 1 失败测试**(test_reasoning_retrieval.py 追加):

```python
def test_run_max_steps_override_caps_reflect_loop(rrepo):
    """max_steps 覆盖 settings.reasoning_max_steps,封顶 reflect 轮数(报告滑块用)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    calls = {"reflect": 0}

    class _LoopLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            calls["reflect"] += 1
            return json.dumps({"next_action": "search_elements", "elements_query": "q"})  # 永不 answer

    rrepo.llm_client = _LoopLLM()
    ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", max_steps=2)
    assert calls["reflect"] <= 2      # 被 max_steps=2 封顶(而非 settings 的 50)
```

- [ ] **Step 2 跑失败** → TypeError unexpected 'max_steps' 或 reflect>2。
- [ ] **Step 3 实现:**
  - `reasoning_retrieval.py:226` 签名加 `max_steps=None`:`def run(self, notebook_id, question, history="", on_step=None, top_n=None, max_steps=None):`;`top_n = top_n or self.settings.retrieval_top_n` 之后加 `max_steps = max_steps or self.settings.reasoning_max_steps`。
  - `:312` `while steps < self.settings.reasoning_max_steps:` → `while steps < max_steps:`。
  - `config.py:96-97` **删** `report_section_concurrency` 字段整块。
  - `test_report_engine.py` 的 `test_report_settings_defaults` **删** `assert s.report_section_concurrency == 3` 一行(若存在)。
  - `.env.example` 删 `REPORT_SECTION_CONCURRENCY=3` 那行(连同其上注释若专属);`README.md`/`README_zh.md` 删 `REPORT_SECTION_CONCURRENCY` 那行。
- [ ] **Step 4 跑过** + `$PY -m pytest backend/tests/test_reasoning_retrieval.py backend/tests/test_report_engine.py -q`。
- [ ] **Step 5 Commit** `feat(reasoning): run(max_steps=) 覆盖 reflect 上限 + 删 REPORT_SECTION_CONCURRENCY(复用 kg_job_concurrency)`

---

### Task 2: report_engine——depth 穿透 + 并发复用 kg_job_concurrency + 节内进度

**Files:** `backend/app/services/report_engine.py`、`backend/tests/test_report_engine.py`

- [ ] **Step 1 失败测试**(test_report_engine.py 追加):

```python
def test_run_sections_concurrency_uses_kg_job_concurrency(repo, monkeypatch):
    """并发 = min(节数, kg_job_concurrency);节数≤上限时全并行。"""
    monkeypatch.setattr(repo.settings, "kg_job_concurrency", 5)
    eng = _mk_engine(repo, _OutlineLLM())
    seen = {"max": 0, "cur": 0}
    import threading as _t
    lk = _t.Lock()
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question, depth=None, on_step=None):
        with lk:
            seen["cur"] += 1; seen["max"] = max(seen["max"], seen["cur"])
        try:
            return ReasoningResult()
        finally:
            with lk: seen["cur"] -= 1
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": f"S{i}", "scope": "s", "sub_queries": ["q"]} for i in range(4)]
    eng._run_sections(nb.id, rid, outline, "q", depth=2)
    assert seen["max"] == 4          # 4 节 ≤ 上限5 → 全并行


def test_deep_dive_passes_depth_as_max_steps(repo, monkeypatch):
    eng = _mk_engine(repo, _OutlineLLM())
    captured = {}
    from app.services.reasoning_retrieval import ReasoningResult
    class _R:
        def __init__(self, *a): pass
        def run(self, nb_id, q, **kw):
            captured.update(kw); return ReasoningResult()
    monkeypatch.setattr("app.services.reasoning_retrieval.ReasoningRetriever", _R)
    eng._deep_dive("nb", {"title": "t", "scope": "s", "sub_queries": ["q"]}, "Q", depth=3, on_step=None)
    assert captured.get("max_steps") == 3


def test_run_sections_writes_section_status(repo, monkeypatch):
    """每节完成后 section_status 落库,各节 phase=完成。"""
    eng = _mk_engine(repo, _OutlineLLM())
    from app.services.reasoning_retrieval import ReasoningResult
    monkeypatch.setattr(eng, "_deep_dive", lambda *a, **k: ReasoningResult())
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]},
               {"title": "B", "scope": "s", "sub_queries": ["q"]}]
    eng._run_sections(nb.id, rid, outline, "q", depth=2)
    detail = repo.get_report(nb.id, rid)
    assert len(detail["section_status"]) == 2
    assert all(x["phase"] == "完成" for x in detail["section_status"])
```

- [ ] **Step 2 跑失败**。
- [ ] **Step 3 实现:**

**3a. `_deep_dive`**(:88)签名加 `depth`/`on_step`,透传:

```python
    def _deep_dive(self, notebook_id, section, question, depth=None, on_step=None):
        from app.services.reasoning_retrieval import ReasoningRetriever
        sec_question = (f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
                        f"[本节检索方向] " + "; ".join(section["sub_queries"]))
        return ReasoningRetriever(self.repo, self.settings, self.cancel_event).run(
            notebook_id, sec_question, on_step=on_step,
            top_n=self.settings.report_section_top_n, max_steps=depth)
```

**3b. `_run_sections`**(:128-153)整体替换(顶部 `import threading, time`;time.monotonic 允许——真 Python 非 workflow):

```python
    def _run_sections(self, notebook_id, rid, outline, question, depth):
        status = [{"title": s["title"], "phase": "排队", "step": 0} for s in outline]
        lock = threading.Lock()
        last = [0.0]

        def persist(force=False):
            now = time.monotonic()
            with lock:
                if not force and now - last[0] < 2.0:
                    return
                last[0] = now
                snap = [dict(x) for x in status]
            done = sum(1 for x in snap if x["phase"] in ("完成", "失败"))
            running = sum(1 for x in snap if x["phase"] not in ("排队", "完成", "失败"))
            self.repo.update_report(
                notebook_id, rid, section_status=snap,
                progress=f"章节 {done}/{len(outline)} 完成 · {running} 进行中")

        _PHASE = {"plan": "规划", "reflect": "深挖", "retrieve": "深挖", "expand": "深挖",
                  "ppr": "深挖", "fallback": "深挖"}

        def _one(i, section):
            raise_if_cancelled(self.cancel_event)
            with lock:
                status[i]["phase"] = "规划"
            persist(force=True)

            def on_step(step, _i=i):
                with lock:
                    ph = _PHASE.get(step.step_type)
                    if ph:
                        status[_i]["phase"] = ph
                    if step.step_type == "reflect":
                        status[_i]["step"] += 1
                persist()

            try:
                result = self._deep_dive(notebook_id, section, question, depth, on_step)
                with lock:
                    status[i]["phase"] = "撰写"
                persist(force=True)
                drafted = self._draft_section(notebook_id, section, question, result)
                with lock:
                    status[i]["phase"] = "完成"
            except AskCancelled:
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                raise
            except Exception as exc:
                drafted = {"title": section["title"], "scope": section["scope"],
                           "markdown": "", "grounded": False, "failed": True,
                           "error": str(exc)[:300], "id_map": {},
                           "attempted": [], "top_concepts": []}
                with lock:
                    status[i]["phase"] = "失败"
            persist(force=True)
            return drafted

        workers = max(1, min(len(outline), int(self.settings.kg_job_concurrency)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _one, i, s)
                       for i, s in enumerate(outline)]
            return [f.result() for f in futures]
```

**3c. `run`**(:156)签名加 `depth=2`,传给 `_run_sections`:

```python
    def run(self, notebook_id, rid, question, history="", depth=2):
        ...
            sections = self._run_sections(notebook_id, rid, outline, question, depth)
```

(顶部 import 段加 `import threading` `import time`;`_run_sections` 中间的旧 `update_report(progress="汇总中")` 保留在 run() 内不变。)

- [ ] **Step 4 跑过**;**Step 5 Commit** `feat(report): depth 穿透 reflect 上限 + 并发复用 kg_job_concurrency + 节内实时进度(section_status,节流2s)`

---

### Task 3: DB 列 + schema + API depth 参数

**Files:** `backend/app/services/sqlite_repository.py`、`backend/app/models/schemas.py`、`backend/app/api/routes.py`、`backend/tests/test_report_api.py`

- [ ] **Step 1 失败测试**(test_report_api.py 生命周期补):

```python
    # 起报告带 depth
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?", "depth": 8})
    ...
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["depth"] == 8
    assert "section_status" in detail
```

- [ ] **Step 2 跑失败**。
- [ ] **Step 3 实现:**
  - **sqlite_repository.py** reports CREATE TABLE 加两列(gaps_json 后):`depth INTEGER NOT NULL DEFAULT 2,` 与 `section_status_json TEXT NOT NULL DEFAULT '[]',`。
  - `create_report(self, notebook_id, question, depth=2)`:INSERT 加 depth 列。
  - `update_report` 循环元组加 `("section_status_json", section_status, True)`;签名加 `section_status=None`。
  - `_report_row_to_dict` full 分支加 `depth=row["depth"]`、`section_status=json.loads(row["section_status_json"] or "[]")`;summary(list)分支也带 `depth`(轻量,可选)。
  - **schemas.py**:`ReportCreate` 加 `depth: int = 2`;`ReportDetail` 加 `depth: int = 2` 与 `section_status: List[dict] = Field(default_factory=list)`。
  - **routes.py** `create_report`:`depth = max(1, min(16, int(payload.depth)))`;`repo.create_report(notebook_id, payload.question.strip(), depth=depth)`;`_launch_report_job(repo, notebook_id, rid, q, history, depth)`;`_launch_report_job` 与其 worker 内 `ReportEngine(...).run(notebook_id, rid, question, history, depth=depth)`。
- [ ] **Step 4 跑过**;**Step 5 Commit** `feat(api): 报告 depth 参数(1-16,默认2)+ section_status 落库暴露`

---

### Task 4: 前端——智能滑块 + 节内进度行

**Files:** `frontend/app/report-view.tsx`(+ 必要 css)

- [ ] **Step 1:** `ReportSummaryT`/`ReportDetailT` 加 `depth?: number`、`ReportDetailT` 加 `section_status?: { title: string; phase: string; step: number }[]`;`createReport(nb, question, depth)` 加 depth 入 body。
- [ ] **Step 2 智能滑块**(生成区,textarea 上/下):5 档 `const DEPTHS = [1, 2, 4, 8, 16]`,`const DEPTH_LABELS = ["最快","快","均衡","深入","最深"]`,state `depthIdx`(默认 1);渲染成「更快 ←→ 更聪明」滑块(`<input type="range" min=0 max=4 step=1>` + 两端标签 + 当前档标签),生成时 `createReport(nb, q, DEPTHS[depthIdx])`。样式对齐现有控件(ui-polish-bar);滑块可用原生 range 加 class 美化。
- [ ] **Step 3 节内进度**:详情/进度视图里,`detail.status` 非终态且 `section_status` 非空时,渲染每节一行:`{title} · {phase}{phase==='深挖'?` 第${step}步`:''}`,phase∈完成 显示对勾、进行中显示 spinner、失败显示叹号。取代原来只有一句 `progress`。列表对齐、phase 文案短。
- [ ] **Step 4 校验** `cd frontend && npx tsc --noEmit && npm run lint && npm run test`;`git diff | grep -c '^-.*[""]'`=0(不动 page.tsx 弯引号)。
- [ ] **Step 5 Commit** `feat(fe): 深度报告智能滑块(更快↔更聪明)+ 节内实时进度行`

---

### Task 5: 全量验证 + 文档 + PR(控制器)
`$PY -m pytest backend/tests -q` + `bash scripts/check.sh` EXIT=0 → README 双语补 depth 滑块说明、删 REPORT_SECTION_CONCURRENCY 行 → rebase origin/master → push → PR。
