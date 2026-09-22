from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace
import time

from app.domain.extensions import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
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
    assert calls == [("experience", "nb-a"), ("search", "user-1")]
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
