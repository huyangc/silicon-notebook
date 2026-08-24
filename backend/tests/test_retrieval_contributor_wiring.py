from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from app.services.ask_service import AskService
from app.services.report_engine import ReportEngine


class _RecordingHost:
    """A host that predates ``has_contributions``: workflows must still call it."""

    def __init__(self, output=None) -> None:
        self.calls = []
        self.output = output

    def run(self, baseline, *, invocation, **_kwargs):
        self.calls.append((baseline, invocation))
        return baseline if self.output is None else self.output


class _CountingHost:
    """Delegates the frozen-topology query to a real host and counts ``run``."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.runs = []
        self.queries = []

    def has_contributions(self, invocation, *, disabled_capabilities=frozenset()):
        answer = self._inner.has_contributions(
            invocation, disabled_capabilities=disabled_capabilities
        )
        self.queries.append((invocation, disabled_capabilities, answer))
        return answer

    def run(self, baseline, *, invocation, **kwargs):
        self.runs.append((invocation, kwargs.get("disabled_capabilities")))
        return self._inner.run(baseline, invocation=invocation, **kwargs)


def _ask_with_host(host, *, graph=None):
    ask = object.__new__(AskService)
    ask.retrieval_contributors = host
    ask.selected_source_graph = graph
    ask.retrieval_connection_probe = SimpleNamespace(
        is_connection_held=lambda: False
    )
    ask.retrieval_contributor_hydrate = (
        lambda _notebook_id, _actor_id, _ids: ()
    )
    ask.current_user_id = lambda: "actor"
    ask.settings = SimpleNamespace(selected_source_graph_enrichment_tokens=1)
    return ask


def _report_with_host(host, *, graph=None):
    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=graph,
        retrieval_connection_probe=SimpleNamespace(
            is_connection_held=lambda: False
        ),
        retrieval_contributor_hydrate=(
            lambda _notebook_id, _actor_id, _ids: ()
        ),
    )
    report.settings = SimpleNamespace(
        ppr_top_chunks=1,
        selected_source_graph_enrichment_tokens=1,
    )
    report.user_id = "actor"
    report.cancel_event = None
    return report


def _graph_service_access_violations(text: str) -> list[str]:
    tree = ast.parse(text)
    graph_aliases = set()

    def assigned_names(node) -> tuple[str, ...]:
        if isinstance(node, ast.Assign):
            return tuple(
                target.id for target in node.targets
                if isinstance(target, ast.Name)
            )
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return (node.target.id,)
        return ()

    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "getattr"
            and len(value.args) >= 2
            and isinstance(value.args[1], ast.Constant)
            and value.args[1].value == "selected_source_graph"
        ):
            graph_aliases.update(assigned_names(node))
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value = getattr(node, "value", None)
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(value, ast.Name)
                and value.id in graph_aliases
            ):
                for name in assigned_names(node):
                    if name not in graph_aliases:
                        graph_aliases.add(name)
                        changed = True

    def graph_value(node) -> bool:
        return (
            isinstance(node, ast.Name) and node.id in graph_aliases
        ) or (
            isinstance(node, ast.Attribute)
            and node.attr == "selected_source_graph"
        )

    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"run", "fail_closed"}
            and graph_value(node.value)
        ):
            violations.append(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and graph_value(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"run", "fail_closed"}
        ):
            violations.append(str(node.args[1].value))
        if isinstance(node, ast.Call):
            bridge_call = (
                isinstance(node.func, ast.Name)
                and node.func.id == "SelectedSourceGraphContributionCall"
            )
            for index, argument in enumerate(node.args):
                if graph_value(argument) and not (bridge_call and index == 0):
                    violations.append("graph_service_forwarded")
            if not bridge_call:
                for keyword in node.keywords:
                    if graph_value(keyword.value):
                        violations.append("graph_service_forwarded")
    return violations


def test_ask_and_report_keep_other_host_output_when_graph_capability_is_absent():
    baseline = [SimpleNamespace(chunk_id="base")]
    appended = [*baseline, SimpleNamespace(chunk_id="plugin")]
    host = _RecordingHost(output=appended)
    connection_probe = SimpleNamespace(is_connection_held=lambda: False)

    ask = object.__new__(AskService)
    ask.retrieval_contributors = host
    ask.selected_source_graph = None
    ask.retrieval_connection_probe = connection_probe
    ask.retrieval_contributor_hydrate = (
        lambda _notebook_id, _actor_id, _ids: ()
    )
    ask.current_user_id = lambda: "actor"
    ask.settings = SimpleNamespace(selected_source_graph_enrichment_tokens=1)
    ask_chunks, ask_status = ask._activate_selected_source_graph("notebook", baseline)

    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
        retrieval_connection_probe=connection_probe,
        retrieval_contributor_hydrate=(
            lambda _notebook_id, _actor_id, _ids: ()
        ),
    )
    report.settings = SimpleNamespace(
        ppr_top_chunks=1,
        selected_source_graph_enrichment_tokens=1,
    )
    report.user_id = "actor"
    report.cancel_event = None
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert ask_chunks == appended
    assert ask_status is None
    assert result.chunks == appended
    assert not hasattr(result, "baseline_chunks")
    assert [invocation for _baseline, invocation in host.calls] == [
        "selected_evidence",
        "selected_evidence",
    ]


def test_application_bootstrap_injects_process_shared_retrieval_host(monkeypatch):
    from app import bootstrap

    host = object()
    parser_host = object()
    observer_host = object()
    gap_host = object()
    runtime = SimpleNamespace(
        retrieval_contributors=host,
        parser_chain=parser_host,
        ask_completed_observers=observer_host,
        report_completed_observers=observer_host,
        gap_consult=gap_host,
    )
    captured = {}

    def fake_repository(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(settings=settings)

    monkeypatch.setattr(bootstrap, "application_extension_runtime", lambda: runtime)
    monkeypatch.setattr(bootstrap, "create_repository", fake_repository)

    created = bootstrap.create_application_repository(
        Settings(database_url="sqlite:////tmp/retrieval-contributor-wiring.db")
    )

    assert created.settings.database_url.startswith("sqlite")
    # A frozen mapping, not a floor: every host the composition root hands the
    # repository is named here, so adding a seat is a deliberate act and
    # dropping one can never pass silently.
    assert captured == {
        "retrieval_contributor_host": host,
        "parser_provider_chain_host": parser_host,
        "ask_completed_observer_host": observer_host,
        "report_completed_observer_host": observer_host,
        "gap_consult_host": gap_host,
    }


def test_default_topology_registers_two_point_specific_atomic_contributors():
    from app.extensions import default_extension_runtime
    from app.extensions.builtin import (
        GENERATED_QUESTION_CONTRIBUTION_ID,
        SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
    )

    runtime = default_extension_runtime()
    contributions = runtime.registry.contributions("retrieval.contributor")

    assert [item.contribution.declaration.id for item in contributions] == [
        GENERATED_QUESTION_CONTRIBUTION_ID,
        SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID
    ]
    frozen = runtime.retrieval_contributors._registrations
    assert {
        item.registered.contribution.declaration.id: item.admission
        for item in frozen
    } == {
        GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
        SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
    }


def test_chunk_candidate_host_anchor_stays_between_baseline_and_selection():
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    source = (services / "retrieval_candidates.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "CandidateRetrievalService"
    )
    retrieve = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_retrieve_chunks"
    )
    calls = [
        node.func.attr
        for node in ast.walk(retrieve)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert calls == [
        "_retrieve_chunks_baseline",
        "_run_chunk_candidate_contributors",
    ]
    assert "_generated_question_supplement(" not in source


def test_ask_and_report_no_longer_call_graph_service_directly():
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("ask_service.py", "report_engine.py"):
        text = (services / name).read_text(encoding="utf-8")
        assert _graph_service_access_violations(text) == []


def test_graph_service_direct_call_guard_catches_alias_and_forwarding_mutations():
    mutations = (
        "self.selected_source_graph.run()",
        "service = getattr(self, 'selected_source_graph'); service.fail_closed()",
        "service = getattr(self, 'selected_source_graph'); runner = service.run; runner()",
        "service = getattr(self, 'selected_source_graph'); getattr(service, 'run')()",
        "service = getattr(self, 'selected_source_graph'); helper(service)",
        "service = getattr(self, 'selected_source_graph'); helper(value=service)",
        "service: object = getattr(self, 'selected_source_graph'); service.run()",
    )
    assert all(_graph_service_access_violations(text) for text in mutations)


def test_report_runs_selected_evidence_without_graph_service():
    original = [SimpleNamespace(chunk_id="base")]
    appended = [*original, SimpleNamespace(chunk_id="plugin")]
    host = _RecordingHost(output=appended)
    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
        retrieval_connection_probe=SimpleNamespace(
            is_connection_held=lambda: False
        ),
        retrieval_contributor_hydrate=(
            lambda _notebook_id, _actor_id, _ids: ()
        ),
    )
    report.settings = SimpleNamespace(
        ppr_top_chunks=1,
        selected_source_graph_enrichment_tokens=1,
    )
    report.user_id = "actor"
    report.cancel_event = None
    result = SimpleNamespace(chunks=original)

    report._activate_selected_source_graph("notebook", result)

    assert not hasattr(result, "baseline_chunks")
    assert result.chunks == appended
    assert host.calls == [(original, "selected_evidence")]


def test_repository_factory_accepts_injected_host_without_importing_registry(monkeypatch):
    from app.repositories import factory

    host = object()
    captured = {}

    def fake_repository(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(settings=settings)

    monkeypatch.setattr(factory, "SQLiteRepository", fake_repository)
    factory.create_repository(
        Settings(database_url="sqlite:////tmp/retrieval-host-factory.db"),
        retrieval_contributor_host=host,
    )

    assert captured == {"retrieval_contributor_host": host}


def test_factory_created_ask_and_report_share_builtin_host(tmp_path):
    from app.bootstrap import (
        application_extension_runtime,
        create_application_repository,
    )

    repository = create_application_repository(Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'wiring.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
    ))
    try:
        host = application_extension_runtime().retrieval_contributors
        ask = repository._runtime.ask_service()
        report = repository._runtime.report_execution.engine_factory(
            user_id="wiring-review"
        )

        assert repository._runtime.retrieval_contributors is host
        assert ask.retrieval_contributors is host
        assert report.dependencies.retrieval_contributors is host

        baseline = [SimpleNamespace(chunk_id="base")]
        ask.selected_source_graph = None
        object.__setattr__(
            report.dependencies, "selected_source_graph", None
        )
        ask_chunks, ask_status = ask._activate_selected_source_graph(
            "notebook", baseline
        )
        report_result = SimpleNamespace(chunks=baseline)
        report._activate_selected_source_graph("notebook", report_result)

        assert ask_chunks == baseline
        assert ask_status is None
        assert report_result.chunks is baseline
    finally:
        repository.close()


def test_dormant_selected_evidence_lane_never_reaches_the_host(monkeypatch):
    """Unconfigured graph + nothing else registered => B before any host work.

    The host is the real frozen default topology, so ``has_contributions``
    gives a real answer rather than a stubbed one; only ``run`` is counted.

    Building the call bridge or the typed envelope is booby-trapped as well:
    the contract is "return the baseline before the call/context is
    constructed", so a short-circuit that merely moves below them is a
    regression even though the visible result would be identical.

    The traps are *recorders*, not raisers: each appends its own name to a
    shared list and then delegates to the real implementation, rather than
    raising ``AssertionError`` directly.  A raising trap's signal depends on
    exactly where its call site's ``try/except`` happens to sit -- both
    ``_activate_selected_source_graph`` implementations wrap
    ``selected_source_graph_call_context`` in a broad ``except Exception:``,
    so a raise from *that* trap is silently absorbed into
    ``fail_closed_result`` instead of surfacing as a test failure, and any
    future refactor that widens the try block around
    ``SelectedSourceGraphContributionCall`` too would absorb that raise the
    same way.  Recording first and returning the real implementation's result
    keeps ``recorder == []`` true precisely when neither bridge was ever
    invoked, independent of that call-site detail.
    """
    from app.extensions import default_extension_runtime
    from app.domain.extensions import SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY
    from app.services import ask_service as ask_module
    from app.services import report_engine as report_module

    recorder: list[tuple[str, str]] = []

    def _recording(module, owner: str, name: str):
        original = getattr(module, name)

        def _wrapped(*args, **kwargs):
            recorder.append((owner, name))
            return original(*args, **kwargs)

        return _wrapped

    for owner, module in (("ask", ask_module), ("report", report_module)):
        monkeypatch.setattr(
            module,
            "SelectedSourceGraphContributionCall",
            _recording(module, owner, "SelectedSourceGraphContributionCall"),
        )
        monkeypatch.setattr(
            module,
            "selected_source_graph_call_context",
            _recording(module, owner, "selected_source_graph_call_context"),
        )

    host = _CountingHost(default_extension_runtime().retrieval_contributors)
    baseline = [SimpleNamespace(chunk_id="base")]

    ask = _ask_with_host(host)
    ask_chunks, ask_status = ask._activate_selected_source_graph(
        "notebook", baseline
    )

    report = _report_with_host(host)
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert recorder == []
    assert host.runs == []
    assert host.queries == [
        (
            "selected_evidence",
            frozenset({SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY}),
            False,
        ),
    ] * 2
    assert ask_chunks == baseline and ask_chunks is not baseline
    assert ask_status is None
    assert result.chunks is baseline
    assert not hasattr(result, "baseline_chunks")


def test_failing_topology_probe_still_enters_the_host():
    """The probe is an optimisation; failing it must never drop a contribution.

    Skipping the host on a probe error would silently discard another
    contributor's output, so the helper has to fall back to the old path.
    """
    baseline = [SimpleNamespace(chunk_id="base")]
    appended = [*baseline, SimpleNamespace(chunk_id="plugin")]

    class _BrokenProbeHost(_RecordingHost):
        def has_contributions(self, _invocation, **_kwargs):
            raise RuntimeError("probe exploded")

    host = _BrokenProbeHost(output=appended)
    ask = _ask_with_host(host)
    ask_chunks, ask_status = ask._activate_selected_source_graph(
        "notebook", baseline
    )

    report = _report_with_host(host)
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert [invocation for _baseline, invocation in host.calls] == [
        "selected_evidence",
        "selected_evidence",
    ]
    assert ask_chunks == appended
    assert ask_status is None
    assert result.chunks == appended


def test_none_topology_probe_still_enters_the_host():
    """The probe is read strictly: only the literal ``False`` answer counts
    as "nothing else contributes here". A probe that answers ``None``
    (neither ``True`` nor ``False``) must keep the old path exactly like a
    missing or broken probe does -- ``lane_is_dormant`` in
    ``app.domain.extensions`` checks ``probe(...) is False``, not
    truthiness, so this must never be mistaken for the dormant signal.
    """
    baseline = [SimpleNamespace(chunk_id="base")]
    appended = [*baseline, SimpleNamespace(chunk_id="plugin")]

    class _NoneProbeHost(_RecordingHost):
        def has_contributions(self, _invocation, **_kwargs):
            return None

    host = _NoneProbeHost(output=appended)
    ask = _ask_with_host(host)
    ask_chunks, ask_status = ask._activate_selected_source_graph(
        "notebook", baseline
    )

    report = _report_with_host(host)
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert [invocation for _baseline, invocation in host.calls] == [
        "selected_evidence",
        "selected_evidence",
    ]
    assert ask_chunks == appended
    assert ask_status is None
    assert result.chunks == appended


def test_registered_independent_contribution_still_enters_the_host():
    """Non-vacuous generality: another real contribution keeps the host call."""
    from app.extensions import build_extension_runtime
    from app.domain.extensions import SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY
    from app.extensions.builtin import (
        SELECTED_SOURCE_GRAPH_BUNDLE,
        SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
    )
    from app.extension_sdk import (
        EXTENSION_API_VERSION,
        RETRIEVAL_CONTRIBUTOR_POINT,
        Availability,
        AvailabilityStatus,
        ContributionDeclaration,
        ContributionKind,
        ContributorResult,
        EvidenceCandidate,
        EvidenceProvenance,
        ExtensionContribution,
        ExtensionManifest,
        ExtensionResultStatus,
        RetrievalHostContext,
    )
    from app.services.retrieval import RetrievedChunk

    graph_decisions = []

    def graph_availability(context):
        graph_decisions.append(context)
        if (
            type(context) is RetrievalHostContext
            and context.selected_source_graph_access is not None
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "selected_source_graph_access_unavailable",
        )

    def chunk(chunk_id):
        return RetrievedChunk(
            chunk_id=chunk_id, source_id="source", source_title="Source",
            section_path="Section", text=chunk_id,
            element_ids=[f"element-{chunk_id}"],
        )

    class _Independent:
        invocations = frozenset({"selected_evidence"})

        def __init__(self):
            self.calls = 0

        def contribute(self, _context):
            self.calls += 1
            return ContributorResult((EvidenceCandidate(
                identity="other", notebook_id="notebook", source_id="source",
                provenance=EvidenceProvenance("chunk", "other"),
                value=object(), token_cost=1,
            ),), ExtensionResultStatus.AVAILABLE)

    contributor = _Independent()
    declaration = ContributionDeclaration(
        "builtin.independent",
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )

    class _Bundle:
        manifest = ExtensionManifest(
            id="builtin.independent", version="1.0.0",
            api_version=EXTENSION_API_VERSION, display_name="Independent",
            trust="builtin", contributions=(declaration,),
        )

        def register(self, registrar):
            registrar.add_contributor(
                ExtensionContribution(declaration, contributor)
            )

    runtime = build_extension_runtime(
        (SELECTED_SOURCE_GRAPH_BUNDLE, _Bundle()),
        capability_decisions={
            SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY: graph_availability,
        },
        retrieval_admission_policies={
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
    )
    host = _CountingHost(runtime.retrieval_contributors)
    baseline = [chunk("base")]

    ask = _ask_with_host(host)
    ask.settings = SimpleNamespace(selected_source_graph_enrichment_tokens=100)
    ask.retrieval_contributor_hydrate = (
        lambda _notebook_id, _actor_id, _ids: [
            RetrievedChunk(
                chunk_id="other", source_id="source", source_title="Source",
                section_path="Section", text="other",
                element_ids=["element-other"], notebook_id="notebook",
            )
        ]
    )
    ask_chunks, ask_status = ask._activate_selected_source_graph(
        "notebook", baseline
    )

    assert [item[2] for item in host.queries] == [True]
    assert host.runs == [
        ("selected_evidence", frozenset({SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY})),
    ]
    assert contributor.calls == 1
    assert [item.chunk_id for item in ask_chunks] == ["base", "other"]
    assert ask_status is None
    # Declaring the capability disabled drops the graph contribution at the
    # registration filter, so its live decision is never even consulted.
    assert graph_decisions == []


def _candidates_with_host(host, *, mode: str = "off", trigger_hits: int = 5):
    """A minimal ``CandidateRetrievalService`` instance for exercising
    ``_run_chunk_candidate_contributors`` without a real repository."""
    from app.services.retrieval_candidates import CandidateRetrievalService

    candidates = object.__new__(CandidateRetrievalService)
    candidates.settings = SimpleNamespace(
        generated_question_index_mode=mode,
        generated_question_trigger_hits=trigger_hits,
        chunk_recall=10,
        generated_question_recall=10,
        max_total_tokens=1000,
    )
    candidates.retrieval_contributors = host
    candidates.database = SimpleNamespace(is_connection_held=lambda: False)
    candidates.event_log = SimpleNamespace(emit=lambda _event: None)
    return candidates


def test_dormant_generated_question_lane_never_reaches_the_host(monkeypatch):
    """Off/untriggered + nothing else registered at ``chunk_candidates`` =>
    the baseline is returned before the call bridge or the typed call-context
    envelope is ever constructed.

    Mirrors ``test_dormant_selected_evidence_lane_never_reaches_the_host``
    (codex #565) for the generated-question lane
    (``retrieval_candidates.CandidateRetrievalService._run_chunk_candidate_contributors``).
    The traps are *recorders*, not raisers, for the same reason as that test:
    a raise from inside the constructor call would be silently absorbed by
    the surrounding ``except Exception:`` in
    ``_run_chunk_candidate_contributors``, so a regression that merely moves
    the short circuit *below* the constructors would still pass a raising
    trap.
    """
    from app.domain.extensions import GENERATED_QUESTION_ACCESS_CAPABILITY
    from app.extensions import default_extension_runtime
    from app.services import generated_question_contribution as gq_module
    from app.services.retrieval_run import retrieval_run

    recorder: list[str] = []

    def _recording(name: str):
        original = getattr(gq_module, name)

        def _wrapped(*args, **kwargs):
            recorder.append(name)
            return original(*args, **kwargs)

        return _wrapped

    monkeypatch.setattr(
        gq_module,
        "GeneratedQuestionContributionCall",
        _recording("GeneratedQuestionContributionCall"),
    )
    monkeypatch.setattr(
        gq_module,
        "generated_question_call_context",
        _recording("generated_question_call_context"),
    )

    host = _CountingHost(default_extension_runtime().retrieval_contributors)
    candidates = _candidates_with_host(host, mode="off")
    # A dormant lane must not even touch the connection probe or emit an
    # event -- both would only be reached past the short circuit.
    candidates.database = SimpleNamespace(
        is_connection_held=lambda: (_ for _ in ()).throw(
            AssertionError("dormant lane touched the connection probe")
        )
    )
    candidates.event_log = SimpleNamespace(
        emit=lambda _event: (_ for _ in ()).throw(
            AssertionError("dormant lane emitted an event")
        )
    )

    baseline = ([], [], None)
    with retrieval_run(run_kind="ask_chunk", actor_id="actor"):
        result = candidates._run_chunk_candidate_contributors(
            "notebook", "question", baseline
        )

    assert recorder == []
    assert host.runs == []
    assert host.queries == [
        (
            "chunk_candidates",
            frozenset({GENERATED_QUESTION_ACCESS_CAPABILITY}),
            False,
        ),
    ]
    assert result is baseline


def test_failing_generated_question_topology_probe_still_enters_the_host():
    """The probe is an optimisation; failing it must never drop a
    contribution. Mirrors ``test_failing_topology_probe_still_enters_the_host``
    for the generated-question lane."""
    from app.services.retrieval_run import retrieval_run

    class _BrokenProbeHost(_RecordingHost):
        def has_contributions(self, _invocation, **_kwargs):
            raise RuntimeError("probe exploded")

    baseline = ([], [], None)
    host = _BrokenProbeHost(output=baseline[0])
    candidates = _candidates_with_host(host, mode="off")

    with retrieval_run(run_kind="ask_chunk", actor_id="actor"):
        result = candidates._run_chunk_candidate_contributors(
            "notebook", "question", baseline
        )

    assert [invocation for _baseline, invocation in host.calls] == [
        "chunk_candidates",
    ]
    assert result is baseline


def test_none_generated_question_topology_probe_still_enters_the_host():
    """Mirrors ``test_none_topology_probe_still_enters_the_host`` for the
    generated-question lane: a probe answering ``None`` must not be mistaken
    for the dormant ``False`` signal.
    """
    from app.services.retrieval_run import retrieval_run

    class _NoneProbeHost(_RecordingHost):
        def has_contributions(self, _invocation, **_kwargs):
            return None

    baseline = ([], [], None)
    host = _NoneProbeHost(output=baseline[0])
    candidates = _candidates_with_host(host, mode="off")

    with retrieval_run(run_kind="ask_chunk", actor_id="actor"):
        result = candidates._run_chunk_candidate_contributors(
            "notebook", "question", baseline
        )

    assert [invocation for _baseline, invocation in host.calls] == [
        "chunk_candidates",
    ]
    assert result is baseline


def test_registered_independent_generated_question_contribution_still_enters_the_host():
    """Non-vacuous generality for the generated-question lane's own short
    circuit: another real ``chunk_candidates`` contribution keeps the host
    call even while the generated-question capability itself stays disabled.
    Mirrors ``test_registered_independent_contribution_still_enters_the_host``.
    """
    from app.domain.extensions import GENERATED_QUESTION_ACCESS_CAPABILITY
    from app.extensions import build_extension_runtime
    from app.extension_sdk import (
        EXTENSION_API_VERSION,
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionDeclaration,
        ContributionKind,
        ContributorResult,
        EvidenceCandidate,
        EvidenceProvenance,
        ExtensionContribution,
        ExtensionManifest,
        ExtensionResultStatus,
    )
    from app.services.retrieval import RetrievedChunk
    from app.services.retrieval_run import retrieval_run

    def chunk(chunk_id: str) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=chunk_id, source_id="source", source_title="Source",
            section_path="Section", text=chunk_id, notebook_id="notebook",
        )

    class _Independent:
        invocations = frozenset({"chunk_candidates"})

        def __init__(self):
            self.calls = 0

        def contribute(self, _context):
            self.calls += 1
            return ContributorResult((EvidenceCandidate(
                identity="other", notebook_id="notebook", source_id="source",
                provenance=EvidenceProvenance("chunk", "other"),
                value=object(), token_cost=1,
            ),), ExtensionResultStatus.AVAILABLE)

    contributor = _Independent()
    declaration = ContributionDeclaration(
        "builtin.independent_chunk_candidate",
        RETRIEVAL_CONTRIBUTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )

    class _Bundle:
        manifest = ExtensionManifest(
            id="builtin.independent_chunk_candidate", version="1.0.0",
            api_version=EXTENSION_API_VERSION, display_name="Independent",
            trust="builtin", contributions=(declaration,),
        )

        def register(self, registrar):
            registrar.add_contributor(
                ExtensionContribution(declaration, contributor)
            )

    runtime = build_extension_runtime((_Bundle(),))
    host = _CountingHost(runtime.retrieval_contributors)
    candidates = _candidates_with_host(host, mode="off")
    candidates.hydrate_retrieval_contribution_chunks = (
        lambda _notebook_id, _actor_id, _ids: [chunk("other")]
    )
    candidates._hydrate_chunk_candidates = (
        lambda cand_ids: ([], list(cand_ids), "MATRIX")
    )

    baseline_chunk = chunk("base")
    baseline = ([baseline_chunk], ["base"], None)

    with retrieval_run(run_kind="ask_chunk", actor_id="actor"):
        result = candidates._run_chunk_candidate_contributors(
            "notebook", "question", baseline
        )

    scored, ids, matrix = result
    assert host.queries == [
        (
            "chunk_candidates",
            frozenset({GENERATED_QUESTION_ACCESS_CAPABILITY}),
            True,
        ),
    ]
    assert host.runs == [
        ("chunk_candidates", frozenset({GENERATED_QUESTION_ACCESS_CAPABILITY})),
    ]
    assert contributor.calls == 1
    assert [item.chunk_id for item in scored] == ["base", "other"]
    assert ids == ["base", "other"]
    assert matrix == "MATRIX"
