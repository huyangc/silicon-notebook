"""Service-level contract of the retrieval-only Ask (``output="evidence"``).

What an Agent gets back must be the context the answer model would have been
given -- under the same ceiling, from the same retrieval, assembled by the same
code -- and nothing about the call may be remembered as an answer: no
conversation, no answer row, no completion hook, no bell push.  The pure
splitting/anchoring helpers are covered by ``test_ask_evidence.py``; this file
drives ``RepositoryFacade.ask(..., output="evidence")`` and the AskService
seams behind it on a real SQLite repository.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.ask import (
    AskEvidence,
    AskResponse,
    ModelError,
    StructuredBatchCoverage,
)
from app.models.schemas import AskRequest, NotebookCreate
from app.services.ask_service import (
    EVIDENCE_OUTPUT_MODE_REFUSAL,
    AskService,
)
from app.services.cancellation import AskCancelled
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import (
    RecordingModelProvider,
    bind_all_embedding_clients,
    bind_chat_client,
)


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _synthetic_elements_are_not_dangling(monkeypatch):
    """The reasoning cases cite synthetic element ids with no
    ``source_elements`` row; J2's liveness pass is not what this file is
    about (same opt-out as ``test_reasoning_chunk_citations``)."""
    monkeypatch.setattr(
        AskService, "_drop_dangling_references", lambda self, response: None)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    result = SQLiteRepository(
        Settings(query_rewrite_enabled=False, kg_query_refine_enabled=False),
        model_provider=RecordingModelProvider(),
    )
    bind_all_embedding_clients(result, FakeEmbedder(dim=16))
    yield result
    result.close()


class _Recorder:
    """A configured chat client that records every call.

    ``answer`` drives the synthesis reply; plan / reflect schemas get the
    minimal shapes the reasoning loop needs; the follow-up rewrite answers
    ``rewritten``."""

    configured = True
    model = "fake"

    def __init__(self, answer: str = "Synthesized [k1].", rewritten: str = ""):
        self.answer = answer
        self.rewritten = rewritten
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[-1]["content"]
        self.calls.append((prompt, schema_hint))
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "增益"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if "relevant" in schema_hint:
            return json.dumps({"relevant": []})
        if '"query"' in schema_hint:
            return json.dumps({"query": self.rewritten or "unchanged"})
        return json.dumps({"answer": self.answer, "grounded": True})


def _seed_chunks(repo, texts, *, name="nb"):
    """Notebook + one source whose elements go through the real chunk+embed
    path (``test_chunk_retrieval_characterization._seed_chunks``)."""
    notebook = repo.create_notebook(NotebookCreate(name=name))
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook.id, "Doc", "document", "s.md", "/tmp/s.md", 0, "h",
             "", "", "extracted", now, now))
        for index, text in enumerate(texts, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{index:04d}", sid, "paragraph", f"p{index}", text,
                 "{}", now))
    repo._chunk_and_embed_source(sid)
    return notebook


_TEXTS = [
    "Mixture of experts routes each token to a few experts.",
    "The router is trained with a load balancing loss.",
    "Expert capacity limits how many tokens one expert takes.",
]


def _rows(repo, sql, params=()):
    with repo._connect() as db:
        return [dict(row) for row in db.execute(sql, params).fetchall()]


def _baseline_spy(monkeypatch):
    """Capture what the answer path hands its baseline manifest: the exact
    context and id_map the answer model received."""
    from app.services import retrieval_baseline

    seen: list[dict] = []
    real = retrieval_baseline.build_retrieval_baseline_manifest

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(retrieval_baseline, "build_retrieval_baseline_manifest", spy)
    return seen


def _evidence(repo, notebook_id, question, mode="chunk", **extra):
    return repo.ask(
        notebook_id, AskRequest(question=question, mode=mode, **extra),
        submitted_via="mcp", output="evidence",
    )


def _assert_items_are_the_context(evidence: AskEvidence, context_block: str, id_map):
    assert evidence.context_chars == len(context_block)
    assert [item.key for item in evidence.items if item.key] == [
        key for key in id_map if f"\n{key}: " in f"\n{context_block}"
    ]
    for item in evidence.items:
        assert item.text and item.text in context_block
        if item.key:
            assert f"{item.key}: {item.text}" in context_block
            assert item.anchor is not None
            assert item.anchor.object_id == id_map[item.key]["object_id"]


# --------------------------------------------------------------------------
# chunk
# --------------------------------------------------------------------------


def test_chunk_evidence_is_the_context_the_answer_was_given(repo, monkeypatch):
    notebook = _seed_chunks(repo, _TEXTS)
    seen = _baseline_spy(monkeypatch)
    answer_llm = _Recorder()
    bind_chat_client(repo, "ask_answer", answer_llm)
    answered = repo.ask(notebook.id, AskRequest(question="expert routing", mode="chunk"))
    assert isinstance(answered, AskResponse) and answered.answer_id
    baseline = seen[-1]
    assert baseline["final_context_block"]

    evidence_llm = _Recorder()
    bind_chat_client(repo, "ask_answer", evidence_llm)
    evidence = _evidence(repo, notebook.id, "expert routing")

    assert isinstance(evidence, AskEvidence)
    assert evidence_llm.calls == [], "evidence never calls the answer model"
    assert evidence.mode == "chunk" and evidence.evidence_kind == "retrieval"
    _assert_items_are_the_context(
        evidence, baseline["final_context_block"], baseline["final_id_map"])
    assert evidence.budget_chars == baseline["final_budget_chars"]
    assert evidence.budget_chars == repo.settings.chunk_answer_budget_chars
    assert evidence.counts.delivered == len(baseline["final_id_map"])
    assert evidence.counts.by_kind["chunk"].delivered == evidence.counts.delivered
    assert evidence.counts.recalled >= evidence.counts.selected >= evidence.counts.delivered
    assert evidence.retrieval_query == "expert routing"
    # Anchors come from the answer path's own parser: same handle fields.
    chunk_items = [item for item in evidence.items if item.kind == "chunk"]
    assert chunk_items and all(item.anchor.object_type == "chunk" for item in chunk_items)
    assert all(item.relevance is not None for item in chunk_items)


def test_mix_evidence_matches_the_mix_synthesis_context(repo):
    """``_answer_mix`` and the evidence path read one assembly function."""
    service = repo._runtime.ask_component
    chunks = [
        RetrievedChunk("c1", "s1", "Doc", "§1", "first passage", relevance=0.9),
        RetrievedChunk("c2", "s1", "Doc", "§2", "second passage", relevance=0.8),
    ]
    kg_map = {"k1001": {"object_id": "n1", "object_type": "concept", "name": "N1"}}
    sink: dict = {}
    service._answer_mix(
        "q", chunks, "k1001: node one", kg_map, notebook_id="nb",
        llm_client=_Recorder(), baseline_sink=sink,
    )
    block, id_map, budget = service._chunk_synthesis_context(
        chunks, "k1001: node one", kg_map, [], overlay_on=True, notebook_id="nb")
    assert (block, id_map, budget) == (
        sink["context_block"], sink["id_map"], sink["budget_chars"])
    assert "[Knowledge graph]\nk1001: node one" in block


def test_chunk_evidence_records_a_job_and_nothing_else(repo, monkeypatch):
    notebook = _seed_chunks(repo, _TEXTS)
    notified, pushed = [], []
    monkeypatch.setattr(
        AskService, "_note_ask_completed",
        lambda self, *args: notified.append(args))
    from app.services import ask_service as module

    monkeypatch.setattr(module, "publish_snapshot", lambda user_id: pushed.append(user_id))
    bind_chat_client(repo, "ask_answer", _Recorder())

    evidence = _evidence(repo, notebook.id, "expert routing")

    assert evidence.items
    assert notified == [] and pushed == []
    assert _rows(repo, "SELECT id FROM conversations WHERE notebook_id=?", (notebook.id,)) == []
    assert _rows(repo, "SELECT id FROM answers") == []
    jobs = _rows(
        repo,
        "SELECT status, answer_id, conversation_id, output, submitted_via, mode "
        "FROM ask_jobs WHERE notebook_id=?", (notebook.id,))
    assert jobs == [{
        "status": "done", "answer_id": "", "conversation_id": "",
        "output": "evidence", "submitted_via": "mcp", "mode": "chunk",
    }]


def test_an_empty_notebook_returns_none_with_the_chunk_sentence(repo):
    notebook = repo.create_notebook(NotebookCreate(name="empty"))
    bind_chat_client(repo, "ask_answer", _Recorder())

    evidence = _evidence(repo, notebook.id, "anything at all")

    assert evidence.evidence_kind == "none"
    assert evidence.items == []
    assert evidence.notice.startswith("No indexed content matches this question yet.")


def test_document_overview_returns_its_own_prepared_evidence(repo):
    notebook = _seed_chunks(repo, _TEXTS)
    answer_llm = _Recorder()
    bind_chat_client(repo, "ask_answer", answer_llm)

    evidence = _evidence(repo, notebook.id, "介绍一下这个notebook中的文章")

    assert evidence.evidence_kind == "document_overview"
    assert answer_llm.calls == []
    assert evidence.items or evidence.notice
    assert _rows(repo, "SELECT id FROM answers") == []


def test_an_extension_mode_is_refused_before_any_job(repo, monkeypatch):
    notebook = _seed_chunks(repo, _TEXTS)
    from types import SimpleNamespace

    monkeypatch.setattr(
        AskService, "_resolve_ask_mode",
        lambda self, mode: SimpleNamespace(
            id="ext.engine", handler="ask_plugin_engine", streaming=True),
    )
    with pytest.raises(ValueError) as caught:
        _evidence(repo, notebook.id, "q", mode="ext.engine")
    assert str(caught.value) == EVIDENCE_OUTPUT_MODE_REFUSAL
    assert _rows(repo, "SELECT id FROM ask_jobs") == []
    # The engine-level entry refuses as well (no lifecycle around it).
    service = repo._runtime.ask_component
    with pytest.raises(ValueError, match="只支持"):
        service.ask_evidence(
            notebook.id, AskRequest(question="q", mode="ext.engine"), user_id="u")


def test_an_unknown_output_value_is_refused(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(ValueError, match="output must be one of"):
        repo.ask(notebook.id, AskRequest(question="q", mode="chunk"), output="both")


@pytest.mark.parametrize("raised, status", [
    (AskCancelled(), "cancelled"),
    (RuntimeError("boom"), "failed"),
])
def test_cancel_and_failure_close_the_job(repo, monkeypatch, raised, status):
    notebook = _seed_chunks(repo, _TEXTS)

    def _raise(self, *args, **kwargs):
        raise raised

    monkeypatch.setattr(AskService, "ask_evidence", _raise)
    with pytest.raises(type(raised)):
        _evidence(repo, notebook.id, "q")
    jobs = _rows(repo, "SELECT status, output FROM ask_jobs")
    assert jobs == [{"status": status, "output": "evidence"}]
    assert _rows(repo, "SELECT id FROM conversations") == []


# --------------------------------------------------------------------------
# D5: conversation_id is read, never written
# --------------------------------------------------------------------------


def test_an_owned_conversation_feeds_the_followup_rewrite_and_is_not_appended(repo):
    notebook = _seed_chunks(repo, _TEXTS)
    bind_chat_client(repo, "ask_answer", _Recorder())
    bind_chat_client(repo, "query_rewrite", _Recorder(rewritten="expert capacity"))
    first = repo.ask(
        notebook.id, AskRequest(question="What limits an expert?", mode="chunk"))
    conversation_id = first.conversation_id
    before = _rows(
        repo, "SELECT updated_at FROM conversations WHERE id=?", (conversation_id,))
    rewrite = _Recorder(rewritten="expert capacity limit")
    bind_chat_client(repo, "query_rewrite", rewrite)
    answer_llm = _Recorder()
    bind_chat_client(repo, "ask_answer", answer_llm)

    evidence = _evidence(
        repo, notebook.id, "and how is it trained?", conversation_id=conversation_id)

    # The history was read: the rewrite saw the prior question and the
    # retrieval ran on its rewrite.
    assert any("User: What limits an expert?" in prompt for prompt, _ in rewrite.calls)
    assert evidence.retrieval_query == "expert capacity limit"
    assert evidence.conversation_id == conversation_id
    assert answer_llm.calls == []
    # ...and nothing was written to it.
    assert _rows(
        repo, "SELECT COUNT(*) AS n FROM answers WHERE conversation_id=?",
        (conversation_id,)) == [{"n": 1}]
    assert _rows(
        repo, "SELECT updated_at FROM conversations WHERE id=?",
        (conversation_id,)) == before
    assert _rows(
        repo, "SELECT conversation_id FROM ask_jobs WHERE output='evidence'"
    ) == [{"conversation_id": ""}]


@pytest.mark.parametrize("foreign", ["someone-else", "missing"])
def test_a_foreign_or_unknown_conversation_reads_as_no_history(repo, foreign):
    notebook = _seed_chunks(repo, _TEXTS)
    conversation_id = f"conv-{uuid.uuid4().hex[:8]}"
    if foreign == "someone-else":
        now = _now()
        with repo._write() as db:
            db.execute(
                "INSERT INTO conversations (id, notebook_id, title, created_by, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (conversation_id, notebook.id, "t", "another-user", now, now))
            db.execute(
                "INSERT INTO answers (id, conversation_id, notebook_id, question, "
                "payload, created_at) VALUES (?,?,?,?,?,?)",
                ("ans-x", conversation_id, notebook.id, "Their secret question",
                 json.dumps({"conclusion": "their answer"}), now))
    rewrite = _Recorder(rewritten="should not be used")
    bind_chat_client(repo, "query_rewrite", rewrite)
    bind_chat_client(repo, "ask_answer", _Recorder())

    evidence = _evidence(
        repo, notebook.id, "expert routing", conversation_id=conversation_id)

    assert evidence.conversation_id == ""
    assert evidence.retrieval_query == "expert routing"
    assert not any("secret" in prompt for prompt, _ in rewrite.calls)
    assert evidence.items


# --------------------------------------------------------------------------
# reasoning
# --------------------------------------------------------------------------


def _reasoning_chunk(index: int, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{index}", source_id=f"src-{index}",
        source_title=f"手册-{index}", section_path=f"§{index} 增益", text=text,
        element_ids=[f"el-{index}-a"], score=1.0, relevance=1.0 - index * 0.1,
    )


def _patch_reasoning_retrieval(monkeypatch, chunks):
    from app.application.ask_reasoning import ReasoningEvidenceSnapshot
    from app.services.reasoning_retrieval import ReasoningRetriever

    def _run_stage(self, stage, runtime):
        return ReasoningEvidenceSnapshot(
            top_hits=(), elements=(), trace=(), chunks=tuple(chunks), chains=(),
            attempted=(), enumerations=(), collection_map_text="",
            outline=(), outline_evidence=(), baseline_manifest=None,
            external_evidence=(),
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _run_stage)


def _seed_kg(repo):
    """One knowledge object so the "no graph and no source" short circuit
    does not fire (``test_reasoning_chunk_citations._seed``)."""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "增益概述", "section_path": "1"}, "evidence": []},
    ], [])
    return notebook


def test_reasoning_evidence_is_the_synthesis_context_without_refine(repo, monkeypatch):
    notebook = _seed_kg(repo)
    repo.settings.kg_query_refine_enabled = True
    _patch_reasoning_retrieval(monkeypatch, [
        _reasoning_chunk(1, "源极负反馈让增益稳定。"),
        _reasoning_chunk(2, "温度漂移对增益的影响有限。"),
    ])
    seen = _baseline_spy(monkeypatch)
    answer = _Recorder("增益稳定 [k1]。")
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, answer)
    repo.ask(notebook.id, AskRequest(question="增益", mode="reasoning"))
    baseline = [kw for kw in seen if kw.get("mode") == "reasoning"][-1]
    # The answer refined (one evidence_refine call, which returned nothing to
    # prepend), so its context is the unrefined assembly.
    assert any("relevant" in hint for _, hint in answer.calls)

    refine, synth = _Recorder(), _Recorder()
    bind_chat_client(repo, "evidence_refine", refine)
    bind_chat_client(repo, "ask_answer", synth)
    consulted = []
    monkeypatch.setattr(
        AskService, "_consult_gap_sources",
        lambda self, *args, **kwargs: consulted.append(1) or ((), None))

    evidence = _evidence(repo, notebook.id, "增益", mode="reasoning")

    assert refine.calls == [] and synth.calls == [] and consulted == []
    assert evidence.mode == "reasoning" and evidence.evidence_kind == "retrieval"
    _assert_items_are_the_context(
        evidence, baseline["final_context_block"], baseline["final_id_map"])
    assert evidence.budget_chars == baseline["final_budget_chars"]
    assert [item.anchor.object_id for item in evidence.items] == ["chunk-1", "chunk-2"]
    assert evidence.counts.by_kind["chunk"].delivered == 2
    assert evidence.intent is not None
    assert _rows(repo, "SELECT COUNT(*) AS n FROM answers") == [{"n": 1}]
    job = _rows(repo, "SELECT id FROM ask_jobs WHERE output='evidence'")[0]["id"]
    steps = [
        json.loads(row["step_json"])
        for row in _rows(
            repo, "SELECT step_json FROM ask_trace_steps WHERE job_id=? ORDER BY seq",
            (job,))
    ]
    assert steps[-1]["step_type"] == "skip"
    assert steps[-1]["summary"].startswith("仅检索：已返回 2 条证据")
    assert not any(step["step_type"] == "synthesis" for step in steps)


def test_reasoning_with_no_evidence_returns_none(repo, monkeypatch):
    notebook = _seed_kg(repo)
    _patch_reasoning_retrieval(monkeypatch, [])
    for workload in ("reasoning_agent", "ask_answer"):
        bind_chat_client(repo, workload, _Recorder())

    evidence = _evidence(repo, notebook.id, "增益", mode="reasoning")

    assert evidence.evidence_kind == "none" and evidence.items == []
    assert evidence.notice.startswith("当前检索没有找到足以支撑回答的来源证据")


def test_reasoning_short_circuit_no_source_in_scope(repo):
    notebook = repo.create_notebook(NotebookCreate(name="empty"))
    for workload in ("reasoning_agent", "ask_answer"):
        bind_chat_client(repo, workload, _Recorder())

    evidence = _evidence(repo, notebook.id, "增益", mode="reasoning")

    assert evidence.evidence_kind == "none"
    assert "当前笔记本没有可检索的来源" in evidence.notice
    assert _rows(repo, "SELECT id FROM answers") == []


def test_reasoning_short_circuit_unconfigured_answer_model(repo, monkeypatch):
    notebook = _seed_kg(repo)
    monkeypatch.setattr(AskService, "_primary_llm_unconfigured", lambda self: True)

    evidence = _evidence(repo, notebook.id, "增益", mode="reasoning")

    assert evidence.evidence_kind == "none" and evidence.notice
    assert [error.message for error in evidence.model_errors] == ["missing_config"]
    assert _rows(repo, "SELECT id FROM answers") == []


def test_structured_enumeration_short_circuit_returns_the_rendered_result():
    from types import SimpleNamespace

    response = AskResponse(
        conclusion="完整枚举 3 行", answer="| a |\n| b |\n| c |",
        llm_mode="structured", retrieval_query="list all",
        result_coverage=StructuredBatchCoverage(
            known_total_rows=3, returned_rows=3, complete=True),
    )
    evidence = AskService._short_circuit_evidence(
        response, SimpleNamespace(conversation_id=""))
    assert evidence.evidence_kind == "structured_enumeration"
    assert [(item.kind, item.text) for item in evidence.items] == [
        ("context", "| a |\n| b |\n| c |")]
    assert (evidence.counts.recalled, evidence.counts.delivered,
            evidence.counts.omitted) == (3, 3, 0)

    unconfigured = response.model_copy(update={
        "model_errors": [ModelError(stage="answer", model="", message="missing_config")],
    })
    assert AskService._short_circuit_evidence(
        unconfigured, SimpleNamespace(conversation_id="")
    ).evidence_kind == "none"


def test_reasoning_reads_an_owned_conversation_and_does_not_append(repo, monkeypatch):
    notebook = _seed_kg(repo)
    _patch_reasoning_retrieval(monkeypatch, [_reasoning_chunk(1, "源极负反馈让增益稳定。")])
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, _Recorder("增益稳定 [k1]。"))
    first = repo.ask(notebook.id, AskRequest(question="增益为什么稳定", mode="reasoning"))
    histories = []
    real = AskService._prepare_reasoning_ask

    def spy(self, *args, **kwargs):
        prepared = real(self, *args, **kwargs)
        histories.append(prepared.history)
        return prepared

    monkeypatch.setattr(AskService, "_prepare_reasoning_ask", spy)

    evidence = _evidence(
        repo, notebook.id, "增益", mode="reasoning",
        conversation_id=first.conversation_id)

    assert evidence.conversation_id == first.conversation_id
    assert histories and "User: 增益为什么稳定" in histories[-1]
    assert _rows(repo, "SELECT COUNT(*) AS n FROM answers") == [{"n": 1}]
