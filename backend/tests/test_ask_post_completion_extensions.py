from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
import time

import pytest

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    AskCompletedObserverCallContext,
    CompletedAskNotification,
)
from app.extension_sdk import (
    ASK_COMPLETED_OBSERVER_POINT,
    EXTENSION_API_VERSION,
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
from app.extensions.ask import AskCompletedObserverHost
from app.extensions.bootstrap import build_extension_runtime, default_extension_runtime
from app.extensions.registry import ExtensionRegistryError
from app.services.repository_runtime import RepositoryRuntime


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


def _deadline() -> float:
    return time.monotonic() + 60.0


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
        deadline_monotonic=_deadline(),
    )


def test_empty_hosts_are_strict_noops_before_validation_clock_probe_and_events():
    def poison(*_args, **_kwargs):
        raise AssertionError("strict empty topology touched a collaborator")

    runtime = build_extension_runtime((), event_sink=poison)
    runtime.ask_completed_observers._clock = poison

    assert runtime.ask_completed_observers.observe_application(object()) is None


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
        _deadline(),
    )
    runtime.ask_completed_observers.observe_application(context)

    assert plugin_calls == ["twice", "raising", "later"]
    assert core_calls == ["core", "core"]


def test_observer_cannot_reset_the_host_owned_at_most_once_latch():
    core_calls = []

    class HostileObserver:
        def observe(self, context):
            first = context.access.notify()
            for name, value in (("_called", False), ("_receipt", None)):
                try:
                    setattr(context.access, name, value)
                except AttributeError:
                    pass
            second = context.access.notify()
            assert first is second
            return second

    cap = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
    runtime = build_extension_runtime(
        (_bundle("obs.hostile_latch", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, HostileObserver(), requires=(cap,)),),
        capability_decisions={cap: lambda _context: Availability.available()},
    )
    runtime.ask_completed_observers.observe_application(
        AskCompletedObserverCallContext(
            CompletedAskNotification("user-1", "notebook-1", "reasoning"),
            _Port("core", core_calls),
            None,
            None,
            _ConnectionProbe(),
            _deadline(),
        )
    )
    assert core_calls == ["core"]


def test_chunk_context_cannot_forge_the_reasoning_only_core_access():
    calls = []
    context = _call_context(calls, mode_id="chunk")
    object.__setattr__(context, "retrieval_experience", _Port("retrieval", calls))

    default_extension_runtime().ask_completed_observers.observe_application(context)

    assert calls == ["agent", "search"]


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
        _deadline(),
    )

    runtime.ask_completed_observers.observe_application(context)
    available = True
    runtime.ask_completed_observers.observe_application(context)
    assert plugin_calls == []
    assert core_calls == []


def test_unavailable_decision_cannot_leave_a_lease_for_later_observers():
    touches = []
    probe = _ConnectionProbe()

    class Observer:
        def observe(self, _context):
            touches.append("plugin")
            return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

    def decision(_context):
        probe.held = True
        return Availability(AvailabilityStatus.UNAVAILABLE, "temporarily_unavailable")

    capability = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
    runtime = build_extension_runtime(
        (
            _bundle(
                "obs.availability_changes_connection",
                ASK_COMPLETED_OBSERVER_POINT,
                ContributionKind.OBSERVER,
                Observer(),
                requires=(capability,),
            ),
            _bundle(
                "obs.later",
                ASK_COMPLETED_OBSERVER_POINT,
                ContributionKind.OBSERVER,
                Observer(),
            ),
        ),
        capability_decisions={capability: decision},
    )

    runtime.ask_completed_observers.observe_application(
        replace(_call_context(touches), connection_probe=probe)
    )

    assert touches == []


def test_unavailable_observer_does_not_resolve_the_core_notify_property():
    touches = []

    class PoisonPort:
        @property
        def notify(self):
            touches.append("getter")
            raise AssertionError("availability touched execution access")

    class Observer:
        def observe(self, _context):
            touches.append("plugin")
            return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

    cap = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
    runtime = build_extension_runtime(
        (_bundle("obs.unavailable", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Observer(), requires=(cap,)),),
        capability_decisions={
            cap: lambda _context: Availability(
                AvailabilityStatus.UNAVAILABLE, "temporarily_unavailable"
            )
        },
    )
    runtime.ask_completed_observers.observe_application(
        AskCompletedObserverCallContext(
            CompletedAskNotification("user-1", "notebook-1", "reasoning"),
            PoisonPort(),
            None,
            None,
            _ConnectionProbe(),
            _deadline(),
        )
    )
    assert touches == []


def test_core_notify_resolution_cannot_cross_the_connection_boundary():
    for raises in (False, True):
        touches = []
        probe = _ConnectionProbe()

        class BoundaryChangingPort:
            @property
            def notify(self):
                touches.append("getter")
                probe.held = True
                if raises:
                    raise RuntimeError("private lazy port failure")
                return lambda: touches.append("core")

        class Observer:
            def observe(self, _context):
                touches.append("plugin")
                return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

        class Later:
            def observe(self, _context):
                touches.append("later")
                return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

        capability = ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY
        events = []
        runtime = build_extension_runtime(
            (
                _bundle(
                    "obs.boundary_changing",
                    ASK_COMPLETED_OBSERVER_POINT,
                    ContributionKind.OBSERVER,
                    Observer(),
                    requires=(capability,),
                ),
                _bundle(
                    "obs.later",
                    ASK_COMPLETED_OBSERVER_POINT,
                    ContributionKind.OBSERVER,
                    Later(),
                ),
            ),
            capability_decisions={
                capability: lambda _context: Availability.available()
            },
            event_sink=events.append,
        )
        runtime.ask_completed_observers.observe_application(
            AskCompletedObserverCallContext(
                CompletedAskNotification("user-1", "notebook-1", "reasoning"),
                BoundaryChangingPort(),
                None,
                None,
                probe,
                _deadline(),
            )
        )

        assert touches == ["getter"]
        assert events[-1]["reason_code"] == "connection_lease_held"


def test_non_finite_clock_cannot_interrupt_later_observers():
    values = iter((0.0, float("nan"), 1.0, 2.0, 3.0, 4.0, 5.0))
    runtime = default_extension_runtime()
    original = runtime.ask_completed_observers._clock
    calls = []
    runtime.ask_completed_observers._clock = lambda: next(values)
    try:
        runtime.ask_completed_observers.observe_application(_call_context(calls))
    finally:
        runtime.ask_completed_observers._clock = original
    assert calls == ["agent", "retrieval", "search"]


def test_finite_clock_subtraction_overflow_cannot_interrupt_later_observers():
    values = iter((
        -1e308, 0.0, 0.0, 1e308,
        0.0, 0.0, 0.0, 0.0,
        0.0, 0.0, 0.0, 0.0,
    ))
    runtime = default_extension_runtime()
    original = runtime.ask_completed_observers._clock
    calls = []
    runtime.ask_completed_observers._clock = lambda: next(values)
    try:
        runtime.ask_completed_observers.observe_application(_call_context(calls))
    finally:
        runtime.ask_completed_observers._clock = original
    assert calls == ["agent", "retrieval", "search"]


def test_future_stable_mode_keeps_mode_agnostic_completion_observers_active():
    calls = []
    context = _call_context(calls, mode_id="future_mode")

    default_extension_runtime().ask_completed_observers.observe_application(context)

    assert calls == ["agent", "search"]


def test_observer_point_budget_stops_before_starting_later_core_work():
    runtime = default_extension_runtime()
    original = runtime.ask_completed_observers._clock
    calls = []
    runtime.ask_completed_observers._clock = lambda: 10.0
    context = replace(_call_context(calls), deadline_monotonic=5.0)
    try:
        runtime.ask_completed_observers.observe_application(context)
    finally:
        runtime.ask_completed_observers._clock = original
    assert calls == []


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
            _deadline(),
        )
    )
    assert all("user-1" not in repr(event) for event in events)
    assert all("notebook-1" not in repr(event) for event in events)


def test_repository_runtime_supplies_its_call_scoped_content_free_event_sink():
    captured = []
    emitted = []

    class ObserverHost:
        def observe_application(self, context, *, event_sink=None):
            captured.append(("observer", context, event_sink))

    runtime = object.__new__(RepositoryRuntime)
    runtime.ask_completed_observers = ObserverHost()
    runtime.agent_profile_jobs = SimpleNamespace(note_ask_completed=lambda *_: None)
    runtime.retrieval_experience_jobs = SimpleNamespace(
        note_ask_completed=lambda: None
    )
    runtime.search_profile_jobs = SimpleNamespace(note_ask_completed=lambda *_: None)
    runtime.memory_suggest_trigger = SimpleNamespace(has_fresh_manual_hint=lambda _owner: False)
    runtime.database = _ConnectionProbe()
    runtime.event_log = SimpleNamespace(emit=emitted.append)
    runtime.settings = SimpleNamespace(
        ask_post_completion_extension_timeout_seconds=30.0,
    )

    runtime._note_ask_completed("notebook-1", "user-1", "reasoning")

    assert [kind for kind, *_ in captured] == ["observer"]
    assert captured[0][2] is runtime.event_log.emit
    assert emitted == []


def _runtime_for_global(host):
    calls: list[tuple] = []
    runtime = object.__new__(RepositoryRuntime)
    runtime.ask_completed_observers = host
    runtime.agent_profile_jobs = SimpleNamespace(
        note_ask_completed=lambda nb, uid: calls.append(("profile", nb, uid))
    )
    runtime.retrieval_experience_jobs = SimpleNamespace(
        note_ask_completed=lambda nb: calls.append(("experience", nb))
    )
    runtime.search_profile_jobs = SimpleNamespace(
        note_ask_completed=lambda uid: calls.append(("search", uid))
    )
    runtime.memory_suggest_trigger = SimpleNamespace(
        has_fresh_manual_hint=lambda _owner: False,
        note_ask_completed=lambda notification: calls.append(
            ("memory_suggest", notification.notebook_id)
        )
    )
    runtime.database = _ConnectionProbe()
    runtime.event_log = SimpleNamespace(emit=lambda *_: None)
    runtime.settings = SimpleNamespace(
        ask_post_completion_extension_timeout_seconds=30.0,
    )
    return runtime, calls


def test_global_ask_completion_sends_one_notification_with_every_participant():
    """A finished global Ask advances the SAME three chains a notebook ask
    does, attributed for a cross-library run: the overlay once per participant
    library behind ONE agent-profile capability, the experience chain once
    into the global partition (reasoning only), the search profile once per
    ask -- and one notification, not one per library."""
    captured = []

    class ObserverHost:
        def observe_application(self, context, *, event_sink=None):
            captured.append(context)

    runtime, calls = _runtime_for_global(ObserverHost())
    runtime._note_global_ask_completed(
        ["nb-b", "nb-a", "nb-b"], "user-1", "reasoning", anchor="nb-anchor",
    )

    assert len(captured) == 1
    notification = captured[0].notification
    assert notification.scope == "global"
    # The anchor is the run's nominal active, NOT the first attributed library.
    assert notification.notebook_id == "nb-anchor"
    assert notification.notebook_ids == ("nb-b", "nb-a")
    assert notification.actor_id == "user-1" and notification.mode_id == "reasoning"
    captured[0].agent_profile.notify()
    captured[0].retrieval_experience.notify()
    captured[0].search_profile.notify()
    assert calls == [
        ("profile", "nb-b", "user-1"), ("profile", "nb-a", "user-1"),
        ("experience", ""), ("search", "user-1"),
    ]

    captured.clear()
    runtime._note_global_ask_completed(["nb-a"], "user-1", "chunk", anchor="nb-a")
    assert captured[0].retrieval_experience is None
    assert captured[0].notification.mode_id == "chunk"

    # No library touched (every leg skipped): the person's ask still counts
    # for the experience partition, the search profile and the observers;
    # only the overlay loop is empty.
    captured.clear(); calls.clear()
    runtime._note_global_ask_completed([], "user-1", "reasoning", anchor="nb-anchor")
    assert captured[0].notification.notebook_id == "nb-anchor"
    assert captured[0].notification.notebook_ids == ()
    captured[0].agent_profile.notify()
    captured[0].retrieval_experience.notify()
    captured[0].search_profile.notify()
    assert calls == [("experience", ""), ("search", "user-1")]

    captured.clear()
    runtime._note_global_ask_completed([], "user-1", "reasoning")
    runtime._note_global_ask_completed(["nb-a"], "", "reasoning", anchor="nb-a")
    assert captured == []


def test_global_ask_completion_without_a_host_calls_the_three_chains_directly(caplog):
    runtime, calls = _runtime_for_global(None)
    runtime._note_global_ask_completed(["nb-a", "nb-b"], "user-1", "reasoning", anchor="nb-a")
    assert calls == [
        ("profile", "nb-a", "user-1"), ("profile", "nb-b", "user-1"),
        ("experience", ""), ("search", "user-1"),
    ]
    calls.clear()
    runtime._note_global_ask_completed(["nb-a"], "user-1", "chunk", anchor="nb-a")
    assert calls == [("profile", "nb-a", "user-1"), ("search", "user-1")]
    calls.clear()
    runtime._note_global_ask_completed([], "user-1", "reasoning", anchor="nb-a")
    assert calls == [("experience", ""), ("search", "user-1")]

    # One chain failing never eats the others (per-chain isolation).
    runtime.agent_profile_jobs = SimpleNamespace(
        note_ask_completed=lambda *_: (_ for _ in ()).throw(
            RuntimeError("secret /private/path")
        )
    )
    calls.clear()
    with caplog.at_level("WARNING", logger="silicon_notebook.repository_runtime"):
        runtime._note_global_ask_completed(["nb-a"], "user-1", "reasoning", anchor="nb-a")
    assert calls == [("experience", ""), ("search", "user-1")]
    _assert_content_free_failure_log(caplog, "secret /private/path")

    # The notebook twin's compat path keeps the same content-free receipt.
    caplog.clear()
    calls.clear()
    with caplog.at_level("WARNING", logger="silicon_notebook.repository_runtime"):
        runtime._note_ask_completed("nb-a", "user-1", "reasoning")
    assert calls == [
        ("experience", "nb-a"), ("search", "user-1"), ("memory_suggest", "nb-a"),
    ]
    _assert_content_free_failure_log(caplog, "secret /private/path")


def _assert_content_free_failure_log(caplog, secret: str) -> None:
    """The failure receipt names the exception CLASS and nothing else: no
    exception text (which may carry SQL, source content or private paths)
    and no traceback (AGENTS.md: exception text stays out of logs)."""
    failures = [record for record in caplog.records if "notification failed" in record.getMessage()]
    assert len(failures) == 1
    record = failures[0]
    assert record.levelname == "WARNING"
    assert record.exc_info is None
    assert "RuntimeError" in record.getMessage()
    assert secret not in record.getMessage()
    assert secret not in caplog.text


def test_global_notification_projects_scope_to_every_observer_and_participants_only_behind_agent_profile():
    """A global Ask's notification reaches plugins as ONE context per observer:
    every observer learns ``scope="global"``; only the agent-profile capability
    (the one that already sees notebook identity) gets the participant list,
    with ``notebook`` still the anchor."""
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
        "obs.search": ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    }
    bundles = tuple(
        _bundle(contribution_id, ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER,
                Observer(label), requires=(capability,))
        for (contribution_id, capability), label in zip(
            capabilities.items(), ("agent", "search"), strict=True)
    )
    runtime = build_extension_runtime(
        bundles,
        capability_decisions={
            capability: lambda context: Availability.available()
            for capability in capabilities.values()
        },
    )
    call_context = replace(
        _call_context(calls, mode_id="chunk"),
        notification=CompletedAskNotification(
            "user-1", "notebook-1", "chunk",
            notebook_ids=("notebook-1", "notebook-2"), scope="global",
        ),
    )
    runtime.ask_completed_observers.observe_application(call_context)

    assert calls == ["agent", "search"]
    assert contexts["agent"].scope == "global"
    assert contexts["agent"].notebook.id == "notebook-1"
    assert [ref.id for ref in contexts["agent"].notebooks] == ["notebook-1", "notebook-2"]
    assert contexts["search"].scope == "global"
    assert contexts["search"].notebook is None
    assert contexts["search"].notebooks == ()

    # A notebook Ask carries the defaults, so an unchanged plugin sees nothing new.
    contexts.clear(); calls.clear()
    runtime.ask_completed_observers.observe_application(_call_context(calls, mode_id="chunk"))
    assert contexts["agent"].scope == "notebook" and contexts["agent"].notebooks == ()


# ---------------------------------------------------------------------------
# Auto user-memory suggestions: the fourth core port (design 2026-10-28 §4.1).
# ---------------------------------------------------------------------------


def _memory_context(calls: list[str], **notification_fields) -> AskCompletedObserverCallContext:
    notification = CompletedAskNotification(
        "user-1", "notebook-1", "reasoning", **notification_fields
    )
    return replace(
        _call_context(calls),
        notification=notification,
        memory_suggest=_Port("memory_suggest", calls),
    )


def test_the_host_admits_exactly_four_core_notification_capabilities():
    from app.extensions.ask import _OBSERVER_ACCESS_CAPABILITIES

    assert _OBSERVER_ACCESS_CAPABILITIES == frozenset({
        ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
        ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
        ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
        ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY,
    })
    assert len(_OBSERVER_ACCESS_CAPABILITIES) == 4
    # Public through the SDK under the same name.
    from app import extension_sdk

    assert (
        extension_sdk.ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY
        == "ask:memory_suggest_completed_access"
    )


def test_an_observer_requesting_memory_suggest_and_another_capability_is_rejected():
    class Observer:
        def observe(self, _context):
            raise AssertionError("never frozen")

    bundle = _bundle(
        "obs.greedy", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Observer(),
        requires=(
            ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY,
            ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
        ),
    )
    with pytest.raises(ExtensionRegistryError, match="multiple core notification"):
        build_extension_runtime(
            (bundle,),
            capability_decisions={
                ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY: lambda _c: Availability.available(),
                ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY: lambda _c: Availability.available(),
            },
        )


def test_memory_suggest_observer_runs_last_among_builtins():
    runtime = default_extension_runtime()
    host = runtime.ask_completed_observers
    builtin_ask_observers = [
        frozen.registered.contribution.declaration.id
        for frozen in host._observers
        if frozen.registered.plugin_id.startswith("builtin.")
    ]
    assert builtin_ask_observers == [
        "builtin.ask_agent_profile",
        "builtin.ask_retrieval_experience",
        "builtin.ask_search_profile",
        "builtin.ask_user_memory_suggest",
    ]
    calls: list[str] = []
    host.observe_application(_memory_context(calls, submitted_via="web"))
    assert calls == ["agent", "retrieval", "search", "memory_suggest"]


def test_budget_exhausted_breaks_before_memory_suggest_first_three_ran():
    """The shared deadline is spent while the third observer runs: the host
    ``break``s at the fourth (memory suggestions) -- the one a tight budget
    is meant to drop first -- after the other three already ran."""
    runtime = default_extension_runtime()
    host = runtime.ask_completed_observers
    now = [100.0]
    calls: list[str] = []
    events: list[dict] = []

    class _SpendingPort(_Port):
        def notify(self) -> None:
            super().notify()
            now[0] = 1_000.0

    context = replace(
        _memory_context(calls, submitted_via="web"),
        search_profile=_SpendingPort("search", calls),
        deadline_monotonic=200.0,
    )
    original = host._clock
    host._clock = lambda: now[0]
    try:
        host.observe_application(context, event_sink=events.append)
    finally:
        host._clock = original
    assert calls == ["agent", "retrieval", "search"]
    assert [
        (event["contribution_id"], event["reason_code"])
        for event in events
        if event["contribution_id"] == "builtin.ask_user_memory_suggest"
    ] == [("builtin.ask_user_memory_suggest", "extension_point_budget_exhausted")]


def test_old_notification_constructor_still_valid():
    """The three fields are appended with defaults: every pre-existing
    positional and keyword construction (and the six-field call context) is
    still accepted by the host, unchanged."""
    notification = CompletedAskNotification("user-1", "notebook-1", "reasoning")
    assert (
        notification.submitted_via,
        notification.had_clarification,
        notification.ask_job_id,
    ) == ("", False, "")
    positional = CompletedAskNotification(
        "user-1", "notebook-1", "chunk", ("notebook-1",), "global"
    )
    assert positional.scope == "global" and positional.ask_job_id == ""
    calls: list[str] = []
    context = AskCompletedObserverCallContext(
        notification, _Port("agent", calls), _Port("retrieval", calls),
        _Port("search", calls), _ConnectionProbe(), _deadline(),
    )
    assert context.memory_suggest is None
    default_extension_runtime().ask_completed_observers.observe_application(context)
    assert calls == ["agent", "retrieval", "search"]


@pytest.mark.parametrize("fields", [
    {"submitted_via": None},
    {"submitted_via": 1},
    {"had_clarification": "yes"},
    {"had_clarification": 1},
    {"had_clarification": None},
    {"ask_job_id": None},
    {"ask_job_id": 7},
])
def test_invalid_new_notification_fields_rejected(fields):
    """``_valid_notification`` holds the three appended fields to their exact
    types (``type(x) is str/bool``): an ill-typed notification is dropped
    before any observer, core port or event runs."""
    calls: list[str] = []
    events: list[dict] = []
    context = _memory_context(calls)
    context = replace(context, notification=replace(context.notification, **fields))
    default_extension_runtime().ask_completed_observers.observe_application(
        context, event_sink=events.append
    )
    assert calls == [] and events == []


def test_a_global_notification_never_reaches_the_memory_suggest_port():
    """Notebook-scope Asks only: even a call context that (wrongly) seats the
    port for a global Ask gets it withheld by the host."""
    calls: list[str] = []
    events: list[dict] = []
    context = _memory_context(
        calls, notebook_ids=("notebook-1",), scope="global", submitted_via="web"
    )
    default_extension_runtime().ask_completed_observers.observe_application(
        context, event_sink=events.append
    )
    assert "memory_suggest" not in calls
    assert [
        event["reason_code"] for event in events
        if event["contribution_id"] == "builtin.ask_user_memory_suggest"
    ] == ["ask_completed_access_unavailable"]


@pytest.mark.parametrize("extra_capability", [False, True])
def test_core_manual_skip_preserves_third_party_ports_availability_and_events(extra_capability):
    from app.extensions.builtin.ask_completion import ASK_MEMORY_SUGGEST_COMPLETED_BUNDLE

    observations, contexts, calls, events = [], [], [], []
    capability = ASK_MEMORY_SUGGEST_COMPLETED_ACCESS_CAPABILITY
    extra = "test:independent_access"

    class Plugin:
        def observe(self, context):
            contexts.append(context)
            return context.access.notify()

    def available(context):
        observations.append(context)
        return Availability.available()

    runtime = build_extension_runtime(
        (ASK_MEMORY_SUGGEST_COMPLETED_BUNDLE,
         _bundle("plugin.memory", ASK_COMPLETED_OBSERVER_POINT, ContributionKind.OBSERVER, Plugin(),
                 requires=(capability,extra) if extra_capability else (capability,), availability=available)),
        capability_decisions={capability:available,extra:available},
    )
    host = runtime.ask_completed_observers
    host._clock = lambda: 0.0
    context = _memory_context(calls,submitted_via="web")
    host.observe_application(context,event_sink=events.append)
    old_events = [event for event in events if event["contribution_id"]=="plugin.memory"]
    old_contexts = list(contexts)
    old_availability = [value for value in observations if value.contribution_id=="plugin.memory"]
    assert calls == ["memory_suggest","memory_suggest"]
    observations.clear(); contexts.clear(); calls.clear(); events.clear()
    host.observe_application(replace(context,skip_builtin_memory_suggest=True),event_sink=events.append)
    assert calls == ["memory_suggest"]
    assert len(contexts) == len(old_contexts) == 1
    assert contexts[0].access is not None and old_contexts[0].access is not None
    assert replace(contexts[0],access=None) == replace(old_contexts[0],access=None)
    assert [value for value in observations if value.contribution_id=="plugin.memory"] == old_availability
    assert events == old_events
    assert all(not hasattr(value,"skip_builtin_memory_suggest") for value in contexts)


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_core_manual_skip_attestation_is_strict_bool(value):
    calls, events = [], []
    context = replace(_memory_context(calls),skip_builtin_memory_suggest=value)
    default_extension_runtime().ask_completed_observers.observe_application(context,event_sink=events.append)
    assert calls == [] and events == []
