from __future__ import annotations

import re
import time
from dataclasses import dataclass, fields
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from app.api import ask_routes
from app.application.ask_reasoning import StageBoundaryError
from app.core.config import Settings
from app.domain.cancellation import AskCancelled
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
from app.extensions.ask_engine import AskEngineHost
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
    complete_plugin_engine_trace,
    finish_plugin_engine_trace,
    plugin_engine_trace_steps,
    release_plugin_engine_ports,
)
from app.services.ask_modes import UnknownAskMode, resolve_mode
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys
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


def _all_live(element_ids):
    """Element-liveness fake: every requested id is live, with no source
    override (empty rows make `_bound_evidence` fall back to the binding's
    own source claim, preserving each test's evidence wiring verbatim)."""
    return {element_id: {} for element_id in element_ids}


def _kg_access(
    *,
    search_knowledge=None,
    object_neighbors=None,
    collection_overview=None,
    search_elements=None,
    source_info=None,
    evidence_elements=None,
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
        evidence_elements=evidence_elements or _all_live,
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
    assert all(mode.streaming for mode in runtime.ask_engines.modes())
    assert runtime.ask_engines.mode("alpha.search").streaming is True

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


def test_citation_admission_strips_forged_and_cross_run_handles_and_discloses():
    """用户拍板的降级语义:校验不过的引用/标记被摘除、答案保留,问题折进
    notes 供调用方落进持久化轨迹披露——不再整份拒绝(fail-closed 的旧行为
    见下面对 forged/replayed 分支的断言:两者都不再 raise)。"""
    first = _retrieval_access()
    first_key = first.search("evidence", 1)[0].evidence_key
    second = _retrieval_access(hit=_hit(element_id="element-2"))
    second_key = second.search("evidence", 1)[0].evidence_key
    assert first_key != second_key, "a run-local handle must not replay in a new run"

    forged_answer, forged_records, forged_notes = admit_plugin_engine_result(
        second, "伪造 [k1]", ("pe-forged",)
    )
    assert forged_records == ()
    assert "[k1]" not in forged_answer
    assert any("无法核验，已移除" in note for note in forged_notes)
    assert any("正文中" in note and "无法核验" in note for note in forged_notes)

    replayed_answer, replayed_records, replayed_notes = admit_plugin_engine_result(
        second, "重放 [k1]", (first_key,)
    )
    assert replayed_records == ()
    assert "[k1]" not in replayed_answer
    assert any("无法核验，已移除" in note for note in replayed_notes)
    assert any("正文中" in note and "无法核验" in note for note in replayed_notes)

    unbound_answer, unbound_records, unbound_notes = admit_plugin_engine_result(
        second, "没有引用标记", (second_key,)
    )
    assert unbound_answer == "没有引用标记"
    assert len(unbound_records) == 1
    assert unbound_records[0].element_id == "element-2"
    assert unbound_notes == ("1 条引用未在正文中被引用，仅列入引用列表",)

    answer, records, notes = admit_plugin_engine_result(
        second, "兼容标记【k1】", (second_key,)
    )
    assert answer == "兼容标记[k1]"
    assert records[0].element_id == "element-2"
    assert notes == ()


def test_citation_admission_strips_unmarked_records_and_discloses():
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

    answer, records, notes = admit_plugin_engine_result(
        access, "只绑定第一条 [k1]", keys
    )
    assert answer == "只绑定第一条 [k1]"
    assert len(records) == 2
    assert notes == ("1 条引用未在正文中被引用，仅列入引用列表",)


def test_citation_admission_truncates_citations_beyond_the_issuable_ceiling():
    access = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: ("source-1",),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [_hit()],
        source_info=lambda _source_ids: {"source-1": {"title": "权威来源"}},
        max_k=1,
        max_calls=1,
        evidence_chars=100,
        query_chars=100,
    )
    key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "[k1]", (key, key)
    )

    assert answer == "[k1]"
    assert len(records) == 1
    assert any("超过本次可签发上限" in note for note in notes)
    assert not any("未在正文中被引用" in note for note in notes)


def test_citation_admission_compacts_indexes_after_stripping_a_forged_key():
    access = _retrieval_access()
    legit_key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1] 与 [k2]", ("pe-forged", legit_key)
    )

    assert answer == "结论  与 [k1]"
    assert len(records) == 1
    assert records[0].element_id == "element-1"
    assert any("无法核验，已移除" in note for note in notes)
    assert any("正文中" in note and "无法核验" in note for note in notes)


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


def test_plugin_port_universe_excludes_sources_added_after_an_all_selected_freeze(
    scoped_sql_repo, monkeypatch
):
    """全选冻结(narrowed=False)之后新增的来源不得进入端口宇宙。

    端口在**每次**提问时按 ``all_visible_source_ids``/``hidden_source_ids``
    实时枚举来源;把这份实时清单当天花板下推,等于让冻结之后完成抽取的来源
    参与本次 run。元素检索这条路没有结果侧防线(``RetrievedElement`` 不带
    notebook_id、``filter_retrieval_items`` 不套它,端口自己的签发后复核又正是
    拿这份清单建的 ``source_origin`` 判定),所以
    ``scoped_allowed_source_ids`` 的交集是这里唯一的执法点。

    **变异锚点**:让 ``scoped_allowed_source_ids`` 在带显式清单时原样透传
    (不与冻结清单取交集)→ ``source-drifted`` 出现在 seam 收到的清单里,
    本条报红。
    """
    repo, active_id, base_id, _alice_id, bob_id = scoped_sql_repo
    seen: dict[str, tuple[str, ...]] = {}
    candidates = repo.retrieval.candidates

    def before_limit(notebook_id, _query, *, recall, allowed_source_ids):
        del recall
        seen[notebook_id] = tuple(allowed_source_ids)
        return [], [], None

    monkeypatch.setattr(candidates, "_retrieve_chunks", before_limit)
    frozen = ResolvedSourceScope(
        mode="include",
        source_ids=["source-selected", "source-outside"],
        narrowed=False,
    )
    frozen._hidden_source_ids = ["source-knowhow", "source-memory-bob"]
    frozen._scope_owner_id = bob_id
    # 冻结之后才完成抽取的来源(并发上传):实时枚举看得到它,冻结快照里没有。
    repo._runtime.source_store.insert_source(
        source_id="source-drifted", notebook_id=active_id, title="drifted",
        source_type="pdf", status="active", parse_status="parsed",
        file_name="", file_path="", file_size=0, file_hash="", summary="",
        doc_type="",
    )

    with source_scope_context(active_id, frozen, None):
        access = _live_scoped_access(repo, active_id, bob_id)
        assert access.search("query", 1) == ()

    assert "source-drifted" not in seen[active_id], (
        "冻结之后新增的来源不得通过端口的实时枚举进入本次 run"
    )
    assert set(seen[active_id]) == {
        "source-selected", "source-outside",
        "source-knowhow", "source-memory-bob",
    }, "端口宇宙必须正好是冻结快照 ∩ 实时枚举"
    # 参考库维度不受本地天花板影响,照旧参与。
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

    streamed = []
    trace = PluginEngineTrace(
        max_steps=2,
        label_chars=3,
        detail_chars=4,
        on_trace=streamed.append,
    )
    trace.step("abcdef", "123456")
    trace.step("ok", "done")
    trace.step("discarded", "secret tail")
    trace.step("discarded again", "another secret")
    steps = plugin_engine_trace_steps(trace)
    assert len(steps) == 3
    assert steps[0].summary == "abc"
    assert steps[0].detail == {"detail": "1234", "truncated": True}
    assert steps[-1].summary == "扩展引擎步骤已截断"
    assert steps[-1].detail == {"truncated": True}
    assert "secret tail" not in str(steps)
    assert streamed == list(steps)
    assert "discarded" not in str(streamed)
    assert "another secret" not in str(streamed)


def test_plugin_trace_core_timing_covers_steps_and_terminal_tail():
    now = [10.0]
    streamed = []
    trace = PluginEngineTrace(
        max_steps=2,
        label_chars=20,
        detail_chars=20,
        on_trace=streamed.append,
        clock=lambda: now[0],
    )

    now[0] = 10.125
    trace.step("检索")
    now[0] = 10.5
    trace.step("生成")
    now[0] = 11.25
    finish_plugin_engine_trace(trace)
    now[0] = 99.0  # Core admission time is not plugin execution time.
    terminal = complete_plugin_engine_trace(trace, status="completed")

    steps = plugin_engine_trace_steps(trace)
    assert [step.duration_ms for step in steps] == [125, 375, 750]
    assert sum(step.duration_ms or 0 for step in steps) == 1250
    assert terminal.summary == "扩展引擎执行完成"
    assert streamed == list(steps)
    with pytest.raises(AskEnginePortError) as closed:
        trace.step("late")
    assert closed.value.code == "plugin_engine_failed"


def test_plugin_trace_first_finish_call_owns_terminal_timestamp():
    now = [10.0]
    trace = PluginEngineTrace(
        max_steps=1,
        label_chars=20,
        detail_chars=20,
        clock=lambda: now[0],
    )

    now[0] = 11.25
    finish_plugin_engine_trace(trace)
    now[0] = 99.0
    finish_plugin_engine_trace(trace)
    terminal = complete_plugin_engine_trace(trace, status="completed")

    assert terminal.duration_ms == 1250


def test_plugin_trace_terminal_step_times_provider_that_emits_no_steps():
    now = [20.0]
    streamed = []
    trace = PluginEngineTrace(
        max_steps=1,
        label_chars=20,
        detail_chars=20,
        on_trace=streamed.append,
        clock=lambda: now[0],
    )
    now[0] = 22.5

    finish_plugin_engine_trace(trace)
    complete_plugin_engine_trace(trace, status="failed")

    steps = plugin_engine_trace_steps(trace)
    assert len(steps) == 1
    assert steps[0].summary == "扩展引擎执行失败"
    assert steps[0].duration_ms == 2500
    assert streamed == list(steps)


def test_plugin_trace_preserves_append_and_delivery_order_across_threads():
    first_entered = Event()
    release_first = Event()
    second_calling = Event()
    second_delivered = Event()
    streamed: list[str] = []

    def on_trace(step) -> None:
        if step.summary == "first":
            first_entered.set()
            assert release_first.wait(2), "first trace delivery was not released"
        if step.summary == "second":
            second_delivered.set()
        streamed.append(step.summary)

    trace = PluginEngineTrace(
        max_steps=2,
        label_chars=20,
        detail_chars=20,
        on_trace=on_trace,
    )
    first = Thread(target=lambda: trace.step("first"), daemon=True)
    def send_second() -> None:
        second_calling.set()
        trace.step("second")

    second = Thread(target=send_second, daemon=True)
    first.start()
    assert first_entered.wait(2), "first trace delivery never started"
    second.start()
    try:
        assert second_calling.wait(2), "second trace call never started"
        assert not second_delivered.wait(0.05), (
            "a later trace callback overtook the earlier in-flight delivery"
        )
        assert [step.summary for step in plugin_engine_trace_steps(trace)] == [
            "first"
        ]
    finally:
        release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert streamed == ["first", "second"]
    assert [step.summary for step in plugin_engine_trace_steps(trace)] == streamed


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
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    streamed = []
    response = service.ask(
        "nb",
        AskRequest(question="问题", mode="alpha.search"),
        user_id="user",
        on_trace=streamed.append,
    )
    assert response.answer_id == "answer-1"
    assert response.mode == "alpha.search"
    assert response.answer == "结论 [k1]"
    assert response.citations[0].source_id == "source-1"
    assert response.citations[0].element_id == "element-1"
    assert response.anchors[0].key == "k1"
    assert response.reasoning_trace[0].step_type == "plugin"
    # Clean admission (every citation verified, every marker bound) must not
    # add verification noise. The only core-authored step is the timed engine
    # terminal that keeps the whole provider tail visible.
    assert len(response.reasoning_trace) == 2
    assert response.reasoning_trace[-1].summary == "扩展引擎执行完成"
    assert all(step.duration_ms is not None for step in response.reasoning_trace)
    assert streamed == response.reasoning_trace


def test_plugin_ask_admits_partial_citations_and_discloses_degraded_verification():
    def answer(_context, retrieval, _model, trace):
        evidence = retrieval.search("evidence", 1)
        trace.step("检索", "命中一条")
        return AskEngineResult(
            "结论 [k1] 与 [k2]", (evidence[0].evidence_key, "pe-forged")
        )

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
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    streamed = []
    response = service.ask(
        "nb",
        AskRequest(question="问题", mode="alpha.search"),
        user_id="user",
        on_trace=streamed.append,
    )
    assert response.answer == "结论 [k1] 与 "
    assert response.anchors[0].key == "k1"
    assert len(response.citations) == 1
    assert response.answer_id == "answer-1"
    disclosure = next(
        step for step in response.reasoning_trace
        if step.summary == "引用核验未全部通过"
    )
    assert disclosure.step_type == "plugin"
    assert "无法核验" in disclosure.detail["detail"]
    assert disclosure.duration_ms is None
    assert response.reasoning_trace[-1].summary == "扩展引擎执行完成"
    assert streamed == response.reasoning_trace


def test_plugin_ask_renumbers_a_middle_drop_onto_the_right_record_end_to_end():
    """服务级的重排对账(评审 P2-3):伪造句柄夹在两条合法之间,`[k3]` 必须一路
    走到 anchors 里仍然指向 B——单条存活 record 的用例证明不了这件事。"""
    def answer(_context, retrieval, _model, trace):
        first = retrieval.search("first", 1)[0]
        second = retrieval.search("second", 1)[0]
        trace.step("检索", "命中两条")
        return AskEngineResult(
            "结论 [k1] 与 [k3]",
            (first.evidence_key, "pe-forged", second.evidence_key),
        )

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.search", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _notebook_id: ("nb",),
        ask_engine_visible_sources=lambda _notebook_id: ("source-1",),
        ask_engine_hidden_sources=lambda _notebook_id, _actor_id: (),
    )
    hits = iter((
        [_hit(element_id="element-A")],
        [_hit(element_id="element-B")],
    ))
    service.retrieval.federated_retrieve_elements = (
        lambda *_args, **_kwargs: next(hits)
    )
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.citation_source_info = lambda _ids: {
        "source-1": {"title": "权威来源", "file_name": "paper.pdf"}
    }

    response = service.ask(
        "nb", AskRequest(question="问题", mode="alpha.search"), user_id="user"
    )

    assert response.answer == "结论 [k1] 与 [k2]"
    assert [citation.element_id for citation in response.citations] == [
        "element-A", "element-B"
    ]
    assert [anchor.key for anchor in response.anchors] == ["k1", "k2"]
    assert [anchor.element_id for anchor in response.anchors] == [
        "element-A", "element-B"
    ]
    disclosure = next(
        step for step in response.reasoning_trace
        if step.summary == "引用核验未全部通过"
    )
    assert "1 条引用无法核验，已移除" in disclosure.detail["detail"]
    assert response.reasoning_trace[-1].summary == "扩展引擎执行完成"


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

    streamed = []
    with pytest.raises(StageBoundaryError, match="request identity changed"):
        service.ask(
            "nb", AskRequest(question="original", mode="alpha.mutating"),
            user_id="user",
            on_trace=streamed.append,
        )
    assert [step.summary for step in streamed] == ["扩展引擎执行失败"]
    assert streamed[0].duration_ms is not None


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


def test_host_freezes_trace_before_result_validation_and_observability():
    now = [10.0]
    order: list[tuple[str, float]] = []

    def answer(_context, _retrieval, _model, _trace):
        now[0] = 11.25
        return AskEngineResult("回答", ())

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.timed", answer=answer)),
    ))

    def emit(_event):
        order.append(("event", now[0]))
        now[0] = 99.0

    host = AskEngineHost(
        runtime.registry,
        event_sink=emit,
        clock=lambda: now[0],
    )
    host.answer(
        "alpha.timed",
        SimpleNamespace(question="question"),
        object(),
        object(),
        object(),
        on_provider_finished=lambda: order.append(("finished", now[0])),
    )

    assert order == [("finished", 11.25), ("event", 11.25)]


@pytest.mark.parametrize(
    ("provider_error", "expected_type", "expected_code"),
    (
        (
            AskEnginePortError("plugin_engine_model_unconfigured"),
            AskPluginEngineError,
            "plugin_engine_model_unconfigured",
        ),
        (AskCancelled("provider cancelled"), AskCancelled, None),
    ),
)
def test_host_preserves_provider_failure_when_finish_callback_fails(
    provider_error,
    expected_type,
    expected_code,
):
    callback_calls = []

    def answer(_context, _retrieval, _model, _trace):
        raise provider_error

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.failure", answer=answer)),
    ))

    def finish():
        callback_calls.append("finished")
        raise RuntimeError("core timing callback failed")

    with pytest.raises(expected_type) as rejected:
        runtime.ask_engines.answer(
            "alpha.failure",
            SimpleNamespace(question="question"),
            object(),
            object(),
            object(),
            on_provider_finished=finish,
        )

    assert callback_calls == ["finished"]
    if expected_type is AskCancelled:
        assert rejected.value is provider_error
    else:
        assert rejected.value.code == expected_code


def test_host_freezes_trace_before_rejecting_malformed_result():
    callback_calls = []

    def answer(_context, _retrieval, _model, _trace):
        return object()

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.invalid", answer=answer)),
    ))

    with pytest.raises(AskPluginEngineError) as rejected:
        runtime.ask_engines.answer(
            "alpha.invalid",
            SimpleNamespace(question="question"),
            object(),
            object(),
            object(),
            on_provider_finished=lambda: callback_calls.append("finished"),
        )

    assert callback_calls == ["finished"]
    assert rejected.value.code == "invalid_plugin_engine_result"


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
        "streaming": True,
        "streams_trace": True,
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


def test_citation_admission_strips_residual_citation_like_markers_and_discloses():
    """畸形「引用样」括号组摘除 + 披露(codex #602 R6 P1 的纪律,执行方式从
    整份拒绝改为摘除):`[k1, nope]` 不被 LOOSE_MARKER_RE 匹配、会原样留在
    正文里,渲染成从未被核验的引用外观——现在这类残留组本身被摘除,合法组
    保留,问题折进 notes。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "合法 [k1] 加畸形 [k1, nope]", (key,)
    )
    assert answer == "合法 [k1] 加畸形 "
    assert len(records) == 1
    assert notes == ("正文中 1 处疑似引用标记无法解析，已移除",)

    cjk = _retrieval_access(hit=_hit(element_id="element-cjk"))
    cjk_key = cjk.search("evidence", 1)[0].evidence_key
    cjk_answer, cjk_records, cjk_notes = admit_plugin_engine_result(
        cjk, "【k1】与【k2、nope】", (cjk_key,)
    )
    assert cjk_answer == "[k1]与"
    assert len(cjk_records) == 1
    assert cjk_notes == ("正文中 1 处疑似引用标记无法解析，已移除",)

    # 合法组照常通过——归一化输出自己写回的组不被残留扫描误伤,且干净路径
    # 不产生任何披露噪音。
    ok = _retrieval_access(hit=_hit(element_id="element-ok"))
    ok_key = ok.search("evidence", 1)[0].evidence_key
    answer, _records, ok_notes = admit_plugin_engine_result(
        ok, "正文 [k1] 结尾", (ok_key,)
    )
    assert answer == "正文 [k1] 结尾"
    assert ok_notes == ()


def _two_record_access():
    """One access whose two searches issue handles for A then B, so a test can
    forge a citation BETWEEN two live ones."""
    hits = iter((
        _hit(element_id="element-A"),
        _hit(element_id="element-B"),
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
    return access, (
        access.search("first", 1)[0].evidence_key,
        access.search("second", 1)[0].evidence_key,
    )


def test_citation_admission_never_mints_a_marker_by_deleting_around_it():
    """删除会拼接(评审 P0):`[k1[k9]2]` 的内层 `[k9]` 一旦就地删掉,两侧文本就
    合成一个从未过 index_map 的 `[k12]`——旧实现会把它留在正文里,让
    `records[index-1]` 越界或悄悄绑到模型从未引用的那条。哨兵隔离 + 摘除到
    不动点让「正文里每个标记都指向存活引用」重新无条件成立。"""
    # 整份正文就是那个畸形组:摘除后无散文可留,按摘空规则整份拒绝。
    bare = _retrieval_access()
    bare_key = bare.search("evidence", 1)[0].evidence_key
    with pytest.raises(AskEnginePortError) as rejected:
        admit_plugin_engine_result(bare, "[k1[k9]2]", (bare_key,))
    assert rejected.value.code == "plugin_engine_unverified_citation"

    # 同样的拼接危险,但周围有散文:逐项降级 + 披露,正文里不留任何标记。
    access = _retrieval_access(hit=_hit(element_id="element-prose"))
    key = access.search("evidence", 1)[0].evidence_key
    answer, records, notes = admit_plugin_engine_result(
        access, "结论：[k1[k9]2] 完毕", (key,)
    )
    assert answer == "结论： 完毕"
    assert len(records) == 1
    assert notes == (
        "正文中 1 个引用标记无法核验，已移除",
        "正文中 1 处疑似引用标记无法解析，已移除",
        "1 条引用未在正文中被引用，仅列入引用列表",
    )

    # `[k[k9]2]` 拼出的是 `[k2]`——第二条 record 真实存在,所以旧实现不会越界,
    # 只会静默把标记绑到模型从未绑定的 B 上。这条比越界更隐蔽,必须钉住。
    two, keys = _two_record_access()
    answer, records, notes = admit_plugin_engine_result(
        two, "只引 [k1]，另见 [k[k9]2]", keys
    )
    assert answer == "只引 [k1]，另见 "
    assert "[k2]" not in answer
    assert [record.element_id for record in records] == [
        "element-A", "element-B"
    ]
    assert "1 条引用未在正文中被引用，仅列入引用列表" in notes


def test_citation_admission_strips_newline_joined_marker_residue():
    """跨行拼接组对疑似正则隐形(复核 P1):`_SUSPECT_MARKER_RE` 的字符类排除
    `\\n`,而 `LOOSE_MARKER_RE` 经 `\\s*` 接受它,前端 linkify 与 `MARKER_RE`
    锚点解析同样接受。所以 `[k1,\\n[k9]k2]` 删掉内层后拼出的 `[k1,\\nk2]` 会被
    渲染成引用、并绑定到模型从未引用的 record——摘除循环必须同时跑 LOOSE。"""
    two, keys = _two_record_access()
    answer, records, notes = admit_plugin_engine_result(
        two, "结论 [k1,\n[k9]k2] 说明", keys
    )
    assert answer == "结论  说明"
    assert LOOSE_MARKER_RE.findall(answer) == []
    assert len(records) == 2
    assert "正文中 1 处疑似引用标记无法解析，已移除" in notes
    assert "2 条引用未在正文中被引用，仅列入引用列表" in notes

    # 拼接出的序号越界时同样必须消失,而不是靠 ask_service 的边界检查兜底——
    # 标记文本本身仍会被浏览器 linkify 成一个点不开的引用。
    out_of_range, oor_keys = _two_record_access()
    answer, records, notes = admit_plugin_engine_result(
        out_of_range, "结论 [k5,\n[k9]k6] 说明", oor_keys
    )
    assert answer == "结论  说明"
    assert LOOSE_MARKER_RE.findall(answer) == []
    assert "正文中 1 处疑似引用标记无法解析，已移除" in notes

    # 「未被引用」重算不得被拼接组投毒:`[k1,\nk5]` 若留在正文里,重算会把 1
    # 和 5 都算成已引用,让真正没被引用的第 3 条永远不被披露。
    hits = iter((
        _hit(element_id="element-A"),
        _hit(element_id="element-B"),
        _hit(element_id="element-C"),
    ))
    triple = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: ("source-1",),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [next(hits)],
        source_info=lambda _source_ids: {"source-1": {"title": "权威来源"}},
        max_k=3,
        max_calls=3,
        evidence_chars=100,
        query_chars=100,
    )
    triple_keys = tuple(
        triple.search(query, 1)[0].evidence_key
        for query in ("first", "second", "third")
    )
    answer, records, notes = admit_plugin_engine_result(
        triple, "结论 [k1,\n[k9]k5] 与 [k2] 说明", triple_keys
    )
    assert answer == "结论  与 [k2] 说明"
    assert len(records) == 3
    # 只有 k2 真的留在正文里,所以 A 与 C 两条必须被如实报为未引用。
    assert "2 条引用未在正文中被引用，仅列入引用列表" in notes


def test_citation_admission_strips_a_comma_less_marker_group():
    """漏逗号的复合引用是常见模型笔误(复核 P2):`[k1 k2]` 未过 index_map,却
    长得像引用,判据必须放宽到「片段由空白分隔的 k<数字> 词构成」。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1 k2] 说明 [k1]", (key,)
    )

    assert answer == "结论  说明 [k1]"
    assert len(records) == 1
    assert notes == ("正文中 1 处疑似引用标记无法解析，已移除",)


def test_citation_admission_survives_a_key_past_the_int_digit_limit():
    """`k\\d+` 的位数无上限,而 CPython 的 int(str) 有 4300 位上限(复核 P2-5):
    裸 int() 会抛 ValueError,被 ask_service 吞成整份拒绝——恰是逐项降级要
    消灭的行为。超长键必须降级成一个被丢弃的标记。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1] 与 [k" + "9" * 5000 + "] 完毕", (key,)
    )

    assert answer == "结论 [k1] 与  完毕"
    assert len(records) == 1
    assert notes == ("正文中 1 个引用标记无法核验，已移除",)


def test_citation_admission_keeps_ordinary_bracket_prose():
    """疑似判据收窄(评审 P1):旧角色是「命中即整份拒绝」(响亮),新角色是就地
    删正文(静默),所以判据必须精确到「某个分隔片段恰好是 k<数字>」,否则普通
    括号散文会被无声吃掉。"""
    cjk = _retrieval_access()
    cjk_key = cjk.search("evidence", 1)[0].evidence_key
    answer, _records, notes = admit_plugin_engine_result(
        cjk, "见【定义 [k1]】结论", (cjk_key,)
    )
    assert answer == "见【定义 [k1]】结论"
    assert notes == ()

    link = _retrieval_access(hit=_hit(element_id="element-link"))
    link_key = link.search("evidence", 1)[0].evidence_key
    answer, _records, notes = admit_plugin_engine_result(
        link, "参见 [k8s 官方文档](https://kubernetes.io) 与 [k1]。", (link_key,)
    )
    assert answer == "参见 [k8s 官方文档](https://kubernetes.io) 与 [k1]。"
    assert notes == ()

    numeric = _retrieval_access(hit=_hit(element_id="element-numeric"))
    numeric_key = numeric.search("evidence", 1)[0].evidence_key
    answer, _records, notes = admit_plugin_engine_result(
        numeric, "退火 [k1]，参数上限 [k1000 档] 未定。", (numeric_key,)
    )
    assert answer == "退火 [k1]，参数上限 [k1000 档] 未定。"
    assert notes == ()


def test_citation_admission_recounts_citedness_from_the_final_text():
    """「未被引用」记账必须按最终正文重算(评审 P2-1):疑似组摘除可以把一个
    已核验标记一起带走,归一化阶段的记账会因此高估被引用条数,让这条 record
    既不在正文里、也不出现在披露中。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    # `【…、k9】` 的顿号不是 LOOSE 分隔符,所以整组不是合法标记;它里面那个
    # 已核验的 `[k1]` 随该组一起被摘除。
    answer, records, notes = admit_plugin_engine_result(
        access, "前置【[k1]、k9】后置", (key,)
    )
    assert answer == "前置后置"
    assert len(records) == 1
    assert notes == (
        "正文中 1 处疑似引用标记无法解析，已移除",
        "1 条引用未在正文中被引用，仅列入引用列表",
    )

    # 反向:没有任何摘除时不得凭空多报未被引用。
    kept = _retrieval_access(hit=_hit(element_id="element-kept"))
    kept_key = kept.search("evidence", 1)[0].evidence_key
    answer, _records, kept_notes = admit_plugin_engine_result(
        kept, "见【见 [k1]】结论", (kept_key,)
    )
    assert answer == "见【见 [k1]】结论"
    assert kept_notes == ()


def test_citation_admission_rejects_an_answer_stripped_to_nothing():
    """摘空即整份拒绝(评审 P2-2):留下一个空白气泡比给出稳定的拒绝文案更糟,
    而「整份答案就是一个无法核验的引用」正是既有拒绝码描述的情形。"""
    access = _retrieval_access()

    with pytest.raises(AskEnginePortError) as rejected:
        admit_plugin_engine_result(access, "[k1]", ("pe-forged",))
    assert rejected.value.code == "plugin_engine_unverified_citation"

    # 只剩空白同样算摘空。
    blank = _retrieval_access(hit=_hit(element_id="element-blank"))
    with pytest.raises(AskEnginePortError):
        admit_plugin_engine_result(blank, "  [k1]  ", ("pe-forged",))

    # 反向:插件本来就交回空正文时不是「摘空」,保持原路径不新增拒绝。
    empty = _retrieval_access(hit=_hit(element_id="element-empty"))
    answer, records, _notes = admit_plugin_engine_result(empty, "", ())
    assert answer == ""
    assert records == ()


def test_citation_admission_renumbers_onto_the_correct_surviving_record():
    """压缩重排必须绑到正确那条(评审 P2-3):只有单条存活 record 时,「绑对了」
    与「绑到唯一一条」不可区分。伪造句柄放在中间,`[k3]` 必须成为指向 B 的
    `[k2]`,绝不能指向 A。"""
    hits = iter((
        _hit(element_id="element-A"),
        _hit(element_id="element-B"),
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
        # ``issuable`` must clear THREE citations or the forged middle handle
        # is truncated away instead of dropped, and the renumbering this test
        # exists to pin never happens.
        max_k=2,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )
    key_a = access.search("first", 1)[0].evidence_key
    key_b = access.search("second", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access, "[k1] 与 [k3]", (key_a, "pe-forged", key_b)
    )

    assert answer == "[k1] 与 [k2]"
    assert [record.element_id for record in records] == [
        "element-A", "element-B"
    ]
    assert notes == ("1 条引用无法核验，已移除",)


def test_citation_admission_ignores_plugin_authored_sentinel_characters():
    """哨兵是核心私有的:插件正文里出现同样的私有区字符时,必须在建表之前被
    清掉,否则它就能寻址替换表(KeyError/IndexError,或更糟——冒名一个合法组)。"""
    access = _retrieval_access()
    key = access.search("evidence", 1)[0].evidence_key

    answer, records, notes = admit_plugin_engine_result(
        access,
        f"前{chr(0xE000)}0{chr(0xE001)}中 [k1] 后{chr(0xE000)}9{chr(0xE001)}",
        (key,),
    )

    assert answer == "前0中 [k1] 后9"
    assert len(records) == 1
    assert notes == ()


def test_admitted_markers_always_name_a_surviving_record():
    """函数 docstring 的无条件不变量:凡在最终正文里长成引用的标记,序号一律
    落在 1..len(records) 内。这条覆盖一批会触发删除拼接的敌意输入。"""
    hostile = (
        "[k1[k9]2]",
        "[k[k9]2]",
        # Needs TWO strip passes: the outer bracket group is not marker-like
        # while the inner `[k9, nope]` is, and removing the inner one joins
        # `[k1` to `2]` into `[k12]` at a position `re.sub` has already passed.
        "结论：[k1[k9, nope]2] 完毕",
        "[[k1]]",
        "[k1【k9 x】2]",
        "[k1【a k9 b】2]",
        "[k[k9【k8 x】]2]",
        "文字 [k1] 与 [k9[k8]9] 与【k7、k1】",
        "[k9][k8][k7]",
        "【k1，k9】与 [k2, k9]",
        # 跨行形态:`_SUSPECT_MARKER_RE` 看不见它们,只有 LOOSE 那一半关得住。
        "结论 [k1,\n[k9]k2] 说明",
        "结论 [k5,\n[k9]k6] 说明",
        "[k1,\n[k9]k2]",
        "前 [k1,\n\t[k9]k9] 后",
        "[k1 k2] 与 [k1,\nk9]",
    )
    for source in hostile:
        access, keys = _two_record_access()
        try:
            answer, records, _notes = admit_plugin_engine_result(
                access, source, keys
            )
        except AskEnginePortError as rejected:
            # 摘空拒绝也满足不变量:根本没有正文上屏。
            assert rejected.code == "plugin_engine_unverified_citation", source
            continue
        groups = [match.group(0) for match in LOOSE_MARKER_RE.finditer(answer)]
        # Range alone is too weak: a newline-joined `[k1,\nk2]` names indexes
        # that ARE in range while naming a binding the model never authored.
        # Core only ever writes back the canonical `[k1]` / `[k1, k2]` form, so
        # every surviving group must wear exactly that shape.
        assert all(
            re.fullmatch(r"\[k\d+(?:, k\d+)*\]", group) for group in groups
        ), (source, answer, groups)
        indexes = [
            int(key[1:])
            for group in groups
            for key in marker_keys(group)
        ]
        assert all(1 <= index <= len(records) for index in indexes), (
            source, answer, indexes
        )


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
    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1]", (hits[0].evidence_key,)
    )
    assert answer == "结论 [k1]"
    # A cited knowledge object opens its first live evidence ELEMENT.
    assert records[0].element_id == "element-1"
    assert records[0].source_id == "source-1"
    assert records[0].source_file_name == "paper.pdf"
    assert notes == ()


def test_search_kg_two_lane_routing_mirrors_the_built_in_ask():
    """ANN 臂债务清偿（原契约点④）:未收窄的 run **不**下推显式清单——接缝据此
    保持 notebook ANN 臂、把冻结天花板留到证据 hydrate（内建未收窄语义）;真收窄
    的 run 仍原样下推冻结 source key,走「词法谓词在 LIMIT 前」的受限 lane。两个
    方向各自变异可验:恒传清单让未收窄半红,恒传 None 让收窄半红。"""
    seen: list[object] = []

    def search_knowledge(_active_notebook_id, _query, **kwargs):
        seen.append(kwargs.get("allowed_source_keys", "MISSING"))
        return []

    unnarrowed = _kg_access(
        search_knowledge=search_knowledge, visible=("source-1", "source-2")
    )
    assert unnarrowed.search_kg("退火", 2) == ()
    assert seen == [None], (
        "an un-narrowed run must pass NO explicit list -- that is what keeps "
        "the seam's notebook ANN arm on"
    )

    seen.clear()
    local = ResolvedSourceScope(
        mode="include", source_ids=["source-1"], narrowed=True
    )
    with source_scope_context("notebook-1", local, None):
        narrowed = _kg_access(
            search_knowledge=search_knowledge,
            visible=("source-1", "source-2"),
        )
        assert narrowed.search_kg("退火", 2) == ()
    assert seen == [(("notebook-1", "source-1"),)], (
        "a genuinely narrowed run must push its frozen keys so the seam takes "
        "the source-restricted lane (predicate before LIMIT)"
    )


def test_knowledge_hit_without_a_live_source_is_context_only():
    access = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(notebook_id="notebook-1")],
        source_info=lambda _source_ids: {},
    )

    hits = access.search_kg("退火", 2)

    assert hits[0].evidence_key == ""
    assert hits[0].source_title == "" and hits[0].location_label == ""
    assert "退火" in hits[0].text
    # Not citable -- the empty context-only key is dropped and the marker
    # naming it is stripped, degrading rather than rejecting the answer ...
    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1]", ("",)
    )
    assert records == ()
    assert "[k1]" not in answer
    assert notes
    # ... and not an expansion anchor either.
    assert access.kg_neighbors("", 2) == ()


def test_bound_evidence_requires_the_source_to_be_in_the_frozen_snapshot_not_just_resolvable():
    """P1 + codex #603 R4 P1: 证据来源全部落在冻结宇宙之外的命中,现在**整体
    不返回**——连名字都不当 context-only 交出去(该形态在生产上就是 Memory
    派生对象或冻结后漂移新增,名字本身就是内容)。混合形态(一条界外 + 一条
    界内)仍走快照判定:界外那条被跳过,界内那条签发。两个方向合起来钉住
    `_bound_evidence` 的 origin 比对与 `_issue_kg_evidence` 的整体丢弃规则。"""
    outside_only = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(
            notebook_id="notebook-1",
            evidence=[_evidence(source_id="source-outside-scope")],
        )],
        # source_info 仍能解析出元数据(该来源本身仍然存在),但它压根不在
        # 这个 run 冻结的可见来源集合(visible=("source-1",))内。
        source_info=lambda source_ids: {
            source_id: {"title": "越界来源", "file_name": "outside.pdf"}
            for source_id in source_ids
        },
    )
    assert outside_only.search_kg("退火", 2) == ()

    mixed = _kg_access(
        search_knowledge=lambda *_args, **_kwargs: [_object(
            notebook_id="notebook-1",
            evidence=[
                _evidence(
                    source_id="source-outside-scope",
                    element_id="element-outside",
                ),
                _evidence(source_id="source-1", element_id="element-in"),
            ],
        )],
        source_info=lambda source_ids: {
            source_id: {"title": "来源", "file_name": "f.pdf"}
            for source_id in source_ids
        },
    )
    hits = mixed.search_kg("退火", 2)
    assert hits[0].evidence_key, "the in-snapshot binding must still be citable"
    _, records, notes = admit_plugin_engine_result(
        mixed, "结论 [k1]", (hits[0].evidence_key,)
    )
    assert records[0].source_id == "source-1"
    assert records[0].element_id == "element-in"
    assert notes == ()


def test_plugin_port_universe_excludes_the_callers_memory_projections():
    """codex #603 R4 P1:MCP 的 memory:read 过滤只认 `Citation.memory_id`,而
    插件引用没有渠道携带 Memory 身份——所以调用者自己的 Memory 投影源在
    ask_service 接线处就被结构性排除出冻结宇宙,Knowhow 投影保留(表格是
    notebook 级共享内容)。删掉接线处的 source_type 过滤必须让本用例变红。"""
    seen_keys: list[tuple[str, str]] = []

    def capture_elements(_nb, _query, *, allowed_source_keys, limit):
        del limit
        seen_keys.extend(allowed_source_keys)
        return []

    def answer(_context, retrieval, _model, _trace):
        retrieval.search("查询", 1)
        return AskEngineResult("无引用回答", ())

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.kg", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _nb: ("nb",),
        ask_engine_visible_sources=lambda _nb: ("source-doc",),
        ask_engine_hidden_sources=lambda _nb, _actor: (
            "hidden-knowhow", "hidden-memory",
        ),
    )
    service.retrieval.federated_retrieve_elements = capture_elements
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.source_metadata = lambda _ids: {
        "hidden-knowhow": {"source_type": "knowhow"},
        "hidden-memory": {"source_type": "memory"},
    }
    service.evidence_context.citation_source_info = lambda _ids: {}
    service.collection_catalog = SimpleNamespace(
        collection_map_text=lambda _nb: ""
    )

    service.ask("nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user")

    sources = {source_id for _nb, source_id in seen_keys}
    assert "source-doc" in sources
    assert "hidden-knowhow" in sources, (
        "notebook-shared Knowhow projections must stay in the plugin universe"
    )
    assert "hidden-memory" not in sources, (
        "the caller's own Memory projection must never enter the plugin "
        "retrieval face -- plugin citations cannot carry the memory identity "
        "the MCP memory:read filter recognizes"
    )


def test_search_kg_slices_after_the_seams_ranking_and_pushes_no_limit_down():
    """codex #603 R4 P2 的驳回护栏(三件套之一):k 是呈现截断不是工作量界。
    融合 top-k 必须先对有界候选窗完整打分——窗界在接缝自己的 recall rails 上,
    两条 lane 都成立(真收窄:显式 keys 的受限词法 lane、谓词在 LIMIT 前;未收窄:
    ANN+词法窗、冻结天花板在 hydrate 应用)。所以切片刻意发生在接缝排序之后,
    且**不向接缝下推 limit**——谁把 limit 推下去,这条用例就要求他先回答排序
    正确性从哪来。"""
    seen_kwargs: dict = {}

    def ranked_seam(_nb, _query, **kwargs):
        seen_kwargs.update(kwargs)
        return [
            _object(object_id="ko-high", name="高分", notebook_id="notebook-1"),
            _object(object_id="ko-low", name="低分", notebook_id="notebook-1"),
        ]

    access = _kg_access(search_knowledge=ranked_seam, kg_max_calls=4)
    hits = access.search_kg("查询", 1)
    assert len(hits) == 1
    assert "高分" in hits[0].text, (
        "the slice must preserve the seam's ranking: k=1 takes the seam's "
        "top-ranked hit"
    )
    assert "limit" not in seen_kwargs and "k" not in seen_kwargs, (
        "deliberately NO limit push-down -- the seam's recall rails bound the "
        "work; see the rebuttal comment at the search_kg call site"
    )


def test_plugin_ask_synthesizes_an_unnarrowed_ceiling_for_scopeless_callers():
    """MCP 与旧直调不带 scope。若不合成天花板,未收窄 run 传 None 时接缝就没有
    任何 hydrate 上限——所以 ask_plugin_engine 为无 scope 的调用合成与浏览器冻结
    快照同形状的 include 天花板(narrowed=False、hidden 半是已剔除 Memory 的插件
    宇宙),让 KG 接缝在每个调用面上行为一致。展示回执是另一个只由 API 路由在真
    收窄时设置的 ContextVar,合成不产生任何用户可见 scope。"""
    from app.services.source_scope import current_source_scope

    captured: list[object] = []

    def capture_scope(_nb, _query, **_kwargs):
        captured.append(current_source_scope())
        return []

    def answer(_context, retrieval, _model, _trace):
        retrieval.search_kg("查询", 1)
        return AskEngineResult("无引用回答", ())

    runtime = build_extension_runtime((
        _bundle("alpha", _Provider("alpha.kg", answer=answer)),
    ))
    service = _minimal_ask_service(
        ask_engine_host=runtime.ask_engines,
        ask_engine_participant_notebooks=lambda _nb: ("nb", "base-1"),
        ask_engine_visible_sources=lambda _nb: ("source-doc",),
        ask_engine_hidden_sources=lambda _nb, _actor: (
            "hidden-knowhow", "hidden-memory",
        ),
    )
    service.retrieval.federated_retrieve = capture_scope
    service.retrieval.federated_retrieve_elements = lambda *_a, **_k: []
    service.evidence_context.evidence_elements = _all_live
    service.evidence_context.source_metadata = lambda _ids: {
        "hidden-knowhow": {"source_type": "knowhow"},
        "hidden-memory": {"source_type": "memory"},
    }
    service.evidence_context.citation_source_info = lambda _ids: {}
    service.collection_catalog = SimpleNamespace(
        collection_map_text=lambda _nb: ""
    )

    response = service.ask(
        "nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user"
    )

    scope = captured[0]
    assert scope is not None, (
        "a scope-less caller must retrieve under a synthesized frozen ceiling"
    )
    assert scope.mode == "include"
    assert "source-doc" in scope.source_ids
    assert scope.hidden_source_ids == {"hidden-knowhow", "hidden-memory"}, (
        "the ceiling's hidden half must be the RAW set (Memory included, the "
        "exact browser-snapshot shape): the seam's universe-drift probe "
        "compares it against the live hidden_source_ids read, and a "
        "Memory-stripped copy never matches -- which would silently re-close "
        "the ANN arm for every user holding one confirmed Memory (P2-1)"
    )
    assert not scope.restricted, (
        "the synthesized ceiling is a FILTERING snapshot, never a narrowing -- "
        "restricted would wrongly close graph channels"
    )
    # The library half freezes too (codex #604 R1 P2): a None base scope
    # leaves base_ceiling_active false, letting a base mounted mid-run join
    # the un-narrowed seam path. include of the mounted-at-synthesis set.
    assert scope.base_mode == "include"
    assert scope.base_notebook_ids == {"base-1"}
    assert scope.covers_notebook("base-1")
    assert not scope.covers_notebook("drifted-base"), (
        "a reference library mounted after synthesis must stay outside the "
        "frozen run"
    )
    assert not scope.base_restricted, (
        "an all-mounted include freeze must not read as a base narrowing"
    )
    assert response.retrieval_scope is None, (
        "synthesis must not fabricate a user-visible scope receipt"
    )


def test_plugin_ask_synthesizes_each_omitted_scope_dimension_independently():
    """codex #604 R2 P2:两个维度各自可选,只提交一维时 `current_source_scope()`
    非空、整体合成分支会跳过,缺的那一维留着不冻结。现在按维度独立合成:已提交
    的半逐字段忠实透传(不得走会丢 hidden ids 的持久化 payload helper),缺失的
    半按浏览器快照形状补齐。"""
    from app.services.source_scope import current_source_scope

    captured: list[object] = []

    def capture_scope(_nb, _query, **_kwargs):
        captured.append(current_source_scope())
        return []

    def answer(_context, retrieval, _model, _trace):
        retrieval.search_kg("查询", 1)
        return AskEngineResult("无引用回答", ())

    def build_service():
        runtime = build_extension_runtime((
            _bundle("alpha", _Provider("alpha.kg", answer=answer)),
        ))
        service = _minimal_ask_service(
            ask_engine_host=runtime.ask_engines,
            ask_engine_participant_notebooks=lambda _nb: ("nb", "base-1"),
            ask_engine_visible_sources=lambda _nb: ("source-doc",),
            ask_engine_hidden_sources=lambda _nb, _actor: (
                "hidden-knowhow", "hidden-memory",
            ),
        )
        service.retrieval.federated_retrieve = capture_scope
        service.retrieval.federated_retrieve_elements = lambda *_a, **_k: []
        service.evidence_context.evidence_elements = _all_live
        service.evidence_context.source_metadata = lambda _ids: {
            "hidden-knowhow": {"source_type": "knowhow"},
            "hidden-memory": {"source_type": "memory"},
        }
        service.evidence_context.citation_source_info = lambda _ids: {}
        service.collection_catalog = SimpleNamespace(
            collection_map_text=lambda _nb: ""
        )
        return service

    # 只提交库维度(本地半省略):本地半被合成,库半逐字段保留(含 narrowed=True
    # 的收窄语义)。
    service = build_service()
    base_only = BaseNotebookScope(
        mode="include", notebook_ids=["base-1"], narrowed=True
    )
    with source_scope_context("nb", None, base_only):
        service.ask(
            "nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user"
        )
    scope = captured[-1]
    assert scope is not None
    assert scope.hidden_source_ids == {"hidden-knowhow", "hidden-memory"}
    assert not scope.restricted
    assert scope.base_mode == "include"
    assert scope.base_notebook_ids == {"base-1"}
    assert scope.base_restricted, (
        "a supplied base narrowing must survive the local-half synthesis "
        "field-faithfully"
    )

    # 只提交本地维度(库半省略):本地半逐字段保留(含 hidden ids),库半被合成
    # 为「合成时已挂载集合」的 include 冻结。
    service = build_service()
    local_only = ResolvedSourceScope(
        mode="include", source_ids=["source-doc"], narrowed=False,
    )
    # 生产接线（ask_routes）就是这样附着隐藏半与 owner 的：私有属性直赋，
    # 公开序列化刻意不带它们，_scope_dict 再从属性读回。
    local_only._hidden_source_ids = ["hidden-knowhow", "hidden-memory"]
    local_only._scope_owner_id = "user"
    with source_scope_context("nb", local_only, None):
        service.ask(
            "nb", AskRequest(question="问题", mode="alpha.kg"), user_id="user"
        )
    scope = captured[-1]
    assert scope is not None
    assert scope.source_ids == {"source-doc"}
    assert scope.hidden_source_ids == {"hidden-knowhow", "hidden-memory"}, (
        "the supplied local half must pass through with its hidden ids intact "
        "-- the persistence payload helpers drop them and would re-break the "
        "drift-probe equality"
    )
    assert scope.base_mode == "include"
    assert scope.base_notebook_ids == {"base-1"}
    assert not scope.covers_notebook("drifted-base"), (
        "the omitted library half must freeze to the mounted-at-synthesis set"
    )


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
    answer, records, notes = admit_plugin_engine_result(
        access, "结论 [k1]", ("",)
    )
    assert records == ()
    assert "[k1]" not in answer
    assert notes
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
    _answer, records, notes = admit_plugin_engine_result(
        access, "邻居 [k1]", (hits[0].evidence_key,)
    )
    assert records[0].element_id == "element-2"
    assert notes == ()


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
        evidence_elements=_all_live,
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
            evidence_elements=_all_live,
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


def test_kg_evidence_requires_the_element_itself_to_survive_not_just_the_source():
    """codex #603 R2 P1:source 元数据只证明来源行还在;重解析会整批轮换元素 id
    而来源行不动,引用绝不能打开一个已被移除的元素。首条绑定元素已死 → 落到
    第二条存活绑定;全部死 → context-only(空 key、不进 ledger)。"""
    hit = _object(
        evidence=[
            _evidence(element_id="element-dead", source_id="source-1"),
            _evidence(element_id="element-live", source_id="source-1"),
        ],
        notebook_id="notebook-1",
    )
    access = _kg_access(
        search_knowledge=lambda *_a, **_k: [hit],
        evidence_elements=lambda ids: {
            element_id: {"source_id": "source-1"}
            for element_id in ids
            if element_id == "element-live"
        },
        kg_max_calls=4,
    )
    key = access.search_kg("查询", 1)[0].evidence_key
    assert key, "a hit with one surviving binding must stay citable"
    _, records, notes = admit_plugin_engine_result(access, "回答 [k1]", (key,))
    assert notes == ()
    assert records[0].element_id == "element-live", (
        "the citation must bind the first SURVIVING evidence element, not the "
        "first listed one"
    )

    all_dead = _object(
        object_id="ko-dead",
        evidence=[_evidence(element_id="element-gone", source_id="source-1")],
        notebook_id="notebook-1",
    )
    orphaned = _kg_access(
        search_knowledge=lambda *_a, **_k: [all_dead],
        evidence_elements=lambda _ids: {},
        kg_max_calls=4,
    ).search_kg("查询", 1)
    assert orphaned[0].evidence_key == "", (
        "an object whose every evidence element is gone must degrade to "
        "context-only -- a durable citation must never open a missing element"
    )


def test_kg_overview_is_suppressed_for_narrowed_active_runs():
    """codex #603 R2 P2:集合地图的计数接缝只认库维度,本地真收窄的 run 里把
    整库计数交给插件就是把界外聚合信息漏出去——通道镜像 kg_neighbors 的收窄
    闸,直接返回空串且零底层调用。"""
    calls: list[str] = []

    def collection_overview(notebook_id: str) -> str:
        calls.append(notebook_id)
        return "整库计数"

    local = ResolvedSourceScope(
        mode="include", source_ids=["source-1"], narrowed=True
    )
    with source_scope_context("notebook-1", local, None):
        access = _kg_access(collection_overview=collection_overview)
    assert access.kg_overview() == ""
    assert calls == [], (
        "a narrowed run must not pay for -- or receive -- the whole-notebook "
        "collection map"
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
    service.evidence_context.evidence_elements = _all_live
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
    service.evidence_context.evidence_elements = _all_live
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
