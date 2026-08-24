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


class _TrippableClock:
    """Fake monotonic clock that jumps past ``deadline`` once tripped."""

    deadline = 1000.5

    def __init__(self) -> None:
        self.tripped = False

    def __call__(self) -> float:
        return 1002.0 if self.tripped else 1000.0


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


def test_admission_examines_only_a_bounded_prefix():
    """A contributor's payload is unbounded input; admitting it is not.

    Without a cap the loop walks every item a plugin cares to return — a
    million of them, on the request's critical path, *after* the deadline that
    was supposed to bound this contributor has already been honoured.  Pinned
    by outcome rather than wall clock: a good item parked past the prefix is
    provably never reached, and one just inside it provably is (so the cap can
    never be quietly tightened to zero either).
    """
    reject = GapSuggestion("no scheme", "example.org/nope")
    good = GapSuggestion("good", "https://example.org/good")
    one_slot = _call(query=_query(max_suggestions=1))

    buried = ContributorResult(
        (reject,) * 1_000_000 + (good,), ExtensionResultStatus.AVAILABLE
    )
    assert _host(_bundle("corp.gap", _Plugin(buried))).consult(one_slot) == ()

    reachable = ContributorResult(
        (reject,) * 8 + (good,), ExtensionResultStatus.AVAILABLE
    )
    assert _host(_bundle("corp.gap", _Plugin(reachable))).consult(one_slot) == (
        good,
    )


def test_oversized_strings_are_cut_before_they_are_walked():
    """`str.strip()` allocates a copy of whatever it is handed.

    Stripping a 20 MB plugin string first and truncating afterwards is an
    unbounded allocation driven by plugin output, so the cut has to come first.
    The observable consequence — and the only way to pin the ORDER, since a
    well-formed value reads identically either way — is the registered edge
    below: leading whitespace wider than the headroom now reads as empty.
    """
    huge = "x" * 20_000_000
    plugin = _Plugin(_suggestions(
        GapSuggestion(huge, "https://example.org/paper", huge, huge),
        # An over-long URL is still rejected outright, never shortened into a
        # different destination — the cut does not weaken that.
        GapSuggestion("long url", "https://example.org/" + huge),
    ))
    (item,) = _host(_bundle("corp.gap", plugin)).consult(_call())
    assert item.title == "x" * GAP_SUGGESTION_TITLE_MAX_CHARS
    assert item.summary == "x" * GAP_SUGGESTION_SUMMARY_MAX_CHARS
    assert item.source_label == "x" * GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS
    assert item.url == "https://example.org/paper"

    spaced = _Plugin(_suggestions(GapSuggestion(
        " " * (GAP_SUGGESTION_TITLE_MAX_CHARS + 4096) + "real title",
        "https://example.org/spaced",
    )))
    # A strip-then-truncate implementation would admit this; the accepted cost
    # of cutting first is that it drops instead, which is the safe direction.
    assert _host(_bundle("corp.gap", spaced)).consult(_call()) == ()


def test_admission_re_reads_cancellation():
    """Cancellation is set from another thread, so it can flip after the join
    loop's own read and before the last item is examined.

    Exercised against the helper directly on purpose: reaching it through
    ``consult`` would need cancellation to land inside a window of a few
    instructions, and the join loop's post-finish read (tested above) covers
    every deterministic ordering before it.
    """
    from app.extensions.gap_consult import _sanitized

    good = GapSuggestion("good", "https://example.org/good")
    seen: set[str] = set()
    assert _sanitized(
        (good,), limit=1, seen_urls=seen, cancellation=_Cancellation()
    ) == (good,)

    try:
        _sanitized(
            (good,), limit=1, seen_urls=set(),
            cancellation=_Cancellation(cancelled=True),
        )
        raise AssertionError("admission must not swallow cancellation")
    except AskCancelled:
        pass


def test_an_unavailable_result_contributes_nothing():
    """A contributor that says it could not serve this call does not get to
    contradict itself with a payload.

    ``status`` is the plugin's own statement; admitting items it disclaimed
    would put material in front of a reader that the plugin itself says is not
    an answer.  PARTIAL is a different statement — "some of it", not "none of
    it" — and is still admitted.
    """
    events: list[dict[str, object]] = []
    stale = "https://example.org/stale"
    disclaimed = _Plugin(ContributorResult(
        (GapSuggestion("stale", stale),),
        ExtensionResultStatus.UNAVAILABLE,
        ExtensionFailure(ExtensionFailureKind.UNAVAILABLE, "corp_upstream_down"),
    ))
    later = _Plugin(_suggestions(
        GapSuggestion("later", "https://example.org/later"),
        # The disclaimed URL never entered `seen_urls`, so a contributor that
        # really can serve this call is not blocked by a withdrawn link.
        GapSuggestion("same link", stale),
    ))
    host = _host(
        _bundle("corp.a_disclaimed", disclaimed),
        _bundle("corp.b_later", later),
        event_sink=events.append,
    )

    assert host.consult(_call()) == (
        GapSuggestion("later", "https://example.org/later", "", ""),
        GapSuggestion("same link", stale, "", ""),
    )
    # The receipt keeps the existing unavailable shape; only the count moves.
    assert events[0]["status"] == "unavailable"
    assert events[0]["reason_code"] == "corp_upstream_down"
    assert events[0]["count"] == 0

    # No failure attached is still unavailable, just without a stable code.
    bare = _Plugin(ContributorResult(
        (GapSuggestion("nope", "https://example.org/nope"),),
        ExtensionResultStatus.UNAVAILABLE,
    ))
    bare_events: list[dict[str, object]] = []
    assert _host(
        _bundle("corp.bare", bare), event_sink=bare_events.append
    ).consult(_call()) == ()
    assert bare_events[-1]["status"] == "unavailable"
    assert bare_events[-1]["count"] == 0
    assert "reason_code" not in bare_events[-1]

    partial = _Plugin(ContributorResult(
        (GapSuggestion("partial", "https://example.org/partial"),),
        ExtensionResultStatus.PARTIAL,
    ))
    assert _host(_bundle("corp.partial", partial)).consult(_call()) == (
        GapSuggestion("partial", "https://example.org/partial", "", ""),
    )


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


def test_a_result_that_lands_after_the_deadline_is_abandoned():
    """A worker may finish *after* its budget is gone; that result is late.

    The gap the join loop used to leave: it read the deadline only while the
    thread was still alive, so a plugin that answered past its budget was
    accepted whenever the 50ms slice happened to land on the far side of its
    last write and abandoned when it did not.  Same plugin, same budget, two
    different answers depending on the scheduler.

    Made deterministic without a sleep: the plugin advances the fake clock past
    the deadline *before* returning, so by the time any pass of the loop reads
    the clock the budget is provably spent — whichever side of ``is_alive()``
    that pass is on.
    """
    clock = _TrippableClock()
    events: list[dict[str, object]] = []

    class SpendsThenReturns:
        def consult(self, _context):
            clock.tripped = True
            return _suggestions(
                GapSuggestion("late", "https://example.org/late")
            )

    later = _Plugin(_suggestions(
        GapSuggestion("later", "https://example.org/later")
    ))
    host = _host(
        _bundle("corp.a_late", SpendsThenReturns()),
        _bundle("corp.b_later", later),
        event_sink=events.append,
    )
    host._clock = clock

    # The answer path gets nothing, and the receipt is the same abandonment
    # shape a plugin that never returned would have produced.
    assert host.consult(_call(deadline=clock.deadline)) == ()
    assert len(events) == 1
    assert events[-1]["reason_code"] == "gap_consult_timeout"
    assert events[-1]["status"] == "unavailable"
    assert events[-1]["count"] == 0
    # A spent deadline ends the point: there is no honest way to start another.
    assert later.contexts == []


def test_a_hung_availability_probe_is_inside_the_same_deadline():
    # The probe is plugin-supplied too (manifest `provides`), so leaving it on
    # the calling thread would make the hard deadline a promise about only half
    # the call: measured at 2.01s against a 0.2s budget before the fix.
    release = threading.Event()

    def sleeping_decision(_context):
        release.wait(30.0)
        return Availability.available()

    events: list[dict[str, object]] = []
    plugin = _Plugin(_suggestions(
        GapSuggestion("never", "https://example.org/never")
    ))
    host = _host(
        _bundle("corp.slowprobe", plugin, requires=("corp.gap.slow",)),
        event_sink=events.append,
        capability_decisions={"corp.gap.slow": sleeping_decision},
    )

    started = time.monotonic()
    try:
        result = host.consult(_call(deadline=time.monotonic() + 0.2))
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert result == ()
    assert elapsed < 1.0, elapsed
    assert events[-1]["reason_code"] == "gap_consult_timeout"
    assert plugin.contexts == []


def test_base_exception_from_a_plugin_is_fail_open():
    # `except Exception` would let this escape the worker target, leaving the
    # cell empty and the failure mis-reported as a malformed result.
    class Exiting:
        def consult(self, _context):
            raise SystemExit("plugin called sys.exit")

    events: list[dict[str, object]] = []
    host = _host(_bundle("corp.exit", Exiting()), event_sink=events.append)

    assert host.consult(_call()) == ()
    assert events[-1]["reason_code"] == "gap_consult_failed"
    assert events[-1]["status"] == "unavailable"


def test_budget_exhausted_skips_the_remaining_contributors():
    first = _Plugin(_suggestions(
        GapSuggestion("first", "https://example.org/first")
    ))
    second = _Plugin(_suggestions(
        GapSuggestion("second", "https://example.org/second")
    ))
    clock = _TrippableClock()
    events: list[dict[str, object]] = []

    def sink(event: dict[str, object]) -> None:
        events.append(event)
        # Tripped from the sink rather than from the plugin so the spend lands
        # deterministically AFTER the first receipt: the join loop reads the
        # same clock, and a mid-flight trip would report a timeout instead.
        clock.tripped = True

    host = _host(
        _bundle("corp.a_first", first),
        _bundle("corp.b_second", second),
        event_sink=sink,
    )
    host._clock = clock

    assert host.consult(_call(deadline=clock.deadline)) == (
        GapSuggestion("first", "https://example.org/first", "", ""),
    )
    assert second.contexts == []
    assert events[-1]["reason_code"] == "gap_consult_budget_exhausted"
    assert events[-1]["contribution_id"] == "corp.b_second"


def test_duplicate_urls_are_dropped_across_contributors():
    shared = "https://example.org/same-paper"
    first = _Plugin(_suggestions(GapSuggestion("first", shared)))
    second = _Plugin(_suggestions(
        GapSuggestion("second", shared),
        GapSuggestion("other", "https://example.org/other"),
    ))
    host = _host(
        _bundle("corp.a_first", first),
        _bundle("corp.b_second", second),
    )

    # One URL is one suggestion no matter how many plugins offer it; a
    # per-contributor `seen_urls` would let the second re-seat the first's link.
    assert host.consult(_call()) == (
        GapSuggestion("first", shared, "", ""),
        GapSuggestion("other", "https://example.org/other", "", ""),
    )


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
    # Cancellation is read before the contributor is started, so an already
    # cancelled run sends nothing outward at all.
    assert plugin.contexts == []

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

    # And cancellation that lands while the worker was FINISHING is still
    # cancellation — the result does not get accepted just because it arrived
    # first.  Two independent reads defend this outcome (the join loop's
    # post-finish read and admission's own stride), which is deliberate: this
    # case pins the outcome, and the two mechanisms are pinned separately by
    # `test_a_result_that_lands_after_the_deadline_is_abandoned` and
    # `test_admission_re_reads_cancellation`.
    fast = _Cancellation()

    class CancelsThenReturns:
        def consult(self, _context):
            fast.set()
            return _suggestions(
                GapSuggestion("raced", "https://example.org/raced")
            )

    try:
        _host(_bundle("corp.raced", CancelsThenReturns())).consult(
            _call(cancellation=fast)
        )
        raise AssertionError("cancellation on the finishing pass must propagate")
    except AskCancelled:
        pass


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
