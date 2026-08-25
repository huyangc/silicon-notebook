from __future__ import annotations

from dataclasses import dataclass, fields
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.api import ask_routes
from app.application.ask_reasoning import StageBoundaryError
from app.core.config import Settings
from app.domain.ask_engine import AskPluginEngineError
from app.domain.retrieval import RetrievedElement
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
        "evidence_key", "text", "source_title", "location_label",
    }
    assert EngineEvidence.__dataclass_params__.frozen is True
    assert "__dict__" not in EngineEvidence.__slots__
    evidence = EngineEvidence("pe-test", "text", "title", "page")
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
