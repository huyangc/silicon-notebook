"""全局问答的**作业层**:准入、范围解析、权限、历史、持久化与生命周期。

D1-4 之后这一层不再拥有检索与合成:它建一次参与集覆盖 + 逐库冻结来源天花板 +
detached turn + 联邦运行计划,然后调同一个单库引擎。所以这里的引擎是一个替身
(``_EngineDouble``),它按参与集逐库发回执、回传检索时刻的指纹、调一个可替换的
合成钩子——「哪一库被搜过」「合成在什么时候发生」因此仍然可以被这一层的用例精确
摆布,而**引擎接缝本身**的约定在 ``test_global_ask_engine_parity.py`` 里钉。
"""
from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace
import time
import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.ask import AskResponse, QueryIntentContract
from app.models.global_ask import GlobalAskRequest, GlobalNotebookScope
from app.models.ask import Citation
from app.services.ask_followup import FollowupResolution
from app.services.ask_modes import resolve_mode
from app.services.federated_run import (
    LibraryOutcome, current_detached_ask_turn, current_federated_run_plan,
)
from app.services.global_ask import GlobalAskService, GlobalAskError
from app.services.retrieval_participants import current_participant_override
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import current_source_scope
from app.services.sqlite_repository import SQLiteRepository


class _EngineDouble:
    """``AskService`` 的替身,三个钩子分别对应作业层要摆布的三件事。

    ``retrieve(notebook_id, query)`` 逐库调用一次,返回 ``(hits, _, _)`` 或抛出;
    ``synthesize(question, chunks, names, history, cancel)`` 是合成那一刻;
    两者之间经 ``plan`` 发逐库回执与指纹,与生产联邦通道同形(回执在调用线程、
    按参与集顺序、每库一次,随后一次 ``on_evidence``)。
    """

    def __init__(self, sources):
        self.sources = sources
        self.retrieve = self._default_retrieve
        self.synthesize = self._default_synthesize
        self.preview_reasoning_intent = self._default_preview_reasoning_intent
        self.retrieved: list = []
        self.syntheses: list = []

    # -- 入口预检 ----------------------------------------------------------
    def _resolve_ask_mode(self, mode):
        return resolve_mode(mode)

    def validate_reasoning_submission(self, notebook_id, payload):
        return None

    def resolve_reasoning_followup(self, notebook_id, payload, history=None):
        question = payload.question.strip()
        return FollowupResolution(
            question=question, resolved_question=question,
            rewrite_ms=None, gate_message="",
        )

    @staticmethod
    def _default_preview_reasoning_intent(notebook_id, question, history="", *, cancel_event=None):
        """语料盲的理解替身:字面回声问题,从不读参与集或来源。"""
        stripped = question.strip()
        return QueryIntentContract(objective=stripped, resolved_question=stripped)

    # -- 默认行为 ----------------------------------------------------------
    @staticmethod
    def _default_retrieve(notebook_id, query):
        return [RetrievedChunk(
            chunk_id=f"c-{notebook_id}", source_id=f"s-{notebook_id}",
            source_title=f"Source {notebook_id}", section_path="Section",
            text=f"evidence {notebook_id}", element_ids=[f"e-{notebook_id}"],
            relevance=0.8,
        )], [], None

    @staticmethod
    def _default_synthesize(question, chunks, names, history, cancel):
        return "answer", False, [], []

    # -- 引擎 --------------------------------------------------------------
    def ask(self, notebook_id, payload, *, user_id, job_id="",
            cancel_event=None, on_trace=None):
        with retrieval_run(
            run_kind=f"ask_{payload.mode}", actor_id=user_id,
            correlation_id=job_id, cancel_event=cancel_event,
        ):
            turn = current_detached_ask_turn()
            plan = current_federated_run_plan()
            override = current_participant_override()
            scope = current_source_scope()
            chunks, receipts = [], []
            for participant in override.notebook_ids:
                allowed = scope.source_ceiling_for(participant)
                try:
                    hits, _ids, _matrix = self.retrieve(
                        participant, payload.question,
                    )
                except _LibraryUnavailable as exc:
                    receipts.append((participant, LibraryOutcome(
                        status="skipped", reason=exc.reason,
                    )))
                    continue
                self.retrieved.append(participant)
                kept = [
                    hit.model_copy(update={"notebook_id": participant})
                    if hasattr(hit, "model_copy")
                    else _stamped(hit, participant)
                    for hit in hits
                    if allowed is None or hit.source_id in allowed
                ]
                chunks.extend(kept)
                receipts.append((participant, LibraryOutcome(
                    status="ok", candidate_count=len(kept),
                )))
            for participant, outcome in receipts:
                plan.on_library(participant, outcome)
            plan.on_evidence(self.sources.evidence_fingerprints([
                element_id for chunk in chunks for element_id in chunk.element_ids
            ]))
            self.syntheses.append((payload.question, chunks, {}, turn.history))
            answer, grounded, anchors, citations = self.synthesize(
                payload.question, chunks, {}, turn.history, cancel_event,
            )
            return AskResponse(
                answer_id="", conclusion=answer, answer=answer,
                grounded=grounded, anchors=list(anchors),
                citations=list(citations), mode=payload.mode,
                conversation_id=turn.conversation_id,
            )


class _LibraryUnavailable(Exception):
    """替身里「这一库这次没搜成」的信号,对应联邦腿的 skip 回执。"""

    def __init__(self, reason="unavailable"):
        super().__init__(reason)
        self.reason = reason


def _stamped(hit, notebook_id):
    import dataclasses

    return dataclasses.replace(hit, notebook_id=notebook_id)


@pytest.fixture
def setup(tmp_path, request):
    # Indirect parametrization overrides Settings fields BEFORE the service is
    # built, which is the only moment the shared retrieval pool is sized.
    overrides = getattr(request, "param", None) or {}
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'global.db'}",
                        storage_dir=str(tmp_path / "storage"), **overrides)
    repo = SQLiteRepository(settings)
    readable = {"a", "b"}
    sources = SimpleNamespace(
        all_visible_source_ids=lambda nb: [f"s-{nb}"],
        evidence_elements=lambda ids: {},
        evidence_fingerprints=lambda ids: {},
    )
    sources.visible_source_ids_by_notebook = lambda ids: {
        nb: sources.all_visible_source_ids(nb) for nb in ids
    }
    engine = _EngineDouble(sources)
    service = GlobalAskService(
        store=repo._runtime.global_ask_store,
        notebooks=lambda user: [SimpleNamespace(id=nb, name=nb.upper()) for nb in sorted(readable)],
        can_read=lambda nb, user: nb in readable and user == "u",
        sources=sources, settings=settings, ask=engine,
    )
    yield service, readable, engine.retrieved, engine.syntheses
    service.close()
    repo.close()


def _stage_job_threads(staged):
    """Hold back ONLY the detached job thread, so it can be run inline.

    ``threading.Thread.start`` is shared by the whole process, including the
    shared retrieval pool's workers. Patching it wholesale leaves that pool with
    no threads at all and every submitted library retrieval waits forever, so
    the interception is keyed on the job thread's own name.
    """
    original = Thread.start

    def start(thread):
        if thread.name == "global-ask":
            staged.append(thread)
            return None
        return original(thread)

    return start


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
    assert sorted(retrieved) == ["a", "b"]
    assert len(syntheses) == 1
    assert {chunk.notebook_id for chunk in syntheses[0][1]} == {"a", "b"}
    assert result.searched_notebook_ids == ["a", "b"]
    turns = service.conversation(job.conversation_id, user_id="u").turns
    assert turns[0].answer.answer == "answer"


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
    # The id is echoed on the job, from the POST and from every later read, so a
    # client that lost the response can recognise its own submission.
    assert job.client_request_id == "once"
    assert service.get_job(job.job_id, user_id="u").client_request_id == "once"
    assert service.conversation(job.conversation_id, user_id="u").turns[0].client_request_id == "once"
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


def test_the_detached_turn_carries_the_history_in_the_engine_s_own_shape(setup):
    """历史交给引擎的形状与单库 ``prepare_turn`` 逐字同形。

    ``User: … / Assistant: …`` 成对、最旧在前,外加只含提问行的那一半。全局对话
    自己渲染一套方言,等于让同一个模型按入口不同读到两种历史块。
    """
    service, _, _, _ = setup
    turns = []
    service.ask.synthesize = lambda question, chunks, names, history, cancel: (
        turns.append(current_detached_ask_turn()) or ("answer", False, [], [])
    )
    first = service.start(GlobalAskRequest(question="低温性能"), user_id="u")
    finished(service, first)
    second = service.start(GlobalAskRequest(question="原因是什么", conversation_id=first.conversation_id), user_id="u")
    assert finished(service, second).status == "done"

    turn = turns[-1]
    assert turn.history == "User: 低温性能\nAssistant: answer"
    assert turn.user_history == "User: 低温性能"
    assert turn.conversation_id == first.conversation_id


def test_explicit_cancel_prevents_late_synthesis_commit(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late answer", False, [], []

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    assert service.cancel(job.job_id, user_id="u").status == "cancelled"
    release.set()
    result = finished(service, job)
    assert result.status == "cancelled" and result.answer is None


def _stopped_job(service, question="打错的问题", conversation_id=None):
    """A job the user stopped mid-synthesis, fully settled (worker unwound)."""
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late answer", False, [], []

    original = service.ask.synthesize
    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question=question, conversation_id=conversation_id), user_id="u")
    assert entered.wait(5)
    assert service.cancel(job.job_id, user_id="u").status == "cancelled"
    release.set()
    finished(service, job)
    service.ask.synthesize = original
    return job


def test_edit_and_resend_replaces_the_stopped_job(setup):
    service, _, _, _ = setup
    stopped = _stopped_job(service)
    request = GlobalAskRequest(
        question="改好的问题", conversation_id=stopped.conversation_id,
        replaces_job_id=stopped.job_id, client_request_id="resend",
    )
    job = service.start(request, user_id="u")
    assert finished(service, job).status == "done"
    turns = service.conversation(stopped.conversation_id, user_id="u").turns
    assert [turn.job_id for turn in turns] == [job.job_id]
    with pytest.raises(GlobalAskError) as missing:
        service.get_job(stopped.job_id, user_id="u")
    assert missing.value.status_code == 404
    # A retry of the SAME submission names a row that is already gone; it must
    # get its job back, not a 409 for a stale replacement.
    assert service.start(request, user_id="u").job_id == job.job_id


def test_a_concurrent_twin_of_a_replacing_submission_gets_the_job_back(setup, monkeypatch):
    """Two deliveries of ONE submission race; the loser must not read as stale.

    The loser passed both early checks before the winner committed, so inside
    the insert it finds the stopped job already gone. That is the race-recovery
    branch's case -- same ``client_request_id``, same request -- not a 409.
    """
    service, _, _, _ = setup
    stopped = _stopped_job(service)
    request = GlobalAskRequest(
        question="改好的问题", conversation_id=stopped.conversation_id,
        replaces_job_id=stopped.job_id, client_request_id="twin",
    )
    winner = service.start(request, user_id="u")
    assert finished(service, winner).status == "done"
    # The loser's view of the world: it saw neither the winner's row nor the deletion.
    monkeypatch.setattr(service, "replay", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_check_replaceable", lambda *args, **kwargs: None)
    assert service.start(request, user_id="u").job_id == winner.job_id
    # The other interleaving: the loser passed the replay, the winner committed,
    # and only then did the loser run the EARLY check (which now finds the old
    # job gone). It must look again before calling its own request stale.
    monkeypatch.undo()
    calls = {"count": 0}
    real_replay = service.replay

    def replay_blind_once(*args, **kwargs):
        calls["count"] += 1
        return None if calls["count"] == 1 else real_replay(*args, **kwargs)

    monkeypatch.setattr(service, "replay", replay_blind_once)
    assert service.start(request, user_id="u").job_id == winner.job_id
    assert calls["count"] == 2
    monkeypatch.undo()
    # A DIFFERENT submission naming the same gone job is still refused.
    with pytest.raises(GlobalAskError) as stale:
        service.start(request.model_copy(update={"client_request_id": "other"}), user_id="u")
    assert stale.value.status_code == 409


def test_stop_with_discard_leaves_no_record_but_never_discards_an_answer(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late answer", False, [], []

    original = service.ask.synthesize
    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    assert service.cancel(job.job_id, user_id="u", discard=True).status == "cancelled"
    release.set()
    deadline = time.monotonic() + 5
    while job.job_id in service._events and time.monotonic() < deadline:
        Event().wait(0.01)
    # The first question opened the conversation; both are gone, and the
    # worker's late writes found no row to touch.
    assert service.store.job(job.job_id, "u") is None
    assert service.store.conversation(job.conversation_id, "u") is None
    service.ask.synthesize = original
    # A record that was ALREADY stopped is the one the rule keeps: a replayed or
    # foreign ``discard`` must not be able to delete it.
    kept = _stopped_job(service, question="留在对话里的那条")
    assert service.cancel(kept.job_id, user_id="u", discard=True).status == "cancelled"
    assert service.store.job(kept.job_id, "u") is not None
    answered = service.start(GlobalAskRequest(question="q2"), user_id="u")
    assert finished(service, answered).status == "done"
    assert service.cancel(answered.job_id, user_id="u", discard=True).status == "done"
    assert service.store.job(answered.job_id, "u").status == "done"


def test_discard_still_happens_when_the_worker_wins_the_cancelled_write(setup, monkeypatch):
    """The worker sees the event and may persist ``cancelled`` before ``cancel``'s
    own ``save`` runs; that save then matches no row. It is still this call's
    cancellation, so the discard must not be skipped (codex #761 r7)."""
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late answer", False, [], []

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    real_save = service.store.save

    def worker_wins(candidate, user_id):
        # Exactly what the worker's own terminal write does, landing first.
        assert real_save(candidate, user_id)
        return False

    monkeypatch.setattr(service.store, "save", worker_wins)
    assert service.cancel(job.job_id, user_id="u", discard=True).status == "cancelled"
    monkeypatch.undo()
    release.set()
    assert service.store.job(job.job_id, "u") is None
    assert service.store.conversation(job.conversation_id, "u") is None


def test_a_stale_replacement_is_refused_before_anything_is_spent(setup):
    service, _, retrieved, _ = setup
    stopped = _stopped_job(service)
    answered = service.start(
        GlobalAskRequest(question="后一个问题", conversation_id=stopped.conversation_id), user_id="u",
    )
    assert finished(service, answered).status == "done"
    before = list(retrieved)
    for payload in (
        # Not the newest job any more.
        GlobalAskRequest(question="q", conversation_id=stopped.conversation_id, replaces_job_id=stopped.job_id),
        # An answer is never replaceable.
        GlobalAskRequest(question="q", conversation_id=stopped.conversation_id, replaces_job_id=answered.job_id),
        # No conversation named, another conversation named, unknown job.
        GlobalAskRequest(question="q", replaces_job_id=stopped.job_id),
        GlobalAskRequest(question="q", conversation_id="gconv-other", replaces_job_id=stopped.job_id),
        GlobalAskRequest(question="q", conversation_id=stopped.conversation_id, replaces_job_id="gask-missing"),
    ):
        with pytest.raises(GlobalAskError) as error:
            service.start(payload, user_id="u")
        assert error.value.status_code == 409
    assert retrieved == before
    turns = service.conversation(stopped.conversation_id, user_id="u").turns
    assert [turn.job_id for turn in turns] == [stopped.job_id, answered.job_id]
    # Somebody else's stopped job reads as "not there", same as any other job.
    with pytest.raises(GlobalAskError) as foreign:
        service.start(GlobalAskRequest(
            question="q", conversation_id=stopped.conversation_id, replaces_job_id=stopped.job_id,
        ), user_id="other")
    assert foreign.value.status_code in {404, 409}


def test_live_token_revocation_stops_before_synthesis(setup):
    service, _, _, syntheses = setup
    current = [["a", "b"]]
    retrieve = service.ask.retrieve

    def revoke_after_retrieval(nb, query):
        result = retrieve(nb, query)
        current[0] = []
        return result

    service.ask.retrieve = revoke_after_retrieval
    job = service.start(GlobalAskRequest(question="q"), user_id="u", allowed_notebook_ids=["a", "b"], authority_check=lambda: current[0])
    result = finished(service, job)
    assert result.status == "failed" and result.answer is None
    assert not syntheses


def test_delete_cancels_active_worker_and_removes_durable_conversation(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    service.delete_conversation(job.conversation_id, user_id="u")
    release.set()
    service.close()
    assert service.store.job(job.job_id, "u") is None
    assert service.list_conversations(user_id="u") == []


def test_sources_are_frozen_before_any_retrieval(setup):
    service, _, _, _ = setup
    frozen = []
    service.sources.all_visible_source_ids = lambda nb: frozen.append(nb) or [f"s-{nb}"]
    retrieve = service.ask.retrieve

    def verify(nb, query):
        assert frozen == ["a", "b"]
        return retrieve(nb, query)

    service.ask.retrieve = verify
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert finished(service, job).status == "done"


def test_source_removed_during_synthesis_cannot_commit_stale_citation(setup):
    service, _, _, _ = setup
    elements = {"e-a": {"id": "e-a", "source_id": "s-a", "text": "original"}}
    service.sources.evidence_fingerprints = lambda ids: {key: (elements[key]["source_id"], elements[key]["text"]) for key in ids if key in elements}

    def synthesis(*args):
        elements.clear()
        citation = Citation(label="source", source_id="s-a", element_id="e-a", location_label="Section", quoted_span="original", notebook_id="a")
        return "claim [k1]", True, [], [citation]

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    result = finished(service, job)
    assert result.status == "done" and not result.answer.grounded
    assert not result.answer.citations
    assert "变化" in result.answer.answer


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
        # ``discard`` reaches the service as a boolean; an answered job is never discarded.
        kept = client.post(f"/api/global-ask/jobs/{job.job_id}/cancel", params={"discard": "true"})
        assert kept.status_code == 200 and kept.json()["status"] == "done"
        assert service.store.job(job.job_id, "u") is not None
        # "Edit and re-send" is refused with a user-facing 409 once it is stale.
        stale = client.post("/api/global-ask/ask", json={
            "question": "q", "conversation_id": job.conversation_id, "replaces_job_id": job.job_id,
        })
        assert stale.status_code == 409 and stale.headers["X-User-Message"] == "1"
        assert client.delete(f"/api/global-ask/conversations/{job.conversation_id}").status_code == 204


def test_storage_restart_recovery_is_explicit_and_releases_conversation(setup):
    service, _, _, _ = setup
    entered, release = Event(), Event()

    def synthesis(*args):
        entered.set()
        assert release.wait(5)
        return "late", False, [], []

    service.ask.synthesize = synthesis
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    assert entered.wait(5)
    service.store.recover()
    release.set()
    assert finished(service, job).status == "interrupted"
    assert service.store.job(job.job_id, "u").answer is None


@pytest.mark.parametrize("change", ["revoke", "cancel"])
def test_worker_checks_authority_and_cancel_before_calling_the_engine(setup, monkeypatch, change):
    """run 开始那一次复核在**引擎之前**:撤权或取消时一次模型都不该花。"""
    service, _, _, _ = setup
    staged, asked = [], []
    allowed = [["a", "b"]]
    monkeypatch.setattr("app.services.global_ask.threading.Thread.start", _stage_job_threads(staged))
    service.ask.retrieve = lambda nb, query: asked.append(nb) or ([], [], None)
    job = service.start(GlobalAskRequest(question="q"), user_id="u", authority_check=lambda: allowed[0])
    if change == "revoke":
        allowed[0] = []
    else:
        service.cancel(job.job_id, user_id="u")
    staged[0].run()
    result = finished(service, job)
    assert result.status == ("failed" if change == "revoke" else "cancelled")
    assert not asked


def test_runtime_wiring_supports_notebooks_without_knowledge_graph(tmp_path):
    from app.models.notebooks import NotebookCreate

    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'runtime.db'}", storage_dir=str(tmp_path / "storage"))
    repo = SQLiteRepository(settings)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Original sources"))
        user_id = repo.current_user().id
        service = repo._runtime.global_ask_service()
        job = service.start(GlobalAskRequest(question="有哪些内容"), user_id=user_id)
        deadline = time.monotonic() + 10
        while job.job_id in service._events and time.monotonic() < deadline:
            Event().wait(0.01)
        result = service.get_job(job.job_id, user_id=user_id)
        assert result.status == "done"
        assert result.resolved_notebook_ids == [notebook.id]
        assert result.answer is not None and result.answer.grounded is False
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

    setattr(service.ask, failure_stage, fail)
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    result = finished(service, job)
    assert result.status == "failed"
    assert result.error == "回答未完成，请检查模型服务和资料状态后重试。"
    assert "secret" not in service.get_job(job.job_id, user_id="u").model_dump_json()


def test_failed_run_records_raw_error_detail_for_admin_only(setup):
    """记录平权:owner-facing ``job.error`` 保持固定文案,原始异常文本单独进
    ``error_detail`` 列,只有 ``admin_job_record`` 这条管理员通道能读到它。"""
    service, _, _, _ = setup

    def fail(*args):
        raise RuntimeError("boom")

    service.ask.retrieve = fail
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    result = finished(service, job)
    assert result.status == "failed"
    assert result.error == "回答未完成，请检查模型服务和资料状态后重试。"
    record = service.store.admin_job_record(job.job_id, "u")
    assert record is not None
    assert record["error_detail"] == "RuntimeError: boom"


def test_asked_at_flows_from_request_to_persisted_job(setup):
    service, _, _, _ = setup
    job = service.start(
        GlobalAskRequest(question="q", asked_at="2026-09-20T00:00:00+00:00"),
        user_id="u",
    )
    assert job.asked_at == "2026-09-20T00:00:00+00:00"
    finished(service, job)
    assert service.get_job(job.job_id, user_id="u").asked_at == "2026-09-20T00:00:00+00:00"


def test_replay_with_different_asked_at_returns_the_same_job_not_409(setup):
    """``asked_at`` 是展示元数据,不是请求身份的一部分(见
    ``_REQUEST_IDENTITY_EXCLUDES``):同一个 ``client_request_id`` 换一个
    ``asked_at`` 重放,必须原样拿回同一个作业,而不是 409。"""
    service, _, _, _ = setup
    first = service.start(
        GlobalAskRequest(
            question="q", client_request_id="idem-1",
            asked_at="2026-09-20T00:00:00+00:00",
        ),
        user_id="u",
    )
    again = service.start(
        GlobalAskRequest(
            question="q", client_request_id="idem-1",
            asked_at="2026-09-20T01:00:00+00:00",
        ),
        user_id="u",
    )
    assert again.job_id == first.job_id
    assert again.asked_at == first.asked_at


def test_global_ask_request_asked_at_requires_offset_but_accepts_z():
    with pytest.raises(Exception):
        GlobalAskRequest(question="q", asked_at="2026-09-22T10:00:00")
    accepted = GlobalAskRequest(question="q", asked_at="2026-09-22T10:00:00Z")
    assert accepted.asked_at == "2026-09-22T10:00:00Z"


def test_submit_feedback_authority_rating_and_completion_gates(setup):
    """``submit_feedback`` reuses ``get_job``'s own authority check (same 404
    for a foreign or nonexistent job), then adds its two own gates: a legal
    rating (422) and a finished job (409). The event it emits carries only
    the rating/engine/library count -- never the question or answer text."""
    service, _, _, _ = setup
    events = []
    service.event_log = SimpleNamespace(emit=lambda event: events.append(event))
    job = service.start(GlobalAskRequest(question="q"), user_id="u")

    with pytest.raises(GlobalAskError) as unfinished:
        service.submit_feedback(job.job_id, "useful", user_id="u")
    assert unfinished.value.status_code == 409

    finished(service, job)

    with pytest.raises(GlobalAskError) as bad_rating:
        service.submit_feedback(job.job_id, "not-a-rating", user_id="u")
    assert bad_rating.value.status_code == 422

    with pytest.raises(GlobalAskError) as foreign_job:
        service.submit_feedback(job.job_id, "useful", user_id="other")
    assert foreign_job.value.status_code == 404

    with pytest.raises(GlobalAskError) as missing_job:
        service.submit_feedback("no-such-job", "useful", user_id="u")
    assert missing_job.value.status_code == 404

    updated = service.submit_feedback(job.job_id, "useful", user_id="u")
    assert updated.feedback == "useful"
    assert service.get_job(job.job_id, user_id="u").feedback == "useful"

    # 事件在成功路径才发，而且没有任何一次失败的调用（未完成/非法评分/越权/
    # 不存在）漏进了这份清单。
    assert [event["rating"] for event in events] == ["useful"]
    assert all(set(event) == {"kind", "rating", "mode", "libraries"} for event in events)
    assert events[0]["kind"] == "global_ask_feedback"
    assert events[0]["mode"] == "chunk"
    assert events[0]["libraries"] == 2


def test_submit_feedback_first_write_wins_through_the_service(setup):
    service, _, _, _ = setup
    job = service.start(GlobalAskRequest(question="q"), user_id="u")
    finished(service, job)
    first = service.submit_feedback(job.job_id, "useful", user_id="u")
    assert first.feedback == "useful"
    second = service.submit_feedback(job.job_id, "not_useful", user_id="u")
    assert second.feedback == "useful"
    assert service.get_job(job.job_id, user_id="u").feedback == "useful"


def test_submit_feedback_route(setup, monkeypatch):
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
        job_id = response.json()["job_id"]
        finished(service, service.store.job(job_id, "u"))
        feedback = client.post(f"/api/global-ask/jobs/{job_id}/feedback", json={"rating": "useful"})
        assert feedback.status_code == 200
        assert feedback.json()["feedback"] == "useful"
        invalid = client.post(f"/api/global-ask/jobs/{job_id}/feedback", json={"rating": "bogus"})
        assert invalid.status_code == 422


def test_a_delivered_global_answer_notes_completion_once_with_every_participant(setup):
    """The post-completion chains fire AFTER the terminal ``done`` row, once,
    with every participant library -- and never for a failed run."""
    service, _, _, _ = setup
    notes = []
    noted = Event()

    def record(ids, user, mode, *, anchor):
        notes.append((ids, user, mode, anchor))
        noted.set()

    service.note_ask_completed = record
    job = service.start(GlobalAskRequest(question="compare"), user_id="u")
    assert finished(service, job).status == "done"
    # ``finished`` returns once the row is terminal and the job left the live
    # registry, which happens BEFORE the hook runs (by design: the hook is the
    # last thing the worker does), so wait for the callback itself.
    assert noted.wait(5), "completion hook never ran"
    assert notes == [(["a", "b"], "u", "chunk", "a")]

    notes.clear()
    original = service.ask.ask

    def failing(*args, **kwargs):
        raise RuntimeError("boom")

    service.ask.ask = failing
    try:
        failed = service.start(GlobalAskRequest(question="again"), user_id="u")
        assert finished(service, failed).status == "failed"
    finally:
        service.ask.ask = original
    assert notes == []


def test_a_failing_completion_note_leaves_the_delivered_answer_untouched(setup, caplog):
    service, _, _, _ = setup

    noted = Event()

    def explode(ids, user, mode, *, anchor):
        noted.set()
        raise RuntimeError("bookkeeping down at /private/path")

    service.note_ask_completed = explode
    with caplog.at_level("WARNING", logger="silicon_notebook.global_ask"):
        job = service.start(GlobalAskRequest(question="compare"), user_id="u")
        result = finished(service, job)
        assert noted.wait(5)
        # The receipt is written by the worker right after the hook raises;
        # give that thread its turn before reading the records.
        for _ in range(500):
            if any("post-completion" in r.getMessage() for r in caplog.records):
                break
            Event().wait(0.01)
    assert result.status == "done"
    assert result.answer is not None and result.answer.answer == "answer"
    # The receipt is content-free: the exception class, no text, no traceback.
    failures = [r for r in caplog.records if "post-completion notification failed" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is None
    assert "RuntimeError" in failures[0].getMessage()
    assert "/private/path" not in caplog.text


def test_a_done_write_that_did_not_win_never_notes_completion(setup):
    """``_execute`` reports whether ITS write moved the row to ``done``; a
    shutdown or a concurrent transition that already took the row leaves
    nothing to learn from, so the chains stay silent even though the answer
    exists in the store."""
    service, _, _, _ = setup
    notes = []
    service.note_ask_completed = lambda ids, user, mode, **kw: notes.append((ids, user, mode))
    original = service._save_if_open

    def lost_race(job, user_id, **kwargs):
        won = original(job, user_id, **kwargs)
        return False if job.status == "done" else won

    service._save_if_open = lost_race
    job = service.start(GlobalAskRequest(question="compare"), user_id="u")
    assert finished(service, job).status == "done"
    assert notes == []
