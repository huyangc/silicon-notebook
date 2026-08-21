from __future__ import annotations

from dataclasses import dataclass

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    AnswerAuditSnapshot,
    AskCompletedObserverCallContext,
    CompletedAskNotification,
)
from app.extension_sdk import (
    ANSWER_AUDITOR_POINT,
    ASK_COMPLETED_OBSERVER_POINT,
    EXTENSION_API_VERSION,
    AnswerAudit,
    AnswerAuditFinding,
    AuditorResult,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionBundle,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    ObserverReceipt,
)
from app.extensions.ask import AnswerAuditorHost, AskCompletedObserverHost
from app.extensions.bootstrap import build_extension_runtime, default_extension_runtime


class _ConnectionProbe:
    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.calls = 0

    def is_connection_held(self) -> bool:
        self.calls += 1
        return self.held


class _Port:
    def __init__(self, label: str, calls: list[str], *, fail: bool = False) -> None:
        self.label = label
        self.calls = calls
        self.fail = fail

    def notify(self) -> None:
        self.calls.append(self.label)
        if self.fail:
            raise RuntimeError("private core failure")


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.add(self.contribution)


def _bundle(
    contribution_id: str,
    point: str,
    kind: ContributionKind,
    implementation: object,
    *,
    requires: tuple[str, ...] = (),
    availability=None,
) -> ExtensionBundle:
    declaration = ContributionDeclaration(contribution_id, point, kind)
    return _Bundle(
        ExtensionManifest(
            id=contribution_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="builtin",
            contributions=(declaration,),
            requires=requires,
        ),
        ExtensionContribution(declaration, implementation, availability),
    )


def _snapshot() -> AnswerAuditSnapshot:
    return AnswerAuditSnapshot(
        mode_id="reasoning",
        grounded=True,
        evidence_level="high",
        citation_count=2,
        anchor_count=3,
        model_error_count=0,
        answer_chars=80,
        conclusion_chars=12,
    )


def _call_context(
    calls: list[str],
    *,
    mode_id: str = "reasoning",
    probe: object | None = None,
) -> AskCompletedObserverCallContext:
    return AskCompletedObserverCallContext(
        notification=CompletedAskNotification("user-1", "notebook-1", mode_id),
        agent_profile=_Port("agent", calls),
        retrieval_experience=(
            _Port("retrieval", calls) if mode_id == "reasoning" else None
        ),
        search_profile=_Port("search", calls),
        connection_probe=probe or _ConnectionProbe(),
    )


def test_empty_hosts_are_strict_noops_before_validation_clock_probe_and_events():
    def poison(*_args, **_kwargs):
        raise AssertionError("strict empty topology touched a collaborator")

    runtime = build_extension_runtime((), event_sink=poison)
    runtime.answer_auditors._clock = poison
    runtime.ask_completed_observers._clock = poison

    assert runtime.answer_auditors.audit_application(
        object(), connection_probe=object(), event_sink=poison
    ) == ()
    assert runtime.ask_completed_observers.observe_application(object()) is None


def test_auditor_receives_only_closed_structural_view_and_cannot_replace_answer():
    seen = []

    class Auditor:
        def audit(self, context):
            seen.append(context)
            return AuditorResult(
                AnswerAudit((AnswerAuditFinding("warning", "weak_grounding", 2),)),
                ExtensionResultStatus.AVAILABLE,
            )

    runtime = build_extension_runtime((
        _bundle("audit.structural", ANSWER_AUDITOR_POINT, ContributionKind.AUDITOR, Auditor()),
    ))
    result = runtime.answer_auditors.audit_application(
        _snapshot(), connection_probe=_ConnectionProbe()
    )

    assert result == (
        AnswerAudit((AnswerAuditFinding("warning", "weak_grounding", 2),)),
    )
    assert len(seen) == 1
    view = seen[0].answer
    assert not hasattr(view, "answer")
    assert not hasattr(view, "question")
    assert not hasattr(view, "citations")
    assert not hasattr(view, "repository")
    assert view.citation_count == 2


def test_auditor_failure_and_malformed_result_do_not_stop_later_auditors():
    calls = []

    class Raising:
        def audit(self, _context):
            calls.append("raise")
            raise RuntimeError("secret answer text")

    class Invalid:
        def audit(self, _context):
            calls.append("invalid")
            return AuditorResult(
                AnswerAudit((AnswerAuditFinding("warning", "bad code"),)),
                ExtensionResultStatus.AVAILABLE,
            )

    class Valid:
        def audit(self, _context):
            calls.append("valid")
            return AuditorResult(AnswerAudit(()), ExtensionResultStatus.AVAILABLE)

    events = []
    runtime = build_extension_runtime(
        (
            _bundle("audit.c_raise", ANSWER_AUDITOR_POINT, ContributionKind.AUDITOR, Raising()),
            _bundle("audit.b_invalid", ANSWER_AUDITOR_POINT, ContributionKind.AUDITOR, Invalid()),
            _bundle("audit.d_valid", ANSWER_AUDITOR_POINT, ContributionKind.AUDITOR, Valid()),
        ),
        event_sink=events.append,
    )

    assert runtime.answer_auditors.audit_application(
        _snapshot(), connection_probe=_ConnectionProbe()
    ) == (AnswerAudit(()),)
    assert calls == ["invalid", "raise", "valid"]
    assert all("secret" not in repr(event) for event in events)
    assert all(set(event) <= {
        "kind", "point", "contribution_id", "status", "duration_ms",
        "count", "reason_code",
    } for event in events)


def test_default_observers_preserve_reasoning_order_and_chunk_filter():
    runtime = default_extension_runtime()
    reasoning_calls: list[str] = []
    runtime.ask_completed_observers.observe_application(
        _call_context(reasoning_calls)
    )
    assert reasoning_calls == ["agent", "retrieval", "search"]

    chunk_calls: list[str] = []
    runtime.ask_completed_observers.observe_application(
        _call_context(chunk_calls, mode_id="chunk")
    )
    assert chunk_calls == ["agent", "search"]


def test_observer_context_projects_only_each_declared_identity():
    contexts = {}
    calls: list[str] = []

    class Observer:
        def __init__(self, label: str) -> None:
            self.label = label

        def observe(self, context):
            contexts[self.label] = context
            return context.access.notify()

    capabilities = {
        "obs.agent": ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
        "obs.retrieval": ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
        "obs.search": ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    }
    bundles = tuple(
        _bundle(
            contribution_id,
            ASK_COMPLETED_OBSERVER_POINT,
            ContributionKind.OBSERVER,
            Observer(label),
            requires=(capability,),
        )
        for (contribution_id, capability), label in zip(
            capabilities.items(), ("agent", "retrieval", "search"), strict=True
        )
    )
    runtime = build_extension_runtime(
        bundles,
        capability_decisions={
            capability: lambda context: (
                Availability.available()
                if context.access_available is True
                else Availability(AvailabilityStatus.UNAVAILABLE, "access_unavailable")
            )
            for capability in capabilities.values()
        },
    )
    runtime.ask_completed_observers.observe_application(_call_context(calls))

    assert calls == ["agent", "retrieval", "search"]
    assert contexts["agent"].actor.id == "user-1"
    assert contexts["agent"].notebook.id == "notebook-1"
    assert contexts["retrieval"].actor is None
    assert contexts["retrieval"].notebook is None
    assert contexts["search"].actor.id == "user-1"
    assert contexts["search"].notebook is None
    for context in contexts.values():
        assert not hasattr(context, "question")
        assert not hasattr(context, "answer")
        assert not hasattr(context, "repository")
        assert not hasattr(context, "settings")


def test_observer_failures_are_isolated_and_core_access_is_at_most_once():
    plugin_calls = []
    core_calls = []

    class Twice:
        def observe(self, context):
            plugin_calls.append("twice")
            first = context.access.notify()
            second = context.access.notify()
            assert first is second
            return second

    class Raising:
        def observe(self, _context):
            plugin_calls.append("raising")
            raise RuntimeError("private actor id")

    class Later:
        def observe(self, context):
            plugin_calls.append("later")
            return context.access.notify()

    cap = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
    runtime = build_extension_runtime(
        (
            _bundle("obs.a_twice", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Twice(), requires=(cap,)),
            _bundle("obs.b_raising", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Raising()),
            _bundle("obs.c_later", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Later(), requires=(cap,)),
        ),
        capability_decisions={cap: lambda _context: Availability.available()},
    )
    context = AskCompletedObserverCallContext(
        CompletedAskNotification("user-1", "notebook-1", "reasoning"),
        _Port("core", core_calls),
        None,
        None,
        _ConnectionProbe(),
    )
    runtime.ask_completed_observers.observe_application(context)

    assert plugin_calls == ["twice", "raising", "later"]
    assert core_calls == ["core", "core"]


def test_live_unavailable_and_connection_flip_skip_plugin_and_core_io():
    plugin_calls = []
    core_calls = []
    available = False
    probe = _ConnectionProbe()

    class Observer:
        def observe(self, context):
            plugin_calls.append("observe")
            return context.access.notify()

    cap = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY

    def decision(_context):
        if available:
            probe.held = True
            return Availability.available()
        return Availability(AvailabilityStatus.UNAVAILABLE, "temporarily_unavailable")

    runtime = build_extension_runtime(
        (_bundle("obs.live", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Observer(), requires=(cap,)),),
        capability_decisions={cap: decision},
    )
    context = AskCompletedObserverCallContext(
        CompletedAskNotification("user-1", "notebook-1", "reasoning"),
        _Port("core", core_calls),
        None,
        None,
        probe,
    )

    runtime.ask_completed_observers.observe_application(context)
    available = True
    runtime.ask_completed_observers.observe_application(context)
    assert plugin_calls == []
    assert core_calls == []


def test_hostile_core_port_shape_and_receipt_fail_open_without_identity_leak():
    events = []

    class HostilePort:
        @property
        def notify(self):
            raise RuntimeError("user-1 notebook-1")

    class Observer:
        def observe(self, _context):
            raise AssertionError("unavailable access must skip the plugin")

    cap = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
    runtime = build_extension_runtime(
        (_bundle("obs.hostile", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Observer(), requires=(cap,)),),
        capability_decisions={
            cap: lambda context: (
                Availability.available()
                if context.access_available is True
                else Availability(AvailabilityStatus.UNAVAILABLE, "access_unavailable")
            )
        },
        event_sink=events.append,
    )
    runtime.ask_completed_observers.observe_application(
        AskCompletedObserverCallContext(
            CompletedAskNotification("user-1", "notebook-1", "reasoning"),
            HostilePort(),
            None,
            None,
            _ConnectionProbe(),
        )
    )
    assert all("user-1" not in repr(event) for event in events)
    assert all("notebook-1" not in repr(event) for event in events)
