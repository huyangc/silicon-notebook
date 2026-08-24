from __future__ import annotations

import contextvars
from dataclasses import dataclass, replace
import threading
import time

from app.domain.cancellation import AskCancelled
from app.domain.gap_consult import (
    GAP_CONSULT_MAX_GAP_PHRASES,
    GAP_CONSULT_MAX_SUGGESTIONS,
    GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_URL_MAX_CHARS,
    GapConsultCallContext,
    GapConsultQuery,
    GapSuggestion,
    gap_consult_host_is_dormant,
)
from app.extension_sdk import (
    ASK_GAP_CONSULT_POINT,
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ExtensionBundle,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    GapConsultAvailabilityContext,
    GapConsultExtensionContext,
)
from app.extensions.bootstrap import build_extension_runtime


class _ConnectionProbe:
    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.calls = 0

    def is_connection_held(self) -> bool:
        self.calls += 1
        return self.held


class _Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def set(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise AskCancelled()


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar: ExtensionRegistrar) -> None:
        registrar.add_contributor(self.contribution)


def _bundle(
    contribution_id: str,
    implementation: object,
    *,
    requires: tuple[str, ...] = (),
    availability=None,
) -> ExtensionBundle:
    declaration = ContributionDeclaration(
        contribution_id, ASK_GAP_CONSULT_POINT, ContributionKind.CONTRIBUTOR
    )
    return _Bundle(
        ExtensionManifest(
            id=contribution_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="deployment",
            contributions=(declaration,),
            requires=requires,
        ),
        ExtensionContribution(declaration, implementation, availability),
    )


class _Plugin:
    """Records the context it was handed and answers a canned result."""

    def __init__(self, result: object, *, label: str = "plugin") -> None:
        self.result = result
        self.label = label
        self.contexts: list[object] = []

    def consult(self, context):
        self.contexts.append(context)
        return self.result


def _suggestions(*items: GapSuggestion) -> ContributorResult[GapSuggestion]:
    return ContributorResult(items, ExtensionResultStatus.AVAILABLE)


def _query(
    *, question: str = "how do lattice codes bound shaping loss?",
    gaps: tuple[str, ...] = ("shaping loss bounds",),
    max_suggestions: int = GAP_CONSULT_MAX_SUGGESTIONS,
) -> GapConsultQuery:
    return GapConsultQuery(question, gaps, max_suggestions)


def _call(
    *,
    query: GapConsultQuery | None = None,
    probe: object | None = None,
    cancellation: object | None = None,
    deadline: float | None = None,
) -> GapConsultCallContext:
    return GapConsultCallContext(
        query if query is not None else _query(),
        cancellation if cancellation is not None else _Cancellation(),
        probe if probe is not None else _ConnectionProbe(),
        deadline if deadline is not None else time.monotonic() + 60.0,
    )


def _host(*bundles, event_sink=None, capability_decisions=None):
    return build_extension_runtime(
        bundles,
        event_sink=event_sink,
        capability_decisions=capability_decisions,
    ).gap_consult


def test_empty_topology_is_a_strict_no_op():
    def poison(*_args, **_kwargs):
        raise AssertionError("strict empty topology touched a collaborator")

    events: list[dict[str, object]] = []
    host = _host(event_sink=events.append)
    host._clock = poison
    probe = _ConnectionProbe()

    # Not even a shaped call context: the short circuit is before validation.
    assert host.consult(object()) == ()
    assert host.consult(_call(probe=probe)) == ()
    assert probe.calls == 0
    assert events == []
    assert host.has_contributions() is False


def test_dormant_probe_reads_defensively():
    assert gap_consult_host_is_dormant(_host()) is True
    assert gap_consult_host_is_dormant(
        _host(_bundle("corp.gap", _Plugin(_suggestions())))
    ) is False

    class NoProbe:
        pass

    class Raising:
        def has_contributions(self):
            raise RuntimeError("probe blew up")

    class NotCallable:
        has_contributions = "nope"

    # Anything other than the literal False keeps the caller INSIDE the host.
    for answer in (None, 0, "", [], "False", True, 1):
        class Answers:
            def has_contributions(self, _answer=answer):
                return _answer

        assert gap_consult_host_is_dormant(Answers()) is False, answer

    for host in (NoProbe(), Raising(), NotCallable(), object()):
        assert gap_consult_host_is_dormant(host) is False


def test_query_is_the_whole_egress_surface():
    # Freezing the field sets is the audit: privacy at this point is a
    # property of what these three objects CAN hold, not of a filter.
    assert set(GapConsultQuery.__dataclass_fields__) == {
        "question", "gaps", "max_suggestions",
    }
    assert set(GapSuggestion.__dataclass_fields__) == {
        "title", "url", "summary", "source_label",
    }
    assert set(GapConsultCallContext.__dataclass_fields__) == {
        "query", "cancellation", "connection_probe", "deadline_monotonic",
    }
    assert set(GapConsultExtensionContext.__dataclass_fields__) == {
        "query", "cancellation", "max_suggestions", "deadline_monotonic",
    }
    assert set(GapConsultAvailabilityContext.__dataclass_fields__) == {
        "contribution_id", "deadline_monotonic",
    }


def test_plugin_receives_no_core_port():
    plugin = _Plugin(_suggestions())
    _host(_bundle("corp.gap", plugin)).consult(_call())

    context = plugin.contexts[0]
    assert type(context) is GapConsultExtensionContext
    for forbidden in (
        "notebook", "notebook_id", "actor", "actor_id", "source_id",
        "evidence", "scope", "connection", "connection_probe", "repository",
        "settings", "model", "access", "registry", "event_sink",
    ):
        assert not hasattr(context, forbidden), forbidden
    assert context.query is not None
    assert context.query.question == _query().question


def test_budget_caps_suggestions_and_phrases():
    greedy = _Plugin(_suggestions(*(
        GapSuggestion(f"paper {index}", f"https://example.org/{index}")
        for index in range(GAP_CONSULT_MAX_SUGGESTIONS + 4)
    )))
    host = _host(_bundle("corp.gap", greedy))

    assert len(host.consult(_call())) == GAP_CONSULT_MAX_SUGGESTIONS

    # A caller asking for less gets less, and the plugin is told so.
    assert len(host.consult(_call(query=_query(max_suggestions=2)))) == 2
    assert greedy.contexts[-1].max_suggestions == 2

    # Too many gap phrases is a rejected query, not a truncated one: the host
    # must never invent an egress payload the caller did not build.
    oversized = _query(gaps=tuple(
        f"phrase {index}" for index in range(GAP_CONSULT_MAX_GAP_PHRASES + 1)
    ))
    calls_before = len(greedy.contexts)
    assert host.consult(_call(query=oversized)) == ()
    assert len(greedy.contexts) == calls_before

    for bad in (
        _query(question=""),
        _query(max_suggestions=0),
        _query(max_suggestions=GAP_CONSULT_MAX_SUGGESTIONS + 1),
        GapConsultQuery("q", ["listy"], 3),
        GapConsultQuery("q", ("ok",), "3"),
    ):
        assert host.consult(_call(query=bad)) == (), bad
    assert len(greedy.contexts) == calls_before


def test_malformed_items_are_dropped_not_fatal():
    plugin = _Plugin(_suggestions(
        GapSuggestion("no scheme", "example.org/paper"),
        GapSuggestion("bad scheme", "javascript:alert(1)"),
        GapSuggestion("no netloc", "https:///paper"),
        GapSuggestion("control char", "https://example.org/a\nb"),
        GapSuggestion("too long", "https://example.org/" + "x" * (
            GAP_SUGGESTION_URL_MAX_CHARS
        )),
        GapSuggestion("", "https://example.org/blank-title"),
        GapSuggestion("   ", "https://example.org/whitespace-title"),
        GapSuggestion("bad types", 7),  # type: ignore[arg-type]
        "not a suggestion at all",  # type: ignore[arg-type]
        GapSuggestion("good", "https://example.org/good", "why", "arXiv"),
        GapSuggestion("dup", "https://example.org/good"),
    ))
    result = _host(_bundle("corp.gap", plugin)).consult(_call())

    assert result == (
        GapSuggestion("good", "https://example.org/good", "why", "arXiv"),
    )


def test_fields_are_truncated():
    plugin = _Plugin(_suggestions(GapSuggestion(
        "  " + "T" * (GAP_SUGGESTION_TITLE_MAX_CHARS + 40) + "  ",
        "  https://example.org/paper  ",
        "S" * (GAP_SUGGESTION_SUMMARY_MAX_CHARS + 40),
        "L" * (GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS + 40),
    )))
    (item,) = _host(_bundle("corp.gap", plugin)).consult(_call())

    assert item.title == "T" * GAP_SUGGESTION_TITLE_MAX_CHARS
    assert item.summary == "S" * GAP_SUGGESTION_SUMMARY_MAX_CHARS
    assert item.source_label == "L" * GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS
    # The URL is stripped but never shortened — a truncated URL is a different
    # destination, so an over-long one is dropped instead (case above).
    assert item.url == "https://example.org/paper"


def test_plugin_exception_is_fail_open():
    class Raising:
        def consult(self, _context):
            raise RuntimeError("private endpoint https://internal/secret")

    events: list[dict[str, object]] = []
    later = _Plugin(_suggestions(
        GapSuggestion("later", "https://example.org/later")
    ))
    host = _host(
        _bundle("corp.a_raising", Raising()),
        _bundle("corp.b_later", later),
        event_sink=events.append,
    )

    assert host.consult(_call()) == (
        GapSuggestion("later", "https://example.org/later", "", ""),
    )
    assert events[0]["reason_code"] == "gap_consult_failed"
    assert events[0]["status"] == "unavailable"
    assert "secret" not in repr(events)


def test_hung_plugin_is_abandoned_within_the_deadline():
    release = threading.Event()

    class Hung:
        def consult(self, _context):
            release.wait(30.0)
            return _suggestions(GapSuggestion("late", "https://example.org/l"))

    events: list[dict[str, object]] = []
    host = _host(_bundle("corp.hung", Hung()), event_sink=events.append)

    started = time.monotonic()
    try:
        result = host.consult(_call(deadline=time.monotonic() + 0.2))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert result == ()
    assert elapsed < 1.0, elapsed
    assert events[-1]["reason_code"] == "gap_consult_timeout"


def test_late_result_from_an_abandoned_plugin_is_inert():
    release = threading.Event()
    finished = threading.Event()

    class Late:
        def consult(self, _context):
            release.wait(30.0)
            finished.set()
            return _suggestions(
                GapSuggestion("late", "https://example.org/late")
            )

    events: list[dict[str, object]] = []
    host = _host(_bundle("corp.late", Late()), event_sink=events.append)
    abandoned = host.consult(_call(deadline=time.monotonic() + 0.2))
    assert abandoned == ()
    events_at_abandon = list(events)

    release.set()
    assert finished.wait(10.0)
    time.sleep(0.05)

    # The worker finished and wrote its answer into a cell nobody reads any
    # more: the returned tuple is still empty and no second receipt appeared.
    # (A later `consult` legitimately starts its own worker — the inertness
    # claimed here is about THIS call's result and telemetry, not the host.)
    assert abandoned == ()
    assert events == events_at_abandon


def test_cancellation_propagates():
    cancellation = _Cancellation(cancelled=True)
    plugin = _Plugin(_suggestions(
        GapSuggestion("never", "https://example.org/never")
    ))
    host = _host(_bundle("corp.gap", plugin))

    try:
        host.consult(_call(cancellation=cancellation))
    except AskCancelled:
        pass
    else:  # pragma: no cover - the assertion below reports the failure
        raise AssertionError("cancellation must propagate, never fail open")

    # And cancellation raised mid-flight is not swallowed by the join loop.
    release = threading.Event()
    mid = _Cancellation()

    class Slow:
        def consult(self, _context):
            mid.set()
            release.wait(30.0)
            return _suggestions()

    slow_host = _host(_bundle("corp.slow", Slow()))
    try:
        slow_host.consult(_call(cancellation=mid, deadline=time.monotonic() + 30))
        raise AssertionError("mid-flight cancellation must propagate")
    except AskCancelled:
        pass
    finally:
        release.set()


def test_connection_lease_blocks_the_call():
    plugin = _Plugin(_suggestions(
        GapSuggestion("blocked", "https://example.org/blocked")
    ))
    host = _host(_bundle("corp.gap", plugin))

    assert host.consult(_call(probe=_ConnectionProbe(held=True))) == ()
    assert plugin.contexts == []

    class Unreadable:
        def is_connection_held(self):
            raise RuntimeError("probe exploded")

    class NotABool:
        def is_connection_held(self):
            return "no"

    for probe in (Unreadable(), NotABool(), object()):
        assert host.consult(_call(probe=probe)) == ()
    assert plugin.contexts == []

    # A live decision that takes a lease on its way must also block execution.
    leasing = _ConnectionProbe()

    def decision(_context):
        leasing.held = True
        return Availability.available()

    events: list[dict[str, object]] = []
    gated = _Plugin(_suggestions(
        GapSuggestion("gated", "https://example.org/gated")
    ))
    leasing_host = _host(
        _bundle("corp.leasing", gated, requires=("corp.gap.available",)),
        event_sink=events.append,
        capability_decisions={"corp.gap.available": decision},
    )
    assert leasing_host.consult(_call(probe=leasing)) == ()
    assert gated.contexts == []
    assert events[-1]["reason_code"] == "connection_lease_held"


def test_unavailable_contribution_is_skipped_but_others_run():
    skipped = _Plugin(_suggestions(
        GapSuggestion("skipped", "https://example.org/skipped")
    ))
    running = _Plugin(_suggestions(
        GapSuggestion("running", "https://example.org/running")
    ))
    events: list[dict[str, object]] = []
    host = _host(
        _bundle("corp.a_skipped", skipped, requires=("corp.gap.offline",)),
        _bundle("corp.b_running", running),
        event_sink=events.append,
        capability_decisions={
            "corp.gap.offline": lambda _context: Availability(
                AvailabilityStatus.UNAVAILABLE, "corp_gap_offline"
            )
        },
    )

    assert host.consult(_call()) == (
        GapSuggestion("running", "https://example.org/running", "", ""),
    )
    assert skipped.contexts == []
    assert events[0]["reason_code"] == "corp_gap_offline"
    assert events[0]["status"] == "unavailable"

    # The availability context names the contribution being decided.
    seen: list[object] = []

    def recording(context):
        seen.append(context)
        return Availability.available()

    _host(
        _bundle("corp.recorded", _Plugin(_suggestions()),
                requires=("corp.gap.recorded",)),
        capability_decisions={"corp.gap.recorded": recording},
    ).consult(_call())
    assert type(seen[0]) is GapConsultAvailabilityContext
    assert seen[0].contribution_id == "corp.recorded"


def test_invalid_result_shape_is_rejected():
    events: list[dict[str, object]] = []
    good = _Plugin(_suggestions(
        GapSuggestion("good", "https://example.org/good")
    ))
    host = _host(
        _bundle("corp.a_bad", _Plugin(["not a contributor result"])),
        _bundle("corp.b_good", good),
        event_sink=events.append,
    )

    assert host.consult(_call()) == (
        GapSuggestion("good", "https://example.org/good", "", ""),
    )
    assert events[0]["reason_code"] == "invalid_gap_consult_result"
    assert events[0]["status"] == "invalid"

    for broken in (
        ContributorResult(["listy"], ExtensionResultStatus.AVAILABLE),
        ContributorResult((), "available"),
        ContributorResult((), ExtensionResultStatus.AVAILABLE, "bad failure"),
        None,
    ):
        assert _host(
            _bundle("corp.broken", _Plugin(broken))
        ).consult(_call()) == (), broken


def test_events_are_content_free():
    events: list[dict[str, object]] = []
    plugin = _Plugin(ContributorResult(
        (GapSuggestion(
            "Lattice shaping bounds",
            "https://example.org/secret-path",
            "summary body",
            "arXiv",
        ),),
        ExtensionResultStatus.PARTIAL,
        ExtensionFailure(ExtensionFailureKind.TIMEOUT, "corp_upstream_slow"),
    ))
    host = _host(_bundle("corp.gap", plugin), event_sink=events.append)

    assert len(host.consult(_call())) == 1
    (event,) = events
    assert set(event) == {
        "kind", "point", "plugin_id", "contribution_id",
        "status", "duration_ms", "count", "reason_code",
    }
    assert event["kind"] == "ask_extension"
    assert event["point"] == ASK_GAP_CONSULT_POINT
    assert event["status"] == "partial"
    assert event["reason_code"] == "corp_upstream_slow"
    assert event["count"] == 1
    rendered = repr(events)
    for secret in (
        "Lattice shaping bounds", "secret-path", "summary body", "arXiv",
        "shaping loss bounds", "lattice codes bound shaping loss",
    ):
        assert secret not in rendered, secret


def test_no_contextvars_leak_into_the_plugin_thread():
    scope = contextvars.ContextVar("frozen_retrieval_scope", default="unset")
    scope.set("notebook-42-frozen-scope")
    observed: list[str] = []

    class Peeking:
        def consult(self, _context):
            observed.append(scope.get())
            return _suggestions()

    _host(_bundle("corp.peek", Peeking())).consult(_call())

    # A fresh empty Context is what keeps a plugin from inheriting this
    # request's scope/run/fan-out slot. copy_context() would break this.
    assert observed == ["unset"]
    assert scope.get() == "notebook-42-frozen-scope"


def test_bad_deadline_and_wrong_context_type_return_nothing():
    plugin = _Plugin(_suggestions(
        GapSuggestion("never", "https://example.org/never")
    ))
    host = _host(_bundle("corp.gap", plugin))

    for deadline in (0.0, -1.0, float("nan"), float("inf")):
        assert host.consult(_call(deadline=deadline)) == (), deadline
    assert host.consult(replace(_call(), deadline_monotonic=1)) == ()

    class LookAlike:
        query = _query()
        cancellation = None
        connection_probe = _ConnectionProbe()
        deadline_monotonic = time.monotonic() + 60.0

    assert host.consult(LookAlike()) == ()
    assert plugin.contexts == []
