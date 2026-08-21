"""Task 25: ReportEngine 端口化 —— 引擎只持窄端口,不再持 repository facade。

全部用窄假件构造(零 SQLite/零 facade):证明
- 引擎无 repo 属性、模块不 import facade;
- 语料侦察各子步 fail-open;
- model_error sink 抛错不拖垮报告;
- 深挖构造的 ReasoningRetriever 是端口化的(拿到的就是 deps 里的同一批端口);
- 节间并行的 copy_context 传播/结果顺序/状态节流落库行为不变;
- 证据上下文经 EvidenceContextPort 且 "(none)" 哨兵归一保持。
"""
import contextvars
from dataclasses import replace
import inspect
import json
import threading

import pytest

from app.core.config import Settings
from app.services.reasoning_retrieval import ReasoningResult


# ---------------------------------------------------------------------------
# 窄假件(与 ports.py 各 Protocol 结构等价)
# ---------------------------------------------------------------------------

class _Reports:
    """dict 背书的 ReportRepository:记录 update 序列,merge 非 None 字段。"""
    def __init__(self):
        self.rows = {}
        self.updates = []

    def seed(self, notebook_id, question, **fields):
        rid = f"rep-{len(self.rows) + 1}"
        row = {"id": rid, "notebook_id": notebook_id, "question": question,
               "status": "pending", "progress": "", "error": "", "depth": 2,
               "outline": [], "sections": [], "gaps": [], "references": [],
               "section_status": [], "content_md": ""}
        row.update(fields)
        self.rows[rid] = row
        return rid

    def get_report(self, notebook_id, report_id):
        if report_id not in self.rows:
            raise KeyError(report_id)
        return dict(self.rows[report_id])

    def update_report(self, notebook_id, report_id, **fields):
        applied = {k: v for k, v in fields.items() if v is not None}
        self.updates.append((report_id, applied))
        if report_id in self.rows:
            self.rows[report_id].update(applied)

    def create_report(self, notebook_id, question, depth=2):
        return self.seed(notebook_id, question, depth=depth)

    def claim_report_generation(self, notebook_id, report_id):
        row = self.rows.get(report_id)
        if not row or row.get("status") != "outline_ready":
            return False
        row.update(status="generating", generation_started_at="fixture-start")
        return True

    def complete_report_generation(self, notebook_id, report_id, **fields):
        row = self.rows.get(report_id)
        if not row or row.get("status") != "generating":
            return False
        self.update_report(
            notebook_id, report_id, status="done", progress="完成", **fields
        )
        return True

    def list_reports(self, notebook_id, *, created_by):
        return list(self.rows.values())

    def delete_report(self, notebook_id, report_id):
        self.rows.pop(report_id, None)

    def export_reports(self, notebook_id, report_ids, *, created_by):
        return []


class _Retrieval:
    def __init__(self, kg=None, chunks=None):
        self._kg = kg or []
        self._chunks = chunks or []

    def federated_retrieve(self, notebook_id, query, **kw):
        return list(self._kg)

    def ppr_retrieve(self, notebook_id, question):
        return list(self._chunks)


class _Evidence:
    def __init__(self):
        self.calls = []

    def chunk_context(self, chunks, *, notebook_id, budget_chars=None):
        self.calls.append(("chunk", notebook_id, budget_chars))
        return "(none)", {}

    # 签名逐字镜像 EvidenceContextPort.knowledge_context —— `budget_chars` 一直在
    # 端口上,只是引擎此前没显式传;替身漏一个关键字参数就会把「引擎按端口调用」
    # 变成「引擎按替身调用」。
    def knowledge_context(self, notebook_id, hits, *, id_offset=0,
                          budget_chars=None):
        self.calls.append(("kg", notebook_id, id_offset, budget_chars))
        return "(none)", {}

    def element_context(self, elements, *, notebook_id, id_offset=4000,
                        budget_chars=None):
        self.calls.append(("element", notebook_id, id_offset, budget_chars))
        return "(none)", {}

    def parse_anchors(self, answer, evidence_by_id):
        return []

    def citation_titles(self, source_ids):
        return {}

    def citation_source_info(self, source_ids):
        return {}


class _SectionLLM:
    """按 prompt 关键字出大纲/节/摘要(镜像 test_report_engine._OutlineLLM)。"""
    configured = True
    model = "fake-m"

    def __init__(self):
        self.prompts = []

    def chat_json(self, messages, schema_hint, **kw):
        content = messages[-1]["content"]
        self.prompts.append(content)
        if "OUTLINE" in content or "PERSPECTIVES" in content:
            return json.dumps({"sections": [
                {"title": "A", "scope": "sa", "sub_queries": ["qa"]},
                {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]})
        if "ONE section" in content:
            return json.dumps({"markdown": "## X\nbody", "grounded": True})
        if "EXECUTIVE SUMMARY" in content:
            return json.dumps({"summary": "总结"})
        return json.dumps({})


class _EmptyLLM:
    configured = True
    model = "fake-empty"

    def __init__(self):
        self.calls = 0

    def chat_json(self, *a, **k):
        self.calls += 1
        return "{}"


class _Models:
    def __init__(self, llm):
        self._llm = llm

    def chat(self, workload_id):
        return self._llm

    def parallelism(self, workload_id):
        return 2


class _Errors:
    def __init__(self, boom=False):
        self.notes = []
        self.boom = boom

    def note_model_error(
        self, stage, error, *, workload_id,
    ):
        if self.boom:
            raise RuntimeError("sink down")
        self.notes.append((stage, workload_id))


class _Sources:
    def __init__(self, rows):
        self.rows = rows

    def report_source_rows(self, notebook_id, *, representative_limit=20,
                           distribution_limit=32):
        return {
            "total_sources": len(self.rows),
            "metadata_sources": 0,
            "known_year_sources": 0,
            "identity_uncertain_sources": len(self.rows),
            "hash_duplicate_excess": 0,
            "title_duplicate_excess": 0,
            "type_distribution": [],
            "year_distribution": [],
            "representatives": list(self.rows)[:representative_limit],
        }

    def report_source_identity_rows(self, source_ids):
        wanted = set(source_ids)
        return [row for row in self.rows if str(row.get("id") or "") in wanted]


class _BoomSources:
    def report_source_rows(self, notebook_id):
        raise RuntimeError("db down")


class _Communities:
    sibling_min_bridge = 2


class _EventLog:
    def __init__(self):
        self.events = []

    def emit(self, payload):
        self.events.append(payload)


def _deps(**over):
    from app.services.report_engine import ReportEngineDependencies
    base = dict(
        reports=_Reports(),
        retrieval=_Retrieval(),
        evidence_context=_Evidence(),
        model_clients=_Models(_SectionLLM()),
        model_errors=_Errors(),
        source_query=_Sources([]),
        communities=_Communities(),
        settings=Settings(_env_file=None),
        event_log=_EventLog(),
    )
    base.update(over)
    return ReportEngineDependencies(**base)


def _engine(deps=None, cancel_event=None):
    from app.services.report_engine import ReportEngine
    return ReportEngine(deps or _deps(), user_id="user-x", cancel_event=cancel_event)


# ---------------------------------------------------------------------------
# 引擎不持 facade
# ---------------------------------------------------------------------------

def test_engine_constructs_from_narrow_fakes_and_keeps_no_facade():
    deps = _deps()
    eng = _engine(deps)
    assert not hasattr(eng, "repo")                    # 不持 repository facade
    assert eng.dependencies is deps and eng.settings is deps.settings
    assert eng.user_id == "user-x"


def test_report_generation_stage_binds_run_actor_cancel_and_connection_authorities(
    monkeypatch,
):
    from app.application.report_pipeline import (
        ReportGenerationInput,
        ReportStageBoundaryError,
        ReportStageRuntime,
    )
    from app.services.retrieval_run import retrieval_run

    class Probe:
        def __init__(self):
            self.held = False
            self.boom = False

        def is_connection_held(self):
            if self.boom:
                raise RuntimeError("probe exploded")
            return self.held

    probe = Probe()
    cancel = __import__("threading").Event()
    eng = _engine(_deps(retrieval_connection_probe=probe), cancel_event=cancel)
    monkeypatch.setattr(
        eng,
        "_run_sections",
        lambda *_args, **_kwargs: [{"title": "A", "markdown": "body"}],
    )
    stage = ReportGenerationInput.create(
        notebook_id="nb",
        report_id="rep",
        original_question="q",
        display_question="q",
        research_question="q",
        depth=2,
        actor_id="user-x",
        understanding={},
        outline=[{"title": "A"}],
    )
    with retrieval_run(
        run_kind="report_generation",
        correlation_id="rep",
        actor_id="user-x",
        cancel_event=cancel,
    ) as run:
        runtime = ReportStageRuntime(None, run, cancel, probe, "user-x")
        assert eng.run_generation_stage(stage, runtime).successful_count == 1
        with pytest.raises(ReportStageBoundaryError):
            eng.run_generation_stage(stage, replace(runtime, actor_id="forged"))
        with pytest.raises(ReportStageBoundaryError):
            eng.run_generation_stage(stage, replace(runtime, connection_probe=Probe()))
        probe.held = True
        with pytest.raises(ReportStageBoundaryError):
            eng.run_generation_stage(stage, runtime)
        probe.held = False
        probe.boom = True
        with pytest.raises(ReportStageBoundaryError, match="connection probe"):
            eng.run_generation_stage(stage, runtime)


def test_planning_stage_preserves_normal_cancelled_return_after_core_write(monkeypatch):
    from app.application.report_pipeline import ReportPlanningInput, ReportStageRuntime
    from app.services.retrieval_run import retrieval_run

    class Probe:
        @staticmethod
        def is_connection_held():
            return False

    cancel = threading.Event()
    probe = Probe()
    eng = _engine(_deps(retrieval_connection_probe=probe), cancel_event=cancel)

    def cancelled_planner(*_args, **_kwargs):
        cancel.set()
        return None

    monkeypatch.setattr(eng, "_plan_outline_run", cancelled_planner)
    stage = ReportPlanningInput.create(
        notebook_id="nb",
        report_id="rep",
        question="q",
        history="",
        actor_id="user-x",
        confirmed_intent=None,
    )
    with retrieval_run(
        run_kind="report_planning",
        correlation_id="rep",
        actor_id="user-x",
        cancel_event=cancel,
    ) as run:
        runtime = ReportStageRuntime(None, run, cancel, probe, "user-x")
        assert eng.run_planning_stage(stage, runtime) is None


def test_planning_stage_preserves_pre_entry_cancellation_as_normal_terminal():
    from app.application.report_pipeline import ReportPlanningInput, ReportStageRuntime
    from app.services.retrieval_run import retrieval_run

    class Probe:
        @staticmethod
        def is_connection_held():
            return False

    reports = _Reports()
    reports.seed("nb", "q")
    cancel = threading.Event()
    cancel.set()
    probe = Probe()
    eng = _engine(
        _deps(reports=reports, retrieval_connection_probe=probe),
        cancel_event=cancel,
    )
    stage = ReportPlanningInput.create(
        notebook_id="nb",
        report_id="rep-1",
        question="q",
        history="",
        actor_id="user-x",
        confirmed_intent=None,
    )
    with retrieval_run(
        run_kind="report_planning",
        correlation_id="rep-1",
        actor_id="user-x",
        cancel_event=cancel,
    ) as run:
        runtime = ReportStageRuntime(None, run, cancel, probe, "user-x")
        assert eng.run_planning_stage(stage, runtime) is None
    assert reports.rows["rep-1"]["status"] == "cancelled"


def test_report_engine_module_has_no_facade_import():
    from app.services import report_engine
    src = inspect.getsource(report_engine)
    assert "sqlite_repository" not in src
    assert "self.repo" not in src


# ---------------------------------------------------------------------------
# 语料侦察:端口投影 + fail-open
# ---------------------------------------------------------------------------

def test_corpus_map_reads_source_titles_through_port():
    eng = _engine(_deps(source_query=_Sources([{"title": " Razavi "}, {"title": ""}])))
    m = eng._build_corpus_map("nb", "q")
    assert "Razavi" in m                               # strip 后入 map
    assert m.count("- ") == 1                          # 空标题过滤


def test_corpus_map_source_query_failure_remains_fail_open():
    from app.services.retrieval import RetrievedKnowledge
    hit = RetrievedKnowledge(object_id="ko1", object_type="concept",
                             payload={"name": "Bandgap Reference"})
    hit.tier = "base"
    eng = _engine(_deps(source_query=_BoomSources(), retrieval=_Retrieval(kg=[hit])))
    m = eng._build_corpus_map("nb", "why 1.2V")
    assert "Bandgap Reference" in m                    # 其余子步不受拖累
    assert "db down" not in m


def test_corpus_map_all_probes_failing_degrades_to_sentinel():
    class _BoomRetrieval:
        def federated_retrieve(self, *a, **k):
            raise RuntimeError("x")

        def ppr_retrieve(self, *a, **k):
            raise RuntimeError("x")

    eng = _engine(_deps(source_query=_BoomSources(), retrieval=_BoomRetrieval()))
    assert eng._build_corpus_map("nb", "q") == "(语料侦察无结果)"


# ---------------------------------------------------------------------------
# model_error sink 抛错不遮蔽报告的真实终态
# ---------------------------------------------------------------------------

def test_model_error_sink_failure_preserves_empty_report_failure(monkeypatch):
    reports = _Reports()
    rid = reports.seed("nb", "q", status="outline_ready",
                       outline=[{"title": "A", "scope": "sa", "sub_queries": ["qa"]}])
    deps = _deps(reports=reports, model_clients=_Models(_EmptyLLM()),
                 model_errors=_Errors(boom=True))
    eng = _engine(deps)
    monkeypatch.setattr(eng, "_deep_dive", lambda *a, **k: ReasoningResult())
    eng.generate("nb", rid, "q", depth=1)
    # sink 自己抛错不能让引擎崩溃，也不能把零正文报告伪装成完成。
    assert reports.rows[rid]["status"] == "failed"
    assert reports.rows[rid]["sections"][0].get("failed") is True


def test_empty_section_content_notes_model_error_through_port(monkeypatch):
    errors = _Errors()
    llm = _EmptyLLM()
    eng = _engine(_deps(model_clients=_Models(llm), model_errors=errors))
    out = eng._draft_section("nb", {"title": "T", "scope": "S"}, "q", ReasoningResult())
    assert llm.calls == 2                              # 空 markdown 有界重试一次
    assert out.get("failed") is True
    assert errors.notes and errors.notes[0][0] == "report_section"


# ---------------------------------------------------------------------------
# 深挖:端口化 ReasoningRetriever
# ---------------------------------------------------------------------------

def test_deep_dive_constructs_port_based_reasoning_retriever(monkeypatch):
    captured = {}

    class _RR:
        def __init__(self, **kw):                      # 仅关键字:端口注入
            captured.update(kw)

        def run(self, notebook_id, question, **kw):
            captured["run"] = (notebook_id, question, kw)
            return ReasoningResult()

    monkeypatch.setattr("app.services.reasoning_retrieval.ReasoningRetriever", _RR)
    deps = _deps()
    eng = _engine(deps)
    eng._deep_dive("nb", {"title": "t", "scope": "s", "sub_queries": ["q1"]},
                   "Q", depth=3)
    assert captured["retrieval"] is deps.retrieval
    assert captured["model_clients"] is deps.model_clients
    assert captured["communities"] is deps.communities
    assert captured["settings"] is deps.settings
    assert captured["run"][2].get("max_steps") == 3
    assert "top_n" not in captured["run"][2]           # 与 ask 同一套自适应证据预算


# ---------------------------------------------------------------------------
# 节间并行:copy_context 传播 + 结果顺序 + 状态经 reports 端口落库
# ---------------------------------------------------------------------------

_PROBE = contextvars.ContextVar("task25_probe", default="")


def test_run_sections_propagates_context_and_preserves_order(monkeypatch):
    _PROBE.set("user-ctx")
    reports = _Reports()
    rid = reports.seed("nb", "q")
    deps = _deps(reports=reports)
    monkeypatch.setattr(deps.settings, "kg_job_concurrency", 2)
    eng = _engine(deps)
    barrier = threading.Barrier(2, timeout=5)
    seen = {}

    def _dd(notebook_id, section, question, depth=None, on_step=None):
        barrier.wait()                                 # 两节真并行(worker 非调用线程)
        seen[section["title"]] = _PROBE.get()
        return ReasoningResult()

    monkeypatch.setattr(eng, "_deep_dive", _dd)
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]},
               {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]
    out = eng._run_sections("nb", rid, outline, "q", depth=2)
    assert [s["title"] for s in out] == ["A", "B"]     # futures 顺序 = 大纲顺序
    assert seen == {"A": "user-ctx", "B": "user-ctx"}  # copy_context 传播
    assert reports.rows[rid]["section_status"] and all(
        x["phase"] == "完成" for x in reports.rows[rid]["section_status"])


def test_plan_and_generate_statuses_flow_through_reports_port(monkeypatch):
    reports = _Reports()
    rid = reports.seed("nb", "why")
    eng = _engine(_deps(reports=reports))
    eng.plan_outline("nb", rid, "why")
    statuses = [a.get("status") for _r, a in reports.updates if a.get("status")]
    assert statuses[0] == "planning" and statuses[-1] == "outline_ready"

    monkeypatch.setattr(eng, "_deep_dive", lambda *a, **k: ReasoningResult())
    eng.generate("nb", rid, "why", depth=1)
    statuses = [a.get("status") for _r, a in reports.updates if a.get("status")]
    assert statuses[-2:] == ["generating", "done"]


def test_generation_cas_loss_does_not_publish_committed_report(monkeypatch):
    reports = _Reports()
    rid = reports.seed(
        "nb",
        "why",
        status="outline_ready",
        outline=[{"title": "A", "scope": "sa", "sub_queries": ["qa"]}],
    )
    eng = _engine(_deps(reports=reports))
    monkeypatch.setattr(eng, "_deep_dive", lambda *a, **k: ReasoningResult())

    def lose_terminal_cas(*_args, **_kwargs):
        reports.rows[rid]["status"] = "cancelled"
        return False

    monkeypatch.setattr(reports, "complete_report_generation", lose_terminal_cas)
    committed = eng.generate("nb", rid, "why", depth=1)

    assert committed is None
    assert reports.rows[rid]["status"] == "cancelled"
    assert not any(
        fields.get("status") == "done" for _report_id, fields in reports.updates
    )


# ---------------------------------------------------------------------------
# 证据上下文经 EvidenceContextPort,(none) 哨兵归一保持
# ---------------------------------------------------------------------------

def test_draft_section_routes_evidence_through_ports():
    deps = _deps()
    eng = _engine(deps)
    llm = deps.model_clients.chat("report_section")
    out = eng._draft_section("nb", {"title": "T", "scope": "S"}, "q", ReasoningResult())
    assert deps.evidence_context.calls == [
        ("chunk", "nb", deps.settings.report_section_chunk_budget),
        ("kg", "nb", 0, deps.settings.answer_context_budget_chars),
        ("element", "nb", 4000,
         max(2000, deps.settings.report_section_chunk_budget // 3)),
    ]
    section_prompt = next(p for p in llm.prompts if "ONE section" in p)
    assert "(no evidence retrieved)" in section_prompt  # 双 (none) 哨兵归一
    assert out["markdown"].startswith("## X")
