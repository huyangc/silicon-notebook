"""Report capacity integration: real prompts, admitted sends and durable receipts."""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.model_context import ContextBudget, ModelContextExceeded, admit_request
from app.models.schemas import NotebookCreate
from app.services.context_spans import entry_lines
from app.services.reasoning_retrieval import ReasoningResult
from app.services.report_context import ContextProjection, EvidenceUnit, ReportContextOverflow
from app.services.report_context_runtime import (
    ReportIntentClient,
    context_disclosure,
    planning_context_call,
    receipt_for,
    report_blueprint_call,
    report_context_runtime,
    report_editor_call,
    section_context_receipt,
)
from app.services.report_engine import ReportEngine
from app.services.report_synthesis import blueprint_for_section
from tests.test_report_engine_ports import _deps, _Evidence, _Models


class Transport:
    """A local provider which applies the same admission boundary as transport."""
    configured = True
    model = "local-test"

    def __init__(self, response=None, *, maximum=200000):
        self.context_budget = ContextBudget(1000000, 100, maximum)
        self.response = response or {"markdown": "## Result\nSupported [k1]", "grounded": True}
        self.calls = []

    def pin_context(self, max_tokens=None):
        return self

    def chat_json(self, messages, schema_hint, **kwargs):
        assert self.context_budget.fits(messages, schema_hint)
        admit_request()
        self.calls.append(deepcopy(messages))
        response = self.response(messages) if callable(self.response) else self.response
        if isinstance(response, Exception):
            raise response
        return json.dumps(response, ensure_ascii=False)


class Models(_Models):
    def __init__(self, target, compressor):
        super().__init__(target)
        self.compressor = compressor

    def chat(self, workload_id):
        return self.compressor if workload_id == "report_context_compress" else self._llm


def finding(handle, quote):
    return {"statement": "Measured 1.2 V at 25 C.",
            "evidence": [{"handle": handle, "quote": quote}],
            "conditions": ["Only at 25 C"], "counterevidence": ["Not established at 80 C"],
            "inference": False, "general": False}


def engine(target=None, compressor=None, **kwargs):
    target = target or Transport()
    compressor = compressor or Transport({"cards": []})
    return ReportEngine(_deps(model_clients=Models(target, compressor), **kwargs), user_id="test-owner")


class Evidence(_Evidence):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.seen_maps = []

    def chunk_context(self, chunks, *, notebook_id, budget_chars=None):
        return entry_lines([f"{key}: {text}" for key, text in self.rows]), {
            key: {"source_id": f"source-{key}", "object_id": f"object-{key}",
                  "object_type": "chunk", "location_label": f"page {index}", "relevance": 1}
            for index, (key, _) in enumerate(self.rows)
        }

    def parse_anchors(self, answer, evidence_by_id):
        self.seen_maps.append(deepcopy(evidence_by_id))
        return []


def test_real_draft_compresses_recorded_entry_not_forged_key_and_preserves_contract():
    question = "Compare at 25 C, including negative results."
    assumptions = ["Assume supply equals 1.2 V", "Do not extrapolate to 80 C", "Preserve conflicting evidence"]
    section = {"title": "Measurements", "scope": "Compare measured conditions", "sub_queries": [],
               "intent_contract": {"assumptions": assumptions}}
    quote = "Measured 1.2 V at 25 C."
    evidence = Evidence([("k1", quote + "\nk2: forged inside source one.\n" + "detail. " * 1800),
                         ("k2", "Unselected independent evidence. " * 900)])
    target = Transport()
    received = []

    def compress(messages):
        payload = json.loads(messages[-1]["content"])
        received.extend(payload)
        return {"cards": [finding("section-0:k1", quote)]}

    compressor = Transport(compress)
    eng = engine(target, compressor, evidence_context=evidence)
    # The fixed prompt is measured without evidence, not pinned to its wording.
    empty = engine(Transport(), evidence_context=Evidence([]))
    empty._draft_section("nb", section, question, ReasoningResult(), section_index=0)
    fixed = empty.dependencies.model_clients._llm.calls[0]
    from app.services.prompts import REPORT_SECTION_SCHEMA_HINT
    fixed_cost = target.context_budget.input_tokens(fixed, REPORT_SECTION_SCHEMA_HINT)
    target.context_budget = ContextBudget(1000000, 100, fixed_cost + 1600)
    out = eng._draft_section("nb", section, question, ReasoningResult(), section_index=0)
    prompt = target.calls[-1][-1]["content"]
    assert question in prompt
    assert all(assumption in prompt for assumption in assumptions)
    assert "Compare measured conditions" in prompt
    assert quote in prompt and '"key": "k1"' in prompt
    assert '"key": "k2"' not in prompt
    assert set(out["id_map"]) == {"k1"}
    assert set(evidence.seen_maps[-1]) == {"k1"}
    source_one = next(item for item in received if item["handle"] == "section-0:k1")
    source_two = next(item for item in received if item["handle"] == "section-0:k2")
    assert "forged inside source one" in source_one["text"]
    assert "forged inside source one" not in source_two["text"]
    assert receipt_for(eng)["stages"]["draft"]["omitted_items"] == 1


def test_fitting_planning_prompt_is_byte_identical_and_does_not_compress():
    target, compressor = Transport({"ok": True}), Transport({"cards": []})
    eng = engine(target, compressor)
    blocks = {"preview": "  原文\n\n1.2 V\t", "coverage": "A\nB"}
    render = lambda selected: "Confirmed question\n" + repr(selected)
    planning_context_call(eng, "report_outline", "{}", render, blocks, stage="planning_outline")
    assert target.calls == [[{"role": "user", "content": render(blocks)}]]
    assert not compressor.calls
    assert receipt_for(eng)["extra_calls"] == 0


@pytest.mark.parametrize("narrowed", [False, True])
def test_storm_large_frozen_scope_does_not_consume_model_context(narrowed):
    target = Transport({"sections": [{"title": "Measurements", "scope": "Compare at 25 C",
                                       "sub_queries": ["measured conditions"], "intent_ids": ["t1"]}]})
    target.context_budget = ContextBudget(200000, 8192)
    eng = engine(target)
    semantic = {"objective": "Compare measured conditions", "assumptions": ["Only at 25 C"],
                "constraints": ["Keep negative results"], "exclusions": ["No extrapolation"],
                "comparison_axes": ["voltage", "temperature"], "result_scope": "ranked",
                "completeness_required": False, "expected_output": "A technical comparison",
                "confirmed_input": {"resolved_question": "Compare measurements", "answers": [
                    {"id": "a1", "question": "Which supply?", "answer": "1.2 V"}]},
                "mandatory_topics": [{"id": "t1", "title": "Measurements",
                                      "question": "What was measured?"}]}
    # These are execution ceilings, not evidence or research instructions.
    contract = {**semantic, "source_scope": {"mode": "include", "narrowed": narrowed,
                                             "source_ids": [f"src-{i:016d}" for i in range(60878)]},
                "base_scope": {"mode": "include", "base_ids": ["base-execution-only"]},
                "context_processing": {"stages": {"planning_intent": {"input_items": 1}}},
                "credibility": {"legacy-audit-only": True}}
    before = deepcopy(contract)
    baseline = eng._storm_outline("nb", "Compare measurements", "", "", intent_contract=semantic)
    sections = eng._storm_outline("nb", "Compare measurements", "", "", intent_contract=contract)
    assert sections == baseline
    assert target.calls[1] == target.calls[0]
    assert receipt_for(eng)["extra_calls"] == 0
    bound = eng._bind_outline_to_intent(sections, contract, [])
    assert bound[0]["intent_contract"] == semantic
    assert contract == before


def test_storm_oversized_semantics_still_fail_without_fallback(monkeypatch):
    eng = engine()
    monkeypatch.setattr(eng, "_plan_outline", lambda *args: pytest.fail("lost confirmed intent"))
    with pytest.raises(ReportContextOverflow):
        eng._storm_outline("nb", "Question", "", "", intent_contract={
            "constraints": ["A necessary user instruction. " * 10000]})
    assert not eng.dependencies.model_clients._llm.calls


def test_blueprint_legacy_contract_does_not_send_execution_metadata():
    target = Transport({"central_answer": "supported"})
    eng = engine(target)
    semantic = {"assumptions": ["Keep all measurements at 25 C"]}
    legacy = {**semantic, "source_scope": {"source_ids": [f"src-{i:016d}" for i in range(60878)]},
              "base_scope": {"base_ids": ["execution-only"]},
              "context_processing": {"extra_calls": 24}, "credibility": {"old-audit": True}}
    evidence = [{"section_id": "s1", "evidence": [{"evidence_id": "e1", "body": "Measured 1.2 V"}]}]
    _, legal = report_blueprint_call(eng, "Question", semantic, None, evidence)
    _, legacy_legal = report_blueprint_call(eng, "Question", legacy, None, evidence)
    assert legacy_legal == legal == {"e1"}
    assert target.calls[1] == target.calls[0]


def test_blueprint_allowed_ids_exclude_evidence_not_received_by_model():
    target = Transport({"central_answer": "supported"}, maximum=9500)
    eng = engine(target)
    eng.settings.report_context_extra_calls_max = 0
    sections = [{"section_id": "s1", "evidence": [
        {"evidence_id": "e1", "body": "small admitted finding"},
        {"evidence_id": "e2", "body": "omitted-large-material " * 3000}]}]
    _, legal = report_blueprint_call(eng, "Confirmed question", {"assumptions": ["all fixed"]}, None, sections)
    assert legal == {"e1"}
    prompt = target.calls[0][-1]["content"]
    assert '"evidence_id": "e1"' in prompt
    assert '"evidence_id": "e2"' not in prompt
    assert "all fixed" in prompt
    assert receipt_for(eng)["stages"]["synthesis"]["omitted_items"] == 1


@pytest.mark.parametrize("dependency", ["claim", "counterevidence", "shared_definition", "central_answer"])
def test_blueprint_derivations_inherit_actual_evidence_depth_before_editor_compression(monkeypatch, dependency):
    quote = "Measured 1.2 V at 25 C."
    raw = {"central_answer": "Global derived answer" if dependency == "central_answer" else "",
           "shared_definitions": [], "claims": [],
           "sections": [{"section_id": f"section-{index}", "thesis": f"Section {index}"}
                        for index in range(1, 4)]}
    if dependency in {"claim", "counterevidence"}:
        raw["claims"] = [{"id": "cross-section", "statement": quote, "type": "fact",
                          "owner_section_id": "section-2",
                          "evidence_keys": ["e1" if dependency == "claim" else "e2"],
                          "counterevidence_keys": ["e1"] if dependency == "counterevidence" else []}]
    if dependency == "shared_definition":
        raw["shared_definitions"] = [{"term": "Measurement", "definition": quote,
                                      "evidence_keys": ["e1"]}]
    seen = []

    def compress(messages):
        payload = json.loads(messages[-1]["content"])
        seen.extend(item["handle"] for item in payload)
        return {"cards": [finding(item["handle"], quote) for item in payload]}

    target = Transport(raw)
    eng = engine(target, Transport(compress))
    runtime = report_context_runtime(eng)
    original_project = runtime.project

    def projected(client, schema, render, units, stage):
        projection = original_project(client, schema, render, units, stage)
        if stage == "synthesis":
            # Model the completed projection boundary: only source section 1's
            # material has reached the configured maximum compression depth.
            projected_units = tuple(replace(unit, level=eng.settings.report_context_compression_max_levels,
                                            quotes=((unit.handle, quote),))
                                    if unit.group == "section-1" else unit for unit in projection.units)
            return replace(projection, units=projected_units)
        return projection

    monkeypatch.setattr(runtime, "project", projected)
    outline = [{"title": f"Section {index}", "scope": "Measurements"} for index in range(1, 4)]
    results = [ReasoningResult(chunks=[SimpleNamespace(chunk_id=f"e{index}", text=quote)])
               for index in range(1, 4)]
    blueprint, status, error = eng._synthesize_report_blueprint(outline, results, "Question", None)
    assert status == "available", error
    empty_draft = SimpleNamespace(projection=ContextProjection((), ()))
    receipts = [section_context_receipt(empty_draft, blueprint_for_section(blueprint, index))
                for index in range(3)]
    maximum = eng.settings.report_context_compression_max_levels
    expected = [maximum, maximum, 0] if dependency in {"claim", "counterevidence"} else [maximum] * 3
    assert [receipt["max_level"] for receipt in receipts] == expected

    target.context_budget = ContextBudget(1000000, 100, 9000)
    chapters = [{"title": f"Section {index}", "markdown": quote + (" detail. " * 2000 if index else ""),
                 "claims": [], "citation_audit": {}, "context_processing": receipt}
                for index, receipt in enumerate(receipts)]
    report_editor_call(eng, "Question", chapters, None, None, "Fixed assumptions")
    # Source 1's max-depth material was moved into section 2 by the blueprint;
    # the editor must never send that derived chapter through another layer.
    assert "editor-section-1" not in seen
    if dependency in {"claim", "counterevidence"}:
        assert "editor-section-2" in seen
    else:
        # Both globally forwarded contracts make every chapter a derivation.
        assert not seen


def test_editor_compresses_all_chapters_without_mutating_bodies_and_reports_gaps():
    seen = []
    def compress(messages):
        payload = json.loads(messages[-1]["content"])
        seen.extend(item["handle"] for item in payload)
        return {"cards": [finding(item["handle"], "1.2 V") for item in payload
                          if "1.2 V" in item["text"]]}
    target, compressor = Transport({"summary": "Summary"}, maximum=9000), Transport(compress)
    eng = engine(target, compressor)
    chapters = [{"title": f"Chapter {index}", "markdown": "1.2 V " + "long. " * 1000,
                 "claims": [], "citation_audit": {}} for index in range(4)]
    before = deepcopy(chapters)
    report_editor_call(eng, "Question", chapters, {"fixed": "frame"}, {"fixed": "blueprint"}, "all assumptions")
    assert chapters == before
    assert set(seen) == {f"editor-section-{index}" for index in range(4)} | {"editor-blueprint:fixed:0"}
    prompt = target.calls[0][-1]["content"]
    assert "frame" in prompt and "blueprint" in prompt and "all assumptions" in prompt
    receipt = receipt_for(eng)
    assert receipt["stages"]["final_editor"]["omitted_items"] == 0
    assert receipt["stages"]["final_editor"]["partial_items"] == 4
    assert "不代表完整覆盖" in "".join(context_disclosure(eng))


@pytest.mark.parametrize("compress", [False, True])
def test_editor_projects_large_valid_blueprint_instead_of_treating_it_as_fixed(compress):
    from app.services.report_synthesis import normalize_synthesis_blueprint

    sections = [{"title": f"Section {index}", "markdown": "A brief finished chapter.",
                 "claims": [], "citation_audit": {}} for index in range(1, 6)]
    raw = {"central_answer": "Global answer", "shared_definitions": [],
           "sections": [{"section_id": f"section-{index}", "thesis": "Result"}
                        for index in range(1, 6)],
           "claims": [{"id": f"c{index}", "statement": "技术结论" * 75, "type": "inference",
                       "owner_section_id": f"section-{index // 12 + 1}",
                       "evidence_keys": ["e1"], "counterevidence_keys": ["e2"],
                       "conditions": ["适用条件" * 60, "实验条件" * 60]} for index in range(60)]}
    blueprint = normalize_synthesis_blueprint(raw, outline=sections,
        legal_evidence_ids={"e1", "e2"}, frame=None)
    assert blueprint is not None
    before = deepcopy(blueprint)
    seen = []

    def compress_material(messages):
        payload = json.loads(messages[-1]["content"])
        seen.extend(payload)
        return {"cards": [finding(item["handle"], "技术结论")
                          for item in payload if item["kind"] == "blueprint_claims"]}

    target = Transport({"summary": "Summary"})
    target.context_budget = ContextBudget(200000, 102400)
    eng = engine(target, Transport(compress_material))
    if not compress:
        eng.settings.report_context_extra_calls_max = 0
    report_editor_call(eng, "Question", sections, {"fixed": "USER-FRAME"}, blueprint, "USER-INTENT")
    prompt = target.calls[0][-1]["content"]
    assert "USER-FRAME" in prompt and "USER-INTENT" in prompt
    assert '"owner_section_id"' in prompt and '"counterevidence_keys"' in prompt
    assert '"e1"' in prompt and '"e2"' in prompt
    assert "适用条件" * 60 in prompt and "实验条件" * 60 in prompt
    if compress:
        assert '"inference": true' in prompt
    assert blueprint == before
    receipt = receipt_for(eng)
    stage = receipt["stages"]["final_editor"]
    assert stage["input_items"] > len(sections)
    assert stage["omitted_items"] or stage["partial_items"]
    assert bool(receipt["extra_calls"]) == compress
    assert any(item["kind"] == "blueprint_claims" for item in seen) == compress
    assert report_context_runtime(eng).editor_limited_sections


@pytest.mark.parametrize("component", ["central_answer", "shared_definitions", "claims"])
def test_editor_blueprint_inherits_depth_and_discloses_affected_sections(component):
    blueprint = {"central_answer": "Short conclusion.",
                 "sections": [{"section_id": f"section-{index}", "_context_level": 2}
                              for index in (1, 2)], "claims": [], "shared_definitions": []}
    if component == "central_answer":
        blueprint[component] = "共享结论" * 400
    elif component == "shared_definitions":
        blueprint[component] = [{"term": "Measurement", "definition": "共享定义" * 200,
                                 "evidence_keys": ["e1"]}]
    else:
        blueprint[component] = [{"id": "c1", "statement": "技术结论" * 300, "type": "inference",
                                 "owner_section_id": "section-2", "evidence_keys": ["e1"]}]
    sections = [{"title": f"Chapter {index}", "markdown": "Small completed chapter.", "claims": [],
                 "context_processing": {"max_level": 2}} for index in (1, 2)]
    target = Transport({"summary": "Summary"}, maximum=3000)
    compressor = Transport({"cards": []})
    eng = engine(target, compressor)
    report_editor_call(eng, "Question", sections, None, blueprint, "USER-INTENT")
    assert not compressor.calls
    expected = {1} if component == "claims" else {0, 1}
    assert report_context_runtime(eng).editor_limited_sections == expected
    assert receipt_for(eng)["stages"]["final_editor"]["omitted_items"] > 0


def test_fitting_editor_blueprint_preserves_prompt_without_extra_calls():
    from app.services.prompts import report_summary_prompt
    from app.services.report_synthesis import fair_editor_context

    sections = [{"title": "A", "markdown": "A small completed chapter.", "claims": []}]
    blueprint = {"central_answer": "Small shared conclusion.", "claims": [],
                 "sections": [{"section_id": "section-1", "thesis": "Supported answer"}]}
    target = Transport({"summary": "Summary"})
    eng = engine(target)
    report_editor_call(eng, "Question", sections, None, blueprint, "USER-INTENT")
    assert target.calls[0][-1]["content"] == report_summary_prompt(
        "Question", fair_editor_context(sections, blueprint=blueprint), intent_block="USER-INTENT")
    assert receipt_for(eng)["extra_calls"] == 0
    assert not report_context_runtime(eng).editor_limited_sections


def test_editor_still_rejects_oversized_user_frame():
    eng = engine(Transport({"summary": "Summary"}, maximum=4000))
    with pytest.raises(ReportContextOverflow):
        report_editor_call(eng, "Question", [{"title": "A", "markdown": "Short"}],
                           {"confirmed": "不可丢弃的用户约束" * 2000}, None, "USER-INTENT")
    assert not eng.dependencies.model_clients._llm.calls


@pytest.mark.parametrize("reject_retry", [False, True])
def test_remote_overflow_gets_one_smaller_retry_and_counts_each_extra_send(reject_retry):
    def respond(messages):
        return ModelContextExceeded() if len(target.calls) == 1 or reject_retry else {"ok": True}
    target = Transport(respond, maximum=12000)
    eng = engine(target)
    eng.settings.report_context_extra_calls_max = 0
    units = tuple(EvidenceUnit(f"e{i}", "evidence. " * 200, group=str(i), level=2) for i in range(4))
    render = lambda selected: [{"role": "user", "content": "Fixed contract.\n" + "\n".join(x.text for x in selected)}]
    # Allow one remote recovery send, but no model compression calls.
    prepared = report_context_runtime(eng).prepare("report_section", "{}", render, units, stage="draft")
    eng.settings.report_context_extra_calls_max = 1
    if reject_retry:
        with pytest.raises(ModelContextExceeded):
            prepared.chat_json()
    else:
        assert json.loads(prepared.chat_json()) == {"ok": True}
    assert len(target.calls) == 2
    assert len(target.calls[1][0]["content"]) < len(target.calls[0][0]["content"])
    assert receipt_for(eng)["extra_calls"] == 1


def test_real_section_does_not_repeat_rejected_adaptive_request():
    target = Transport(lambda messages: ModelContextExceeded(), maximum=24000)
    compressor = Transport({"cards": []})
    evidence = Evidence([("k1", "First original sentence. " * 100),
                         ("k2", "Second source sentence. " * 400)])
    eng = engine(target, compressor, evidence_context=evidence)
    out = eng._draft_section("nb", {"title": "A", "scope": "B"}, "Q", ReasoningResult(), section_index=0)
    assert out["failed"]
    assert len(target.calls) == 2
    assert len(target.calls[1][0]["content"]) < len(target.calls[0][0]["content"])
    assert receipt_for(eng)["extra_calls"] == len(compressor.calls) + 1


@pytest.mark.parametrize("limit", [1, 2])
def test_section_retry_after_empty_recovery_remains_under_shared_admission(limit):
    replies = iter([ModelContextExceeded(), {"markdown": ""}, {"markdown": "Recovered body"}])
    target = Transport(lambda messages: next(replies), maximum=9000)
    eng = engine(target)
    eng.settings.report_context_extra_calls_max = 0
    units = tuple(EvidenceUnit(f"e{i}", "evidence. " * 200, group=str(i), level=2) for i in range(4))
    render = lambda selected: [{"role": "user", "content": "Fixed contract.\n" + "\n".join(x.text for x in selected)}]
    prepared = report_context_runtime(eng).prepare("report_section", "{}", render, units, stage="draft")
    eng.settings.report_context_extra_calls_max = limit
    markdown, _, _, failure = eng._section_attempts(prepared.chat_json, "{}", report_id=None, section_index=0)
    assert len(target.calls) == 1 + limit
    assert receipt_for(eng)["extra_calls"] == limit
    if limit == 1:
        assert not markdown
        assert failure[2].code == "report_context_budget_exhausted"
    else:
        assert markdown == "Recovered body"


@pytest.mark.parametrize("all_hops_fit,compress", [(False, False), (True, False), (True, True)])
def test_projected_section_keeps_graph_semantics_only_with_their_citation_dependencies(all_hops_fit, compress):
    from app.domain.graph import ChainHop, InferredChain
    from app.services.prompts import REPORT_SECTION_SCHEMA_HINT

    hops = tuple(ChainHop(f"r{index}", "nb", "personal", source, dest, "derived_from", source, dest,
                          evidence=[{"quote": f"Direct hop {index}.", "source_id": f"source-{index}"}])
                 for index, (source, dest) in enumerate([("A", "B"), ("B", "C")], 1))
    chain = InferredChain("A", "B", "C", "A", "B", "C", "derived_from", hops,
                          validity_scope={"temperature": "25 C only"})

    class GraphEvidence(Evidence):
        def knowledge_context(self, *args, **kwargs):
            if not all_hops_fit:
                return "(none)", {}
            return entry_lines(["k1001: Graph A.", "k1002: Graph B.",
                                "relations: A --derived_from--> B"]), {
                key: {"object_type": "concept", "object_id": key, "source_id": key}
                for key in ("k1001", "k1002")}

    section = {"title": "Graph", "scope": "Explain qualified inference", "sub_queries": []}
    empty_target = Transport()
    engine(empty_target, evidence_context=Evidence([]))._draft_section(
        "nb", section, "Q", ReasoningResult(), section_index=0)
    fixed = empty_target.context_budget.input_tokens(empty_target.calls[0], REPORT_SECTION_SCHEMA_HINT)
    target = Transport()
    target.context_budget = ContextBudget(1000000, 100, fixed + (5000 if all_hops_fit else 350))
    compressor = Transport({"cards": [finding("section-0:k1", "Large source sentence.")]})
    eng = engine(target, compressor, evidence_context=GraphEvidence([("k1", "Large source sentence. " * 2000)]))
    if not compress:
        eng.settings.report_context_extra_calls_max = 0
    out = eng._draft_section("nb", section, "Q", ReasoningResult(chains=[chain]), section_index=0)
    prompt = target.calls[-1][-1]["content"]
    if all_hops_fit:
        assert {"k1001", "k1002", "k2001", "k2002"} <= set(out["id_map"])
        assert "relations: A --derived_from--> B" in prompt
        assert "path 1: [k2001] + [k2002]" in prompt
        assert "25 C only" in prompt
        assert "attach NO [k] marker" in prompt
    else:
        assert not {"k2001", "k2002"} <= set(out["id_map"])
        assert "inference 1: A" not in prompt
    stage = receipt_for(eng)["stages"]["draft"]
    assert stage["omitted_items"] >= 1 if not compress else stage["partial_items"] == 1


@pytest.fixture
def stored_report(tmp_path):
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import RecordingModelProvider
    repo = SQLiteRepository(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'db.sqlite'}",
                                    storage_dir=str(tmp_path / "storage")),
                            model_provider=RecordingModelProvider())
    nb = repo.create_notebook(NotebookCreate(name="context receipts"))
    reports = repo._runtime.report_store
    rid = reports.create_report(nb.id, "Confirmed question")
    return reports, nb.id, rid


def test_sqlite_receipt_survives_planning_to_generation_and_process_restart(stored_report):
    reports, nb, rid = stored_report
    reports.update_report(nb, rid, status="planning", understanding={"assumptions": ["fixed"]})
    eng = engine(reports=reports)
    runtime = report_context_runtime(eng, nb, rid)
    runtime.session.admit_attempt("planning_outline")
    runtime.session.admit_attempt("planning_sufficiency")
    reports.update_report(nb, rid, status="outline_ready")
    assert reports.claim_report_generation(nb, rid)
    restarted = engine(reports=reports)
    recovered = report_context_runtime(restarted, nb, rid)
    assert recovered.session.receipt()["extra_calls"] == 2
    assert set(recovered.session.receipt()["stages"]) == {"planning_outline", "planning_sufficiency"}
    recovered.session.admit_attempt("draft")
    row = reports.get_report(nb, rid)
    assert row["understanding"]["context_processing"]["extra_calls"] == 3
    assert row["understanding"]["assumptions"] == ["fixed"]
    assert row["progress"] == "正在分批整理报告材料"


@pytest.mark.parametrize("replace_understanding", [False, True])
def test_failed_report_retry_resets_receipt_but_keeps_confirmed_contract(stored_report, replace_understanding):
    reports, nb, rid = stored_report
    contract = {"assumptions": ["fixed"], "context_processing": {"extra_calls": 24}, "credibility": {"old": True}}
    reports.update_report(nb, rid, understanding=contract, status="failed")
    assert reports.claim_report_generation(nb, rid, contract if replace_understanding else None)
    row = reports.get_report(nb, rid)
    assert "context_processing" not in row["understanding"]
    assert "credibility" not in row["understanding"]
    assert row["understanding"]["assumptions"] == ["fixed"]
    assert report_context_runtime(engine(reports=reports), nb, rid).session.receipt()["extra_calls"] == 0


def test_generation_persists_compression_receipt_and_keeps_planning_count(stored_report, monkeypatch):
    reports, nb, rid = stored_report
    quote = "Measured 1.2 V at 25 C."
    reports.update_report(nb, rid, status="outline_ready",
                          outline=[{"title": "A", "scope": "B", "sub_queries": []}],
                          understanding={"context_processing": {"extra_calls": 2,
                              "stages": {"planning_outline": {"input_items": 1}}}})
    target = Transport(lambda messages: ({"summary": "A summary"}
        if "EXECUTIVE SUMMARY" in messages[-1]["content"]
        else {"markdown": "## A\nMeasured 1.2 V [k1]", "grounded": True}), maximum=9000)
    compressor = Transport({"cards": [finding("section-0:k1", quote)]})
    eng = engine(target, compressor, reports=reports,
                 evidence_context=Evidence([("k1", quote + " detail. " * 2000)]))
    monkeypatch.setattr(eng, "_deep_dive", lambda *args, **kwargs: ReasoningResult())
    eng.generate(nb, rid, "Confirmed question", depth=1)
    row = reports.get_report(nb, rid)
    assert row["status"] == "done", row.get("error")
    receipt = row["understanding"]["context_processing"]
    assert receipt["extra_calls"] == 3
    assert receipt["compressed"]
    assert receipt["stages"]["planning_outline"]["input_items"] == 1
    assert receipt["stages"]["draft"]["partial_items"] == 1
    assert "材料处理" in row["content_md"]
    assert "Measured 1.2 V" in row["sections"][0]["markdown"]


@pytest.mark.parametrize("legacy_outline", [False, True])
def test_generation_keeps_frozen_scope_and_summarizes_legacy_large_contract(
        stored_report, monkeypatch, legacy_outline):
    reports, nb, rid = stored_report
    semantic = {"objective": "Compare measured conditions", "assumptions": ["Only at 25 C"],
                "constraints": ["Do not extrapolate"], "result_scope": "ranked",
                "completeness_required": False}
    contract = {**semantic,
                "source_scope": {"mode": "include", "narrowed": True,
                                 "source_ids": [f"src-{i:016d}" for i in range(60878)]},
                "base_scope": {"mode": "include", "narrowed": True, "base_ids": ["base-only"]},
                "context_processing": {"extra_calls": 0, "stages": {}},
                "confirmed_input": {"resolved_question": "Compare measurements", "answers": []}}
    before = deepcopy(contract)
    reports.update_report(nb, rid, status="outline_ready", understanding=contract,
                          outline=[{"title": "A", "scope": "Measurements", "sub_queries": [],
                                    "intent_contract": contract if legacy_outline else semantic}])
    target = Transport(lambda messages: ({"summary": "A summary"}
        if "EXECUTIVE SUMMARY" in messages[-1]["content"]
        else {"markdown": "## A\nMeasured 1.2 V", "grounded": True}))
    eng = engine(target, reports=reports, evidence_context=Evidence([]))
    monkeypatch.setattr(eng, "_deep_dive", lambda *args, **kwargs: ReasoningResult())
    eng.generate(nb, rid, "Compare measurements", depth=1)
    row = reports.get_report(nb, rid)
    assert row["status"] == "done", row.get("error")
    assert "A summary" in row["content_md"]
    for key in ("source_scope", "base_scope", "confirmed_input"):
        assert row["understanding"][key] == before[key]
    for messages in target.calls:
        prompt = messages[-1]["content"]
        assert "source_scope" not in prompt and "base_scope" not in prompt
        assert "context_processing" not in prompt
        assert "Only at 25 C" in prompt
    assert not row["understanding"]["context_processing"]["stages"]["final_editor"].get("failed")


def test_summary_capacity_failure_keeps_finished_body_and_discloses_unfinished_stage(stored_report, monkeypatch):
    reports, nb, rid = stored_report
    reports.update_report(nb, rid, status="outline_ready",
                          outline=[{"title": "A", "scope": "B", "sub_queries": []}])
    target, summary = Transport(), Transport(maximum=1)

    class SummaryLimitedModels(Models):
        def chat(self, workload_id):
            return summary if workload_id == "report_summary" else super().chat(workload_id)

    eng = ReportEngine(_deps(reports=reports,
        model_clients=SummaryLimitedModels(target, Transport({"cards": []}))), user_id="test-owner")
    monkeypatch.setattr(eng, "_deep_dive", lambda *args, **kwargs: ReasoningResult())
    eng.generate(nb, rid, "Confirmed question", depth=1)
    row = reports.get_report(nb, rid)
    assert row["status"] == "done"
    assert row["sections"][0]["markdown"] == "## Result\nSupported [k1]"
    assert row["understanding"]["context_processing"]["stages"]["final_editor"]["failed"]
    assert "未完成的材料处理阶段：摘要与审校" in row["content_md"]
    assert not summary.calls


def test_remote_context_rejection_of_small_fixed_prompt_does_not_repeat_identical_send():
    target = Transport(lambda messages: ModelContextExceeded(), maximum=12000)
    eng = engine(target)
    messages = [{"role": "user", "content": "Immutable confirmed research question and assumptions."}]
    call = report_context_runtime(eng).prepare(
        "report_section", "{}", lambda _units: messages, stage="draft")
    with pytest.raises(ModelContextExceeded) as raised:
        call.chat_json()
    assert raised.value.code == "model_context_exceeded"
    assert target.calls == [messages]
    receipt = receipt_for(eng)
    assert receipt["extra_calls"] == 0
    assert receipt["stages"]["draft"]["failed"]


def test_report_intent_keeps_full_question_history_and_propagates_capacity_failure():
    from app.services.prompts import QUERY_INTENT_SCHEMA_HINT, query_intent_prompt
    from app.services.query_intent import plan_query_intent

    question = "Compare measured conditions. " * 400 + "QUESTION-END"
    history = "HISTORY-BEGIN " + "Earlier user constraint. " * 600 + "HISTORY-END"
    target = Transport({"normalized_question": question, "intent_type": "compare",
                        "result_scope": "ranked", "completeness_required": False,
                        "mandatory_topics": [], "ambiguities": [], "needs_clarification": False})
    eng = engine(target)
    client = ReportIntentClient(eng)
    status = {}
    plan_query_intent(client, question, history, preserve_history=True,
                     fail_on_context_error=True, status=status)
    assert status["understanding_succeeded"]
    prompt = target.calls[0][-1]["content"]
    assert question in prompt
    assert history in prompt
    # Capacity that would accommodate the old clipped history must still refuse
    # the complete confirmed input, rather than succeeding through fallback.
    clipped_messages = [{"role": "user", "content": query_intent_prompt(
        question, history_block=history[:8000], purpose="deep report")}]
    clipped_cost = target.context_budget.input_tokens(clipped_messages, QUERY_INTENT_SCHEMA_HINT)
    target.context_budget = ContextBudget(1000000, 100, clipped_cost + 100)
    with pytest.raises(ReportContextOverflow):
        eng._plan_intent_contract(question, history)
    assert len(target.calls) == 1
    receipt = receipt_for(eng)
    assert receipt["stages"]["planning_intent"]["failed"]
    assert receipt["extra_calls"] == 0


@pytest.mark.parametrize("remote_rejection", [False, True])
def test_legacy_outline_capacity_failure_never_falls_through_to_query_rewrite(remote_rejection):
    target = Transport(lambda messages: ModelContextExceeded(),
                       maximum=200000 if remote_rejection else 1)
    requested = []

    class TrackingModels(Models):
        def chat(self, workload_id):
            requested.append(workload_id)
            return super().chat(workload_id)

    eng = ReportEngine(_deps(model_clients=TrackingModels(target, Transport({"cards": []}))),
                       user_id="test-owner")
    with pytest.raises((ModelContextExceeded, ReportContextOverflow)):
        eng._plan_outline("nb", "Immutable question", "Immutable history")
    assert "query_rewrite" not in requested
    assert len(target.calls) == int(remote_rejection)
    assert receipt_for(eng)["stages"]["planning_outline"]["failed"]


@pytest.mark.parametrize("full_context_fits", [False, True])
def test_editor_never_silently_loses_middle_of_very_large_chapter(full_context_fits):
    middle = "UNIQUE-MIDDLE-RESULT: measured 1.2 V at 25 C."
    body = "Earlier sentence. " * 5000 + middle + " Later sentence." * 6000
    target = Transport({"summary": "Summary"}, maximum=300000 if full_context_fits else 9000)
    compressor = Transport({"cards": [finding("editor-section-0", middle)]}, maximum=300000)
    eng = engine(target, compressor)
    sections = [{"title": "Long chapter", "markdown": body, "claims": [], "citation_audit": {}}]
    before = deepcopy(sections)
    report_editor_call(eng, "Question", sections, None, None, "Fixed assumptions")
    assert sections == before
    prompt = target.calls[0][-1]["content"]
    assert middle in prompt
    receipt = receipt_for(eng)
    stage = receipt["stages"]["final_editor"]
    assert stage["omitted_items"] == 0
    if full_context_fits:
        assert body in prompt
        assert stage["partial_items"] == 0
        assert receipt["extra_calls"] == 0
        assert not compressor.calls
    else:
        assert stage["compressed"]
        assert stage["partial_items"] == 1
        assert receipt["extra_calls"] == 1
        assert "不代表完整覆盖" in "".join(context_disclosure(eng))


def test_editor_receives_complete_claim_when_legacy_field_projection_would_hide_its_tail():
    marker = "UNIQUE-CLAIM-CONDITION: only at 25 C, not at 80 C."
    target = Transport({"summary": "Summary"}, maximum=200000)
    compressor = Transport({"cards": []})
    eng = engine(target, compressor)
    sections = [{"title": "Measurements", "markdown": "Short chapter body.",
                 "claims": [{"claim_id": "claim-1", "statement": "Prior finding. " * 300 + marker,
                             "type": "fact", "evidence_keys": ["k1"]}], "citation_audit": {}}]
    report_editor_call(eng, "Question", sections, None, None, "Fixed assumptions")
    assert marker in target.calls[0][-1]["content"]
    assert not compressor.calls
    stage = receipt_for(eng)["stages"]["final_editor"]
    assert stage["partial_items"] == 0
    assert stage["omitted_items"] == 0


@pytest.mark.parametrize("chapter_level", [0, 1])
def test_unrelated_planning_depth_does_not_prevent_chapter_compression_with_literal_quotes(chapter_level):
    quote = "Measured 1.2 V at 25 C."
    target = Transport({"summary": "Summary"}, maximum=9000)
    compressor = Transport({"cards": [finding("editor-section-0", quote)]})
    eng = engine(target, compressor)
    from app.services.report_context_runtime import ReportContextRuntime
    eng._context_runtime = ReportContextRuntime(eng, initial_receipt={
        "max_level": 2, "extra_calls": 1,
        "stages": {"planning_outline": {"input_items": 8, "compressed": True}}})
    sections = [{"title": "Fresh chapter", "markdown": quote + " detail. " * 2000,
                 "claims": [], "citation_audit": {},
                 "context_processing": {"max_level": chapter_level}}]
    report_editor_call(eng, "Question", sections, None, None, "Fixed assumptions")
    assert len(compressor.calls) == 1
    assert quote in target.calls[0][-1]["content"]
    receipt = receipt_for(eng)
    assert receipt["extra_calls"] == 2
    assert receipt["stages"]["final_editor"]["compressed"]
    assert receipt["stages"]["final_editor"]["invalid_items"] == 0
    assert receipt["stages"]["final_editor"]["partial_items"] == 1


@pytest.mark.parametrize("claim_type", ["inference", "general"])
def test_editor_compression_inherits_claim_qualifiers_even_when_compressor_drops_them(claim_type):
    quote = "Measured 1.2 V at 25 C."
    card = finding("editor-section-0", quote)
    card.update(conditions=[], counterevidence=[], inference=False, general=False)
    target = Transport({"summary": "Summary"}, maximum=9000)
    eng = engine(target, Transport({"cards": [card]}))
    sections = [{"title": "Qualified chapter", "markdown": quote + " detail. " * 2000,
                 "claims": [{"statement": quote, "type": claim_type,
                             "conditions": ["Only under the stated baseline"],
                             "counterevidence": ["Contrary result at 80 C"]}], "citation_audit": {}}]
    report_editor_call(eng, "Question", sections, None, None, "Fixed assumptions")
    prompt = target.calls[0][-1]["content"]
    assert f'"{claim_type}": true' in prompt
    assert "Only under the stated baseline" in prompt
    assert "Contrary result at 80 C" in prompt
    assert receipt_for(eng)["stages"]["final_editor"]["compressed"]


@pytest.mark.parametrize("compress", [False, True])
def test_editor_projection_marks_only_affected_mandatory_topics_uncertain(compress):
    topics = [{"id": "required-large", "title": "完整测量"},
              {"id": "required-small", "title": "短章结论"}]
    quote = "Measured 1.2 V at 25 C."
    target = Transport({"summary": "Summary", "coverage": [
        {"intent_id": topic["id"], "covered": True} for topic in topics]}, maximum=9000)
    eng = engine(target, Transport({"cards": [finding("editor-section-0", quote)]}))
    if not compress:
        eng.settings.report_context_extra_calls_max = 0
    outline = [{"title": "Measurements", "intent_contract": {"mandatory_topics": topics}}]
    sections = [{"title": title, "scope": "Scope", "intent_ids": [topic["id"]],
                 "markdown": body, "grounded": True, "id_map": {}, "claims": [], "citation_audit": {}}
                for title, topic, body in [
                    ("Large", topics[0], quote + " detail. " * 2000),
                    ("Small", topics[1], "Short independent chapter.")]]
    content, gaps, _ = eng._assemble("nb", "rid", "Question", outline, sections)
    expected = "必答主题「完整测量」因材料容量限制，完整性尚未确认"
    assert expected in gaps
    assert expected in content
    assert not any("短章结论" in gap for gap in gaps)
    stage = receipt_for(eng)["stages"]["final_editor"]
    assert stage["partial_items"] == int(compress)
    assert stage["omitted_items"] == int(not compress)


@pytest.mark.parametrize("compress", [False, True])
def test_capacity_projection_keeps_named_mandatory_gap_even_when_editor_claims_covered(stored_report, monkeypatch, compress):
    reports, nb, rid = stored_report
    topic = {"id": "intent-measurements", "title": "测量条件与反例", "question": "Compare conditions and counterexamples"}
    reports.update_report(nb, rid, status="outline_ready", outline=[{
        "title": "Measurements", "scope": "Compare conditions", "sub_queries": [],
        "intent_ids": [topic["id"]], "intent_contract": {"mandatory_topics": [topic]}}])
    quote = "Measured 1.2 V at 25 C."
    target = Transport(lambda messages: ({"summary": "Summary", "coverage": [
        {"intent_id": topic["id"], "covered": True, "note": "All covered"}]}
        if "EXECUTIVE SUMMARY" in messages[-1]["content"]
        else {"markdown": "## Measurements\nMeasured 1.2 V [k1]", "grounded": True}), maximum=9000)
    compressor = Transport({"cards": [finding("section-0:k1", quote)]})
    rows = [("k1", quote + " detail. " * 2000)] if compress else [
        ("k1", quote), ("k2", "Oversized contradictory evidence. " * 1000)]
    eng = engine(target, compressor, reports=reports, evidence_context=Evidence(rows))
    if not compress:
        eng.settings.report_context_extra_calls_max = 0
    monkeypatch.setattr(eng, "_deep_dive", lambda *args, **kwargs: ReasoningResult())
    eng.generate(nb, rid, "Confirmed question", depth=1)
    row = reports.get_report(nb, rid)
    assert row["status"] == "done", row.get("error")
    assert row["sections"][0]["context_processing"]["coverage_limited"]
    expected = "必答主题「测量条件与反例」因材料容量限制，完整性尚未确认"
    assert expected in row["gaps"]
    assert expected in row["content_md"]


@pytest.mark.parametrize("failure", ["malformed", "provider"])
def test_generic_final_editor_failure_keeps_body_and_discloses_failed_stage(stored_report, monkeypatch, failure):
    reports, nb, rid = stored_report
    reports.update_report(nb, rid, status="outline_ready",
                          outline=[{"title": "A", "scope": "B", "sub_queries": []}])

    class FailingEditor(Transport):
        def chat_json(self, messages, schema_hint, **kwargs):
            admit_request()
            self.calls.append(deepcopy(messages))
            if failure == "provider":
                raise RuntimeError("upstream unavailable")
            return "{malformed JSON"

    target, summary = Transport(), FailingEditor()

    class EditorModels(Models):
        def chat(self, workload_id):
            return summary if workload_id == "report_summary" else super().chat(workload_id)

    eng = ReportEngine(_deps(reports=reports, model_clients=EditorModels(target, Transport())),
                       user_id="test-owner")
    monkeypatch.setattr(eng, "_deep_dive", lambda *args, **kwargs: ReasoningResult())
    eng.generate(nb, rid, "Confirmed question", depth=1)
    row = reports.get_report(nb, rid)
    assert row["status"] == "done", row.get("error")
    assert row["sections"][0]["markdown"] == "## Result\nSupported [k1]"
    assert row["understanding"]["context_processing"]["stages"]["final_editor"]["failed"]
    assert "未完成的材料处理阶段：摘要与审校" in row["content_md"]
