"""``ask.reflect_action``'s execution host (design document §五).

Everything here is about what the CORE guarantees regardless of how a plugin
behaves: which actions a run may be offered (admin switch included), the hard
wall-clock deadline over probe *and* invoke, cancellation, and the admission
rails on whatever came back.  The reflect loop's own six skip criteria, the
per-turn gate and the candidate-summary block are in
``test_reasoning_plugin_action.py``; descriptor validation at freeze is in
``test_reflect_action_contract.py``.

Shape mirrors ``test_gap_consult_host.py`` deliberately — the two hosts share
``extensions/host_admission.py``, so their tests should be readable side by
side when one of those shared rails changes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from types import MappingProxyType

import pytest

from app.domain.reflect_action import (
    EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL,
    EXTERNAL_EVIDENCE_TITLE_MAX_CHARS,
    REFLECT_ACTION_NOTE_MAX_CHARS,
    ReflectActionCall,
    ReflectActionDescriptor,
    ReflectActionItem,
    ReflectActionParameter,
)
from app.extension_sdk import (
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
)
from app.extension_sdk.reflect_action import (
    ASK_REFLECT_ACTION_POINT,
    ReflectActionCallContext,
    ReflectActionResult,
)
from app.extensions.bootstrap import build_extension_runtime
from app.extensions.host_admission import ADMISSION_SCAN_FACTOR
from app.extensions.reflect_action import (
    CANCELLED,
    FAILED,
    INVALID_RESULT,
    TIMEOUT,
    UNAVAILABLE,
)


DESCRIPTOR = ReflectActionDescriptor(
    name="search_papers",
    description="Search an external paper index.",
    source_label="IEEE Xplore",
    parameters=(
        ReflectActionParameter(
            name="query", description="what to look for", kind="text",
            required=True,
        ),
    ),
    max_calls_per_run=2,
)


class _Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def set(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled


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
        contribution_id, ASK_REFLECT_ACTION_POINT, ContributionKind.CONTRIBUTOR
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


class _SwitchableProbe:
    """An availability probe whose answer can be changed mid-test.

    ``ExtensionContribution`` is frozen, so a test that needs the run-level
    decision and the per-call one to differ has to vary the probe itself.
    """

    def __init__(self) -> None:
        self.answer = lambda _context: Availability.available()

    def __call__(self, context):
        return self.answer(context)


class _Plugin:
    """Records the context it was handed and answers a canned result."""

    descriptor = DESCRIPTOR

    def __init__(self, result: object) -> None:
        self.result = result
        self.contexts: list[object] = []

    def invoke(self, context):
        self.contexts.append(context)
        return self.result


def _result(*items: ReflectActionItem, note: str = "") -> ReflectActionResult:
    return ReflectActionResult(items, note, ExtensionResultStatus.AVAILABLE)


def _item(
    *, title="Lattice shaping bounds", excerpt="the excerpt",
    url="https://example.org/a", location_label="§3.2",
) -> ReflectActionItem:
    return ReflectActionItem(title, excerpt, url, location_label)


def _call(
    *,
    question: str = "how do lattice codes bound shaping loss?",
    arguments: dict | None = None,
    cancellation: object | None = None,
    deadline: float | None = None,
    max_items: int = EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL,
) -> ReflectActionCall:
    return ReflectActionCall(
        question,
        {"query": "shaping loss"} if arguments is None else arguments,
        cancellation if cancellation is not None else _Cancellation(),
        deadline if deadline is not None else time.monotonic() + 60.0,
        max_items,
    )


def _host(*bundles, event_sink=None, disabled_ids_provider=None,
          capability_decisions=None):
    return build_extension_runtime(
        bundles,
        event_sink=event_sink,
        capability_decisions=capability_decisions,
        disabled_ids_provider=disabled_ids_provider,
    ).reflect_actions


def _spec(host):
    (spec,) = host.specs(time.monotonic() + 60.0)
    return spec


def test_empty_topology_is_a_strict_no_op():
    def poison(*_args, **_kwargs):
        raise AssertionError("strict empty topology touched a collaborator")

    events: list[dict] = []
    host = _host(event_sink=events.append)
    host._clock = poison

    assert host.specs(time.monotonic() + 60.0) == ()
    assert host.has_contributions() is False
    assert events == []


def test_a_registered_available_action_is_offered_and_carries_its_origin():
    host = _host(_bundle("papers", _Plugin(_result())))

    spec = _spec(host)

    assert spec.descriptor is DESCRIPTOR
    assert spec.plugin_id == "papers"
    assert spec.contribution_id == "papers"
    assert host.has_contributions() is True


def test_an_admin_disabled_plugin_offers_nothing():
    """The admin switch is IN the availability call, not beside it.

    ``specs()`` deliberately goes through ``registry.availability`` rather than
    the contribution's own probe: a deployment that turned this plugin off in
    the admin page must stop the action reaching the model's option list, and
    that switch is only visible at that entry point (#635).
    """
    disabled: set[str] = set()
    host = _host(
        _bundle("papers", _Plugin(_result())),
        disabled_ids_provider=lambda: set(disabled),
    )
    assert host.specs(time.monotonic() + 60.0) != ()

    disabled.add("papers")

    assert host.specs(time.monotonic() + 60.0) == ()
    # The topology itself is unchanged — only the live decision moved.
    assert host.has_contributions() is True


def test_a_raising_probe_is_treated_as_unavailable_and_recorded():
    def explode(_context):
        raise RuntimeError("probe fault")

    events: list[dict] = []
    host = _host(
        _bundle("papers", _Plugin(_result()), availability=explode),
        event_sink=events.append,
    )

    assert host.specs(time.monotonic() + 60.0) == ()
    assert [event["status"] for event in events] == ["unavailable"]
    assert events[0]["action"] == "search_papers"
    assert events[0]["plugin_id"] == "papers"


def test_a_hung_probe_cannot_stall_the_run_level_offer():
    """``specs()`` is bounded too, not just ``invoke()``.

    The probe is only *contractually* I/O-free.  A plugin that blocks in it —
    on a lock, on a lazily-opened client, on anything — would otherwise hold
    the request thread for as long as it liked, on a call made before the
    run's own retrieval even starts.  Real wall clock here, deliberately: the
    assertion is that the caller came back, not that a fake clock said so.
    """
    release = threading.Event()

    def _hang(_context):
        release.wait(30)
        return Availability.available()

    events: list[dict] = []
    host = _host(
        _bundle("papers", _Plugin(_result()), availability=_hang),
        event_sink=events.append,
    )
    started = time.monotonic()
    try:
        offered = host.specs(time.monotonic() + 0.2)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert offered == ()
    assert elapsed < 1.5, elapsed
    assert events[-1]["code"] == TIMEOUT


def test_a_cancelled_probe_answers_a_code_like_every_other_path():
    cancellation = _Cancellation(cancelled=True)
    release = threading.Event()

    def _hang(_context):
        release.wait(30)
        return Availability.available()

    events: list[dict] = []
    host = _host(
        _bundle("papers", _Plugin(_result()), availability=_hang),
        event_sink=events.append,
    )
    try:
        offered = host.specs(
            time.monotonic() + 30.0, cancellation=cancellation)
    finally:
        release.set()

    assert offered == ()
    assert events[-1]["code"] == CANCELLED


def test_a_declined_probe_keeps_the_action_out_of_the_offer():
    host = _host(_bundle(
        "papers",
        _Plugin(_result()),
        availability=lambda _c: Availability(
            AvailabilityStatus.UNAVAILABLE, "index_offline"
        ),
    ))

    assert host.specs(time.monotonic() + 60.0) == ()


def test_a_successful_call_is_admitted_and_the_plugin_sees_only_its_inputs():
    plugin = _Plugin(_result(_item(), note="only reviews found"))
    events: list[dict] = []
    host = _host(_bundle("papers", plugin), event_sink=events.append)
    spec = _spec(host)

    outcome = host.invoke(spec, _call())

    assert outcome.failure_code == ""
    assert [item.title for item in outcome.items] == ["Lattice shaping bounds"]
    assert outcome.note == "only reviews found"
    (context,) = plugin.contexts
    assert type(context) is ReflectActionCallContext
    assert context.question == "how do lattice codes bound shaping loss?"
    assert dict(context.arguments) == {"query": "shaping loss"}
    # The SDK cancellation face, not the raw event: a compliant plugin may call
    # ``raise_if_cancelled()`` without an AttributeError being read as a fault.
    assert callable(context.cancellation.raise_if_cancelled)
    # No core port of any kind reached the plugin.
    assert not [
        name for name in dir(context)
        if name in {"retrieval", "notebook_id", "actor", "repository"}
    ]
    assert events[-1]["count"] == 1
    assert events[-1]["action"] == "search_papers"
    # Content-free receipt: no question, no argument value, no URL.
    assert "shaping loss" not in repr(events[-1])
    assert "example.org" not in repr(events[-1])


def test_a_hung_plugin_costs_the_deadline_and_nothing_more():
    """The MAIN thread returns on its own slice while the worker never does."""
    clock = _TrippableClock()
    entered = threading.Event()
    release = threading.Event()

    class _Hang:
        descriptor = DESCRIPTOR

        def invoke(self, _context):
            entered.set()
            clock.tripped = True
            release.wait(30)
            return _result(_item())

    host = _host(_bundle("papers", _Hang()))
    spec = _spec(host)
    host._clock = clock
    try:
        outcome = host.invoke(spec, _call(deadline=clock.deadline))
    finally:
        release.set()

    assert entered.is_set()
    assert outcome.failure_code == TIMEOUT
    assert outcome.items == ()


def test_a_hung_probe_is_bounded_by_the_same_deadline():
    """The availability decision runs INSIDE the budget, not beside it.

    Deciding on the calling thread would make "hard deadline" a promise about
    only half the call — the same measurement that moved gap consultation's
    probe onto its worker.
    """
    clock = _TrippableClock()
    release = threading.Event()
    slow = _SwitchableProbe()

    def _slow(_context):
        clock.tripped = True
        release.wait(30)
        return Availability.available()

    host = _host(_bundle("papers", _Plugin(_result(_item())), availability=slow))
    # ``specs()`` runs first with the fast probe: this test is about the
    # per-call decision on the worker, not the run-level one.
    spec = _spec(host)
    slow.answer = _slow
    host._clock = clock
    try:
        outcome = host.invoke(spec, _call(deadline=clock.deadline))
    finally:
        release.set()

    assert outcome.failure_code == TIMEOUT


def test_cancellation_comes_back_as_a_code_not_an_exception():
    """One shape out of this host, cancellation included.

    The loop re-reads its OWN token immediately after the call, so the run
    still ends at the first opportunity — but on core's reading of core's
    token, never on a host's report about it.
    """
    cancellation = _Cancellation()

    class _CancelDuring:
        descriptor = DESCRIPTOR

        def invoke(self, _context):
            cancellation.set()
            return _result(_item())

    host = _host(_bundle("papers", _CancelDuring()))
    spec = _spec(host)

    outcome = host.invoke(spec, _call(cancellation=cancellation))

    assert outcome.failure_code == CANCELLED
    assert outcome.items == ()


def test_a_raising_plugin_fails_open_with_a_stable_code():
    class _Boom:
        descriptor = DESCRIPTOR

        def invoke(self, _context):
            raise RuntimeError("upstream 500")

    host = _host(_bundle("papers", _Boom()))

    outcome = host.invoke(_spec(host), _call())

    assert outcome.failure_code == FAILED
    assert outcome.items == ()
    assert outcome.note == ""


@pytest.mark.parametrize("payload", [
    None,
    "not a result",
    (ReflectActionItem("t", "e", "https://example.org/a"),),
])
def test_a_result_that_is_not_the_declared_type_is_refused(payload):
    host = _host(_bundle("papers", _Plugin(payload)))

    outcome = host.invoke(_spec(host), _call())

    assert outcome.failure_code == INVALID_RESULT


def test_an_unavailable_result_contributes_nothing_it_disclaimed():
    host = _host(_bundle("papers", _Plugin(ReflectActionResult(
        (_item(),),
        "",
        ExtensionResultStatus.UNAVAILABLE,
        ExtensionFailure(ExtensionFailureKind.UNAVAILABLE, "index_offline"),
    ))))

    outcome = host.invoke(_spec(host), _call())

    assert outcome.items == ()
    assert outcome.failure_code == "index_offline"


def test_admission_drops_malformed_items_and_clamps_long_strings():
    class _Fake:
        """Duck-typed lookalike: everything an item has, none of its type."""

        title = "impostor"
        excerpt = "e"
        url = "https://example.org/impostor"
        location_label = ""

    host = _host(_bundle("papers", _Plugin(_result(
        _Fake(),                                   # not the exact type
        _item(url="javascript:alert(1)"),          # scheme rail
        _item(url="ftp://example.org/x"),          # scheme rail
        _item(url="not a url at all"),
        _item(title="", url="https://example.org/no-title"),
        _item(excerpt="", url="https://example.org/no-excerpt"),
        _item(title="T" * (EXTERNAL_EVIDENCE_TITLE_MAX_CHARS + 500),
              url="https://example.org/long"),
        _item(url="https://example.org/keep"),
    ))))

    outcome = host.invoke(_spec(host), _call())

    urls = [item.url for item in outcome.items]
    assert urls == ["https://example.org/long", "https://example.org/keep"]
    assert len(outcome.items[0].title) == EXTERNAL_EVIDENCE_TITLE_MAX_CHARS


def test_a_note_is_clamped_rather_than_refused():
    host = _host(_bundle("papers", _Plugin(
        _result(_item(), note="n" * (REFLECT_ACTION_NOTE_MAX_CHARS + 200))
    )))

    outcome = host.invoke(_spec(host), _call())

    assert len(outcome.note) == REFLECT_ACTION_NOTE_MAX_CHARS


def test_one_call_de_duplicates_by_url_and_reports_truncation():
    host = _host(_bundle("papers", _Plugin(_result(
        _item(url="https://example.org/same", title="first"),
        _item(url="https://example.org/same", title="second"),
        _item(url="https://example.org/other", title="third"),
    ))))

    outcome = host.invoke(_spec(host), _call(max_items=1))

    assert [item.title for item in outcome.items] == ["first"]
    assert outcome.truncated is True


def test_the_admission_scan_is_bounded_by_the_scan_factor():
    """An unbounded payload costs a BOUNDED scan on the critical path.

    Every item below is a reject, so admission can never fill its one slot and
    would otherwise walk the whole tuple — after the deadline that was supposed
    to bound this plugin has already been honoured.
    """
    rejects = tuple(
        _item(url=f"javascript:{index}")
        for index in range(ADMISSION_SCAN_FACTOR * 50)
    )
    keeper = _item(url="https://example.org/keep")
    host = _host(_bundle("papers", _Plugin(_result(*rejects, keeper))))

    outcome = host.invoke(_spec(host), _call(max_items=1))

    # The keeper sits far past the scan budget, so it is never reached — and
    # the flag says so rather than pretending the plugin found nothing.
    assert outcome.items == ()
    assert outcome.truncated is True


def test_max_items_never_exceeds_the_per_call_domain_rail():
    items = tuple(
        _item(url=f"https://example.org/{index}")
        for index in range(EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL + 5)
    )
    host = _host(_bundle("papers", _Plugin(_result(*items))))

    outcome = host.invoke(_spec(host), _call(max_items=999))

    assert len(outcome.items) == EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL


@pytest.mark.parametrize("call", [
    object(),
    _call(question=""),
    _call(arguments={}),                        # a declared parameter missing
    _call(arguments={"query": "q", "extra": "x"}),
    _call(arguments={"query": 7}),
    _call(max_items=0),
])
def test_the_host_does_not_trust_its_caller_either(call):
    """A hand-built context must not be able to widen the egress surface."""
    plugin = _Plugin(_result(_item()))
    host = _host(_bundle("papers", plugin))

    outcome = host.invoke(_spec(host), call)

    assert outcome.failure_code == INVALID_RESULT
    assert plugin.contexts == []


def test_the_production_read_only_mapping_is_accepted_at_the_egress_check():
    """The reflect loop hands a ``MappingProxyType``, not a ``dict``.

    Every other test in this file builds the call with a plain ``dict``, which
    is exactly how an egress re-check that admitted only ``dict`` shipped: in
    production it refused EVERY plugin action before the contributor was
    reached (codex PR#714 R1 P1).  ``test_reasoning_plugin_action.py`` pins the
    loop-to-plugin path end to end; this pins the accepted type at the frame
    that decides it.
    """
    plugin = _Plugin(_result(_item()))
    host = _host(_bundle("papers", plugin))

    outcome = host.invoke(
        _spec(host),
        _call(arguments=MappingProxyType({"query": "shaping loss"})),
    )

    assert outcome.failure_code == ""
    assert len(plugin.contexts) == 1
    assert dict(plugin.contexts[0].arguments) == {"query": "shaping loss"}


def test_a_dict_subclass_is_still_refused_at_the_egress_check():
    """Widening to a read-only mapping must not widen to ``isinstance``.

    A ``dict`` subclass can answer the validation walk with one thing and the
    copy taken for the plugin with another, so the frame that decides what
    leaves the deployment keeps refusing it outright.
    """

    class _TwoFaced(dict):
        def __init__(self) -> None:
            super().__init__({"query": "shaping loss"})
            self._reads = 0

        def values(self):  # pragma: no cover - refused before it is walked
            self._reads += 1
            return super().values()

    plugin = _Plugin(_result(_item()))
    host = _host(_bundle("papers", plugin))

    outcome = host.invoke(_spec(host), _call(arguments=_TwoFaced()))

    assert outcome.failure_code == INVALID_RESULT
    assert plugin.contexts == []


def test_the_plugin_cannot_mutate_the_arguments_the_core_recorded():
    """What left the deployment is what the trace says left it.

    The loop records ``arguments`` verbatim as the egress-transparency landing
    point (§九 invariant 1).  A plugin handed the loop's own dict could edit
    that record from underneath it, so it gets a read-only view over a private
    copy — and the edit raises inside the plugin rather than silently
    succeeding.
    """
    seen: list = []

    class _Mutator:
        descriptor = DESCRIPTOR

        def invoke(self, context):
            seen.append(dict(context.arguments))
            try:
                context.arguments["query"] = "rewritten"
            except TypeError:
                seen.append("refused")
            return _result(_item())

    host = _host(_bundle("papers", _Mutator()))
    caller_arguments = {"query": "shaping loss"}

    outcome = host.invoke(
        _spec(host), _call(arguments=caller_arguments))

    assert outcome.items
    assert seen == [{"query": "shaping loss"}, "refused"]
    assert caller_arguments == {"query": "shaping loss"}


def test_the_descriptor_checked_against_is_the_frozen_one():
    """A caller cannot widen the egress surface by passing its own spec.

    ``_valid_arguments`` re-reads the descriptor out of the frozen topology by
    contribution id.  A spec object whose descriptor declares an extra
    parameter is therefore not a way to send that parameter outward.
    """
    from app.domain.reflect_action import ReflectActionSpec

    plugin = _Plugin(_result(_item()))
    host = _host(_bundle("papers", plugin))
    widened = replace(
        DESCRIPTOR,
        parameters=DESCRIPTOR.parameters + (
            ReflectActionParameter(
                name="secret", description="d", kind="text",
            ),
        ),
    )

    outcome = host.invoke(
        ReflectActionSpec("papers", "papers", widened),
        _call(arguments={"query": "q", "secret": "leak"}),
    )

    assert outcome.failure_code == INVALID_RESULT
    assert plugin.contexts == []


def test_a_refusal_before_the_call_still_leaves_a_receipt():
    """An out-of-contract call is exactly what an operator needs to see."""
    events: list[dict] = []
    host = _host(_bundle("papers", _Plugin(_result())), event_sink=events.append)

    host.invoke(_spec(host), _call(arguments={}))

    assert events[-1]["status"] == "invalid"
    assert events[-1]["code"] == INVALID_RESULT


def test_an_unknown_spec_reaches_no_contributor():
    from app.domain.reflect_action import ReflectActionSpec

    plugin = _Plugin(_result(_item()))
    host = _host(_bundle("papers", plugin))

    outcome = host.invoke(
        ReflectActionSpec("ghost", "ghost", DESCRIPTOR), _call()
    )

    assert outcome.failure_code == INVALID_RESULT
    assert plugin.contexts == []


def test_a_per_call_probe_decline_is_reported_as_unavailable():
    probe = _SwitchableProbe()
    host = _host(_bundle("papers", _Plugin(_result(_item())), availability=probe))
    spec = _spec(host)
    probe.answer = lambda _c: Availability(
        AvailabilityStatus.DISABLED, "admin_disabled")

    outcome = host.invoke(spec, _call())

    assert outcome.failure_code == UNAVAILABLE
