from __future__ import annotations

import time
from dataclasses import dataclass, fields
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from app.api import ask_routes
from app.application.ask_reasoning import StageBoundaryError
from app.core.config import Settings
from app.domain.ask_engine import AskPluginEngineError
from app.domain.retrieval import (
    NeighborExpansion,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.models.common import Evidence
from app.extension_sdk import (
    ASK_ENGINE_POINT,
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    AskEngineDescriptor,
    AskEnginePortError,
    AskEngineResult,
    ContributionDeclaration,
    ContributionKind,
    EngineEvidence,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extensions.bootstrap import build_extension_runtime
from app.extensions.discovery import ExtensionDiscoveryError
from app.extensions.registry import ExtensionRegistryError
from app.models.ask import AskRequest
from app.models.schemas import NotebookCreate
from app.services.plugin_ask_engine import (
    PluginCancellationToken,
    PluginEngineModelAccess,
    PluginEngineTrace,
    PluginRetrievalAccess,
    admit_plugin_engine_result,
    plugin_engine_trace_steps,
    release_plugin_engine_ports,
)
from app.services.ask_modes import UnknownAskMode, resolve_mode
from app.services.retrieval_run import current_retrieval_run, retrieval_run
from app.services import plugin_ask_engine as plugin_ports
from app.services.source_scope import source_scope_context
from app.models.source_scope import BaseNotebookScope, ResolvedSourceScope
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)
from tests.test_ask_service_boundary import _minimal_ask_service


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contributions: tuple[ExtensionContribution, ...]

    def register(self, registrar) -> None:
        for contribution in self.contributions:
            registrar.add_provider(contribution)


class _Provider:
    def __init__(
        self,
        mode_id: str,
        *,
        label: str = "企业检索",
        description: str = "使用部署内检索策略回答",
        requires_kg: bool = False,
        answer=None,
    ) -> None:
        self.descriptor = AskEngineDescriptor(
            mode_id, label, description, requires_kg
        )
        self._answer = answer

    def answer(self, context, retrieval, model, trace):
        if self._answer is not None:
            return self._answer(context, retrieval, model, trace)
        return AskEngineResult("没有引用的回答", ())


def _bundle(
    plugin_id: str,
    *providers: _Provider,
    trust: str = "deployment",
    availability=None,
) -> _Bundle:
    declarations = tuple(
        ContributionDeclaration(
            f"{plugin_id}-engine-{index}",
            ASK_ENGINE_POINT,
            ContributionKind.PROVIDER,
        )
        for index, _provider in enumerate(providers, 1)
    )
    return _Bundle(
        ExtensionManifest(
            id=plugin_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=plugin_id,
            trust=trust,
            contributions=declarations,
        ),
        tuple(
            ExtensionContribution(declaration, provider, availability)
            for declaration, provider in zip(declarations, providers)
        ),
    )


def _hit(*, element_id: str = "element-1", source_id: str = "source-1"):
    return RetrievedElement(
        element_id=element_id,
        source_id=source_id,
        source_title="权威来源",
        location_label="第 3 页",
        element_type="paragraph",
        text="可核验的证据正文",
        score=0.9,
    )


def _evidence(*, element_id: str = "element-1", source_id: str = "source-1"):
    return Evidence(
        source_id=source_id,
        source_title="权威来源",
        element_id=element_id,
        element_type="paragraph",
        location_label="第 3 页",
        quoted_span="对象出处摘录",
        confidence=0.9,
    )


def _object(
    *,
    object_id: str = "ko-1",
    object_type: str = "concept",
    name: str = "退火",
    evidence=None,
    notebook_id: str = "",
):
    """``notebook_id`` defaults to "" (the ``RetrievedKnowledge`` field
    default) to mirror the production seam this double stands in for MOST
    often: `_retrieve_neighbors` (the `kg_neighbors` seat) never sets
    `.notebook_id`. `search_kg` callers must pass it explicitly — production
    `federated_retrieve` always tags every hit with a real participant
    library (`_federated_retrieve_impl`), so a search_kg double that left it
    at "" would silently mask the P1 origin bug this file's cross-library
    tests exist to catch."""
    return RetrievedKnowledge(
        object_id=object_id,
        object_type=object_type,
        payload={"name": name, "definition": "把材料缓慢冷却的热处理"},
        evidence=list(evidence if evidence is not None else [_evidence()]),
        score=0.8,
        notebook_id=notebook_id,
    )


def _kg_access(
    *,
    search_knowledge=None,
    object_neighbors=None,
    collection_overview=None,
    search_elements=None,
    source_info=None,
    visible: tuple[str, ...] = ("source-1",),
    kg_max_calls: int = 2,
) -> PluginRetrievalAccess:
    return PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: visible,
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=search_elements or (lambda *_args, **_kwargs: [_hit()]),
        source_info=source_info or (lambda source_ids: {
            source_id: {"title": "权威来源", "file_name": "paper.pdf"}
            for source_id in source_ids
            if source_id in visible
        }),
        search_knowledge=search_knowledge or (lambda *_args, **_kwargs: []),
        object_neighbors=object_neighbors or (
            lambda *_args, **_kwargs: NeighborExpansion([], False)
        ),
        collection_overview=collection_overview or (lambda _notebook_id: ""),
        kg_max_calls=kg_max_calls,
        max_k=2,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )


def _retrieval_access(*, hit=None, max_calls: int = 2) -> PluginRetrievalAccess:
    value = hit or _hit()
    return PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: (value.source_id,),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [value],
        source_info=lambda _source_ids: {
            value.source_id: {"title": "权威来源", "file_name": "paper.pdf"}
        },
        max_k=2,
        max_calls=max_calls,
        evidence_chars=100,
        query_chars=100,
    )


def test_ask_engine_registry_allows_a_provider_set_and_rejects_bad_identity():
    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.search")),
        _bundle("beta", _Provider("beta.search")),
    ))
    assert [
        registration.descriptor.mode_id
        for registration in runtime.ask_engines.registrations()
    ] == ["alpha.search", "beta.search"]

    with pytest.raises(ExtensionDiscoveryError) as rejected:
        build_extension_runtime((
            _bundle("alpha", _Provider("other.search")),
        ))
    assert rejected.value.plugin_id == "alpha"
    assert rejected.value.reason == "invalid_ask_engine"
    assert "other.search" not in str(rejected.value), (
        "deployment-authored descriptor text must not cross startup diagnostics"
    )

    with pytest.raises(ExtensionRegistryError, match="duplicate Ask engine mode"):
        build_extension_runtime((
            _bundle(
                "alpha",
                _Provider("alpha.same"),
                _Provider("alpha.same"),
                trust="builtin",
            ),
        ))


def test_sdk_evidence_surface_is_frozen_and_has_no_addressable_ids():
    assert {field.name for field in fields(EngineEvidence)} == {
        "evidence_key", "text", "source_title", "location_label", "object_type",
    }
    assert EngineEvidence.__dataclass_params__.frozen is True
    assert "__dict__" not in EngineEvidence.__slots__
    # The KG field carries a default so a v1 provider that builds the
    # four-positional form keeps working.
    evidence = EngineEvidence("pe-test", "text", "title", "page")
    assert evidence.object_type == ""
    for forbidden in (
        "source_id", "element_id", "notebook_id", "chunk_id", "repository",
    ):
        assert not hasattr(evidence, forbidden)


def test_plugin_port_instances_reach_only_opaque_core_authority_tokens():
    class Client:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            return '{"text":"ok"}'

    event = SimpleNamespace(is_set=lambda: False)
    retrieval = _retrieval_access()
    model = PluginEngineModelAccess(
        Client(), cancellation=event, max_calls=1, max_chars=20
    )
    trace = PluginEngineTrace(max_steps=1, label_chars=10, detail_chars=10)
    cancellation = PluginCancellationToken(event)
    ports = (retrieval, model, trace, cancellation)
    try:
        for port in ports:
            assert "__dict__" not in type(port).__slots__
            assert type(port).__slots__ == ("__authority_token",)
            token = object.__getattribute__(
                port, f"_{type(port).__name__}__authority_token"
            )
            assert type(token) is str and token.startswith("authority-")
            assert not any(value in token for value in (
                "notebook-1", "source-1", "element-1", "user-1"
            ))
        assert not hasattr(retrieval, "admit")
        assert not hasattr(trace, "steps")
        for forbidden in (
            "_client", "_event", "_search_elements", "_source_keys",
            "_source_origin", "_ledger", "_active_notebook_id", "_actor_id",
        ):
            assert all(not hasattr(port, forbidden) for port in ports)
    finally:
        release_plugin_engine_ports(*ports)


def test_release_revokes_inflight_retrieval_and_discards_its_result():
    entered = Event()
    unblock = Event()
    metadata_started = Event()

    def search_elements(*_args, **_kwargs):
        entered.set()
        assert unblock.wait(2), "test barrier was not released"
        return [_hit()]

    access = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: ("source-1",),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=search_elements,
        source_info=lambda _source_ids: metadata_started.set() or {},
        max_k=1,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )
    outcome: list[object] = []

    def invoke() -> None:
        try:
            outcome.append(access.search("query", 1))
        except BaseException as exc:
            outcome.append(exc)

    worker = Thread(target=invoke, daemon=True)
    worker.start()
    assert entered.wait(2), "retrieval never entered raw I/O"
    released = Event()
    releaser = Thread(
        target=lambda: (
            release_plugin_engine_ports(access), released.set()
        ),
        daemon=True,
    )
    releaser.start()
    try:
        assert released.wait(2), "revoke waited on in-flight raw I/O"
        with pytest.raises(AskEnginePortError) as rejected:
            access.search("new call", 1)
        assert rejected.value.code == "plugin_engine_failed"
    finally:
        unblock.set()
    worker.join(2)
    releaser.join(2)

    assert not worker.is_alive()
    assert not metadata_started.is_set(), (
        "revoke after the leaf read must prevent the follow-up authority read"
    )
    assert len(outcome) == 1
    assert isinstance(outcome[0], AskEnginePortError)
    assert outcome[0].code == "plugin_engine_failed"


def test_release_revokes_inflight_model_completion_and_discards_its_result():
    entered = Event()
    unblock = Event()

    class Client:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            entered.set()
            assert unblock.wait(2), "test barrier was not released"
            return '{"text":"must not escape after revoke"}'

    model = PluginEngineModelAccess(
        Client(), cancellation=None, max_calls=2, max_chars=100
    )
    outcome: list[object] = []

    def invoke() -> None:
        try:
            outcome.append(model.complete("prompt"))
        except BaseException as exc:
            outcome.append(exc)

    worker = Thread(target=invoke, daemon=True)
    worker.start()
    assert entered.wait(2), "model never entered raw I/O"
    release_plugin_engine_ports(model)
    with pytest.raises(AskEnginePortError) as rejected:
        model.complete("new prompt")
    assert rejected.value.code == "plugin_engine_failed"
    unblock.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], AskEnginePortError)
    assert outcome[0].code == "plugin_engine_failed"


def test_later_port_construction_failure_revokes_earlier_retrieval(
    monkeypatch,
):
    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.search")),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
        ask_engine_visible_sources=lambda _notebook_id: (),
        ask_engine_hidden_sources=lambda _notebook_id, _actor_id: (),
    )
    before = len(plugin_ports._RETRIEVAL_STATES)
    # Disable destructor cleanup so this assertion proves the construction
    # block's explicit finally owns the already-created retrieval port.
    monkeypatch.setattr(PluginRetrievalAccess, "__del__", lambda _self: None)

    def fail_model_resolution(_workload_id):
        raise RuntimeError("deployment binding failed")

    monkeypatch.setattr(service.model_clients, "chat", fail_model_resolution)
    with pytest.raises(AskPluginEngineError) as rejected:
        service.ask(
            "nb", AskRequest(question="question", mode="alpha.search"),
            user_id="user",
        )

    assert rejected.value.code == "plugin_engine_failed"
    assert len(plugin_ports._RETRIEVAL_STATES) == before


def test_citation_admission_rejects_forged_and_cross_run_handles():
    first = _retrieval_access()
    first_key = first.search("evidence", 1)[0].evidence_key
    second = _retrieval_access(hit=_hit(element_id="element-2"))
    second_key = second.search("evidence", 1)[0].evidence_key
    assert first_key != second_key, "a run-local handle must not replay in a new run"

    with pytest.raises(AskEnginePortError) as forged:
        admit_plugin_engine_result(second, "伪造 [k1]", ("pe-forged",))
    assert forged.value.code == "plugin_engine_unverified_citation"

    with pytest.raises(AskEnginePortError) as replayed:
        admit_plugin_engine_result(second, "重放 [k1]", (first_key,))
    assert replayed.value.code == "plugin_engine_unverified_citation"

    with pytest.raises(AskEnginePortError) as unbound:
        admit_plugin_engine_result(second, "没有引用标记", (second_key,))
    assert unbound.value.code == "plugin_engine_unverified_citation"

    answer, records = admit_plugin_engine_result(
        second, "兼容标记【k1】", (second_key,)
    )
    assert answer == "兼容标记[k1]"
    assert records[0].element_id == "element-2"


def test_citation_admission_requires_a_marker_for_every_admitted_record():
    hits = iter((
        _hit(element_id="element-1"),
        _hit(element_id="element-2"),
    ))
    access = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: ("source-1",),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [next(hits)],
        source_info=lambda _source_ids: {"source-1": {"title": "权威来源"}},
        max_k=1,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )
    keys = (
        access.search("first", 1)[0].evidence_key,
        access.search("second", 1)[0].evidence_key,
    )

    with pytest.raises(AskEnginePortError) as partial:
        admit_plugin_engine_result(access, "只绑定第一条 [k1]", keys)
    assert partial.value.code == "plugin_engine_unverified_citation"


@pytest.fixture
def scoped_sql_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plugin-scope.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    alice = repo.create_user("a00123456", "password-12")
    bob = repo.create_user("b00123456", "password-12")
    token = set_request_user(alice)
    try:
        active = repo.create_notebook(NotebookCreate(name="shared active"))
        base = repo.create_notebook(NotebookCreate(name="reference"))
    finally:
        reset_request_user(token)
    repo._runtime.sharing.add_member(active.id, bob.id)
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(active.id, [base.id], alice.id)

    def source(notebook_id: str, source_id: str, source_type: str, memory_id=""):
        repo._runtime.source_store.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="active",
            parse_status="parsed",
            file_name="",
            file_path="",
            file_size=0,
            file_hash="",
            summary="",
            doc_type="",
            memory_id=memory_id,
        )

    source(active.id, "source-selected", "pdf")
    source(active.id, "source-outside", "pdf")
    source(active.id, "source-knowhow", "knowhow")
    source(base.id, "source-base", "pdf")
    now = "2026-08-25T00:00:00+00:00"
    for actor, memory_id, source_id in (
        (alice, "memory-alice", "source-memory-alice"),
        (bob, "memory-bob", "source-memory-bob"),
    ):
        with repo._write() as db:
            db.execute(
                "INSERT INTO memory_items"
                "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
                "origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed',?,?,?,?)",
                (memory_id, active.id, actor.id, "private", "private", now, now),
            )
        source(active.id, source_id, "memory", memory_id)
    return repo, active.id, base.id, alice.id, bob.id


def _live_scoped_access(repo, active_id: str, actor_id: str):
    return PluginRetrievalAccess(
        active_notebook_id=active_id,
        actor_id=actor_id,
        cancellation=None,
        participant_notebook_ids=repo._runtime.notebook_store.participant_notebook_ids,
        all_visible_source_ids=repo._runtime.source_store.all_visible_source_ids,
        hidden_source_ids=repo._runtime.source_store.hidden_source_ids,
        search_elements=repo.retrieval.federated_retrieve_elements,
        source_info=repo._runtime.evidence_context.citation_source_info,
        max_k=1,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )


def test_plugin_port_pushes_source_and_base_scope_before_candidate_limit(
    scoped_sql_repo, monkeypatch
):
    repo, active_id, _base_id, _alice_id, bob_id = scoped_sql_repo
    seen: list[tuple[str, tuple[str, ...], int]] = []
    candidates = repo.retrieval.candidates

    def before_limit(notebook_id, _query, *, recall, allowed_source_ids):
        seen.append((notebook_id, tuple(allowed_source_ids), recall))
        return [], [], None

    monkeypatch.setattr(candidates, "_retrieve_chunks", before_limit)
    local = ResolvedSourceScope(
        mode="include", source_ids=["source-selected"], narrowed=True
    )
    base = BaseNotebookScope(mode="include", notebook_ids=[], narrowed=True)
    with source_scope_context(active_id, local, base):
        access = _live_scoped_access(repo, active_id, bob_id)
        assert access.search("query", 1) == ()

    assert [(notebook_id, sources) for notebook_id, sources, _recall in seen] == [
        (active_id, ("source-selected",)),
    ], (
        "the existing source-scoped candidate seam must receive the predicate "
        "before it performs its configured recall/LIMIT read"
    )


def test_plugin_port_preserves_frozen_scope_and_retrieval_run_in_worker_thread(
    scoped_sql_repo, monkeypatch
):
    repo, active_id, _base_id, _alice_id, bob_id = scoped_sql_repo
    seen: list[tuple[str, tuple[str, ...], object]] = []
    candidates = repo.retrieval.candidates

    def before_limit(notebook_id, _query, *, recall, allowed_source_ids):
        del recall
        seen.append((
            notebook_id,
            tuple(allowed_source_ids),
            current_retrieval_run(),
        ))
        return [], [], None

    monkeypatch.setattr(candidates, "_retrieve_chunks", before_limit)
    local = ResolvedSourceScope(
        mode="include", source_ids=["source-selected"], narrowed=True
    )
    base = BaseNotebookScope(mode="include", notebook_ids=[], narrowed=True)
    outcomes: list[object] = []
    with source_scope_context(active_id, local, base):
        with retrieval_run(run_kind="ask", actor_id=bob_id) as frozen_run:
            access = _live_scoped_access(repo, active_id, bob_id)

            def invoke() -> None:
                try:
                    outcomes.append(access.search("query", 1))
                except BaseException as exc:
                    outcomes.append(exc)

            worker = Thread(target=invoke, daemon=True)
            worker.start()
            worker.join(2)

    assert not worker.is_alive()
    assert outcomes == [()]
    assert [(notebook_id, sources) for notebook_id, sources, _run in seen] == [
        (active_id, ("source-selected",)),
    ]
    assert seen[0][2] is frozen_run


def test_plugin_port_private_memory_ceiling_uses_the_exact_actor_in_sql(
    scoped_sql_repo, monkeypatch
):
    repo, active_id, base_id, _alice_id, bob_id = scoped_sql_repo
    seen: dict[str, tuple[str, ...]] = {}
    candidates = repo.retrieval.candidates

    def before_limit(notebook_id, _query, *, recall, allowed_source_ids):
        seen[notebook_id] = tuple(allowed_source_ids)
        return [], [], None

    monkeypatch.setattr(candidates, "_retrieve_chunks", before_limit)
    access = _live_scoped_access(repo, active_id, bob_id)
    assert access.search("query", 1) == ()

    assert set(seen[active_id]) == {
        "source-selected",
        "source-outside",
        "source-knowhow",
        "source-memory-bob",
    }
    assert "source-memory-alice" not in seen[active_id]
    assert seen[base_id] == ("source-base",)


def test_model_and_trace_ports_enforce_budgets_without_leaking_raw_errors():
    class Client:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            return '{"text":"完成"}'

    model = PluginEngineModelAccess(
        Client(), cancellation=None, max_calls=1, max_chars=4
    )
    assert model.complete("提示") == "完成"
    with pytest.raises(AskEnginePortError) as exhausted:
        model.complete("再次")
    assert exhausted.value.code == "plugin_engine_model_call_limit"

    trace = PluginEngineTrace(max_steps=2, label_chars=3, detail_chars=4)
    trace.step("abcdef", "123456")
    trace.step("ok", "done")
    trace.step("discarded", "secret tail")
    steps = plugin_engine_trace_steps(trace)
    assert len(steps) == 2
    assert steps[0].summary == "abc"
    assert steps[0].detail == {"detail": "1234", "truncated": True}
    assert steps[-1].detail["truncated"] is True
    assert "secret tail" not in str(steps)


def test_full_plugin_ask_admits_core_citations_and_persists_mode():
    def answer(_context, retrieval, _model, trace):
        evidence = retrieval.search("evidence", 1)
        trace.step("检索", "命中一条")
        return AskEngineResult("结论 [k1]", (evidence[0].evidence_key,))

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.search", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
        ask_engine_visible_sources=lambda _notebook_id: ("source-1",),
        ask_engine_hidden_sources=lambda _notebook_id, _actor_id: (),
    )
    service.retrieval.federated_retrieve_elements = (
        lambda *_args, **_kwargs: [_hit()]
    )
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    response = service.ask(
        "nb", AskRequest(question="问题", mode="alpha.search"), user_id="user"
    )
    assert response.answer_id == "answer-1"
    assert response.mode == "alpha.search"
    assert response.answer == "结论 [k1]"
    assert response.citations[0].source_id == "source-1"
    assert response.citations[0].element_id == "element-1"
    assert response.anchors[0].key == "k1"
    assert response.reasoning_trace[0].step_type == "plugin"


def test_plugin_cannot_mutate_the_core_request_identity_before_commit():
    def mutate(context, _retrieval, _model, _trace):
        object.__setattr__(context, "question", "changed")
        return AskEngineResult("answer", ())

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.mutating", answer=mutate)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
    )
    service.retrieval.federated_retrieve_elements = lambda *_args, **_kwargs: []

    with pytest.raises(StageBoundaryError, match="request identity changed"):
        service.ask(
            "nb", AskRequest(question="original", mode="alpha.mutating"),
            user_id="user",
        )


def test_host_sanitizes_plugin_authored_port_codes_and_observability():
    def fail(context, _retrieval, _model, _trace):
        raise AskEnginePortError(f"secret_{context.question}")

    events: list[dict[str, object]] = []
    runtime = build_extension_runtime(
        (_bundle("alpha", _Provider("alpha.fail", answer=fail)),),
        event_sink=events.append,
    )
    context = SimpleNamespace(question="private question")
    with pytest.raises(AskPluginEngineError) as rejected:
        runtime.ask_engines.answer(
            "alpha.fail", context, object(), object(), object()
        )
    assert rejected.value.code == "plugin_engine_failed"
    assert len(events) == 1
    assert set(events[0]) == {
        "kind", "plugin_id", "mode_id", "stage", "status", "reason_code",
        "duration_ms", "citation_count",
    }
    assert "private question" not in str(events[0])
    assert events[0]["reason_code"] == "plugin_engine_failed"


def test_ask_modes_projection_is_sanitized_and_availability_filtered(monkeypatch):
    unavailable = lambda _context: Availability(
        AvailabilityStatus.UNAVAILABLE, "deployment_internal_reason"
    )
    visible = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.search", requires_kg=True)),
    ))
    hidden = build_extension_runtime((
        _bundle(
            "secret",
            _Provider("secret.search", label="internal label"),
            availability=unavailable,
        ),
    ))

    monkeypatch.setattr(
        ask_routes,
        "application_extension_runtime",
        lambda: visible,
    )
    rows = ask_routes.ask_modes()
    plugin = next(row for row in rows if row["id"] == "alpha.search")
    assert plugin == {
        "id": "alpha.search",
        "group": "extension",
        "label": "企业检索",
        "desc": "使用部署内检索策略回答",
        "requires_kg": True,
        "streaming": False,
        "streams_trace": False,
    }
    assert not ({"plugin_id", "contribution_id", "reason", "endpoint"} & plugin.keys())

    monkeypatch.setattr(
        ask_routes,
        "application_extension_runtime",
        lambda: hidden,
    )
    assert all(row["id"] != "secret.search" for row in ask_routes.ask_modes())
    assert "secret.search" not in ask_routes._valid_ask_mode_ids()
    with pytest.raises(UnknownAskMode):
        resolve_mode("secret.search", ask_routes._extension_ask_modes())

    service = _minimal_ask_service(ask_engine_host=hidden.ask_engines)
    with pytest.raises(UnknownAskMode):
        service.ask(
            "nb", AskRequest(question="question", mode="secret.search"),
            user_id="user",
        )


def test_citation_admission_rejects_residual_citation_like_markers():
    """畸形「引用样」括号组整份拒绝(codex #602 R6 P1):`[k1, nope]` 不被
    LOOSE_MARKER_RE 匹配、会原样留在正文里,渲染成从未被核验的引用外观。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    with pytest.raises(AskEnginePortError) as malformed:
        admit_plugin_engine_result(access, "合法 [k1] 加畸形 [k1, nope]", (key,))
    assert malformed.value.code == "plugin_engine_unverified_citation"

    cjk = _retrieval_access(hit=_hit(element_id="element-cjk"))
    cjk_key = cjk.search("evidence", 1)[0].evidence_key
    with pytest.raises(AskEnginePortError) as cjk_malformed:
        admit_plugin_engine_result(cjk, "【k1】与【k2、nope】", (cjk_key,))
    assert cjk_malformed.value.code == "plugin_engine_unverified_citation"

    # 合法组照常通过——归一化输出自己写回的组不被残留扫描误伤。
    ok = _retrieval_access(hit=_hit(element_id="element-ok"))
    ok_key = ok.search("evidence", 1)[0].evidence_key
    answer, _records = admit_plugin_engine_result(ok, "正文 [k1] 结尾", (ok_key,))
    assert answer == "正文 [k1] 结尾"


def test_search_kg_issues_element_addressed_handles_admitted_as_citations():
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")]
    )

    hits = access.search_kg("退火", 2)

    assert len(hits) == 1
    assert hits[0].object_type == "concept"
    assert hits[0].evidence_key
    assert "退火" in hits[0].text and "缓慢冷却" in hits[0].text
    assert hits[0].location_label == "第 3 页"
    answer, records = admit_plugin_engine_result(
        access, "结论 [k1]", (hits[0].evidence_key,)
    )
    assert answer == "结论 [k1]"
    # A cited knowledge object opens its first live evidence ELEMENT.
    assert records[0].element_id == "element-1"
    assert records[0].source_id == "source-1"
    assert records[0].source_file_name == "paper.pdf"


def test_search_kg_pushes_the_frozen_source_keys_into_the_candidate_seam():
    """其他成员私有 Memory 派生的对象结构上不进结果:端口冻结的 source key 必须
    原样下推进候选接缝(在它的 LIMIT 之前),不能靠事后过滤。"""
    seen: list[object] = []

    def search_knowledge(_active_notebook_id, _query, **kwargs):
        seen.append(kwargs.get("allowed_source_keys", "MISSING"))
        return []

    access = _kg_access(
        search_knowledge=search_knowledge, visible=("source-1", "source-2")
    )

    assert access.search_kg("退火", 2) == ()
    assert seen == [(("notebook-1", "source-1"), ("notebook-1", "source-2"))]


def test_knowledge_hit_without_a_live_source_is_context_only():
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")],
        source_info=lambda _source_ids: {},
    )

    hits = access.search_kg("退火", 2)

    assert hits[0].evidence_key == ""
    assert hits[0].source_title == "" and hits[0].location_label == ""
    assert "退火" in hits[0].text
    # Not citable ...
    with pytest.raises(AskEnginePortError) as rejected:
        admit_plugin_engine_result(access, "结论 [k1]", ("",))
    assert rejected.value.code == "plugin_engine_unverified_citation"
    # ... and not an expansion anchor either.
    assert access.kg_neighbors("", 2) == ()


def test_bound_evidence_requires_the_source_to_be_in_the_frozen_snapshot_not_just_resolvable():
    """P1: `_bound_evidence` 的两个判据必须都是承重的——快照成员判定
    (`state.source_origin.get(source_id) != origin`)必须在元数据解析之前
    生效。一个证据来源即使仍能被 `source_info` 解析出元数据(比如它仍然
    真实存在于数据库里),只要不在这个 run 冻结的 `source_keys` 集合内,
    就必须判越界、不可引用——不能靠“元数据解析不出来”这唯一一条判据兜底。
    删掉 `_bound_evidence` 的这道 origin 比对(保留元数据解析)会让全部
    既有用例通过,只有这条新用例能钉住它。"""
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(
            notebook_id="notebook-1",
            evidence=[_evidence(source_id="source-outside-scope")],
        )],
        # source_info 仍能解析出元数据(该来源本身仍然存在),但它压根不在
        # 这个 run 冻结的可见来源集合(visible=("source-1",))内——正是
        # `source_origin` 判据要拦的那种「resolvable 但不在快照里」的证据。
        source_info=lambda source_ids: {
            source_id: {"title": "越界来源", "file_name": "outside.pdf"}
            for source_id in source_ids
        },
    )

    hits = access.search_kg("退火", 2)

    assert hits[0].evidence_key == ""
    assert hits[0].source_title == "" and hits[0].location_label == ""
    with pytest.raises(AskEnginePortError) as rejected:
        admit_plugin_engine_result(access, "结论 [k1]", ("",))
    assert rejected.value.code == "plugin_engine_unverified_citation"


def test_kg_budget_is_shared_and_early_exits_never_spend_it():
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")],
        kg_max_calls=1,
    )

    assert access.search_kg("退火", 1, ("not-a-type",)) == ()
    assert access.kg_neighbors("pe-unknown", 1) == ()
    assert access.kg_neighbors("pe-unknown", 1, edge_type="not-an-edge") == ()

    anchor = access.search_kg("退火", 1)[0].evidence_key
    with pytest.raises(AskEnginePortError) as exhausted:
        access.kg_neighbors(anchor, 1)
    assert exhausted.value.code == "plugin_engine_kg_call_limit"
    with pytest.raises(AskEnginePortError) as also_exhausted:
        access.search_kg("退火", 1)
    assert also_exhausted.value.code == "plugin_engine_kg_call_limit"


def test_search_kg_rejects_malformed_object_types_before_spending_budget():
    """P3: 容器必须是 tuple/list、元素必须是 str。裸字符串本身就是
    「字符组成的可迭代对象」,`tuple("concept")` 会被拆成
    `('c','o','n','c','e','p','t')`——不是「未知类型名被静默丢弃」,而是
    请求本身就不是模型想表达的样子,必须响亮拒绝。两次畸形调用都不能消耗
    共享 KG 预算(校验必须排在 `_claim_kg_call` 之前)。"""
    access = _kg_access(kg_max_calls=2)

    with pytest.raises(AskEnginePortError) as bare_string:
        access.search_kg("退火", 1, object_types="concept")
    assert bare_string.value.code == "plugin_engine_invalid_kg_request"

    with pytest.raises(AskEnginePortError) as non_str_element:
        access.search_kg("退火", 1, object_types=(1,))
    assert non_str_element.value.code == "plugin_engine_invalid_kg_request"

    # Neither malformed call spent the shared KG budget: exactly
    # `kg_max_calls` well-formed calls still succeed before exhaustion.
    assert access.search_kg("退火", 1) == ()
    assert access.search_kg("退火", 1) == ()
    with pytest.raises(AskEnginePortError) as exhausted:
        access.search_kg("退火", 1)
    assert exhausted.value.code == "plugin_engine_kg_call_limit"


def test_search_kg_all_empty_text_hit_is_context_only_not_a_ledger_entry():
    """P3: name/definition/quoted_span 全空时 `_kg_text` 组出空串——即使
    证据来源仍然存活,也没有任何用户可核验的文字,因此不能签发可引用 key、
    不能进 ledger、也不能当 `kg_neighbors` 的锚点。"""
    empty_evidence = Evidence(
        source_id="source-1", source_title="", element_id="element-1",
        element_type="paragraph", location_label="第 3 页",
        quoted_span="", confidence=0.9,
    )
    empty_hit = RetrievedKnowledge(
        object_id="ko-empty", object_type="concept",
        payload={}, evidence=[empty_evidence], score=0.8,
        notebook_id="notebook-1",
    )
    access = _kg_access(search_knowledge=lambda *_args, **_kwargs: [empty_hit])

    hits = access.search_kg("退火", 1)

    assert hits[0].evidence_key == "" and hits[0].text == ""
    with pytest.raises(AskEnginePortError) as rejected:
        admit_plugin_engine_result(access, "结论 [k1]", ("",))
    assert rejected.value.code == "plugin_engine_unverified_citation"
    # Not registered as a `kg_neighbors` anchor either.
    assert access.kg_neighbors("", 2) == ()


def test_kg_neighbors_resolves_only_object_anchors_and_validates_its_request():
    forwarded: list[tuple] = []
    neighbor = _object(
        object_id="ko-2",
        name="淬火",
        evidence=[_evidence(element_id="element-2")],
    )

    def object_neighbors(*args):
        forwarded.append(args)
        return NeighborExpansion([neighbor], False)

    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")],
        object_neighbors=object_neighbors,
        kg_max_calls=4,
    )
    anchor = access.search_kg("退火", 1)[0].evidence_key
    element_key = access.search("evidence", 1)[0].evidence_key

    assert element_key != anchor
    assert access.kg_neighbors(element_key, 1) == ()
    assert access.kg_neighbors("pe-forged", 1) == ()
    assert access.kg_neighbors(anchor, 1, edge_type="knows_about") == ()
    with pytest.raises(AskEnginePortError) as bad_direction:
        access.kg_neighbors(anchor, 1, direction="downstream")
    assert bad_direction.value.code == "plugin_engine_invalid_kg_request"
    with pytest.raises(AskEnginePortError) as bad_key:
        access.kg_neighbors(None, 1)
    assert bad_key.value.code == "plugin_engine_invalid_evidence_key"
    assert forwarded == []

    hits = access.kg_neighbors(anchor, 1, edge_type="depends_on", direction="out")

    assert forwarded == [("notebook-1", "ko-1", "depends_on", "out")]
    assert hits[0].object_type == "concept" and hits[0].evidence_key
    _answer, records = admit_plugin_engine_result(
        access, "邻居 [k1]", (hits[0].evidence_key,)
    )
    assert records[0].element_id == "element-2"


def test_kg_neighbors_resolves_origin_against_the_anchors_own_library_not_active():
    """P1(codex 双评审复现):生产 seam `_retrieve_neighbors` 构造的
    `RetrievedKnowledge` 不设 `.notebook_id`(默认 ""),旧的 `_hit_origin` 会
    回退 active 库——挂载参考库锚点的每个邻居因此被 `_bound_evidence` 误判
    越界、evidence_key 全部降级为 ""(context-only、不可引用)。此用例的两个
    participant(active="notebook-1"、base="base-1")互不相同,才会真正暴露
    这个 bug:单 participant 场景下「回退 active」与「回退锚点自己的库」永远
    算出同一个值,盖不住它。"""
    anchor_object = _object(
        object_id="ko-anchor", name="基座锚点",
        evidence=[_evidence(source_id="source-base", element_id="element-anchor")],
        notebook_id="base-1",
    )
    # 生产形状:kg_neighbors 命中不带 notebook_id——它的证据来源属于 base-1,
    # 但 hit 本身对这一点保持沉默,必须靠端口从锚点自己的库推出正确 origin。
    shared_object_via_neighbors = _object(
        object_id="ko-shared", name="共享概念",
        evidence=[_evidence(source_id="source-base", element_id="element-shared")],
    )

    def object_neighbors(_notebook_id, _object_id, _edge_type, _direction):
        return NeighborExpansion([shared_object_via_neighbors], False)

    calls = {"n": 0}

    def search_knowledge(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [anchor_object]
        # 第二次调用模拟 federated_retrieve 命中同一个对象——生产上它总是
        # 标注真实 notebook_id(`_federated_retrieve_impl`)。
        return [_object(
            object_id="ko-shared", name="共享概念",
            evidence=[_evidence(source_id="source-base", element_id="element-shared")],
            notebook_id="base-1",
        )]

    access = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1", "base-1"),
        all_visible_source_ids=lambda notebook_id: (
            ("source-1",) if notebook_id == "notebook-1" else ("source-base",)
        ),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [],
        source_info=lambda source_ids: {
            source_id: {"title": "基座来源", "file_name": "base.pdf"}
            for source_id in source_ids
            if source_id in ("source-1", "source-base")
        },
        search_knowledge=search_knowledge,
        object_neighbors=object_neighbors,
        collection_overview=lambda _notebook_id: "",
        kg_max_calls=4,
        max_k=2,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )

    anchor_key = access.search_kg("查询", 1)[0].evidence_key
    assert anchor_key, "the base-1 anchor itself must be citable"

    neighbor_hits = access.kg_neighbors(anchor_key, 1)
    assert len(neighbor_hits) == 1
    neighbor_key = neighbor_hits[0].evidence_key
    assert neighbor_key, (
        "a mounted-base anchor's neighbor must resolve against the anchor's "
        "own participant library (base-1), not the active notebook "
        "(notebook-1) -- otherwise its base-1 evidence source is judged "
        "out of the frozen snapshot and the neighbor is silently downgraded "
        "to context-only"
    )

    # Same knowledge object reached via search_kg and kg_neighbors reuses one
    # run-local handle (the `kg_reverse` identity is keyed on the resolved
    # origin, so a wrong origin would also break this dedup).
    reused_key = access.search_kg("查询二", 1)[0].evidence_key
    assert reused_key == neighbor_key, (
        "the same knowledge object reached through search_kg and "
        "kg_neighbors must share one evidence handle"
    )


def test_kg_neighbors_channel_closes_for_narrowed_active_runs_but_not_base_anchors():
    """codex #603 R1 P2:一跳展开的有界读取之下没有来源谓词,真收窄的 active run
    里界外邻居会占满窗口、filter-after 救不回没被返回的行。通道按内建纪律整体
    关闭:active 锚点返回空、零底层调用、零预算;参考库锚点不受本地收窄影响
    (库维度是整库勾选,库内没有逐源收窄,两维正交是登记契约)。"""
    active_object = _object(
        object_id="ko-active", name="本库概念",
        evidence=[_evidence(source_id="source-1", element_id="element-a")],
        notebook_id="notebook-1",
    )
    base_object = _object(
        object_id="ko-base", name="基座概念",
        evidence=[_evidence(source_id="source-base", element_id="element-b")],
        notebook_id="base-1",
    )
    neighbor_calls: list[str] = []

    def object_neighbors(notebook_id, _object_id, _edge_type, _direction):
        neighbor_calls.append(notebook_id)
        return NeighborExpansion([_object(
            object_id="ko-neighbor", name="基座邻居",
            evidence=[_evidence(source_id="source-base", element_id="element-n")],
        )], False)

    local = ResolvedSourceScope(
        mode="include", source_ids=["source-1"], narrowed=True
    )
    with source_scope_context("notebook-1", local, None):
        access = PluginRetrievalAccess(
            active_notebook_id="notebook-1",
            actor_id="user-1",
            cancellation=None,
            participant_notebook_ids=lambda _nb: ("notebook-1", "base-1"),
            all_visible_source_ids=lambda notebook_id: (
                ("source-1",) if notebook_id == "notebook-1"
                else ("source-base",)
            ),
            hidden_source_ids=lambda _nb, _actor: (),
            search_elements=lambda *_args, **_kwargs: [],
            source_info=lambda source_ids: {
                source_id: {"title": "来源", "file_name": "f.pdf"}
                for source_id in source_ids
            },
            search_knowledge=lambda *_a, **_k: [active_object, base_object],
            object_neighbors=object_neighbors,
            collection_overview=lambda _nb: "",
            kg_max_calls=4,
            max_k=2,
            max_calls=2,
            evidence_chars=100,
            query_chars=100,
        )
        hits = access.search_kg("查询", 2)
        active_key, base_key = hits[0].evidence_key, hits[1].evidence_key
        assert active_key and base_key

        assert access.kg_neighbors(active_key, 1) == ()
        assert neighbor_calls == [], (
            "a narrowed active-notebook anchor must close the channel before "
            "any underlying expansion I/O"
        )

        base_neighbors = access.kg_neighbors(base_key, 1)
        assert len(base_neighbors) == 1 and base_neighbors[0].evidence_key
        assert neighbor_calls == ["base-1"], (
            "a mounted-base anchor stays open under LOCAL narrowing -- the "
            "library dimension is orthogonal to source_scope_restricted"
        )


def test_kg_overview_is_bounded_and_computed_once_per_run():
    calls: list[str] = []

    def collection_overview(notebook_id: str) -> str:
        calls.append(notebook_id)
        return "地图" * 1000

    access = _kg_access(collection_overview=collection_overview)

    first = access.kg_overview()
    second = access.kg_overview()

    assert calls == ["notebook-1"]
    assert first == second
    assert len(first) == plugin_ports.KG_OVERVIEW_MAX_CHARS


def test_kg_overview_is_single_flight_under_concurrent_first_calls():
    """P2:读 memo→计算→写 memo 此前不在同一临界区,并发首调各自观察到空
    memo、各自重复触发底层全量计数(生产实测 8 并发线程 8 次调用)。用 Event
    卡住第一个调用者、Barrier 让 8 个线程一起起跑,证明底层 callable 只被
    真正调用一次。gate 之后的短暂 `time.sleep` 只是给其余线程一个排到锁
    后面的机会(镜像 `test_scale_index_version_singleflight.py` 里同一类
    并发首调证明手法),真正的判据是 `calls`/`outcomes` 的确定性断言,不是
    计时本身。"""
    calls: list[str] = []
    gate = Event()
    release = Event()

    def collection_overview(notebook_id: str) -> str:
        calls.append(notebook_id)
        gate.set()
        assert release.wait(2), "single-flight loader stuck waiting on release"
        return "地图"

    access = _kg_access(collection_overview=collection_overview)
    worker_count = 8
    barrier = Barrier(worker_count)
    outcomes: list[str] = []
    outcomes_lock = Lock()

    def worker() -> None:
        barrier.wait(timeout=5)
        value = access.kg_overview()
        with outcomes_lock:
            outcomes.append(value)

    threads = [Thread(target=worker, daemon=True) for _ in range(worker_count)]
    for thread in threads:
        thread.start()

    assert gate.wait(2), "no caller ever entered the underlying computation"
    # Give the other barrier-released callers a chance to queue behind the
    # per-run overview lock before releasing the one in-flight computation.
    time.sleep(0.05)
    release.set()

    for thread in threads:
        thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert calls == ["notebook-1"], (
        "kg_overview's read-compute-write must be one atomic critical "
        "section: any second call must observe the already-filled memo "
        "rather than recomputing"
    )
    assert outcomes == ["地图"] * worker_count


def test_the_same_knowledge_object_reuses_one_handle_across_calls():
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")],
        kg_max_calls=3,
    )

    first = access.search_kg("退火", 1)[0].evidence_key
    second = access.search_kg("annealing", 1)[0].evidence_key

    assert first and first == second


def test_ask_service_wires_the_kg_seats_and_persists_object_citations():
    seen: list[tuple] = []

    def answer(_context, retrieval, _model, trace):
        hits = retrieval.search_kg("图谱问题", 2)
        seen.append(hits)
        trace.step("图谱检索", f"概览 {len(retrieval.kg_overview())} 字")
        # Exercises the `object_neighbors` seat too (P3): a typo in the
        # `getattr(self.retrieval, "retrieve_neighbors", None)` attribute
        # name in ask_service.py would otherwise resolve silently to `None`
        # and only surface as a generic `plugin_engine_failed` the first time
        # some provider actually calls `kg_neighbors` -- which no test here
        # exercised before this addition.
        neighbor_hits = retrieval.kg_neighbors(hits[0].evidence_key, 1)
        seen.append(neighbor_hits)
        return AskEngineResult(
            "结论 [k1][k2]",
            (hits[0].evidence_key, neighbor_hits[0].evidence_key),
        )

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.kg", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
        ask_engine_visible_sources=lambda _notebook_id: ("source-1",),
        ask_engine_hidden_sources=lambda _notebook_id, _actor_id: (),
    )
    service.retrieval.federated_retrieve_elements = lambda *_args, **_kwargs: []
    service.retrieval.federated_retrieve = lambda *_args, **_kwargs: [
        _object(notebook_id="nb")
    ]
    service.retrieval.retrieve_neighbors = lambda *_args, **_kwargs: NeighborExpansion(
        [_object(
            object_id="ko-2", name="淬火",
            evidence=[_evidence(element_id="element-2")],
        )],
        False,
    )
    service.collection_catalog = SimpleNamespace(
        collection_map_text=lambda _notebook_id: "[Collections in scope] …"
    )
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    response = service.ask(
        "nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user"
    )

    assert response.mode == "alpha.kg"
    assert seen[0][0].object_type == "concept"
    assert seen[1][0].object_type == "concept"
    assert response.answer == "结论 [k1][k2]"
    assert response.citations[0].source_id == "source-1"
    assert response.citations[0].element_id == "element-1"
    assert response.citations[1].element_id == "element-2"
    assert response.anchors[0].key == "k1"
    assert response.anchors[1].key == "k2"


def test_kg_citation_and_anchor_carry_the_verbatim_grounding_excerpt_not_the_model_summary():
    """P2: `_kg_text` 的组合文本(模型抽取产物的 name+definition)此前原样
    进了持久化 Citation.quoted_span 与 anchor.snippet——用户会把一句模型
    撰写的话当成原文引文。绑定证据自己的 `quoted_span`(逐字原文)必须
    优先;anchor.object_type 也必须是真实节点类型而不是硬编码 "element"。"""

    def answer(_context, retrieval, _model, _trace):
        hits = retrieval.search_kg("退火", 1)
        return AskEngineResult("结论 [k1]", (hits[0].evidence_key,))

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.kg", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
        ask_engine_visible_sources=lambda _notebook_id: ("source-1",),
        ask_engine_hidden_sources=lambda _notebook_id, _actor_id: (),
    )
    service.retrieval.federated_retrieve_elements = lambda *_args, **_kwargs: []
    service.retrieval.federated_retrieve = lambda *_args, **_kwargs: [
        _object(notebook_id="nb")
    ]
    service.collection_catalog = SimpleNamespace(
        collection_map_text=lambda _notebook_id: ""
    )
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    response = service.ask(
        "nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user"
    )

    # `_evidence()`'s quoted_span ("对象出处摘录") is the verbatim excerpt;
    # `_object()`'s payload name/definition ("退火"/"把材料缓慢冷却的热处理")
    # is the model-authored summary that must NOT leak into either field.
    assert response.citations[0].quoted_span == "对象出处摘录"
    assert "退火" not in response.citations[0].quoted_span
    assert "缓慢冷却" not in response.citations[0].quoted_span
    assert response.anchors[0].snippet == "对象出处摘录"
    # Same-sourcing guard: anchor.object_id IS an element id (the object's
    # first surviving evidence element — the registered citation contract:
    # a cited KG handle opens that element, never a graph node view), so
    # object_type must stay "element". Any non-element value here makes the
    # browser render a "在知识图谱中定位" button that feeds an element id to
    # the graph as a node id — a click that can never succeed.
    assert response.anchors[0].object_type == "element"
    assert response.anchors[0].object_id == response.anchors[0].element_id
