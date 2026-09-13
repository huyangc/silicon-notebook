"""Hard-deadline host for ``ask.reflect_action`` contributors.

The reflect loop chooses *whether* to call a plugin action; this host owns
everything about *how*, and a contributor can influence none of it:

1. **The egress surface.**  A contributor receives the question the caller
   built plus the arguments the model wrote — no notebook, no actor, no
   candidate text, no identifier, and no core port of any kind.  The host adds
   nothing: it translates the core-side
   :class:`~app.domain.reflect_action.ReflectActionCall` field for field into
   the SDK's ``ReflectActionCallContext`` after checking its shape.
2. **The deadline.**  Availability probe *and* ``invoke`` run together on one
   daemon worker joined in slices, so a plugin that never returns — in either
   half — costs this turn its configured budget and nothing more.
3. **Admission.**  Titles, excerpts, URLs and location labels are validated and
   truncated here, after the plugin returns, so a hostile or sloppy plugin
   cannot put an unbounded string or a ``javascript:`` URL in front of a
   reader.  The work is bounded in both dimensions — how many items are
   examined, and how much of each string is walked — so an unbounded payload
   costs a bounded scan on the request's critical path.

Everything fails open into the same shape: a raising, hanging, cancelled,
unavailable or malformed contributor yields a stable failure code, zero items,
and a run otherwise byte-identical to one where the plugin was never
registered.  Cancellation is included in that list on purpose — ``invoke``
answers ``plugin_action_cancelled`` rather than raising, so the reflect loop
learns of cancellation by re-reading its OWN token (it does so immediately
after this call) instead of trusting a host's report about it.  ``specs()``
answers the same way — a cancelled or timed-out probe drops that action from
the offer and leaves a receipt — and its caller re-reads its own token too.

Two things this host does NOT do, both deliberate.  It mints no evidence keys:
``ext:{plugin_id}:{n}`` is run-scoped and only the reflect loop knows the run
(design document §6.1).  And it applies no budget of its own beyond
``max_items``: how many calls a run may make, and at which effort tiers, is a
per-turn fact the loop owns (§3.1) — a host that also counted would be a second
place for that number to drift.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from types import MappingProxyType
from typing import Callable

from app.domain.cancellation import AskCancelled
from app.domain.reflect_action import (
    EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS,
    EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS,
    EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL,
    EXTERNAL_EVIDENCE_TITLE_MAX_CHARS,
    EXTERNAL_EVIDENCE_URL_MAX_CHARS,
    REFLECT_ACTION_NOTE_MAX_CHARS,
    ReflectActionCall,
    ReflectActionItem,
    ReflectActionOutcome,
    ReflectActionSpec,
)
from app.extension_sdk import (
    AvailabilityStatus,
    ContributionKind,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
)
from app.extension_sdk.reflect_action import (
    ASK_REFLECT_ACTION_POINT,
    ReflectActionAvailabilityContext,
    ReflectActionCallContext,
    ReflectActionResult,
)
from app.extensions.host_admission import (
    ADMISSION_CANCEL_STRIDE,
    ADMISSION_SCAN_FACTOR,
    JOIN_SLICE_SECONDS,
    SdkCancellation,
    clean_text,
    clean_url,
    deadline_open,
    elapsed_ms,
    is_cancelled,
    raise_if_cancelled,
    safe_clock,
    stable_code,
    valid_deadline,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


# Stable, lower-case, core-minted.  The reflect loop puts whichever of these
# came back into a skip step's ``detail.code``, and the front end renders the
# step by its ``reason`` — so these strings are a contract, not diagnostics.
FAILED = "plugin_action_failed"
TIMEOUT = "plugin_action_timeout"
CANCELLED = "plugin_action_cancelled"
INVALID_RESULT = "plugin_action_invalid_result"
UNAVAILABLE = "plugin_action_unavailable"


@dataclass
class _WorkerCell:
    """Private mailbox for one invocation attempt.

    ``abandoned`` is not a cancellation signal to the worker — nothing can stop
    a thread that refuses to return.  It records that the main thread has moved
    on, which is why a late write here is inert: no one reads the cell again.
    """

    done: bool = False
    failed: bool = False
    reason: str | None = None
    result: object = None
    abandoned: bool = False


class ReflectActionHost:
    """Offer and run plugin reflect actions under a hard wall-clock deadline."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Reflect action host requires a frozen registry"
            )
        contributors: dict[str, RegisteredContribution] = {}
        for item in registry.contributions(ASK_REFLECT_ACTION_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            if (
                declaration.kind is not ContributionKind.CONTRIBUTOR
                or not callable(getattr(implementation, "invoke", None))
            ):
                raise ExtensionRegistryError(
                    f"Reflect action contributor {declaration.id!r} does not "
                    "implement the contributor contract"
                )
            contributors[declaration.id] = item
        self._registry = registry
        self._contributors = contributors
        # The registry already validated and ordered these at freeze; the host
        # never re-derives the topology, it only filters it per run.
        self._specs = registry.reflect_action_specs()
        self._event_sink = event_sink
        self._clock = clock

    def has_contributions(self) -> bool:
        """Read the startup-frozen topology.  No clock, no I/O, no request state."""

        return bool(self._specs)

    def specs(
        self,
        deadline_monotonic: float,
        *,
        cancellation: object = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[ReflectActionSpec, ...]:
        """Which registered actions this run may be offered, decided live.

        ``registry.availability`` is the single call that answers it, and using
        that entry point rather than the contribution's own probe is the whole
        point: it evaluates the administrator's plugin switch (#635's three
        gate points) *and* the plugin's ``requires`` capabilities *and* the
        contribution probe, in that order.  Asking the probe directly would
        offer the model an action an admin had just turned off.

        Each probe runs on its own daemon worker joined in slices, exactly like
        ``invoke``'s, and for the same measured reason: the probe is only
        *contractually* I/O-free, and a plugin that ignores that contract —
        or simply blocks on a lock — would otherwise stall the request thread
        for as long as it liked, on a call the caller made before its own
        retrieval even started.  "Bounded by a deadline" has to be true of
        every entry point on this host or it is true of none of them.  A probe
        that raises, times out, or is cancelled is treated as unavailable and
        recorded; that is the fail-open direction (the run continues without
        the action).

        Zero registered actions is a strict no-op: no clock read, no probe, no
        thread, no event.  A deployment with no reflect-action plugin pays
        nothing — and neither does a run whose caller decided, before asking,
        that it would not offer the channel anyway (that gate is the reflect
        loop's ``_plugin_action_kwargs``, which short-circuits ahead of this
        call).
        """

        if not self._specs:
            return ()
        if not valid_deadline(deadline_monotonic):
            return ()
        sink = event_sink if event_sink is not None else self._event_sink
        offered: list[ReflectActionSpec] = []
        for spec in self._specs:
            started = safe_clock(self._clock)
            if not deadline_open(started, deadline_monotonic):
                # Out of budget before even asking: stop, do not silently keep
                # probing past the caller's own wall clock.
                break
            reason = self._probe(spec, deadline_monotonic, cancellation)
            if reason is None:
                offered.append(spec)
                continue
            _emit(
                sink,
                spec=spec,
                status="unavailable",
                reason_code=reason,
                duration_ms=elapsed_ms(self._clock, started),
            )
        return tuple(offered)

    def _spec_of(self, contribution_id: object) -> ReflectActionSpec | None:
        """The FROZEN spec for this contribution id, or ``None``.

        Linear scan on purpose: a deployment's reflect actions are a
        single-digit tuple, and a dict built at construction would be a second
        copy of a topology the registry already owns.
        """
        for spec in self._specs:
            if spec.contribution_id == contribution_id:
                return spec
        return None

    def _probe(
        self, spec: ReflectActionSpec, deadline: float, cancellation: object,
    ) -> str | None:
        """``None`` when this action is offerable, else a stable reason code.

        Same worker/slice/deadline shape as :meth:`_execute` — see ``specs``
        for why the availability half gets it too.  Cancellation comes back as
        a code here as well, so the whole host has one failure shape; the
        caller re-reads its own token right after.
        """
        cell = _WorkerCell()

        def _target() -> None:
            try:
                availability = self._registry.availability(
                    spec.contribution_id,
                    ReflectActionAvailabilityContext(
                        spec.contribution_id, deadline
                    ),
                )
                if availability.status is not AvailabilityStatus.AVAILABLE:
                    cell.reason = availability.reason_code or UNAVAILABLE
            except BaseException:  # noqa: BLE001 — a probe fault is fail-open
                cell.failed = True
            finally:
                cell.done = True

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        while True:
            now = safe_clock(self._clock)
            worker.join(
                JOIN_SLICE_SECONDS
                if now is None
                else max(0.0, min(JOIN_SLICE_SECONDS, deadline - now))
            )
            finished = not worker.is_alive()
            if is_cancelled(cancellation):
                cell.abandoned = True
                return CANCELLED
            if not deadline_open(safe_clock(self._clock), deadline):
                cell.abandoned = True
                return TIMEOUT
            if finished:
                break
        if cell.failed or not cell.done:
            return UNAVAILABLE
        return cell.reason

    def invoke(
        self,
        spec: ReflectActionSpec,
        call: ReflectActionCall,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> ReflectActionOutcome:
        """Run one action once, bounded, and admit whatever came back.

        ``call`` is the CORE-side :class:`ReflectActionCall`; the SDK-side
        ``ReflectActionCallContext`` the plugin actually receives is built on
        the worker below.  The two exist separately because the reflect loop
        lives in ``app.services``, which may not import the Extension SDK — and
        because the translation is where the raw cancel event is projected onto
        the SDK's full token face.
        """

        sink = event_sink if event_sink is not None else self._event_sink
        started = safe_clock(self._clock)
        item = self._contributors.get(spec.contribution_id)
        # The host does not trust its caller either: a hand-built context would
        # otherwise be forwarded verbatim to a third party.  The descriptor the
        # arguments are checked against is re-read from the FROZEN topology by
        # contribution id, never taken off the ``spec`` object the caller
        # handed in — otherwise a caller could widen the egress surface simply
        # by passing a spec whose descriptor declares the parameter it wants to
        # send.
        registered = self._spec_of(spec.contribution_id)
        if (
            item is None
            or registered is None
            or type(call) is not ReflectActionCall
            or not _valid_arguments(registered, call.arguments)
            or not valid_deadline(call.deadline_monotonic)
            or type(call.question) is not str
            or not call.question
            or type(call.max_items) is not int
            or call.max_items < 1
        ):
            # A receipt even here.  A refusal at this line means core or a
            # caller is out of contract, which is exactly the thing an operator
            # would otherwise have no way to see: the reflect loop records a
            # skip step, but the event stream would show the call never
            # happening at all.
            _emit(
                sink,
                spec=spec,
                status="invalid",
                reason_code=INVALID_RESULT,
                duration_ms=elapsed_ms(self._clock, started),
            )
            return ReflectActionOutcome(failure_code=INVALID_RESULT)
        reason, result = self._execute(item, call)
        if reason is not None:
            _emit(
                sink,
                spec=spec,
                status="unavailable",
                reason_code=reason,
                duration_ms=elapsed_ms(self._clock, started),
            )
            return ReflectActionOutcome(failure_code=reason)
        if not _valid_result(result):
            _emit(
                sink,
                spec=spec,
                status="invalid",
                reason_code=INVALID_RESULT,
                duration_ms=elapsed_ms(self._clock, started),
            )
            return ReflectActionOutcome(failure_code=INVALID_RESULT)
        if result.status is ExtensionResultStatus.UNAVAILABLE:
            # The contributor's own statement that it could not serve this
            # call.  Letting the payload it disclaimed reach a reader would put
            # material in front of them the plugin itself says is not an
            # answer, so admission is skipped outright.  PARTIAL stays admitted
            # — it means "some of it", not "none of it".
            _emit(
                sink,
                spec=spec,
                status="unavailable",
                reason_code=_failure_code(result) or UNAVAILABLE,
                duration_ms=elapsed_ms(self._clock, started),
            )
            return ReflectActionOutcome(
                failure_code=_failure_code(result) or UNAVAILABLE
            )
        limit = min(call.max_items, EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL)
        try:
            items, truncated = _admitted(
                result.items, limit=limit, cancellation=call.cancellation
            )
        except AskCancelled:
            # ONE cancellation shape out of this host: a stable failure code,
            # never an exception.  The reflect loop re-reads its own cancel
            # event immediately after this call and propagates from there, so
            # cancellation still ends the run at the first opportunity — but it
            # ends it on the loop's own reading of its own token, not on a
            # host's report about it.
            _emit(
                sink,
                spec=spec,
                status="unavailable",
                reason_code=CANCELLED,
                duration_ms=elapsed_ms(self._clock, started),
            )
            return ReflectActionOutcome(failure_code=CANCELLED)
        outcome = ReflectActionOutcome(
            items=items,
            note=clean_text(result.note, REFLECT_ACTION_NOTE_MAX_CHARS) or "",
            truncated=truncated,
        )
        _emit(
            sink,
            spec=spec,
            status=result.status.value,
            reason_code=_failure_code(result),
            duration_ms=elapsed_ms(self._clock, started),
            count=len(items),
        )
        return outcome

    def _execute(
        self, item: RegisteredContribution, call: ReflectActionCall,
    ) -> tuple[str | None, object]:
        """Decide availability and run one contributor on one daemon thread.

        The availability decision runs on the worker, inside the deadline, for
        the same reason ``invoke`` does: a plugin at this point supplies its
        own probe through its manifest's ``provides``, so a slow or hung probe
        spends the reader's latency exactly as effectively as a slow or hung
        ``invoke``.  ``specs()`` above already asked the same question at the
        top of the run — this is not redundant, it is the *fresh* answer at the
        moment of the call, and the only one running under this deadline.

        The thread is started WITHOUT ``contextvars.copy_context()`` and that
        omission is load-bearing, not an oversight — do not "fix" it.  A fresh
        empty Context is precisely what keeps a plugin from inheriting this
        request's frozen retrieval scope, its retrieval run, or a slot in the
        leaf-I/O fan-out gate simply by virtue of running underneath it.

        A thread pool is likewise wrong here: one hung plugin would occupy a
        shared worker forever and turn a single plugin's fault into a
        deployment-wide outage.  The registered cost of a private daemon thread
        is that a genuinely hung plugin leaks one thread per affected request.
        """
        contribution_id = item.contribution.declaration.id
        implementation = item.contribution.implementation
        deadline = call.deadline_monotonic
        cell = _WorkerCell()

        def _target() -> None:
            try:
                availability = self._registry.availability(
                    contribution_id,
                    ReflectActionAvailabilityContext(contribution_id, deadline),
                )
                if availability.status is not AvailabilityStatus.AVAILABLE:
                    cell.reason = UNAVAILABLE
                else:
                    cell.result = implementation.invoke(
                        ReflectActionCallContext(
                            call.question,
                            # Read-only view over a private copy.  The reflect
                            # loop already passes one; this is the frame that
                            # makes it true for EVERY caller, because the trace
                            # entry claiming "this is what left the deployment"
                            # is written from the caller's dict and a plugin
                            # must not be able to edit it from underneath.
                            MappingProxyType(dict(call.arguments)),
                            # The SDK face, not the raw event: a compliant
                            # plugin may call ``raise_if_cancelled()``.
                            SdkCancellation(call.cancellation),
                            deadline,
                            call.max_items,
                        )
                    )
            except BaseException:  # noqa: BLE001 — a plugin fault is fail-open
                cell.failed = True
            finally:
                cell.done = True

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        while True:
            # Each join slice is bounded by the budget that is actually left,
            # not just the fixed slice width: a deployment may configure the
            # timeout below one slice, and waiting the full slice would make
            # real latency a multiple of the configured budget.  A broken
            # clock (None) falls back to the plain slice — the post-join
            # deadline check treats that clock as open for the same reason.
            now = safe_clock(self._clock)
            worker.join(
                JOIN_SLICE_SECONDS
                if now is None
                else max(0.0, min(JOIN_SLICE_SECONDS, deadline - now))
            )
            finished = not worker.is_alive()
            # Both reads happen on EVERY pass, the one that observes the worker
            # finish included.  "Past the deadline" is a fact about the clock,
            # not about which slice happened to notice.
            if is_cancelled(call.cancellation):
                cell.abandoned = True
                return CANCELLED, None
            if not deadline_open(safe_clock(self._clock), deadline):
                cell.abandoned = True
                return TIMEOUT, None
            if finished:
                break
        if cell.failed or not cell.done:
            return FAILED, None
        if cell.reason is not None:
            return cell.reason, None
        return None, cell.result


def _valid_arguments(spec: ReflectActionSpec, arguments: object) -> bool:
    """The parser's post-condition, re-checked at the egress boundary.

    ``reflect``'s ``_parse_plugin_action_arguments`` already guarantees exactly
    this shape — every declared parameter present, every value a ``str``.  It
    is re-checked here rather than trusted because this is the last frame
    before text leaves the deployment, and the port is public: a hand-built
    context must not be able to send a plugin a key it never declared or a
    value that is not a string.  ``spec`` is therefore always the caller's
    ``_spec_of`` lookup into the frozen topology, never the object the caller
    passed in.
    """
    if type(arguments) is not dict:
        return False
    declared = {parameter.name for parameter in spec.descriptor.parameters}
    return declared == set(arguments) and all(
        type(value) is str for value in arguments.values()
    )


def _valid_failure(value: object) -> bool:
    return value is None or (
        type(value) is ExtensionFailure
        and type(value.kind) is ExtensionFailureKind
        and stable_code(value.code)
    )


def _valid_result(value: object) -> bool:
    try:
        return (
            type(value) is ReflectActionResult
            and type(value.items) is tuple
            and type(value.status) is ExtensionResultStatus
            and _valid_failure(value.failure)
        )
    except Exception:  # noqa: BLE001 — a hostile attribute must not propagate
        return False


def _failure_code(value: object) -> str:
    failure = getattr(value, "failure", None)
    return failure.code if _valid_failure(failure) and failure is not None else ""


def _admitted(
    items: tuple[object, ...], *, limit: int, cancellation: object,
) -> tuple[tuple[ReflectActionItem, ...], bool]:
    """Core-owned admission: validate, bound, de-duplicate within the call, cap.

    Nothing here touches the network or the database — whether a URL is
    reachable, or really holds what the excerpt claims, is not a question this
    host speculatively answers.

    The work is bounded twice over, because ``items`` is plugin output and
    therefore unbounded input: at most ``ADMISSION_SCAN_FACTOR`` items per
    accepting slot are examined at all, and each string is cut to its own limit
    plus headroom before anything walks it.  Cancellation is re-read on a
    stride through that scan and propagates rather than failing open.

    De-duplication here is WITHIN one call only; the run-level ``seen_urls``
    set lives in the reflect loop, because "already brought back this turn" and
    "already brought back this run, possibly by another action" are different
    questions and only the loop can answer the second.

    The returned flag says the scan stopped with items still unexamined or
    unadmitted — the reader is owed the difference between "the plugin found
    two" and "we kept two of many".
    """
    admitted: list[ReflectActionItem] = []
    seen_urls: set[str] = set()
    scan_budget = limit * ADMISSION_SCAN_FACTOR
    truncated = False
    for scanned, item in enumerate(items):
        if len(admitted) >= limit or scanned >= scan_budget:
            truncated = True
            break
        if scanned % ADMISSION_CANCEL_STRIDE == 0:
            raise_if_cancelled(cancellation)
        if type(item) is not ReflectActionItem:
            continue
        title = clean_text(item.title, EXTERNAL_EVIDENCE_TITLE_MAX_CHARS)
        url = clean_url(item.url, EXTERNAL_EVIDENCE_URL_MAX_CHARS)
        if not title or url is None or url in seen_urls:
            continue
        excerpt = clean_text(item.excerpt, EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS)
        location_label = clean_text(
            item.location_label, EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS
        )
        # An item with no quotable text is not citable material, and the whole
        # contract of ``excerpt`` is that it is what a citation card shows.
        if not excerpt or location_label is None:
            continue
        seen_urls.add(url)
        admitted.append(
            ReflectActionItem(title, excerpt, url, location_label)
        )
    return tuple(admitted), truncated


def _emit(
    sink: Callable[[dict[str, object]], None] | None,
    *,
    spec: ReflectActionSpec,
    status: str,
    reason_code: str,
    duration_ms: int,
    count: int = 0,
) -> None:
    """Content-free receipt: ids, the action name, a stable code, a count.

    Never the question, an argument value, a title, an excerpt or a URL.  The
    arguments ARE disclosed — verbatim — but in the run trace, where the person
    who asked the question can see what left the deployment on their behalf
    (design document §九 invariant 1); an operational event stream read by
    everyone is the wrong place for one user's text.
    """
    if sink is None:
        return
    event: dict[str, object] = {
        "kind": "ask_extension",
        "point": ASK_REFLECT_ACTION_POINT,
        "plugin_id": spec.plugin_id,
        "contribution_id": spec.contribution_id,
        "action": spec.descriptor.name,
        "status": status,
        "duration_ms": duration_ms,
        "count": count,
    }
    if reason_code and stable_code(reason_code):
        event["code"] = reason_code
    try:
        sink(event)
    except Exception:  # noqa: BLE001 — telemetry must never break the request
        pass


__all__ = [
    "CANCELLED",
    "FAILED",
    "INVALID_RESULT",
    "TIMEOUT",
    "UNAVAILABLE",
    "ReflectActionHost",
]
