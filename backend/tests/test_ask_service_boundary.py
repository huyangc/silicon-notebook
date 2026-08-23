"""Task 24 contract: the Ask mode engines + synthesis live in
app.services.ask_service.AskService — ONE lazily-composed runtime service over
narrow ports (ask-state / retrieval / evidence-context / model clients /
communities / scale profile), never over the facade.

Frozen here (the RED items of the move):

1. non-streaming ``repo.ask()`` uses the same durable-job and atomic-final-save
   lifecycle as streaming, while keeping the job id out of its response;
2. the runtime owns ONE AskService (identity-stable across resolutions) and
   the facade's frozen ``ask_chunk``/``ask_reasoning``/``ask_graph``
   signatures adapt ``current_user().id`` into the service's keyword-only
   ``user_id``;
3. system workload clients resolve through the service's model-client port and
   remain independent of request-user context;
4. module boundary: ask_service.py never imports the facade or the runtime,
   never opens private DB seams and never reads the request ContextVar
   directly (persistence identity is explicit ``user_id``; model identity
   rides the injected provider).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, AskResponse, NotebookCreate
from app.services.ask_service import AskService
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
import asyncio
import queue
from types import SimpleNamespace
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_non_streaming_ask_uses_hidden_terminal_job(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    response = repo.ask(nb.id, AskRequest(question="q", mode="chunk"))

    assert response.answer_id                       # 答案照存(_save_answer 收口)
    with repo._connect() as db:
        jobs = db.execute(
            "SELECT status,answer_id FROM ask_jobs"
        ).fetchall()
    assert [(row["status"], row["answer_id"]) for row in jobs] == [
        ("done", response.answer_id)
    ]
    assert "job_id" not in response.model_dump()
    assert repo.get_conversation(response.conversation_id).active_job is None


def test_runtime_owns_one_ask_service_and_facade_adapts_identity(repo, monkeypatch):
    service = repo._runtime.ask_service()
    assert isinstance(service, AskService)
    assert service is repo._runtime.ask_service()   # one owner, identity-stable

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    seen: dict = {}

    def fake_chunk(
        notebook_id, payload, *, user_id, job_id="", cancel_event=None
    ):
        seen["args"] = (notebook_id, user_id, job_id, cancel_event)
        return AskResponse(conclusion="stubbed")

    monkeypatch.setattr(service, "ask_chunk", fake_chunk, raising=False)
    out = repo.ask_chunk(nb.id, AskRequest(question="q", mode="chunk"))

    assert out.conclusion == "stubbed"
    assert seen["args"] == (nb.id, repo.current_user().id, "", None)


def test_system_model_provider_is_independent_of_request_user_context(repo):
    service = repo._runtime.ask_service()
    assert service.model_clients is repo._runtime.models
    assert service.model_clients.chat("ask_answer") is repo._runtime.models.chat(
        "ask_answer"
    )


def test_ask_service_module_never_imports_facade_or_private_db():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ask_service.py"
    ).read_text(encoding="utf-8")
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert not any(m.startswith("app.services.sqlite_repository") for m in modules)
    assert not any(m.startswith("app.services.repository_runtime") for m in modules)
    assert "sqlite3" not in modules
    # 持久化身份显式(user_id 由调用方传入);模型身份走注入的 provider —— 本模块
    # 绝不直接碰请求 ContextVar,也绝不开私有 DB 缝。
    assert "_REQUEST_USER" not in source
    assert "._connect(" not in source and "._write(" not in source


class _MinimalAskState:
    def prepare_turn(self, notebook_id, conversation_id, question, user_id):
        return SimpleNamespace(conversation_id="conv", history="")

    def save_answer(self, notebook_id, conversation_id, question, response, user_id):
        return "answer-1"


class _MinimalModels:
    _chat_client = SimpleNamespace(configured=False, model="")

    class _Reranker:
        configured = True

        def rerank(self, query, documents, *, on_error=None):
            return list(range(len(documents)))

    _reranker = _Reranker()

    def configured(self, workload_id: str) -> bool:
        return True

    def chat(self, workload_id: str):
        return self._chat_client

    def rerank(self, workload_id: str):
        assert workload_id == "retrieval_rerank"
        return self._reranker

    def primary_unconfigured(self) -> bool:
        return False


class _MinimalCandidates:
    def notebook_languages(self, notebook_id):
        return ["en"]

    def chunk_plan(self, notebook_id, queries):
        return SimpleNamespace(
            strategy="mix", overlay_on=True, mmr_k=8, mmr_lambda=0.7,
            fuse_k=8,
        )

    def keyword_chunk_candidates(self, notebook_id, keywords):
        return []

    def exact_lookup_chunks(self, notebook_id, query):
        return []

    def retrieve_chunk_candidates(self, notebook_id, query):
        return [], [], []

    def retrieve_chunk_candidates_multi(self, notebook_id, queries):
        return {}, [], [], None

    def mixed_chunk_candidates(self, notebook_id, query, high_level, queries):
        return [RetrievedChunk(
            chunk_id="chunk-1", source_id="source-1", source_title="Source",
            section_path="§1", text="evidence", element_ids=["element-1"],
            relevance=0.9,
        )], "", {}, [], 0

    def merge_chunk_candidates(self, base, extra):
        return base

    def select_chunk_candidates(self, scored, ids, matrix, k, lambda_):
        return []

    def has_kg(self, notebook_id):
        return True

    def any_base_has_kg(self, notebook_id):
        return False

    def graph_is_large(self, notebook_id):
        return False

    def fuse_graph_seeds(self, notebook_id, question, seeds, cancel_event):
        return seeds


class _MinimalGraph:
    def federated_graph(self, notebook_id):
        from app.services.kg.graph_reason import build_rx_graph

        return build_rx_graph(
            {"ko-1": {"type": "concept", "name": "one"}}, []
        )

    def source_chunks(self, notebook_id, object_ids):
        return []


class _MinimalRetrieval:
    def federated_retrieve(self, notebook_id, query, **kwargs):
        return [RetrievedKnowledge(
            object_id="ko-1", notebook_id=notebook_id, object_type="concept",
            payload={"name": "one"}, status="approved", score=1.0,
            relevance=1.0, evidence=[],
        )]

    def ppr_retrieve(self, notebook_id, question):
        return []


class _MinimalEvidence:
    def tier_map(self, notebook_ids):
        return {}

    def source_metadata(self, source_ids):
        return {}

    def citation_titles(self, source_ids):
        return {}

    def citation_source_info(self, source_ids):
        return {}

    def citations_from(self, hits, element_ids, label, notebook_id=""):
        return []

    def truncate_kg_block(self, block, max_tokens):
        return block

    def parse_anchors(self, answer, evidence_by_id):
        return []

    def knowhow_refs_for(self, element_ids):
        # Task 12b: the four chunk/graph inline Citation(...) sites now call
        # this once per answer to enrich .knowhow — a newly-declared port
        # call this minimal boundary fixture must implement to stay reachable.
        return {}

    def attach_citation_images(self, targets):
        # T1（检索结果带图）: the same four assembly points now also call this
        # once per answer to fill Citation/AnswerAnchor.images. Mirrors the
        # knowhow_refs_for entry above — a newly-declared port call this
        # minimal boundary fixture must implement to stay reachable.
        return None


def _minimal_ask_service(**overrides):
    """Narrow AskService double.  ``overrides`` reaches the real constructor so
    a caller can exercise a construction-time seam (the injectable
    ``response_draft_stage``, a connection probe) without a second factory."""
    return AskService(
        ask_state=_MinimalAskState(), retrieval=_MinimalRetrieval(),
        candidates=_MinimalCandidates(), graph=_MinimalGraph(),
        evidence_context=_MinimalEvidence(), model_clients=_MinimalModels(),
        model_errors=SimpleNamespace(note_model_error=lambda *args, **kwargs: None),
        communities=lambda: SimpleNamespace(),
        scale_profiles=lambda: SimpleNamespace(requires_index=lambda *args, **kwargs: False),
        scale_index_probe=lambda notebook_id: False,
        settings=Settings(query_rewrite_enabled=False, graph_ppr_enabled=False),
        event_log=SimpleNamespace(emit=lambda event: None),
        notebooks=SimpleNamespace(get_notebook=lambda notebook_id: object()),
        schemas=SimpleNamespace(effective_schemas=lambda: {}),
        community_reports=lambda notebook_id: [], source_titles=lambda ids: {},
        **overrides,
    )


def test_chunk_and_graph_ask_execute_on_declared_ports_only():
    service = _minimal_ask_service()

    chunk = service.ask_chunk("nb", AskRequest(question="q"), user_id="user")
    graph = service.ask_graph(
        "nb", AskRequest(question="q", mode="graph"), user_id="user"
    )

    assert chunk.answer_id == "answer-1"
    assert graph.answer_id == "answer-1"
    assert chunk.conclusion == "Retrieved 1 relevant passage(s) for this question."
    assert "Graph traversal found 1 node(s)" in graph.conclusion
    assert not hasattr(service.retrieval, "candidates")
    assert not hasattr(service.retrieval, "graph")
    assert not hasattr(service.model_clients, "identity")


class _EnumerableKnowhow:
    def __init__(self):
        self.catalog_calls = 0

    def list_knowhow_tables(self, notebook_id):
        raise AssertionError("structured Ask must use the lightweight catalog")

    def knowhow_enumeration_catalog(self, notebook_id, *, limit=8, query=""):
        self.catalog_calls += 1
        return {
            "tables": [{"id": "table-1", "title": "方法表", "row_count": 100}],
            "known_tables": 1,
            "known_total_rows": 100,
            "fingerprint": [1, 100, 1, 0],
        }

    def enumerate_knowhow_rows(
        self, notebook_id, *, table_ids, cursor=None, page_size=25, column_ids=None
    ):
        start = int((cursor or {}).get("position", -1)) + 1
        raw_rows = [
            {
                "id": f"row-{index}",
                "table_id": "table-1",
                "position": index,
                "cells": (
                    {"method": f"方法 {index}"}
                    if column_ids != [] else {}
                ),
            }
            for index in range(start, min(start + page_size, 100))
        ]
        has_more = start + len(raw_rows) < 100
        return {
            "tables": [{
                "id": "table-1",
                "title": "方法表",
                "description": "",
                "mutation_seq": 1,
                "row_count": 100,
                "columns": [{
                    "id": "method", "name": "方法", "role": "anchor", "position": 0,
                }],
            }],
            "rows": raw_rows,
            "next_cursor": (
                {
                    "table_id": "table-1",
                    "position": raw_rows[-1]["position"],
                    "id": raw_rows[-1]["id"],
                }
                if has_more else None
            ),
            "has_more": has_more,
            "counts": {
                "scope_total_rows": 100,
                "scope_nonempty_rows": 100,
                "page_rows": len(raw_rows),
            },
        }


def test_reasoning_complete_knowhow_bypasses_ranked_top_n_and_model():
    service = _minimal_ask_service()
    service.knowhow_store = _EnumerableKnowhow()

    response = service.ask_reasoning(
        "nb",
        AskRequest(
            question="所有方法有哪些？",
            mode="reasoning",
            retrieval_effort="overview",
        ),
        user_id="user",
    )

    assert response.llm_mode == "structured"
    assert response.retrieval_effort == "overview"
    assert response.result_sets[0].coverage.complete is True
    assert response.result_sets[0].coverage.returned_rows == 100
    assert len(response.result_sets[0].rows) == 100
    assert "100/100" in response.conclusion
    assert response.result_coverage is not None
    assert response.result_coverage.complete is True
    assert service.knowhow_store.catalog_calls == 2


def test_reasoning_hybrid_without_kg_keeps_batch_coverage_metadata():
    service = _minimal_ask_service()
    service.knowhow_store = _EnumerableKnowhow()
    service.candidates.has_kg = lambda notebook_id: False
    service.candidates.any_base_has_kg = lambda notebook_id: False

    response = service.ask_reasoning(
        "nb",
        AskRequest(
            question="列出所有方法并比较优缺点",
            mode="reasoning",
        ),
        user_id="user",
    )

    assert response.result_sets[0].coverage.complete is True
    assert response.result_coverage is not None
    assert response.result_coverage.complete is True
    assert response.result_coverage.returned_rows == 100
    assert "尚未构建知识图谱" in response.conclusion


def test_reasoning_conditional_complete_query_does_not_claim_full_table():
    service = _minimal_ask_service()
    service.knowhow_store = _EnumerableKnowhow()
    service.model_clients.primary_unconfigured = lambda: True

    response = service.ask_reasoning(
        "nb",
        AskRequest(
            question="列出所有低功耗方法",
            mode="reasoning",
        ),
        user_id="user",
    )

    assert response.result_sets == []
    assert response.result_coverage is None
    assert "不能视为全部结果" in response.conclusion
    assert service.knowhow_store.catalog_calls == 1


def test_reasoning_renders_federated_hits_with_their_owning_schema(
    monkeypatch,
):
    from app.services.extraction_profiles import ObjectSchema
    from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever

    hits = [
        RetrievedKnowledge(
            object_id="ko-active", notebook_id="nb-active",
            object_type="lab_recipe",
            payload={"active_field": "a", "base_field": "b"},
            status="approved", score=1.0, relevance=1.0, evidence=[],
        ),
        RetrievedKnowledge(
            object_id="ko-base", notebook_id="nb-base",
            object_type="lab_recipe",
            payload={"active_field": "a", "base_field": "b"},
            status="approved", score=0.9, relevance=0.9, evidence=[],
        ),
    ]
    monkeypatch.setattr(
        ReasoningRetriever,
        "run",
        lambda self, *args, **kwargs: ReasoningResult(top_hits=hits),
    )

    calls: list[str] = []
    registries = {
        "nb-active": {
            "lab_recipe": ObjectSchema(
                type="lab_recipe", plural="lab_recipes",
                fields=["active_field"], primary="active_field",
                description="", list_fields=[],
            )
        },
        "nb-base": {
            "lab_recipe": ObjectSchema(
                type="lab_recipe", plural="lab_recipes",
                fields=["base_field"], primary="base_field",
                description="", list_fields=[],
            )
        },
    }

    class Schemas:
        def effective_schemas(self, notebook_id):
            calls.append(notebook_id)
            return registries[notebook_id]

    service = _minimal_ask_service()
    service.schemas = Schemas()
    service._activate_selected_source_graph = (
        lambda *args, **kwargs: ([], None)
    )
    response = service.ask_reasoning(
        "nb-active",
        AskRequest(
            question="How do the active and mounted base lab recipes differ?",
            mode="reasoning",
        ),
        user_id="user",
    )

    records = {item.id: item for item in response.related_knowledge}
    assert set(calls) == {"nb-active", "nb-base"}
    assert [field.key for field in records["ko-active"].fields] == [
        "active_field", "base_field",
    ]
    assert [field.key for field in records["ko-base"].fields] == [
        "base_field", "active_field",
    ]


def test_reasoning_retriever_failure_returns_a_final_degraded_answer(monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever

    def fail_run(self, *args, **kwargs):
        raise RuntimeError("retriever failed")

    monkeypatch.setattr(ReasoningRetriever, "run", fail_run)
    service = _minimal_ask_service()

    response = service.ask_reasoning(
        "nb",
        AskRequest(question="What happened?", mode="reasoning"),
        user_id="user",
    )

    assert response.answer_id == "answer-1"
    assert response.mode == "reasoning"
    assert response.llm_mode == "deterministic"
    assert response.conclusion


def test_stream_route_helper_uses_ask_stream_port_without_runtime():
    from app.api.ask_routes import _stream_ask_events
    from app.services.ask_modes import resolve_mode

    class MinimalAskStream:
        def __init__(self):
            self.started = None

        def current_user(self):
            return SimpleNamespace(id="user-1")

        def start_ask_stream(self, notebook_id, payload, mode, *, user_id):
            self.started = (notebook_id, payload.question, mode.id, user_id)
            events = queue.Queue()
            events.put({"type": "started", "job_id": "job-1"})
            events.put(None)
            return events

    class Request:
        async def is_disconnected(self):
            return False

    async def collect(repo):
        return [line async for line in _stream_ask_events(
            repo, "nb", AskRequest(question="q"), resolve_mode("chunk"), Request()
        )]

    repo = MinimalAskStream()
    lines = asyncio.run(collect(repo))
    assert repo.started == ("nb", "q", "chunk", "user-1")
    assert lines == ['{"type": "started", "job_id": "job-1"}\n']
    assert not hasattr(repo, "_runtime")


def test_stream_route_helper_sends_content_free_idle_heartbeat(monkeypatch):
    from app.api import ask_routes
    from app.services.ask_modes import resolve_mode

    class IdleThenDone:
        def __init__(self):
            self.nonblocking_calls = 0

        def get_nowait(self):
            self.nonblocking_calls += 1
            if self.nonblocking_calls == 1:
                return {
                    "event": "started",
                    "job_id": "job-1",
                    "conversation_id": "conv-1",
                }
            if self.nonblocking_calls == 2:
                raise queue.Empty
            return None

        def get(self, *_args):
            raise queue.Empty

    class MinimalAskStream:
        def current_user(self):
            return SimpleNamespace(id="user-1")

        def start_ask_stream(self, *_args, **_kwargs):
            return IdleThenDone()

    class Request:
        async def is_disconnected(self):
            return False

    ticks = iter((0.0, 0.0, ask_routes.ASK_STREAM_HEARTBEAT_SECONDS))
    monkeypatch.setattr(ask_routes, "monotonic", lambda: next(ticks))

    async def collect():
        return [line async for line in ask_routes._stream_ask_events(
            MinimalAskStream(), "nb", AskRequest(question="q"),
            resolve_mode("chunk"), Request()
        )]

    lines = asyncio.run(collect())
    assert lines[0].startswith('{"event": "started"')
    assert lines[1] == "\n"
    assert len(lines) == 2
