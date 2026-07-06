# 深度报告大纲 STORM+两阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把深度报告大纲从"单次盲规划"升级为「Corpus map 接地 → STORM 多视角预写作 → 充分性探针+Judge → 两阶段(用户确认大纲后再生成全文)」。

**Architecture:** Stage A(阶段1,秒级)= `_build_corpus_map`(0 LLM)+ STORM 规划(1 LLM)+ 充分性探针(0 LLM)+ Judge(1 LLM,走 flash)→ 富大纲存 `reports.outline_json` → `status=outline_ready`。用户 `PATCH /outline` 编辑后 `POST /generate` 跑现有 Stage B/C/D。前后端同 PR。

**Tech Stack:** Python/FastAPI/SQLite/pytest;Next.js/React/TS。

**Spec:** `docs/superpowers/specs/2026-07-06-report-outline-storm-twophase-design.md`(已批;Judge 走 flash、张力 v1 文字标签、不内置商业框架、状态机 planning→outline_ready→generating→done)。

## Global Constraints
- **效率一等**:Stage A 仅 **2 次 LLM**(STORM 规划走 `reasoning_llm_client`;Judge 走 `rewrite_llm_client`=flash);其余零 LLM 走现成检索原语。不新增朴素多智能体调用。
- **不新增 env**(能复用就复用);必须新增用 `validation_alias`(pydantic-settings v2,`Field(env=)` 失效)。
- **前后端同一 PR** co-design。
- 前端**勿动 page.tsx 中文弯引号**;校验 `git diff | grep -c '^-.*[""]'`=0(用真弯引号)。
- 测试解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`(记为 `$PY`)。commit 中文 conventional,末尾单独一行 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。不 push/不启停服务/不用 dangerouslyDisableSandbox。

**关键接线事实**(已核,直接用):
- `ReportEngine.run(notebook_id, rid, question, history="", depth=2)`(report_engine.py:190):`update(running)`→`_plan_outline`→`update(outline=)`→`_run_sections`→`update(sections=)`→`_assemble`→`update(content_md/gaps/references/status=done)`。
- `_plan_outline(notebook_id, question, history)`(:~55)= 1 次盲规划,回退 expand_query 单节。
- `repo.update_report(notebook_id, rid, *, status=, progress=, error=, outline=, sections=, gaps=, content_md=, references=, section_status=)`(kwarg 灵活;`outline`/`sections`/…→JSON 列)。
- `repo.create_report(notebook_id, question, depth=2)` 起始 `status='pending'`。`repo.get_report/list_reports/delete_report/export_reports` 已在。
- `reports` 列已含 `outline_json/sections_json/gaps_json/references_json/depth/section_status_json/content_md/status/progress/error/created_by`。**本特性无需加列**(status 是字符串,新值直接可用;富字段塞进 outline_json 的 section dict)。
- API:`routes.py` `_launch_report_job(repo,nb,rid,q,history,depth)` 用 `background_jobs.submit(worker, name=)`(已 copy_context+兜底);`create_report`/`list_reports`/`get_report`/`cancel`/`delete`/`export` 端点在;守卫 `require_notebook_write`/`require_notebook_read`;`_report_llm_ready(repo)`。
- schemas:`ReportCreate{question, depth=2, history=""}`、`ReportDetail(ReportSummary){outline,sections,gaps,references,content_md,error,depth,section_status}`。
- `repo.federated_retrieve(active_id, q)`→List[RetrievedKnowledge(.object_id/.payload/.tier/.notebook_id/.relevance)];`repo._ppr_retrieve(nb, q)`→List[RetrievedChunk(.chunk_id/.source_id/.source_title/.section_path/.text)];`repo.reasoning_llm_client`/`repo.rewrite_llm_client`(.chat_json(messages, schema_hint, cancel_event=, timeout=, max_retries=), .configured)。
- 前端 report-view.tsx:`ReportsPanel`;`createReport(nb,q,depth)`(page.tsx 定义、prop 传入);`ReportStatusBadge`(:209);`isReportActive(status)= pending|running`(:60);详情视图 :510+;生成区 :573+;`DEPTHS=[1,2,4,8,16]`。

**验证命令**:
```bash
$PY -m pytest backend/tests/test_report_engine.py backend/tests/test_report_api.py -q
$PY -m pytest backend/tests -q
cd frontend && npx tsc --noEmit && npm run test
bash scripts/check.sh
```

---

### Task 1: Corpus map 构建(0 LLM 语料侦察)

**Files:** Modify `backend/app/services/report_engine.py`;Test `backend/tests/test_report_engine.py`

**Interfaces:**
- Produces: `ReportEngine._build_corpus_map(self, notebook_id: str, question: str) -> str` — 返回紧凑 map 字符串(来源标题 + federated KG top-N + PPR chunk 来源·路径,标 [base]/[personal]);容错:任一子步异常返回该段空。

- [ ] **Step 1 失败测试**(test_report_engine.py 追加;复用现有 `repo`/`_mk_engine`/`_mk_nb`,stub 检索):

```python
def test_build_corpus_map_grounds_on_corpus(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.retrieval import RetrievedKnowledge, RetrievedChunk
    nb = _mk_nb(repo)
    eng = ReportEngine(repo, repo.settings)
    # 来源
    with repo._write() as db:
        db.execute("INSERT INTO sources(id,notebook_id,title,status,parse_status,created_at,updated_at)"
                   " VALUES('s1',?, 'Razavi Analog CMOS','uploaded','parsed',?,?)",
                   (nb.id, "2026", "2026"))
    def _fed(active, q):
        h = RetrievedKnowledge(object_id="ko1", object_type="concept", payload={"name": "Bandgap Reference"})
        h.tier = "base"; h.notebook_id = "nb-base"
        return [h]
    def _ppr(nbid, q):
        return [RetrievedChunk(chunk_id="c1", source_id="s2", source_title="Gray & Meyer",
                               section_path="§11.2", text="……很长的正文不该进 map……")]
    monkeypatch.setattr(repo, "federated_retrieve", _fed)
    monkeypatch.setattr(repo, "_ppr_retrieve", _ppr)
    m = eng._build_corpus_map(nb.id, "why is bandgap 1.2V")
    assert "Razavi Analog CMOS" in m            # 来源标题
    assert "Bandgap Reference" in m and "[base]" in m   # KG + tier
    assert "Gray & Meyer" in m and "§11.2" in m         # chunk 来源·路径
    assert "不该进 map" not in m                # 不含 chunk 正文
    assert len(m) <= 4000
```

- [ ] **Step 2 跑失败**：`$PY -m pytest backend/tests/test_report_engine.py -q -k corpus_map` → AttributeError。
- [ ] **Step 3 实现**（report_engine.py，ReportEngine 内新增；顶部已 import json）:

```python
    _SCOUT_KG_N = 12
    _SCOUT_CHUNK_N = 8

    def _build_corpus_map(self, notebook_id: str, question: str) -> str:
        """0-LLM 语料侦察:来源标题 + federated KG 命中 + PPR chunk 来源·路径。
        给 STORM 规划接地(治盲规划)。任一子步失败静默降级为空段。"""
        parts: List[str] = []
        try:
            with self.repo._connect() as db:
                rows = db.execute(
                    "SELECT title FROM sources WHERE notebook_id=? ORDER BY created_at LIMIT 20",
                    (notebook_id,)).fetchall()
            titles = [str(r["title"]).strip() for r in rows if str(r["title"]).strip()]
            if titles:
                parts.append("本 notebook 来源文件:\n" + "\n".join(f"- {t}" for t in titles))
        except Exception:
            pass
        try:
            kg = self.repo.federated_retrieve(notebook_id, question)[: self._SCOUT_KG_N]
            if kg:
                parts.append("检索到的知识条目(name[type][tier]):\n" + "\n".join(
                    f"- {str(h.payload.get('name','')).strip()}"
                    f"[{h.object_type}][{getattr(h,'tier','personal')}]" for h in kg))
        except Exception:
            pass
        try:
            chunks = self.repo._ppr_retrieve(notebook_id, question)[: self._SCOUT_CHUNK_N]
            if chunks:
                parts.append("相关原文所在(来源·章节,不含正文):\n" + "\n".join(
                    f"- {c.source_title} · {c.section_path}" for c in chunks))
        except Exception:
            pass
        return ("\n\n".join(parts))[:4000] if parts else "(语料侦察无结果)"
```

- [ ] **Step 4 跑过**；**Step 5 Commit** `feat(report): Corpus map 0-LLM 语料侦察(来源+KG+chunk路径)`

---

### Task 2: STORM 大纲 prompt(多视角预写作)

**Files:** Modify `backend/app/services/prompts.py`;Test `backend/tests/test_report_engine.py`

**Interfaces:**
- Produces: `prompts.report_storm_outline_prompt(question, corpus_map, max_sections=6, history_block="") -> str`;`prompts.REPORT_STORM_SCHEMA_HINT: str`。

- [ ] **Step 1 失败测试**:

```python
def test_storm_outline_prompt_contract():
    from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
    p = report_storm_outline_prompt("Q问题", "CORPUSMAP内容", max_sections=5, history_block="H历史")
    for kw in ("expert perspectives", "raise", "cluster", "tension", "MECE",
               "vocabulary", "CORPUSMAP内容", "Q问题", "H历史", "3-5"):
        assert kw in p
    assert "perspectives" in REPORT_STORM_SCHEMA_HINT and "tensions" in REPORT_STORM_SCHEMA_HINT
```

- [ ] **Step 2 跑失败**；**Step 3 实现**(prompts.py 末尾):

```python
REPORT_STORM_SCHEMA_HINT = (
    '{"sections":[{"title":"","scope":"","sub_queries":[""],'
    '"perspectives":[""],"tensions":[""]}]}')


def report_storm_outline_prompt(question: str, corpus_map: str,
                                max_sections: int = 6, history_block: str = "") -> str:
    history_section = (f"Prior conversation:\n{history_block}\n\n" if history_block else "")
    return (
        "You plan the OUTLINE of a deep, insightful technical report — NOT a shallow "
        "summary. Derive it by PRE-WRITING, not by writing it directly:\n"
        "1. Adopt 3-4 expert PERSPECTIVES: first dynamically generate 2-3 lenses "
        "tailored to THIS question & corpus; then add 1-2 from the general set "
        "(domain expert / hands-on practitioner / risk-skeptic). Perspectives must "
        "serve answering the user's question — do not add lenses for mere variety.\n"
        "2. From each perspective, RAISE 2-3 deep questions about the user's question "
        "(e.g. the skeptic asks about failure modes / risks / missing evidence).\n"
        "3. Dedup and CLUSTER these questions by theme into report sections.\n"
        "4. PRESERVE the TENSION: where perspectives disagree, keep the conflict "
        "explicit as an insight — never flatten into one-sided praise/summary.\n"
        "5. Sections must be MECE (mutually exclusive, no overlap; collectively cover "
        "the question).\n"
        "6. Ground in the corpus map below: sub_queries MUST reuse the actual "
        "vocabulary / entity names that appear in the map (verbatim spelling).\n"
        "7. Keep a section the question explicitly asks for EVEN IF the map lacks it "
        "(the map is a sample; the writing stage can bridge gaps as 【通识】).\n"
        f"Produce 3-{max_sections} sections. Do NOT include executive-summary / "
        "references / knowledge-gap sections (auto-appended). Each section: title "
        "(question's language), scope (one line), sub_queries (2-4 focused ENGLISH "
        "retrieval queries), perspectives (which lenses it came from), tensions "
        "(one line each; which other section/lens it conflicts with, or []).\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        f"Corpus map (what the library actually contains):\n{corpus_map}\n\n"
        'Return JSON only: {"sections":[{"title":"","scope":"","sub_queries":[""],'
        '"perspectives":[""],"tensions":[""]}]}'
    )
```

- [ ] **Step 4 跑过**；**Step 5 Commit** `feat(prompts): STORM 多视角预写作大纲 prompt(接地+张力+MECE)`

---

### Task 3: 充分性探针 + Judge prompt

**Files:** Modify `backend/app/services/report_engine.py`、`backend/app/services/prompts.py`;Test `backend/tests/test_report_engine.py`

**Interfaces:**
- Produces: `ReportEngine._probe_sufficiency(self, notebook_id, sections) -> List[dict]`(每节 `{"title","hits","base_hits"}`;hits=该节各 sub_query federated 命中并集数);`prompts.report_sufficiency_prompt(question, probe_block) -> str`;`prompts.REPORT_SUFFICIENCY_SCHEMA_HINT`。

- [ ] **Step 1 失败测试**:

```python
def test_probe_sufficiency_counts_hits(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.retrieval import RetrievedKnowledge
    eng = ReportEngine(repo, repo.settings)
    def _fed(active, q):
        h = RetrievedKnowledge(object_id="k-"+q, object_type="concept", payload={})
        h.notebook_id = "nb-base" if "base" in q else "nb-x"; h.tier="base" if "base" in q else "personal"
        return [h]
    monkeypatch.setattr(repo, "federated_retrieve", _fed)
    out = eng._probe_sufficiency("nb", [{"title":"A","sub_queries":["base-x","y"]},
                                        {"title":"B","sub_queries":[]}])
    assert out[0]["title"]=="A" and out[0]["hits"]==2 and out[0]["base_hits"]==1
    assert out[1]["hits"]==0

def test_sufficiency_prompt_contract():
    from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
    p = report_sufficiency_prompt("Q", "PROBEBLOCK")
    assert "sufficiency" in p and "PROBEBLOCK" in p and "Q" in p
    assert "gap_note" in REPORT_SUFFICIENCY_SCHEMA_HINT and "action" in REPORT_SUFFICIENCY_SCHEMA_HINT
```

- [ ] **Step 2 跑失败**；**Step 3 实现**:

report_engine.py（ReportEngine 内）:
```python
    def _probe_sufficiency(self, notebook_id: str, sections: List[dict]) -> List[dict]:
        """0-LLM 客观信号:每节各 sub_query 跑 federated_retrieve,统计命中并集(base 拆分)。"""
        out = []
        for s in sections:
            seen, base = set(), set()
            for q in (s.get("sub_queries") or []):
                try:
                    for h in self.repo.federated_retrieve(notebook_id, str(q)):
                        seen.add(h.object_id)
                        if getattr(h, "tier", "") == "base":
                            base.add(h.object_id)
                except Exception:
                    continue
            out.append({"title": s.get("title", ""), "hits": len(seen), "base_hits": len(base)})
        return out
```

prompts.py:
```python
REPORT_SUFFICIENCY_SCHEMA_HINT = (
    '{"verdicts":[{"title":"","sufficiency":"充足|薄弱|缺失",'
    '"gap_note":"","action":"keep|supplement|external"}]}')


def report_sufficiency_prompt(question: str, probe_block: str) -> str:
    return (
        "You judge whether the notebook library has ENOUGH evidence for each planned "
        "report section. You are given each section's title and its OBJECTIVE retrieval "
        "hit counts (hits = distinct knowledge items its sub-queries matched; base_hits "
        "= from the authoritative base library). Trust the counts as the ground truth "
        "of coverage; your job is to interpret them into a verdict + a one-line gap note "
        "+ a suggested action. Rough guide: many hits → 充足(keep); few/only-tangential "
        "→ 薄弱(supplement, note what's missing); ~0 hits → 缺失(external, the library "
        "cannot support it). Do not invent coverage the counts don't show.\n\n"
        f"Report question: {question}\n\n"
        f"Sections with hit counts:\n{probe_block}\n\n"
        'Return JSON only: {"verdicts":[{"title":"","sufficiency":"","gap_note":"","action":""}]}'
    )
```

- [ ] **Step 4 跑过**；**Step 5 Commit** `feat(report): 充分性探针(0-LLM 命中数)+ Judge prompt`

---

### Task 4: plan_outline 编排(map→STORM→探针→Judge→富大纲→outline_ready)

**Files:** Modify `backend/app/services/report_engine.py`;Test `backend/tests/test_report_engine.py`

**Interfaces:**
- Produces: `ReportEngine.plan_outline(self, notebook_id, rid, question, history="") -> None`（跑 Stage A,把富大纲存 outline_json + `status='outline_ready'`;STORM 坏 JSON → 回退现行 `_plan_outline` 骨架、仍 outline_ready;取消/异常 → cancelled/failed）。富 section dict = `{title,scope,sub_queries,perspectives,tensions,sufficiency,gap_note,action}`。

- [ ] **Step 1 失败测试**:

```python
def test_plan_outline_produces_enriched_outline_ready(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    class _LLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            c = messages[-1]["content"]
            if "PRE-WRITING" in c or "expert PERSPECTIVES" in c:
                return json.dumps({"sections":[{"title":"机理","scope":"s","sub_queries":["bandgap"],
                                                "perspectives":["领域专家"],"tensions":[]}]})
            if "ENOUGH evidence" in c:
                return json.dumps({"verdicts":[{"title":"机理","sufficiency":"薄弱",
                                                "gap_note":"缺实测","action":"supplement"}]})
            return "{}"
    repo.llm_client = _LLM()          # reasoning/rewrite 都回退到它(测试桩)
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self,n,q: "MAP")
    monkeypatch.setattr(repo, "federated_retrieve", lambda a,q: [])
    eng = ReportEngine(repo, repo.settings)
    rid = repo.create_report(nb.id, "why bandgap 1.2V")
    eng.plan_outline(nb.id, rid, "why bandgap 1.2V")
    d = repo.get_report(nb.id, rid)
    assert d["status"] == "outline_ready"
    sec = d["outline"][0]
    assert sec["title"]=="机理" and sec["perspectives"]==["领域专家"]
    assert sec["sufficiency"]=="薄弱" and sec["action"]=="supplement"

def test_plan_outline_falls_back_on_bad_storm_json(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    class _Bad:
        configured=True
        def chat_json(self, *a, **k): return "not json"
    repo.llm_client=_Bad()
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self,n,q:"MAP")
    monkeypatch.setattr(repo, "federated_retrieve", lambda a,q: [])
    eng=ReportEngine(repo, repo.settings)
    rid=repo.create_report(nb.id,"q")
    eng.plan_outline(nb.id, rid, "q")
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="outline_ready" and len(d["outline"])>=1   # 回退骨架
```

- [ ] **Step 2 跑失败**；**Step 3 实现**(report_engine.py，ReportEngine 内新增；`AskCancelled`/`raise_if_cancelled`/`json` 已 import):

```python
    def plan_outline(self, notebook_id, rid, question, history="") -> None:
        try:
            self.repo.update_report(notebook_id, rid, status="planning", progress="侦察语料中")
            corpus_map = self._build_corpus_map(notebook_id, question)
            raise_if_cancelled(self.cancel_event)
            self.repo.update_report(notebook_id, rid, progress="多视角规划大纲中")
            sections = self._storm_outline(notebook_id, question, history, corpus_map)
            # 充分性:探针(0 LLM)+ Judge(flash)
            probe = self._probe_sufficiency(notebook_id, sections)
            sections = self._judge_sufficiency(question, sections, probe)
            self.repo.update_report(notebook_id, rid, outline=sections,
                                    status="outline_ready",
                                    progress=f"大纲就绪({len(sections)} 节),待确认")
        except AskCancelled:
            self.repo.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            self.repo.update_report(notebook_id, rid, status="failed",
                                    error=str(exc)[:500], progress="规划失败")

    def _storm_outline(self, notebook_id, question, history, corpus_map) -> List[dict]:
        from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
        try:
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": report_storm_outline_prompt(
                    question, corpus_map, max_sections=self.settings.report_max_sections,
                    history_block=history)}],
                REPORT_STORM_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            out = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    out.append({
                        "title": title, "scope": str(s.get("scope", "")).strip(),
                        "sub_queries": subs[:4],
                        "perspectives": [str(p).strip() for p in (s.get("perspectives") or []) if str(p).strip()],
                        "tensions": [str(t).strip() for t in (s.get("tensions") or []) if str(t).strip()]})
            if out:
                return out
        except AskCancelled:
            raise
        except Exception:
            pass
        return self._plan_outline(notebook_id, question, history)   # 回退现行骨架

    def _judge_sufficiency(self, question, sections, probe) -> List[dict]:
        from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
        by_title = {p["title"]: p for p in probe}
        # 缺省:按探针命中给保守判定(Judge 失败也有充分性信号)
        for s in sections:
            h = by_title.get(s["title"], {"hits": 0, "base_hits": 0})
            s.setdefault("sufficiency", "充足" if h["hits"] >= 3 else "薄弱" if h["hits"] else "缺失")
            s.setdefault("gap_note", "")
            s.setdefault("action", "keep" if h["hits"] >= 3 else "supplement" if h["hits"] else "external")
        try:
            block = "\n".join(f"- {p['title']}: hits={p['hits']} base_hits={p['base_hits']}" for p in probe)
            raw = self.repo.rewrite_llm_client.chat_json(
                [{"role": "user", "content": report_sufficiency_prompt(question, block)}],
                REPORT_SUFFICIENCY_SCHEMA_HINT, cancel_event=self.cancel_event)
            for v in (json.loads(raw).get("verdicts") or []):
                for s in sections:
                    if s["title"] == str(v.get("title", "")).strip():
                        if v.get("sufficiency"): s["sufficiency"] = str(v["sufficiency"])
                        if v.get("gap_note") is not None: s["gap_note"] = str(v.get("gap_note", ""))
                        if v.get("action"): s["action"] = str(v["action"])
        except AskCancelled:
            raise
        except Exception:
            pass
        return sections
```

- [ ] **Step 4 跑过**；**Step 5 Commit** `feat(report): plan_outline 编排(map+STORM+探针+Judge→富大纲 outline_ready)`

---

### Task 5: 引擎拆 generate 阶段 + run 兼容

**Files:** Modify `backend/app/services/report_engine.py`;Test `backend/tests/test_report_engine.py`

**Interfaces:**
- Produces: `ReportEngine.generate(self, notebook_id, rid, question, depth=2) -> None`(读 outline_json → `_run_sections`→`_assemble`→done;取消/异常兜底)。`run()` 改为 `plan_outline` +(可选)`generate` 以保留一键直出。

- [ ] **Step 1 失败测试**:

```python
def test_generate_runs_sections_on_stored_outline(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.reasoning_retrieval import ReasoningResult
    nb=_mk_nb(repo)
    rid=repo.create_report(nb.id,"q")
    repo.update_report(nb.id, rid, outline=[{"title":"A","scope":"s","sub_queries":["q"]}],
                       status="outline_ready")
    eng=ReportEngine(repo, repo.settings)
    monkeypatch.setattr(eng, "_deep_dive", lambda *a,**k: ReasoningResult())
    class _S:
        configured=True
        def chat_json(self,*a,**k): return json.dumps({"summary":"总"})
    repo.llm_client=_S()
    eng.generate(nb.id, rid, "q", depth=2)
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="done" and d["content_md"].startswith("#")

def test_run_backcompat_plans_then_generates(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    eng=ReportEngine(repo, repo.settings)
    calls=[]
    monkeypatch.setattr(eng,"plan_outline", lambda *a,**k: calls.append("plan"))
    monkeypatch.setattr(eng,"generate", lambda *a,**k: calls.append("gen"))
    # outline_ready 由 stub plan_outline 不写,run 需自己判定;这里断言两阶段都被调用
    monkeypatch.setattr(repo,"get_report", lambda n,r: {"status":"outline_ready","outline":[{"title":"A"}]})
    eng.run("nb","rid","q", auto_generate=True)
    assert calls==["plan","gen"]
```

- [ ] **Step 2 跑失败**；**Step 3 实现**:把现 `run()` 的 Stage B/C/D 抽成 `generate`;`run` 改为编排。

```python
    def generate(self, notebook_id, rid, question, depth: int = 2) -> None:
        try:
            d = self.repo.get_report(notebook_id, rid)
            outline = d.get("outline") or []
            if not outline:
                self.repo.update_report(notebook_id, rid, status="failed",
                                        error="no outline to generate", progress="无大纲")
                return
            self.repo.update_report(notebook_id, rid, status="generating",
                                    progress=f"章节 0/{len(outline)} 完成")
            sections = self._run_sections(notebook_id, rid, outline, question, depth)
            self.repo.update_report(notebook_id, rid, progress="汇总中")
            content_md, gaps, references = self._assemble(notebook_id, rid, question, outline, sections)
            for s in sections:
                s.pop("id_map", None)
            self.repo.update_report(notebook_id, rid, sections=sections, content_md=content_md,
                                    gaps=gaps, references=references, status="done", progress="完成")
        except AskCancelled:
            self.repo.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            self.repo.update_report(notebook_id, rid, status="failed", error=str(exc)[:500], progress="失败")

    def run(self, notebook_id, rid, question, history="", depth: int = 2,
            auto_generate: bool = False) -> None:
        self.plan_outline(notebook_id, rid, question, history)
        if not auto_generate:
            return
        if self.repo.get_report(notebook_id, rid).get("status") == "outline_ready":
            self.generate(notebook_id, rid, question, depth)
```

（注:`_run_sections` 现签名 `(notebook_id, rid, outline, question, depth)` 不变;`_assemble` 不变。删除 run 内原 Stage A/B/D 直跑代码,全部改由 plan_outline/generate 承接。）

**⚠ 现有测试适配(必做)**:test_report_engine.py 里以 `eng.run(...)` 期望直达 `done`/`section_status` 的老用例(如 `test_engine_runs_sections_in_parallel_and_tolerates_one_failure`、`test_engine_cancel_marks_cancelled`、`test_run_sections_*`、Task 6-format 的 `_assemble` 测试)——现 `run()` 默认只规划到 `outline_ready`。逐个改为:**改调 `eng.generate(...)`**(直接测生成阶段,配合预置 `update_report(outline=..., status="outline_ready")`),或给 `run(..., auto_generate=True)`。语义按新两阶段对齐,不得放宽被测行为;commit message 里列出改了哪些。

- [ ] **Step 4 跑过 + 全文件回归** `$PY -m pytest backend/tests/test_report_engine.py -q`；**Step 5 Commit** `feat(report): 引擎拆 generate 阶段 + run 编排(auto_generate 兼容一键直出)`

---

### Task 6: 两阶段 API + schemas

**Files:** Modify `backend/app/api/routes.py`、`backend/app/models/schemas.py`;Test `backend/tests/test_report_api.py`

**Interfaces:**
- `ReportCreate` +`auto_generate: bool = False`;新 `ReportOutlineUpdate{sections: List[dict]}`。
- `POST /reports` → 起 `plan_outline` job(auto_generate=True 则接 generate);`PATCH /reports/{id}/outline`;`POST /reports/{id}/generate {depth?}`。

- [ ] **Step 1 失败测试**(test_report_api.py;复用现有 client fixture + stub job):

```python
def test_two_phase_report_lifecycle(client, monkeypatch):
    import app.api.routes as R
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched = {}
    monkeypatch.setattr(R, "_launch_plan_job", lambda repo,nb,rid,q,h,ag: launched.setdefault("plan", (rid, ag)))
    monkeypatch.setattr(R, "_launch_generate_job", lambda repo,nb,rid,q,d: launched.setdefault("gen", rid))
    nb = client.post("/api/notebooks", json={"name":"t","purpose":"p","primary_domain":"d"}).json()
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question":"why?"})
    rid = r.json()["report_id"]; assert launched["plan"][0]==rid and launched["plan"][1] is False
    # 模拟 planning 完成:写 outline_ready + outline
    from app.api.deps import repository
    repository().update_report(nb["id"], rid, status="outline_ready",
                               outline=[{"title":"A","scope":"s","sub_queries":["q"]}])
    d = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert d["status"]=="outline_ready" and d["outline"][0]["title"]=="A"
    # 编辑大纲
    assert client.patch(f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
                        json={"sections":[{"title":"A2","scope":"s","sub_queries":["q2"]}]}).status_code==200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()["outline"][0]["title"]=="A2"
    # 触发生成
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/generate", json={}).status_code==200
    assert launched["gen"]==rid

def test_generate_rejects_when_not_outline_ready(client, monkeypatch):
    import app.api.routes as R
    monkeypatch.setattr(R,"_report_llm_ready",lambda repo:True)
    monkeypatch.setattr(R,"_launch_plan_job",lambda *a,**k:None)
    nb=client.post("/api/notebooks",json={"name":"t","purpose":"p","primary_domain":"d"}).json()
    rid=client.post(f"/api/notebooks/{nb['id']}/reports",json={"question":"q"}).json()["report_id"]
    # 仍 planning(无 outline)→ generate 应 409
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/generate",json={}).status_code==409
```

- [ ] **Step 2 跑失败**；**Step 3 实现**:
  - schemas.py:`ReportCreate` 加 `auto_generate: bool = False`;新增 `class ReportOutlineUpdate(BaseModel): sections: List[dict] = Field(default_factory=list)`。
  - routes.py:把 `_launch_report_job` 拆成 `_launch_plan_job(repo,nb,rid,q,history,auto_generate)`(worker 调 `ReportEngine(...).run(nb,rid,q,history,depth,auto_generate=auto_generate)`;depth 从 report 读或传)与 `_launch_generate_job(repo,nb,rid,q,depth)`(worker 调 `.generate(nb,rid,q,depth)`);均 `background_jobs.submit`。改 `create_report` 调 `_launch_plan_job`(传 payload.auto_generate)。新增:

```python
@router.patch("/notebooks/{notebook_id}/reports/{report_id}/outline",
              dependencies=[Depends(require_notebook_write)])
def update_report_outline(notebook_id: str, report_id: str, payload: ReportOutlineUpdate) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="outline editable only when outline_ready")
    secs = [s for s in payload.sections
            if str(s.get("title","")).strip() and (s.get("sub_queries") or [])]
    if not secs:
        raise HTTPException(status_code=422, detail="at least one valid section required")
    repo.update_report(notebook_id, report_id, outline=secs)
    return {"status": "ok", "sections": len(secs)}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/generate",
             dependencies=[Depends(require_notebook_write)])
def generate_report(notebook_id: str, report_id: str, payload: ReportGenerateRequest) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="generate only from outline_ready")
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    _launch_generate_job(repo, notebook_id, report_id, cur["question"], depth)
    return {"status": "generating"}
```
  （`ReportGenerateRequest{depth: int | None = None}` 加进 schemas。）

- [ ] **Step 4 跑过 + 回归** `$PY -m pytest backend/tests/test_report_api.py backend/tests/test_report_engine.py -q`；**Step 5 Commit** `feat(api): 深度报告两阶段(规划→outline_ready→PATCH 大纲→generate)`

---

### Task 7: 前端两阶段 + 大纲编辑器

**Files:** Modify `frontend/app/report-view.tsx`、`frontend/app/page.tsx`(api 函数)、`frontend/app/globals.css`

**Interfaces:**
- page.tsx 加 `updateReportOutline(nb,rid,sections)`、`generateReport(nb,rid,depth?)`;`createReport` body 不变(auto_generate 默认 false)。传入 ReportsPanel。

- [ ] **Step 1 类型 + api**:`ReportDetailT.outline` 项扩 `{title,scope,sub_queries,perspectives?,tensions?,sufficiency?,gap_note?,action?}`;`isReportActive` 加 `planning|generating`;`ReportStatusBadge` 认新状态(planning=规划中/outline_ready=待确认/generating=生成中)。
- [ ] **Step 2 大纲编辑器**:详情视图 `status==='outline_ready'` 时渲染**可编辑大纲**:每节卡片(title/scope 可编辑 input、拖拽或上下移排序、删节按钮、增节按钮);每节徽章=`perspectives` 标签 + `sufficiency`(充足绿/薄弱橙/缺失红)+ `gap_note` + `tensions` 文字标记;底部「生成完整报告」按钮 → `updateReportOutline` 后 `generateReport` → 进现有 section_status 进度视图。`planning` 时显"规划中"loading。
- [ ] **Step 3 列表**:`ReportStatusBadge` 在列表行显示新状态;`outline_ready` 的报告点击进大纲编辑器(非终态轮询涵盖 planning/generating)。
- [ ] **Step 4 校验**:`cd frontend && npx tsc --noEmit && npm run test`;弯引号删除=0;UI 达 ui-polish。
- [ ] **Step 5 Commit** `feat(fe): 深度报告两阶段——大纲编辑器(视角/张力/充分性)+ 确认后生成`

---

### Task 8: 全量验证 + README + PR
- [ ] `$PY -m pytest backend/tests -q` 全绿;`bash scripts/check.sh` EXIT=0。
- [ ] README/README_zh 更新深度报告条目(两阶段:规划→确认大纲→生成;STORM 多视角;充分性徽章)+ 新端点(PATCH /outline、POST /generate)。
- [ ] rebase origin/master → push → `gh pr create --base master`(spec+plan 已在分支)。
