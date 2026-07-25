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

    def truncate_kg_block(self, block, max_tokens):
        return block

    def parse_anchors(self, answer, evidence_by_id):
        return []

    def knowhow_refs_for(self, element_ids):
        # Task 12b: the four chunk/graph inline Citation(...) sites now call
        # this once per answer to enrich .knowhow — a newly-declared port
        # call this minimal boundary fixture must implement to stay reachable.
        return {}


def _minimal_ask_service():
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
