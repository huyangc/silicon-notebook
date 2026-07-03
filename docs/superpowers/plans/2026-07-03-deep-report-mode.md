# 深度报告模式(Deep Report)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** notebook 级后台任务生成多节深度技术报告:大纲规划→每节完整 reasoning 深挖(节间并行)→逐节撰写(证据三层 [k]/（推断）/【通识】)→汇总(执行摘要/参考/知识缺口/分析计划),前后端同 PR。

**Architecture:** 新服务模块 `report_engine.py`(镜像 ReasoningRetriever 的 (repo, settings, cancel_event) 形态)编排四阶段;repo 层只加 reports 表+CRUD;复用 `ReasoningRetriever.run`(小改:暴露 attempted、支持 top_n 覆盖)、`_answer_context`/`_chunk_answer_context`、cap_kwargs、copy_context 线程范式。前端在 CHAT_MODES 加「深度报告」tab,复用 AnswerMarkdown 渲染栈(react-markdown+KaTeX 已在)。

**Tech Stack:** Python/FastAPI/SQLite/pytest;Next.js/React/TypeScript。

**Spec:** `docs/superpowers/specs/2026-07-03-deep-report-mode-design.md`(已随本计划入库;用户已批:每节完整深挖+节间并行3、【通识】默认开、复用现成 react-markdown)。

**验证命令**(worktree 根):
```bash
PY=/opt/homebrew/Caskroom/miniconda/base/bin/python
$PY -m pytest backend/tests/test_report_engine.py backend/tests/test_report_api.py -q
$PY -m pytest backend/tests -q          # 全量(收尾)
bash scripts/check.sh                    # 收尾(含前端 npm run test/lint)
cd frontend && npx tsc --noEmit          # 前端任务
```

**提交规范:** 中文 conventional commits,末尾单独一行 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`;不 push、不启停服务、不用 dangerouslyDisableSandbox。

**关键接线事实**(侦察确认,直接引用勿再探):
- `ReasoningRetriever.run(notebook_id, question, history="", on_step=None) -> ReasoningResult`(reasoning_retrieval.py:224;`ReasoningResult` dataclass :82-87 有 top_hits/elements/trace/chunks);`attempted: Dict[str, _QueryAttempt]` 在 run 内 :265,`_QueryAttempt(query,new,tries)` :53-58,目前未暴露。
- quota/top_n 消费点 :452-467(`self.settings.retrieval_top_n` 两处)。
- 后台线程范式 routes.py:621:`ctx = contextvars.copy_context(); threading.Thread(target=lambda: ctx.run(worker), daemon=True).start()`(必须用它,per-user 模型靠 `_REQUEST_USER` ContextVar,见 sqlite_repository.py:203/1450)。
- 守卫:deps.py:52 `require_notebook_write`(owner)、:61 `require_notebook_read`(owner∪成员),路由用 `dependencies=[Depends(...)]`(routes.py:814 范本)。
- 建表集中在 sqlite_repository.py `_migrate()` 的 `db.executescript` 块(:459-950);id 范式 `f"rep-{uuid4().hex[:10]}"`(仿 :11836 ans-);时间 `_now()`。
- config 新字段必须 `Field(..., validation_alias="ENV_NAME")`(config.py:80-83 范本;`Field(env=)` 静默失效)。
- LLM 上限:`**cap_kwargs(client, "<settings字段名>")`(llm.py:49,调用范本 sqlite_repository.py:11271)。
- 上下文组装:`_chunk_answer_context(chunks, budget_chars=None) -> (block, id_map)`(:10461);`_answer_context(notebook_id, top_hits, id_offset=0) -> (block, id_map)`(:11065)。
- 邻居:`repo._retrieve_neighbors(notebook_id, object_id, edge_type, direction="both") -> List[RetrievedKnowledge]`(:10184)。
- 前端:api helper page.tsx:495;toast :893;CHAT_MODES 常量 :211-214(现 ask/rules 两项);chat tab 渲染 :3199-3207;chat-body 分支 :3327 起;startKgBuild 范本 :957-962 + kg 轮询 :996-1016;AnswerMarkdown 组件在 frontend/app/answer-markdown.tsx(ReactMarkdown+remarkGfm+remarkMath+rehypeKatex 已装);无现成下载,用 Blob+`<a download>`。

---

### Task 1: REPORT_* 配置项

**Files:** Modify `backend/app/core/config.py`;Test `backend/tests/test_report_engine.py`(新建)

- [ ] **Step 1: 失败测试**(新建 test_report_engine.py,头部 `import json`、`import pytest` 按需):

```python
def test_report_settings_defaults():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.report_max_sections == 6
    assert s.report_section_top_n == 12
    assert s.report_section_chunk_budget == 20000
    assert s.report_section_max_tokens == 8192
    assert s.report_allow_parametric is True
    assert s.report_section_concurrency == 3


def test_report_settings_env(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("REPORT_MAX_SECTIONS", "4")
    monkeypatch.setenv("REPORT_ALLOW_PARAMETRIC", "false")
    s = Settings(_env_file=None)
    assert s.report_max_sections == 4
    assert s.report_allow_parametric is False
```

- [ ] **Step 2: 跑失败** `$PY -m pytest backend/tests/test_report_engine.py -q` → AttributeError
- [ ] **Step 3: 实现**——config.py 在 answer_max_tokens 附近追加(注意 validation_alias):

```python
    # --- 深度报告(report_engine) ---
    report_max_sections: int = Field(6, validation_alias="REPORT_MAX_SECTIONS")
    report_section_top_n: int = Field(12, validation_alias="REPORT_SECTION_TOP_N")
    report_section_chunk_budget: int = Field(
        20000, validation_alias="REPORT_SECTION_CHUNK_BUDGET")
    report_section_max_tokens: int = Field(
        8192, validation_alias="REPORT_SECTION_MAX_TOKENS")
    # 【通识】层:允许报告引入库外参数知识(行内标注,仅报告管线读取)。
    report_allow_parametric: bool = Field(
        True, validation_alias="REPORT_ALLOW_PARAMETRIC")
    # 节间并行度(节深挖无耦合;尊重全局限流退避)。
    report_section_concurrency: int = Field(
        3, validation_alias="REPORT_SECTION_CONCURRENCY")
```

- [ ] **Step 4: 跑过** → 2 PASS
- [ ] **Step 5: Commit** `feat(config): 深度报告 REPORT_* 配置项(validation_alias)`

---

### Task 2: reports 表 + repo CRUD

**Files:** Modify `backend/app/services/sqlite_repository.py`;Test `backend/tests/test_report_engine.py`

- [ ] **Step 1: 失败测试**(复用现有测试的 repo fixture 风格——文件内新建 `rrepo` 同款 fixture,拷 test_reasoning_retrieval.py:89-121 的 env 打底写法):

```python
@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SqliteRepository
    return SqliteRepository(Settings(_env_file=None))


def _mk_nb(repo):
    from app.models.schemas import NotebookCreate
    return repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))
    # ↑ 若 NotebookCreate 必填字段不同,以 schemas.py 真实定义为准最小适配。


def test_report_crud_roundtrip(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "为什么 bandgap 是 1.2V?")
    assert rid.startswith("rep-")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "pending" and detail["question"].startswith("为什么")
    repo.update_report(nb.id, rid, status="running", progress="大纲规划中")
    repo.update_report(nb.id, rid, outline=[{"title": "机理", "scope": "s", "sub_queries": ["q1"]}],
                       sections=[{"title": "机理", "markdown": "md", "grounded": True}],
                       gaps=["缺 X"], content_md="# 报告", status="done", progress="完成")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done" and detail["content_md"] == "# 报告"
    assert detail["outline"][0]["title"] == "机理" and detail["gaps"] == ["缺 X"]
    lst = repo.list_reports(nb.id)
    assert len(lst) == 1 and lst[0]["id"] == rid and "content_md" not in lst[0]
    repo.delete_report(nb.id, rid)
    assert repo.list_reports(nb.id) == []
    with pytest.raises(KeyError):
        repo.get_report(nb.id, rid)
```

- [ ] **Step 2: 跑失败** → AttributeError create_report
- [ ] **Step 3: 实现**——(a) `_migrate()` 的 executescript 块内追加:

```sql
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  outline_json TEXT NOT NULL DEFAULT '[]',
  sections_json TEXT NOT NULL DEFAULT '[]',
  gaps_json TEXT NOT NULL DEFAULT '[]',
  content_md TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  progress TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_nb_created ON reports(notebook_id, created_at DESC);
```

(b) repo 方法(放 conversations CRUD 附近,风格一致;JSON 列 json.dumps/loads):

```python
    # --- 深度报告 ---
    def create_report(self, notebook_id: str, question: str) -> str:
        self.get_notebook(notebook_id)          # 不存在则 KeyError
        rid = f"rep-{uuid4().hex[:10]}"
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO reports(id, notebook_id, question, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (rid, notebook_id, question, self.current_user().id, now, now))
        return rid

    def update_report(self, notebook_id: str, report_id: str, *, status=None,
                      progress=None, error=None, outline=None, sections=None,
                      gaps=None, content_md=None) -> None:
        sets, args = ["updated_at = ?"], [_now()]
        for col, val, dump in (("status", status, False), ("progress", progress, False),
                               ("error", error, False), ("content_md", content_md, False),
                               ("outline_json", outline, True),
                               ("sections_json", sections, True), ("gaps_json", gaps, True)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(json.dumps(val, ensure_ascii=False) if dump else val)
        args.extend([report_id, notebook_id])
        with self._connect() as db:
            db.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = ? AND notebook_id = ?", args)

    def _report_row_to_dict(self, row, *, full: bool) -> dict:
        d = {"id": row["id"], "notebook_id": row["notebook_id"], "question": row["question"],
             "status": row["status"], "progress": row["progress"], "error": row["error"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "updated_at": row["updated_at"],
             "section_count": len(json.loads(row["outline_json"] or "[]"))}
        if full:
            d.update(outline=json.loads(row["outline_json"] or "[]"),
                     sections=json.loads(row["sections_json"] or "[]"),
                     gaps=json.loads(row["gaps_json"] or "[]"),
                     content_md=row["content_md"])
        return d

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id = ? AND notebook_id = ?",
                             (report_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self._report_row_to_dict(row, full=True)

    def list_reports(self, notebook_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM reports WHERE notebook_id = ? ORDER BY created_at DESC, id",
                (notebook_id,)).fetchall()
        return [self._report_row_to_dict(r, full=False) for r in rows]

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM reports WHERE id = ? AND notebook_id = ?",
                       (report_id, notebook_id))
```

- [ ] **Step 4: 跑过**;**Step 5: Commit** `feat(repo): reports 表 + CRUD(深度报告落库)`

---

### Task 3: ReasoningResult 暴露 attempted + run(top_n=) 覆盖

**Files:** Modify `backend/app/services/reasoning_retrieval.py`;Test `backend/tests/test_reasoning_retrieval.py`

- [ ] **Step 1: 失败测试**(追加到 test_reasoning_retrieval.py 末尾,复用该文件 `rrepo`/`_seed_two_nodes`):

```python
def test_run_exposes_attempted_ledger_and_top_n_override(rrepo):
    """报告管线依赖:run() 返回 attempted 账目(query/new/tries),且 top_n 参数
    覆盖 settings.retrieval_top_n(每节独立预算)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _PlanOnlyLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            return json.dumps({"next_action": "answer", "sufficient": True})

    rrepo.llm_client = _PlanOnlyLLM()
    result = ReasoningRetriever(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "", top_n=1)
    assert len(result.top_hits) <= 1                       # top_n 覆盖生效
    assert result.attempted and result.attempted[0]["query"] == "RTL到GDSII流程"
    assert set(result.attempted[0]) == {"query", "new", "tries"}
```

- [ ] **Step 2: 跑失败**(TypeError top_n / AttributeError attempted)
- [ ] **Step 3: 实现**:
  - `ReasoningResult`(:82-87)加字段 `attempted: List[dict] = field(default_factory=list)`。
  - `run` 签名(:224)改 `def run(self, notebook_id, question, history="", on_step=None, top_n=None):`,首行后加 `top_n = top_n or self.settings.retrieval_top_n`;:456 与 :465 两处 `self.settings.retrieval_top_n` 改用局部 `top_n`。
  - return(:471-472)改为:

```python
        return ReasoningResult(
            top_hits=top_hits, elements=elements, trace=trace, chunks=chunks,
            attempted=[{"query": a.query, "new": a.new, "tries": a.tries}
                       for a in attempted.values()])
```

- [ ] **Step 4: 跑过 + 全文件回归** `$PY -m pytest backend/tests/test_reasoning_retrieval.py -q`
- [ ] **Step 5: Commit** `feat(reasoning): run() 暴露 attempted 账目 + top_n 覆盖(供报告逐节深挖)`

---

### Task 4: 报告三 prompt(大纲/节撰写/执行摘要)

**Files:** Modify `backend/app/services/prompts.py`;Test `backend/tests/test_report_engine.py`

- [ ] **Step 1: 失败测试**:

```python
def test_report_prompts_contract():
    from app.services.prompts import (
        report_outline_prompt, report_section_prompt, report_summary_prompt,
        REPORT_OUTLINE_SCHEMA_HINT, REPORT_SECTION_SCHEMA_HINT)
    op = report_outline_prompt("q", max_sections=5, history_block="h")
    assert "3-5" in op and "sub_queries" in op and "Prior conversation" in op
    sp = report_section_prompt("失效机理", "应力如何改变 VBE", "总问题", "CTX",
                               allow_parametric=True)
    assert "【通识】" in sp and "[k" in sp and "layer by layer" not in sp  # 独立文本,非复用 answer_prompt
    assert "ONLY this section" in sp
    sp2 = report_section_prompt("t", "s", "q", "CTX", allow_parametric=False)
    assert "【通识】" not in sp2
    su = report_summary_prompt("总问题", "## 节1\nmd")
    assert "executive summary" in su.lower()
```

- [ ] **Step 2: 跑失败**;**Step 3: 实现**(prompts.py 末尾追加;英文指令+中文标记,风格同现有):

```python
# ---------------------------------------------------------------------------
# 深度报告(report_engine)
# ---------------------------------------------------------------------------

REPORT_OUTLINE_SCHEMA_HINT = (
    '{"sections":[{"title":"","scope":"","sub_queries":[""]}]}')


def report_outline_prompt(question: str, max_sections: int = 6,
                          history_block: str = "") -> str:
    history_section = (
        "Prior conversation (for context):\n" f"{history_block}\n\n"
        if history_block else "")
    return (
        "You plan the OUTLINE of a deep technical report that answers an "
        "engineer's question from a document corpus. Produce 3-" f"{max_sections} "
        "sections. Rules:\n"
        "- Sections follow the question's own structure; for a multi-layer "
        "mechanism question, one section per abstraction layer (e.g. circuit "
        "principle / device physics / statistical & solid-state physics / "
        "quantum-lattice origin / engineering requirements such as packaging & "
        "materials).\n"
        "- Do NOT include executive-summary / references / knowledge-gap "
        "sections — the system appends those automatically.\n"
        "- Each section: title (in the question's language), scope (one line, "
        "what the section must establish), sub_queries (2-4 focused ENGLISH "
        "retrieval queries for that section's evidence).\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"sections":[{"title":"","scope":"","sub_queries":[""]}]}'
    )


REPORT_SECTION_SCHEMA_HINT = '{"markdown":"","grounded":true}'


def report_section_prompt(section_title: str, section_scope: str, question: str,
                          context_block: str, allow_parametric: bool = True) -> str:
    parametric_rule = (
        "4. You MAY use domain general knowledge beyond the items when the "
        "items do not cover a needed link — but EVERY such sentence must start "
        "with the marker 【通识】, carry NO [k] marker, and numeric values must "
        "be given as typical ranges, not point values.\n"
        if allow_parametric else
        "4. Do NOT introduce facts beyond the knowledge items; where evidence "
        "is missing, state the gap explicitly.\n")
    return (
        "You write ONE section of a deep technical report for an engineer. "
        "Write ONLY this section — no report title, no executive summary, no "
        "other sections' content.\n"
        f"Report question: {question}\n"
        f"Section title: {section_title}\n"
        f"Section scope: {section_scope}\n"
        "Rules:\n"
        "1. When a sentence uses a knowledge item, append its id marker like "
        "[k1] at the end of that sentence. A [k] marker may ONLY be attached "
        "to a sentence whose content comes DIRECTLY from that item.\n"
        "2. When a sentence is your own inference bridging the items, prefix "
        "it with （推断） and attach NO [k].\n"
        "3. Keep the derivation chain complete within this section's scope; "
        "keep formulas dimensionally consistent and prefer circuit-realizable "
        "forms; single-source numeric values: attribute as that source's "
        "stated value, ranges may be added as （推断）.\n"
        f"{parametric_rule}"
        "5. Answer in the question's language. Typeset ALL math as LaTeX "
        "($...$ inline, $$...$$ display); keep [k] markers outside math.\n"
        "6. Start the section body directly with a '## <section title>' "
        "heading, then prose (tables allowed in GitHub markdown).\n"
        "7. grounded=true only if at least one [k] appears in the section.\n\n"
        f"Knowledge items (id: [type][tier] name — context):\n{context_block}\n\n"
        'Return JSON only: {"markdown":"","grounded":true|false}'
    )


def report_summary_prompt(question: str, sections_block: str) -> str:
    return (
        "Write the EXECUTIVE SUMMARY (one tight paragraph, 120-250 words, in "
        "the question's language) of the report below: the direct answer "
        "first, then the load-bearing findings and the key engineering "
        "recommendations. No new facts, no citations markers, no headings.\n\n"
        f"Question: {question}\n\nReport sections:\n{sections_block}\n\n"
        'Return JSON only: {"summary":""}'
    )
```

- [ ] **Step 4: 跑过**;**Step 5: Commit** `feat(prompts): 深度报告三 prompt(大纲/节撰写三层证据/执行摘要)`

---

### Task 5: report_engine.py——大纲 + 逐节并行深挖 + 撰写

**Files:** Create `backend/app/services/report_engine.py`;Test `backend/tests/test_report_engine.py`

- [ ] **Step 1: 失败测试**(stub 一切外部依赖,只测编排逻辑):

```python
def _mk_engine(repo, llm):
    from app.services.report_engine import ReportEngine
    repo.llm_client = llm
    return ReportEngine(repo, repo.settings)


class _OutlineLLM:
    configured = True
    def __init__(self, n_fail_sections=0):
        self.section_calls = []
        self._fail_left = n_fail_sections
    def chat_json(self, messages, schema_hint, **kw):
        content = messages[-1]["content"]
        if "OUTLINE" in content:
            return json.dumps({"sections": [
                {"title": "A", "scope": "sa", "sub_queries": ["qa"]},
                {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]})
        if "ONE section" in content:
            self.section_calls.append(content)
            if self._fail_left > 0:
                self._fail_left -= 1
                raise RuntimeError("boom")
            return json.dumps({"markdown": "## X\nbody [k1]", "grounded": True})
        if "EXECUTIVE SUMMARY" in content:
            return json.dumps({"summary": "总结"})
        return json.dumps({})


def test_engine_outline_fallback_on_bad_json(repo):
    nb = _mk_nb(repo)
    class _Bad:
        configured = True
        def chat_json(self, *a, **k):
            return "not json"
    eng = _mk_engine(repo, _Bad())
    outline = eng._plan_outline(nb.id, "q", "")
    assert len(outline) >= 1 and outline[0]["sub_queries"]      # 回退骨架仍可跑


def test_engine_runs_sections_in_parallel_and_tolerates_one_failure(repo, monkeypatch):
    nb = _mk_nb(repo)
    llm = _OutlineLLM(n_fail_sections=1)
    eng = _mk_engine(repo, llm)
    # stub 每节深挖:不跑真检索,返回空 ReasoningResult(编排测试与检索解耦)
    from app.services.reasoning_retrieval import ReasoningResult
    monkeypatch.setattr(eng, "_deep_dive",
                        lambda nb_id, section, question: ReasoningResult())
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done"
    secs = detail["sections"]
    assert len(secs) == 2
    ok = [s for s in secs if not s.get("failed")]
    bad = [s for s in secs if s.get("failed")]
    assert len(ok) == 1 and len(bad) == 1          # 单节失败不拖垮整报告
    assert detail["content_md"].startswith("#")     # 汇总仍生成


def test_engine_cancel_marks_cancelled(repo, monkeypatch):
    import threading
    nb = _mk_nb(repo)
    llm = _OutlineLLM()
    cancel = threading.Event()
    from app.services.report_engine import ReportEngine
    repo.llm_client = llm
    eng = ReportEngine(repo, repo.settings, cancel_event=cancel)
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question):
        cancel.set()                                # 深挖中途被取消
        from app.services.cancellation import raise_if_cancelled
        raise_if_cancelled(cancel)
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "")
    assert repo.get_report(nb.id, rid)["status"] == "cancelled"
```

- [ ] **Step 2: 跑失败**(ModuleNotFoundError)
- [ ] **Step 3: 实现**——新建 `backend/app/services/report_engine.py`:

```python
"""深度报告引擎:大纲规划 → 每节完整 reasoning 深挖(节间并行) → 逐节撰写
(证据三层 [k]/（推断）/【通识】) → 汇总(执行摘要/参考/知识缺口/分析计划)。

设计对齐 docs/superpowers/specs/2026-07-03-deep-report-mode-design.md。
形态镜像 ReasoningRetriever:持 (repo, settings, cancel_event),写库经 repo。
线程要点:节间 ThreadPoolExecutor 并行,worker 不继承 ContextVar——每个 submit
用 contextvars.copy_context().run 包裹,保住 per-user 模型解析。
"""
from __future__ import annotations
import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from app.core.llm import cap_kwargs
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled

_GAP_PAIR_CAP = 40          # 跨节概念连通性检查的最大 pair 数(成本护栏)
_TOP_CONCEPTS_PER_SECTION = 3


class ReportEngine:
    def __init__(self, repo, settings, cancel_event: CancelEvent = None):
        self.repo = repo
        self.settings = settings
        self.cancel_event = cancel_event

    # --- Stage A ---
    def _plan_outline(self, notebook_id: str, question: str, history: str) -> List[dict]:
        from app.services.prompts import report_outline_prompt, REPORT_OUTLINE_SCHEMA_HINT
        client = self.repo.reasoning_llm_client
        try:
            raw = client.chat_json(
                [{"role": "user", "content": report_outline_prompt(
                    question, max_sections=self.settings.report_max_sections,
                    history_block=history)}],
                REPORT_OUTLINE_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            sections = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    sections.append({"title": title,
                                     "scope": str(s.get("scope", "")).strip(),
                                     "sub_queries": subs[:4]})
            if sections:
                return sections
        except AskCancelled:
            raise
        except Exception:
            pass
        # 回退骨架:expand_query 的子查询平铺为单节(保证总能出报告)。
        from app.services.query_rewrite import expand_query
        ex = expand_query(self.repo.rewrite_llm_client, question, history)
        return [{"title": "分析", "scope": question,
                 "sub_queries": [s.query for s in ex.sub_queries][:4] or [question]}]

    # --- Stage B(单节):完整 reasoning 深挖 ---
    def _deep_dive(self, notebook_id: str, section: dict, question: str):
        from app.services.reasoning_retrieval import ReasoningRetriever
        sec_question = (f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
                        f"[本节检索方向] " + "; ".join(section["sub_queries"]))
        return ReasoningRetriever(self.repo, self.settings, self.cancel_event).run(
            notebook_id, sec_question, top_n=self.settings.report_section_top_n)

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result) -> dict:
        from app.services.prompts import report_section_prompt, REPORT_SECTION_SCHEMA_HINT
        chunk_block, chunk_map = self.repo._chunk_answer_context(
            result.chunks, budget_chars=self.settings.report_section_chunk_budget)
        kg_block, kg_map = self.repo._answer_context(
            notebook_id, result.top_hits, id_offset=len(chunk_map))
        context_block = (f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
                         if chunk_block else kg_block) or "(no evidence retrieved)"
        client = self.repo.reasoning_llm_client
        raw = client.chat_json(
            [{"role": "user", "content": report_section_prompt(
                section["title"], section["scope"], question, context_block,
                allow_parametric=self.settings.report_allow_parametric)}],
            REPORT_SECTION_SCHEMA_HINT, cancel_event=self.cancel_event,
            **cap_kwargs(client, "report_section_max_tokens"))
        data = json.loads(raw)
        id_map = {**chunk_map, **kg_map}
        return {"title": section["title"], "scope": section["scope"],
                "markdown": str(data.get("markdown", "")).strip(),
                "grounded": bool(data.get("grounded", False)),
                "id_map_sources": self._sources_of(id_map),
                "attempted": list(getattr(result, "attempted", []) or []),
                "top_concepts": [
                    {"object_id": h.object_id,
                     "name": str(h.payload.get("name", "")).strip() or h.object_id}
                    for h in result.top_hits[:_TOP_CONCEPTS_PER_SECTION]]}

    @staticmethod
    def _sources_of(id_map: dict) -> List[str]:
        seen, out = set(), []
        for ctx in id_map.values():
            title = str(ctx.get("source_title", "") or ctx.get("name", "")).strip()
            if title and title not in seen:
                seen.add(title)
                out.append(title)
        return out

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id: str, rid: str, outline: List[dict],
                      question: str) -> List[dict]:
        done_count = {"n": 0}

        def _one(section: dict) -> dict:
            raise_if_cancelled(self.cancel_event)
            try:
                result = self._deep_dive(notebook_id, section, question)
                drafted = self._draft_section(notebook_id, section, question, result)
            except AskCancelled:
                raise
            except Exception as exc:
                drafted = {"title": section["title"], "scope": section["scope"],
                           "markdown": "", "grounded": False, "failed": True,
                           "error": str(exc)[:300], "id_map_sources": [],
                           "attempted": [], "top_concepts": []}
            done_count["n"] += 1
            self.repo.update_report(notebook_id, rid,
                                    progress=f"章节深挖 {done_count['n']}/{len(outline)}")
            return drafted

        workers = max(1, int(self.settings.report_section_concurrency))
        with ThreadPoolExecutor(max_workers=min(workers, len(outline))) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _one, s)
                       for s in outline]
            return [f.result() for f in futures]     # 保大纲序

    # --- 入口 ---
    def run(self, notebook_id: str, rid: str, question: str, history: str = "") -> None:
        try:
            self.repo.update_report(notebook_id, rid, status="running", progress="大纲规划中")
            outline = self._plan_outline(notebook_id, question, history)
            self.repo.update_report(notebook_id, rid, outline=outline,
                                    progress=f"大纲就绪({len(outline)} 节),章节深挖 0/{len(outline)}")
            sections = self._run_sections(notebook_id, rid, outline, question)
            self.repo.update_report(notebook_id, rid, sections=sections, progress="汇总中")
            content_md, gaps = self._assemble(notebook_id, rid, question, outline, sections)
            self.repo.update_report(notebook_id, rid, content_md=content_md, gaps=gaps,
                                    status="done", progress="完成")
        except AskCancelled:
            self.repo.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            self.repo.update_report(notebook_id, rid, status="failed",
                                    error=str(exc)[:500], progress="失败")

    # --- Stage D(Task 6 实现;本任务先放最小占位,Task 6 内替换) ---
    def _assemble(self, notebook_id, rid, question, outline, sections):
        body = "\n\n".join(s["markdown"] for s in sections if s.get("markdown"))
        return f"# 深度报告\n\n{body}", []
```

注意:`rewrite_llm_client` 若 repo 无该属性名,grep 实际名称(改写层客户端,memory 记为 rewrite_llm_client)并适配。

- [ ] **Step 4: 跑过** `$PY -m pytest backend/tests/test_report_engine.py -q`
- [ ] **Step 5: Commit** `feat(report): ReportEngine 大纲+逐节并行完整深挖+三层证据撰写`

---

### Task 6: Stage D 汇总——执行摘要 + 参考 + 知识缺口 + 分析计划

**Files:** Modify `backend/app/services/report_engine.py`;Test `backend/tests/test_report_engine.py`

- [ ] **Step 1: 失败测试**:

```python
def test_assemble_builds_full_report_with_gaps(repo, monkeypatch):
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]},
               {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]
    sections = [
        {"title": "A", "scope": "sa", "markdown": "## A\nbody [k1]", "grounded": True,
         "id_map_sources": ["Razavi"], "attempted": [
             {"query": "qa", "new": 3, "tries": 1},
             {"query": "qa-dry", "new": 0, "tries": 2}],
         "top_concepts": [{"object_id": "ko-1", "name": "Bandgap"}]},
        {"title": "B", "scope": "sb", "markdown": "## B\n【通识】x", "grounded": False,
         "id_map_sources": [], "attempted": [],
         "top_concepts": [{"object_id": "ko-2", "name": "Packaging Stress"}]},
    ]
    monkeypatch.setattr(eng.repo, "_retrieve_neighbors", lambda *a, **k: [])  # 两概念无边
    rid = repo.create_report(nb.id, "q")
    md, gaps = eng._assemble(nb.id, rid, "q", outline, sections)
    assert "## 执行摘要" in md and "总结" in md
    assert "## A" in md and "## B" in md
    assert "## 知识缺口" in md and "qa-dry" in md            # 零命中子查询
    assert "Bandgap" in md and "Packaging Stress" in md      # 无边概念对
    assert "## 参考文献" in md and "Razavi" in md
    assert "## 分析计划" in md and "qa" in md
    assert any("qa-dry" in g for g in gaps)
```

- [ ] **Step 2: 跑失败**;**Step 3: 实现**——替换 Task 5 的 `_assemble` 占位:

```python
    def _assemble(self, notebook_id, rid, question, outline, sections):
        from app.services.prompts import report_summary_prompt
        # 执行摘要(容错:失败则空段,不拖垮报告)。
        summary = ""
        try:
            sections_block = "\n\n".join(
                s["markdown"][:2000] for s in sections if s.get("markdown"))
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_summary_prompt(question, sections_block)}],
                '{"summary":""}', cancel_event=self.cancel_event)
            summary = str(json.loads(raw).get("summary", "")).strip()
        except AskCancelled:
            raise
        except Exception:
            pass

        gaps: List[str] = []
        # 缺口一:零命中/干涸子查询(each 节 attempted 里 new==0)。
        for s in sections:
            for a in s.get("attempted", []):
                if a.get("new") == 0:
                    gaps.append(f"「{s['title']}」节:子查询 “{a['query']}” 在库内未检得新证据")
        # 缺口二:跨节高相关概念对在 KG 中无边(结构性缺口)。
        pairs_checked = 0
        concepts = [(s["title"], c) for s in sections for c in s.get("top_concepts", [])]
        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                if concepts[i][0] == concepts[j][0]:
                    continue                     # 只查跨节
                if pairs_checked >= _GAP_PAIR_CAP:
                    break
                pairs_checked += 1
                a, b = concepts[i][1], concepts[j][1]
                try:
                    neigh = self.repo._retrieve_neighbors(notebook_id, a["object_id"],
                                                          None, "both")
                except Exception:
                    continue
                if not any(h.object_id == b["object_id"] for h in neigh):
                    gaps.append(f"图谱缺口:「{a['name']}」与「{b['name']}」尚无关联边")
        # 缺口三:整节无 [k] 支撑。
        for s in sections:
            if s.get("markdown") and not s.get("grounded"):
                gaps.append(f"「{s['title']}」节无库内引用支撑(全部为推断/通识,建议补充语料)")
        gaps = list(dict.fromkeys(gaps))[:30]

        refs = list(dict.fromkeys(t for s in sections for t in s.get("id_map_sources", [])))
        plan_lines = [
            f"- {s['title']}: " + "; ".join(o.get("sub_queries", []))
            for s, o in zip(sections, outline)]
        parts = [f"# 深度报告:{question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        for s in sections:
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif s.get("markdown"):
                parts += [s["markdown"], ""]
        if gaps:
            parts += ["## 知识缺口", ""] + [f"- {g}" for g in gaps] + [""]
        if refs:
            parts += ["## 参考文献(库内来源;[k] 编号为节内编号)", ""] + \
                     [f"- {r}" for r in refs] + [""]
        parts += ["## 分析计划", ""] + plan_lines
        return "\n".join(parts), gaps
```

- [ ] **Step 4: 跑过**(连同 Task 5 全部用例);**Step 5: Commit** `feat(report): 汇总段——执行摘要+参考+知识缺口(零命中/无边概念对/无支撑节)+分析计划`

---

### Task 7: API 端点 + 后台线程 + 取消注册表

**Files:** Modify `backend/app/api/routes.py`、`backend/app/models/schemas.py`、`backend/app/services/report_engine.py`(取消注册表);Test `backend/tests/test_report_api.py`(新建)

- [ ] **Step 1: 失败测试**(新建,仿现有 API 测试的 TestClient fixture 风格——grep `TestClient` 找现成 conftest/夹具,复用其 app+登录方式;若现有 API 测试以 `client` fixture 提供已认证客户端,直接用):

```python
def test_report_endpoints_lifecycle(client, monkeypatch):
    # 建 notebook
    nb = client.post("/api/notebooks", json={"name": "t", "purpose": "p",
                                             "primary_domain": "d"}).json()
    # 起报告:LLM 未配置 → 409
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "q"})
    assert r.status_code == 409
    # stub 引擎线程:不真跑(单测不起真深挖)
    import app.api.routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_report_job", lambda *a, **k: None)
    monkeypatch.setattr(
        routes_mod, "_report_llm_ready", lambda repo: True)
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"})
    assert r.status_code == 200
    rid = r.json()["report_id"]
    lst = client.get(f"/api/notebooks/{nb['id']}/reports").json()
    assert lst[0]["id"] == rid and lst[0]["status"] == "pending"
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["question"] == "为什么?" and "content_md" in detail
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel").status_code == 200
    assert client.delete(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 404
```

(若仓库现有 API 测试的路径前缀非 `/api`,以现状为准适配。)

- [ ] **Step 2: 跑失败**;**Step 3: 实现**:
  - **schemas.py**(NotebookSummary 附近风格):

```python
class ReportCreate(BaseModel):
    question: str
    history: str = ""


class ReportSummary(BaseModel):
    id: str
    question: str
    status: str
    progress: str = ""
    section_count: int = 0
    created_at: str = ""
    created_by: str = ""


class ReportDetail(ReportSummary):
    outline: List[dict] = Field(default_factory=list)
    sections: List[dict] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    content_md: str = ""
    error: str = ""
```

  - **report_engine.py** 追加模块级取消注册表:

```python
import threading

_ACTIVE_CANCELS: Dict[str, threading.Event] = {}
_CANCELS_LOCK = threading.Lock()


def register_cancel(report_id: str) -> threading.Event:
    ev = threading.Event()
    with _CANCELS_LOCK:
        _ACTIVE_CANCELS[report_id] = ev
    return ev


def cancel_report(report_id: str) -> bool:
    with _CANCELS_LOCK:
        ev = _ACTIVE_CANCELS.get(report_id)
    if ev is not None:
        ev.set()
        return True
    return False


def unregister_cancel(report_id: str) -> None:
    with _CANCELS_LOCK:
        _ACTIVE_CANCELS.pop(report_id, None)
```

  - **routes.py**(kg/build 端点附近;import ReportCreate/ReportSummary/ReportDetail 与 report_engine):

```python
def _report_llm_ready(repo) -> bool:
    return bool(getattr(repo.reasoning_llm_client, "configured", False))


def _launch_report_job(repo, notebook_id: str, rid: str, question: str, history: str) -> None:
    from app.services.report_engine import ReportEngine, register_cancel, unregister_cancel
    cancel = register_cancel(rid)
    ctx = contextvars.copy_context()          # per-user 模型经 ContextVar 传播

    def worker():
        try:
            ReportEngine(repo, repo.settings, cancel_event=cancel).run(
                notebook_id, rid, question, history)
        finally:
            unregister_cancel(rid)

    threading.Thread(target=lambda: ctx.run(worker),
                     name=f"report-{rid}", daemon=True).start()


@router.post("/notebooks/{notebook_id}/reports",
             dependencies=[Depends(require_notebook_write)])
def create_report(notebook_id: str, payload: ReportCreate) -> dict:
    repo = repository()
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="question required")
    if not _report_llm_ready(repo):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        rid = repo.create_report(notebook_id, payload.question.strip())
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    _launch_report_job(repo, notebook_id, rid, payload.question.strip(), payload.history)
    return {"report_id": rid, "status": "pending"}


@router.get("/notebooks/{notebook_id}/reports",
            dependencies=[Depends(require_notebook_read)])
def list_reports(notebook_id: str) -> List[ReportSummary]:
    return [ReportSummary(**r) for r in repository().list_reports(notebook_id)]


@router.get("/notebooks/{notebook_id}/reports/{report_id}",
            dependencies=[Depends(require_notebook_read)])
def get_report(notebook_id: str, report_id: str) -> ReportDetail:
    try:
        return ReportDetail(**repository().get_report(notebook_id, report_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")


@router.post("/notebooks/{notebook_id}/reports/{report_id}/cancel",
             dependencies=[Depends(require_notebook_write)])
def cancel_report_endpoint(notebook_id: str, report_id: str) -> dict:
    from app.services.report_engine import cancel_report as _cancel
    live = _cancel(report_id)
    if not live:                               # 线程已结束/不存在:direct 标记
        try:
            repository().update_report(notebook_id, report_id, status="cancelled",
                                       progress="已取消")
        except Exception:
            raise HTTPException(status_code=404, detail="Report not found")
    return {"status": "cancelling" if live else "cancelled"}


@router.delete("/notebooks/{notebook_id}/reports/{report_id}",
               dependencies=[Depends(require_notebook_write)])
def delete_report(notebook_id: str, report_id: str) -> dict:
    repository().delete_report(notebook_id, report_id)
    return {"status": "deleted"}
```

  注意:`ReportSummary(**r)` 的 r 含多余键(notebook_id 等)会报错——`_report_row_to_dict` 已知字段与模型对齐,多余键用 `ReportSummary(**{k: v for k, v in r.items() if k in ReportSummary.model_fields})` 过滤,或让模型 `model_config = ConfigDict(extra="ignore")`,取仓库现有风格。
- [ ] **Step 4: 跑过 + 相关回归** `$PY -m pytest backend/tests/test_report_api.py backend/tests/test_report_engine.py -q`
- [ ] **Step 5: Commit** `feat(api): 深度报告端点(创建/列表/详情/取消/删除)+后台 job(copy_context)+取消注册表`

---

### Task 8: 前端——「深度报告」tab + 生成 + 轮询 + 查看 + 导出

**Files:** Modify `frontend/app/page.tsx`;Create `frontend/app/report-view.tsx`;不碰 ask-modes.ts(报告非 mode,契约脚本无涉)

- [ ] **Step 1: 类型 + api 函数**(page.tsx,Notebook 类型定义区附近加类型;api helper 之后加函数):

```typescript
type ReportSummaryT = {
  id: string; question: string; status: string; progress: string;
  section_count: number; created_at: string; created_by: string;
};
type ReportDetailT = ReportSummaryT & {
  outline: { title: string; scope: string; sub_queries: string[] }[];
  sections: { title: string; markdown: string; grounded: boolean; failed?: boolean }[];
  gaps: string[]; content_md: string; error: string;
};

const createReport = (nb: string, question: string) =>
  api<{ report_id: string }>(`/notebooks/${nb}/reports`, {
    method: "POST", body: JSON.stringify({ question }) });
const listReports = (nb: string) => api<ReportSummaryT[]>(`/notebooks/${nb}/reports`);
const getReport = (nb: string, rid: string) =>
  api<ReportDetailT>(`/notebooks/${nb}/reports/${rid}`);
const cancelReport = (nb: string, rid: string) =>
  api<{ status: string }>(`/notebooks/${nb}/reports/${rid}/cancel`, { method: "POST" });
const deleteReport = (nb: string, rid: string) =>
  api<{ status: string }>(`/notebooks/${nb}/reports/${rid}`, { method: "DELETE" });
```

- [ ] **Step 2: tab 接线**——CHAT_MODES(:211-214)加 `["reports", "深度报告"]`;chat-body(:3327 起)加 `chatMode === "reports"` 分支渲染 `<ReportsPanel …/>`(内联组件或抽到 report-view.tsx,取文件现状轻的做法;**推荐抽文件**,page.tsx 已 5343 行)。
- [ ] **Step 3: ReportsPanel(report-view.tsx)**——props 传 `notebookId`、api 函数、toast;内部状态:list、activeReport(detail | null)、question 输入、生成中;行为:
  - 列表:进 tab 即 `listReports`;卡片区显示 question 截断 + 状态徽章(pending/running 亮色+progress 文字,done/failed/cancelled 沉色);点击卡片 → `getReport` 进详情视图(返回按钮回列表)。
  - 生成:textarea + 「生成深度报告」按钮 → `createReport` → toast(「已开始生成(后台,约 5-15 分钟)」)→ 刷新列表。
  - 轮询:存在非终态报告时每 6s `listReports`(镜像 kg 轮询 :996-1016 的写法:setInterval + 终态即停 + 卸载清理);详情视图打开且非终态时轮询 `getReport`。
  - 详情:标题 + 状态/progress 行 +(running 时)「取消」按钮 + content_md 渲染 + 「下载 .md」:

```typescript
const downloadMd = (r: ReportDetailT) => {
  const blob = new Blob([r.content_md], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `report-${r.id}.md`; a.click();
  URL.revokeObjectURL(url);
};
```

  - 渲染:新建轻组件 `ReportMarkdown`(在 report-view.tsx 内),复用 answer-markdown.tsx 同款栈但不带 citations 插件:

```tsx
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

function ReportMarkdown({ markdown }: { markdown: string }) {
  return (
    <div className="report-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
```

  - 样式:沿用现有 class 体系(chat-session-card 等),新样式加在全局 css 现有段落旁;对齐/省略号/加载态按 ui-polish-bar。
- [ ] **Step 4: 校验** `cd frontend && npm install && npx tsc --noEmit`(clean)+ `npm run lint`
- [ ] **Step 5: Commit** `feat(fe): 深度报告 tab——生成/列表/进度轮询/查看(ReportMarkdown)/取消/下载md`

---

### Task 9: 全量验证 + PR

- [ ] **Step 1:** `$PY -m pytest backend/tests -q` 全绿
- [ ] **Step 2:** `bash scripts/check.sh` EXIT=0(含前端 test/lint;smoke 不涉报告端点,若受 schemas 变更影响按语义修断言)
- [ ] **Step 3:**(控制器)README.md + README_zh.md 补「深度报告」小节(通用口径:入口/耗时预期/【通识】含义与开关/导出);spec+plan 已在分支;rebase origin/master → push → `gh pr create --base master`
