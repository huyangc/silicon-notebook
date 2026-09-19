from __future__ import annotations

from threading import Event
from types import SimpleNamespace
import time
import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.global_ask import GlobalAskRequest, GlobalNotebookScope
from app.models.ask import Citation
from app.services.global_ask import GlobalAskService, GlobalAskError
from app.services.source_scope import current_source_scope
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'global.db'}", storage_dir=str(tmp_path / "storage"))
    repo = SQLiteRepository(settings)
    readable = {"a", "b"}
    retrieved, syntheses = [], []
    sources = SimpleNamespace(
        all_visible_source_ids=lambda nb: [f"s-{nb}"],
        evidence_elements=lambda ids: {},
    )

    def retrieve(nb, query):
        scope = current_source_scope()
        assert scope.source_ids == frozenset({f"s-{nb}"})
        assert scope.base_mode == "include" and not scope.base_notebook_ids
        retrieved.append(nb)
        return [RetrievedChunk(
            chunk_id=f"c-{nb}", source_id=f"s-{nb}", source_title=f"Source {nb}",
            section_path="Section", text=f"evidence {nb}", element_ids=[f"e-{nb}"], relevance=0.8,
        )], [], None

    def synthesize(question, chunks, names, history, event):
        syntheses.append((question, chunks, names, history))
        return "answer", False, [], []

    service = GlobalAskService(
        store=repo._runtime.global_ask_store,
        notebooks=lambda user: [SimpleNamespace(id=nb, name=nb.upper()) for nb in sorted(readable)],
        can_read=lambda nb, user: nb in readable and user == "u",
        sources=sources, retrieve=retrieve, synthesize=synthesize, settings=settings,
    )
    yield service, readable, retrieved, syntheses
    service.close()
    repo.close()


def finished(service, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.store.job(job.job_id, "u")
        if row.status != "running" and job.job_id not in service._events:
            return row
        Event().wait(0.01)
    pytest.fail("global worker did not settle")


def test_all_searches_each_notebook_once_and_synthesizes_once(setup):
    service, _, retrieved, syntheses = setup
    job = service.start(GlobalAskRequest(question="compare"), user_id="u")
    result = finished(service, job)
    assert result.status == "done"
    assert retrieved == ["a", "b"]
    assert len(syntheses) == 1
    assert {chunk.notebook_id for chunk in syntheses[0][1]} == {"a", "b"}
    assert result.searched_notebook_ids == ["a", "b"]
    assert service.conversation(job.conversation_id, user_id="u").turns[0].response.answer == "answer"


def test_empty_include_means_all_and_unknown_scope_is_rejected(setup):
    service, _, retrieved, _ = setup
    assert GlobalNotebookScope(mode="include", notebook_ids=[]).mode == "all"
    with pytest.raises(GlobalAskError):
        service.start(GlobalAskRequest(question="q", notebook_scope={"mode": "include", "notebook_ids": ["foreign"]}), user_id="u")
    assert not retrieved


def test_include_scope_and_idempotency_and_owner_isolation(setup):
    service, _, retrieved, _ = setup
    request = GlobalAskRequest(question="q", notebook_scope={"mode": "include", "notebook_ids": ["a"]}, client_request_id="once")
    job = service.start(request, user_id="u")
    assert finished(service, job).status == "done"
    assert service.start(request, user_id="u").job_id == job.job_id
    assert retrieved == ["a"]
    with pytest.raises(GlobalAskError) as error:
        service.start(request.model_copy(update={"question": "different"}), user_id="u")
    assert error.value.status_code == 409
    with pytest.raises(GlobalAskError):
        service.get_job(job.job_id, user_id="other")


def test_token_allowlist_and_revoked_answer_read(setup):
    service, readable, retrieved, _ = setup
    job = service.start(GlobalAskRequest(question="q"), user_id="u", allowed_notebook_ids=["a"])
    assert finished(service, job).status == "done"
    assert retrieved == ["a"]
    with pytest.raises(GlobalAskError):
        service.get_job(job.job_id, user_id="u", allowed_notebook_ids=["b"])
    readable.remove("a")
    with pytest.raises(GlobalAskError):
        service.get_job(job.job_id, user_id="u")


def test_followup_inherits_and_narrowing_drops_prior_context(setup):
    service, _, _, syntheses = setup
    first = service.start(GlobalAskRequest(question="private all-context"), user_id="u")
    finished(service, first)
    second = service.start(GlobalAskRequest(question="only a", conversation_id=first.conversation_id, notebook_scope={"mode": "include", "notebook_ids": ["a"]}), user_id="u")
    finished(service, second)
    assert syntheses[-1][3] == ""
    third = service.start(GlobalAskRequest(question="followup", conversation_id=first.conversation_id), user_id="u")
    finished(service, third)
    assert third.resolved_notebook_ids == ["a"]
    assert "only a" in syntheses[-1][3]
    assert "private all-context" not in syntheses[-1][3]


def test_explicit_cancel_prevents_late_synthesis_commit(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late answer", False, [], []

    service.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    assert service.cancel(job.job_id, user_id="u").status == "cancelled"
    release.set()
    result = finished(service, job)
    assert result.status == "cancelled" and result.response is None


def test_live_token_revocation_stops_before_synthesis(setup):
    service, _, _, syntheses = setup
    current = [["a", "b"]]
    retrieve = service.retrieve

    def revoke_after_retrieval(nb, query):
        result = retrieve(nb, query)
        current[0] = []
        return result

    service.retrieve = revoke_after_retrieval
    job = service.start(GlobalAskRequest(question="q"), user_id="u", allowed_notebook_ids=["a", "b"], authority_check=lambda: current[0])
    result = finished(service, job)
    assert result.status == "failed" and result.response is None
    assert not syntheses


def test_delete_cancels_active_worker_and_removes_durable_conversation(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    service.delete_conversation(job.conversation_id, user_id="u")
    release.set()
    service.close()
    assert service.store.job(job.job_id, "u") is None
    assert service.list_conversations(user_id="u") == []


def test_followup_rewrite_is_used_for_retrieval(setup):
    service, _, _, _ = setup
    first = service.start(GlobalAskRequest(question="低温性能"), user_id="u")
    finished(service, first)
    queries = []
    retrieve = service.retrieve
    service.rewrite_query = lambda history, question, event: "低温性能的原因" if history else question

    def capture(nb, query):
        queries.append(query)
        return retrieve(nb, query)

    service.retrieve = capture
    job = service.start(GlobalAskRequest(question="原因是什么", conversation_id=first.conversation_id), user_id="u")
    assert finished(service, job).status == "done"
    assert queries == ["低温性能的原因", "低温性能的原因"]


def test_sources_are_frozen_before_any_retrieval(setup):
    service, _, _, _ = setup
    frozen = []
    service.sources.all_visible_source_ids = lambda nb: frozen.append(nb) or [f"s-{nb}"]
    retrieve = service.retrieve

    def verify(nb, query):
        assert frozen == ["a", "b"]
        return retrieve(nb, query)

    service.retrieve = verify
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert finished(service, job).status == "done"


def test_source_removed_during_synthesis_cannot_commit_stale_citation(setup):
    service, _, _, _ = setup
    elements = {"e-a": {"id": "e-a", "source_id": "s-a", "text": "original"}}
    service.sources.evidence_elements = lambda ids: {key: dict(elements[key]) for key in ids if key in elements}

    def synthesis(*args):
        elements.clear()
        citation = Citation(label="source", source_id="s-a", element_id="e-a", location_label="Section", quoted_span="original", notebook_id="a")
        return "claim [k1]", True, [], [citation]

    service.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    result = finished(service, job)
    assert result.status == "failed" and result.response is None
    assert "变化" in result.error


def test_http_job_conversation_and_error_contract(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import global_ask_routes
    from app.api.deps import get_current_user

    service, _, _, _ = setup
    monkeypatch.setattr(global_ask_routes, "global_ask_service", lambda: service)
    app = FastAPI()
    app.include_router(global_ask_routes.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u")
    with TestClient(app) as client:
        response = client.post("/api/global-ask/ask", json={"question": "hello"})
        assert response.status_code == 202
        body = response.json()
        job = service.store.job(body["job_id"], "u")
        finished(service, job)
        assert client.get(f"/api/global-ask/jobs/{job.job_id}").json()["status"] == "done"
        assert client.get("/api/global-ask/conversations").json()[0]["id"] == job.conversation_id
        renamed = client.patch(f"/api/global-ask/conversations/{job.conversation_id}", json={"title": "Comparison"})
        assert renamed.json()["title"] == "Comparison"
        invalid = client.post("/api/global-ask/ask", json={"question": "q", "notebook_scope": {"mode": "include", "notebook_ids": ["no"]}})
        assert invalid.status_code == 404 and invalid.headers["X-User-Message"] == "1"
        assert client.delete(f"/api/global-ask/conversations/{job.conversation_id}").status_code == 204


def test_storage_restart_recovery_is_explicit_and_releases_conversation(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    service.store.recover()
    release.set()
    assert finished(service, job).status == "interrupted"
    assert service.store.job(job.job_id, "u").response is None


@pytest.mark.parametrize("change", ["revoke", "cancel"])
def test_worker_checks_authority_and_cancel_before_history_rewrite(setup, monkeypatch, change):
    service, _, _, _ = setup
    staged, rewrites = [], []
    allowed = [["a", "b"]]
    monkeypatch.setattr("app.services.global_ask.threading.Thread.start", lambda thread: staged.append(thread))
    service.rewrite_query = lambda *args: rewrites.append(args) or "rewritten"
    job = service.start(GlobalAskRequest(question="q"), user_id="u", authority_check=lambda: allowed[0])
    if change == "revoke":
        allowed[0] = []
    else:
        service.cancel(job.job_id, user_id="u")
    staged[0].run()
    result = finished(service, job)
    assert result.status == ("failed" if change == "revoke" else "cancelled")
    assert not rewrites


def test_runtime_wiring_supports_notebooks_without_knowledge_graph(tmp_path):
    from app.models.notebooks import NotebookCreate

    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'runtime.db'}", storage_dir=str(tmp_path / "storage"))
    repo = SQLiteRepository(settings)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Original sources"))
        user_id = repo.current_user().id
        service = repo._runtime.global_ask_service()
        job = service.start(GlobalAskRequest(question="有哪些内容"), user_id=user_id)
        deadline = time.monotonic() + 5
        while job.job_id in service._events and time.monotonic() < deadline:
            Event().wait(0.01)
        result = service.get_job(job.job_id, user_id=user_id)
        assert result.status == "done"
        assert result.resolved_notebook_ids == [notebook.id]
        assert result.response.grounded is False
    finally:
        repo.close()


def test_conversation_pagination_preserves_all_turns(setup):
    service, _, _, _ = setup
    first = service.start(GlobalAskRequest(question="first"), user_id="u")
    finished(service, first)
    for question in ("second", "third"):
        job = service.start(GlobalAskRequest(question=question, conversation_id=first.conversation_id), user_id="u")
        finished(service, job)
    latest = service.conversation(first.conversation_id, user_id="u", limit=2)
    earlier = service.conversation(first.conversation_id, user_id="u", limit=2, offset=latest.next_offset)
    assert [job.question for job in earlier.turns + latest.turns] == ["first", "second", "third"]
    assert latest.has_more and not earlier.has_more


@pytest.mark.parametrize("failure_stage", ["retrieve", "synthesize"])
def test_job_errors_never_expose_raw_exception_content(setup, failure_stage):
    service, _, _, _ = setup

    def fail(*args):
        raise RuntimeError("secret upstream response/path")

    setattr(service, failure_stage, fail)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    result = finished(service, job)
    assert result.status == "failed"
    assert result.error == "回答未完成，请检查模型服务和资料状态后重试。"
    assert "secret" not in service.get_job(job.job_id, user_id="u").model_dump_json()
