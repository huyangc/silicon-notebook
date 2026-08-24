"""Hard-deadline host for ``ask.gap_consult`` contributors.

This host is the only place a gap suggestion can become core-visible, and it
owns three things no plugin can influence:

1. **The egress surface.**  Contributors receive the frozen
   :class:`~app.domain.gap_consult.GapConsultQuery` the caller built and
   nothing else.
2. **The deadline.**  A contribution's availability probe *and* its consult
   call run together on one worker thread joined in slices, so a plugin that
   never returns — in either half — costs this request its remaining budget and
   nothing more.
3. **Sanitization.**  Titles, URLs, summaries and labels are validated and
   truncated here, after the plugin returns, so a hostile or sloppy plugin
   cannot put an unbounded string or a ``javascript:`` URL in front of a user.
   The *work* of doing so is bounded in both dimensions — how many items are
   examined and how much of each string is walked — so an unbounded payload
   costs a bounded scan rather than an unbounded one on the request's critical
   path, after the deadline above has already been honoured.

Everything else fails open: a raising, hanging, or malformed contributor
contributes nothing and the answer it was consulted for is unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse

from app.domain.cancellation import AskCancelled
from app.domain.gap_consult import (
    GAP_CONSULT_MAX_GAP_PHRASES,
    GAP_CONSULT_MAX_SUGGESTIONS,
    GAP_CONSULT_PHRASE_MAX_CHARS,
    GAP_CONSULT_QUESTION_MAX_CHARS,
    GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_URL_MAX_CHARS,
    GapConsultCallContext,
    GapConsultQuery,
    GapSuggestion,
)
from app.extension_sdk import (
    AvailabilityStatus,
    ContributionKind,
    ContributorResult,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
)
from app.extension_sdk.gap_consult import (
    ASK_GAP_CONSULT_POINT,
    GapConsultAvailabilityContext,
    GapConsultExtensionContext,
)
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
# The main thread never blocks longer than this without re-reading cancellation
# and the deadline, so both stay responsive against an uncooperative plugin.
_JOIN_SLICE_SECONDS = 0.05
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
# How many items admission will EXAMINE, as a multiple of how many it could
# still accept.  A contributor's payload is unbounded input: without a cap, a
# tuple of a million rejects makes core walk all million of them on the
# request's critical path — *after* the deadline that was supposed to bound
# this contributor has already been honoured, so the budget above buys nothing.
# The factor is deliberately generous rather than tight: the point is to deny
# an unbounded scan, not to police sloppiness, so a plugin whose good items sit
# behind a handful of malformed ones still gets them admitted.
_ADMISSION_SCAN_FACTOR = 20
# Re-read cancellation every this many examined items.  Belt and braces: the
# scan above is short, but cancellation is the one signal this host never fails
# open on, and it is set from another thread — it can flip after the join loop's
# own read and before the last item is examined.
_ADMISSION_CANCEL_STRIDE = 8
# Slack allowed above a field's own limit before the raw contributor string is
# cut.  This cut lands on PLUGIN OUTPUT, never on user data — the "never
# silently truncate what a user typed" rail governs write and render paths —
# and it exists so `str.strip()` can never be pointed at a 50 MB string.  The
# headroom is wide enough that every realistic value stays byte-identical to an
# unbounded strip; only a value whose LEADING whitespace alone exceeds it now
# reads as empty, which drops the item — the safe direction.
_ADMISSION_SLICE_HEADROOM_CHARS = 1024


class _SdkCancellation:
    """Project the caller's raw cancel event onto the SDK's full token face.

    ``GapConsultExtensionContext.cancellation`` is typed as the SDK
    ``CancellationToken`` protocol, whose face is ``is_set()`` **and**
    ``raise_if_cancelled()`` — but the production caller hands the host a raw
    ``threading.Event``, which only has the first half.  A compliant plugin
    calling ``raise_if_cancelled()`` on the raw event would AttributeError,
    which the worker's fail-open guard then records as a plugin failure: a
    well-behaved contributor loses its suggestions for following the contract
    (codex #584 R5 P1).  This mirrors the established adapter in
    ``generated_question_contribution._CancellationToken`` — monotonic caching
    included, so an ``is_set()``/``raise_if_cancelled()`` sequence cannot be
    turned into a hostile second-read type change — rather than inventing a
    second cancellation shape.
    """

    __slots__ = ("_event", "_observed_cancelled")

    def __init__(self, event: object) -> None:
        self._event = event
        self._observed_cancelled = False

    def is_set(self) -> bool:
        if self._observed_cancelled:
            return True
        if self._event is None:
            return False
        cancelled = self._event.is_set()
        if type(cancelled) is not bool:
            raise TypeError("malformed cancellation state")
        if cancelled:
            self._observed_cancelled = True
        return cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise AskCancelled()


@dataclass
class _WorkerCell:
    """Private mailbox for one contributor attempt.

    ``abandoned`` is not a cancellation signal to the worker — nothing can stop
    a thread that refuses to return.  It records that the main thread has moved
    on, which is why a late write here is inert: no one reads the cell again.
    """

    done: bool = False
    failed: bool = False
    reason: str | None = None
    ends_budget: bool = False
    result: object = None
    abandoned: bool = False


@dataclass(frozen=True, slots=True)
class _Attempt:
    """What one contributor attempt produced.

    ``reason`` is ``None`` exactly when the contributor ran to completion; its
    (still unvalidated) return value is then in ``result``.  ``ends_budget`` is
    core-owned and never derived from a plugin-supplied reason string, so a
    plugin cannot cut the remaining contributors' turns short by naming its own
    unavailability reason ``gap_consult_timeout``.
    """

    reason: str | None = None
    result: object = None
    ends_budget: bool = False


class GapConsultHost:
    """Run gap-consultation contributors under a hard wall-clock deadline."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Gap consult host requires a frozen registry"
            )
        contributors: list[RegisteredContribution] = []
        for item in registry.contributions(ASK_GAP_CONSULT_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            if (
                declaration.kind is not ContributionKind.CONTRIBUTOR
                or not callable(getattr(implementation, "consult", None))
            ):
                raise ExtensionRegistryError(
                    f"Gap consult contributor {declaration.id!r} does not "
                    "implement the contributor contract"
                )
            contributors.append(item)
        self._registry = registry
        self._contributors = tuple(contributors)
        self._event_sink = event_sink
        self._clock = clock

    def has_contributions(self) -> bool:
        """Read the startup-frozen topology.  No clock, no I/O, no request state."""

        return bool(self._contributors)

    def consult(
        self,
        call_context: GapConsultCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[GapSuggestion, ...]:
        # Empty topology is a strict no-op: no validation, no clock read, no
        # probe call, no event.  A deployment without gap-consult plugins pays
        # exactly nothing here.
        if not self._contributors:
            return ()
        if type(call_context) is not GapConsultCallContext:
            return ()
        query = call_context.query
        # The host does not trust its caller either: an out-of-contract query
        # would otherwise be forwarded verbatim to a third party.
        if not _valid_query(query):
            return ()
        if not _valid_deadline(call_context.deadline_monotonic):
            return ()
        # The CALLER's own lease is checked here, on the calling thread — the
        # only thread that can observe it, because both backends answer this
        # probe from thread-local (SQLite) or ContextVar (PostgreSQL) state.
        if not _connection_clear(call_context.connection_probe):
            return ()
        sink = event_sink if event_sink is not None else self._event_sink
        accepted: list[GapSuggestion] = []
        # Deliberately outside the loop: two contributors offering the same URL
        # is one suggestion, not two.  Per-contributor de-duplication would let
        # a second plugin re-seat a link the first already spent a slot on.
        seen_urls: set[str] = set()
        for item in self._contributors:
            if len(accepted) >= query.max_suggestions:
                break
            _raise_if_cancelled(call_context.cancellation)
            contribution_id = item.contribution.declaration.id
            plugin_id = item.plugin_id
            started = _safe_clock(self._clock)
            if not _deadline_open(started, call_context.deadline_monotonic):
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="gap_consult_budget_exhausted",
                    duration_ms=0,
                )
                break
            attempt = self._execute(
                item, call_context, query.max_suggestions - len(accepted)
            )
            if attempt.reason is not None:
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=attempt.reason,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                # A spent deadline or a held lease ends the point: there is no
                # honest way to start another contributor.  A plugin fault is
                # local and the next one still gets its turn.
                if attempt.ends_budget:
                    break
                continue
            result = attempt.result
            if not _valid_result(result):
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="invalid",
                    reason_code="invalid_gap_consult_result",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            # An UNAVAILABLE result contributes nothing, items or not.  That
            # status is the contributor's own statement that it could not serve
            # this call; letting the payload it disclaimed reach a reader would
            # put material in front of them that the plugin itself says is not
            # an answer.  Skipping admission outright (rather than sanitizing
            # and discarding) also keeps the disclaimed URLs out of `seen_urls`,
            # so a later contributor that really can serve this call is not
            # blocked by a link the first one withdrew.  PARTIAL stays admitted
            # — it means "some of it", not "none of it".  The receipt below is
            # unchanged: `status` already renders "unavailable" and carries the
            # plugin's own stable failure code; only `count` moves, to 0.
            admitted = (
                ()
                if result.status is ExtensionResultStatus.UNAVAILABLE
                else _sanitized(
                    result.items,
                    limit=query.max_suggestions - len(accepted),
                    seen_urls=seen_urls,
                    cancellation=call_context.cancellation,
                )
            )
            accepted.extend(admitted)
            _emit(
                sink,
                plugin_id=plugin_id,
                contribution_id=contribution_id,
                status=result.status.value,
                reason_code=_failure_code(result),
                duration_ms=_elapsed_ms(self._clock, started),
                count=len(admitted),
            )
        return tuple(accepted[: query.max_suggestions])

    def _execute(
        self,
        item: RegisteredContribution,
        call_context: GapConsultCallContext,
        remaining: int,
    ) -> _Attempt:
        """Decide availability and run one contributor on one daemon thread.

        The availability decision runs on the worker, inside the deadline, for
        the same reason ``consult`` does: a plugin at this point supplies its
        own probe through its manifest's ``provides``, so a slow or hung probe
        spends the reader's latency exactly as effectively as a slow or hung
        ``consult``.  Deciding on the calling thread made the "hard deadline" a
        promise about only half the call — measured: a probe that sleeps 2s
        pushed a ``consult()`` with a 0.2s budget to 2.01s of wall clock.

        The post-decision connection re-check moves onto the worker with it,
        and must: ``is_connection_held`` answers from thread-local (SQLite) or
        ContextVar (PostgreSQL) state, so the only thread that can observe a
        lease *the decision* took is the thread the decision ran on.  The
        caller's own lease is checked before the loop, on the calling thread,
        where it is likewise the only place it is visible.

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
        deadline = call_context.deadline_monotonic
        cell = _WorkerCell()

        def _target() -> None:
            try:
                availability = self._registry.availability(
                    contribution_id,
                    GapConsultAvailabilityContext(contribution_id, deadline),
                )
                # A live decision must not have taken a core connection on the
                # way; this thread is where such a lease would be visible.
                if not _connection_clear(call_context.connection_probe):
                    cell.reason = "connection_lease_held"
                    cell.ends_budget = True
                elif availability.status is not AvailabilityStatus.AVAILABLE:
                    cell.reason = availability.reason_code
                else:
                    cell.result = implementation.consult(
                        GapConsultExtensionContext(
                            call_context.query,
                            # The SDK face, not the raw event: a compliant
                            # plugin may call ``raise_if_cancelled()``.
                            _SdkCancellation(call_context.cancellation),
                            remaining,
                            deadline,
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
            now = _safe_clock(self._clock)
            worker.join(
                _JOIN_SLICE_SECONDS
                if now is None
                else max(0.0, min(_JOIN_SLICE_SECONDS, deadline - now))
            )
            finished = not worker.is_alive()
            # Both reads happen on EVERY pass, the one that observes the worker
            # finish included.  Reading them only while the thread was still
            # alive made the outcome a property of scheduling: a plugin that
            # answered *after* its budget was spent got accepted whenever the
            # join slice happened to land on the far side of its last write,
            # and abandoned when it did not.  "Past the deadline" is a fact
            # about the clock, not about which slice noticed.
            if _is_cancelled(call_context.cancellation):
                cell.abandoned = True
                raise AskCancelled()
            if not _deadline_open(_safe_clock(self._clock), deadline):
                cell.abandoned = True
                return _Attempt("gap_consult_timeout", ends_budget=True)
            if finished:
                break
        if cell.failed or not cell.done:
            return _Attempt("gap_consult_failed")
        if cell.reason is not None:
            return _Attempt(cell.reason, ends_budget=cell.ends_budget)
        return _Attempt(result=cell.result)


def _valid_query(value: object) -> bool:
    # Length is part of validity, not just shape: the query IS the egress
    # surface, and this port is public — a manually constructed context must
    # not be able to forward more text to a plugin than the documented bounds
    # (AskService's own egress construction already stays inside them).
    return (
        type(value) is GapConsultQuery
        and type(value.question) is str
        and 0 < len(value.question) <= GAP_CONSULT_QUESTION_MAX_CHARS
        and type(value.gaps) is tuple
        and len(value.gaps) <= GAP_CONSULT_MAX_GAP_PHRASES
        and all(
            type(phrase) is str and len(phrase) <= GAP_CONSULT_PHRASE_MAX_CHARS
            for phrase in value.gaps
        )
        and type(value.max_suggestions) is int
        and 1 <= value.max_suggestions <= GAP_CONSULT_MAX_SUGGESTIONS
    )


def _valid_failure(value: object) -> bool:
    return value is None or (
        type(value) is ExtensionFailure
        and type(value.kind) is ExtensionFailureKind
        and type(value.code) is str
        and bool(_STABLE_CODE.fullmatch(value.code))
    )


def _valid_result(value: object) -> bool:
    try:
        return (
            type(value) is ContributorResult
            and type(value.items) is tuple
            and type(value.status) is ExtensionResultStatus
            and _valid_failure(value.failure)
        )
    except Exception:  # noqa: BLE001 — a hostile attribute must not propagate
        return False


def _failure_code(value: object) -> str:
    failure = getattr(value, "failure", None)
    return failure.code if _valid_failure(failure) and failure is not None else ""


def _clean_text(value: object, limit: int) -> str | None:
    if type(value) is not str:
        return None
    # Cut BEFORE stripping.  `str.strip()` allocates a full copy of whatever it
    # is handed, so stripping first is an unbounded allocation driven by plugin
    # output; the bound has to come first for it to be a bound at all.
    return value[: limit + _ADMISSION_SLICE_HEADROOM_CHARS].strip()[:limit]


def _clean_url(value: object) -> str | None:
    if type(value) is not str:
        return None
    # Same cut-before-strip rule as `_clean_text`, and it does not weaken the
    # rejection below: anything longer than the limit plus the headroom is
    # still longer than the limit after the cut, so it is dropped exactly as it
    # was before — never shortened into a different destination.
    url = value[
        : GAP_SUGGESTION_URL_MAX_CHARS + _ADMISSION_SLICE_HEADROOM_CHARS
    ].strip()
    # A URL is the one field that must NOT be truncated to fit: a shortened URL
    # is a different, silently wrong destination.  Over-long ones are rejected.
    if not url or len(url) > GAP_SUGGESTION_URL_MAX_CHARS:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 — malformed input is a drop, not a crash
        return None
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        return None
    return url


def _sanitized(
    items: tuple[object, ...],
    *,
    limit: int,
    seen_urls: set[str],
    cancellation: object,
) -> tuple[GapSuggestion, ...]:
    """Core-owned admission: validate, bound, de-duplicate, cap.

    Nothing here touches the network or the database — whether a URL is
    reachable, or is really a PDF, is answered by the import endpoint's own
    probe when the user asks for it, not by speculatively fetching it now.

    The work this does is bounded twice over, because ``items`` is plugin
    output and therefore unbounded input: at most ``_ADMISSION_SCAN_FACTOR``
    items per accepting slot are examined at all, and each string is cut to its
    own limit plus headroom before anything walks it.  Cancellation is re-read
    on a stride through that scan, and propagates rather than failing open —
    the whole host treats it as the one signal it never swallows.
    """
    admitted: list[GapSuggestion] = []
    scan_budget = limit * _ADMISSION_SCAN_FACTOR
    for scanned, item in enumerate(items):
        if len(admitted) >= limit or scanned >= scan_budget:
            break
        if scanned % _ADMISSION_CANCEL_STRIDE == 0:
            _raise_if_cancelled(cancellation)
        if type(item) is not GapSuggestion:
            continue
        title = _clean_text(item.title, GAP_SUGGESTION_TITLE_MAX_CHARS)
        url = _clean_url(item.url)
        if not title or url is None or url in seen_urls:
            continue
        summary = _clean_text(item.summary, GAP_SUGGESTION_SUMMARY_MAX_CHARS)
        source_label = _clean_text(
            item.source_label, GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS
        )
        if summary is None or source_label is None:
            continue
        seen_urls.add(url)
        admitted.append(GapSuggestion(title, url, summary, source_label))
    return tuple(admitted)


def _connection_clear(probe: object) -> bool:
    try:
        checker = getattr(probe, "is_connection_held", None)
        if not callable(checker):
            return False
        held = checker()
        return type(held) is bool and not held
    except Exception:  # noqa: BLE001 — an unreadable probe fails closed
        return False


def _is_cancelled(cancellation: object) -> bool:
    try:
        is_set = getattr(cancellation, "is_set", None)
        if not callable(is_set):
            return False
        return is_set() is True
    except Exception:  # noqa: BLE001 — an unreadable token is not cancellation
        return False


def _raise_if_cancelled(cancellation: object) -> None:
    if _is_cancelled(cancellation):
        raise AskCancelled()


def _safe_clock(clock: Callable[[], float]) -> float | None:
    try:
        value = clock()
        normalized = float(value) if type(value) in {int, float} else None
        return (
            normalized
            if normalized is not None and math.isfinite(normalized)
            else None
        )
    except Exception:  # noqa: BLE001 — a broken clock is not a plugin verdict
        return None


def _valid_deadline(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value > 0


def _deadline_open(now: float | None, deadline: float) -> bool:
    # An unreadable clock is an observability problem, never a reason to
    # abandon work the deployment asked for.
    return now is None or now <= deadline


def _elapsed_ms(clock: Callable[[], float], started: float | None) -> int:
    if started is None:
        return 0
    ended = _safe_clock(clock)
    if ended is None:
        return 0
    try:
        delta = ended - started
        milliseconds = delta * 1000
        if not math.isfinite(delta) or not math.isfinite(milliseconds):
            return 0
        return max(0, int(milliseconds))
    except (OverflowError, ValueError):
        return 0


def _emit(
    sink: Callable[[dict[str, object]], None] | None,
    *,
    plugin_id: str,
    contribution_id: str,
    status: str,
    reason_code: str,
    duration_ms: int,
    count: int = 0,
) -> None:
    """Content-free receipt: ids, a stable code, a duration and a count.

    Never the question, a gap phrase, a suggestion title, or a URL.
    """
    if sink is None:
        return
    event: dict[str, object] = {
        "kind": "ask_extension",
        "point": ASK_GAP_CONSULT_POINT,
        "plugin_id": plugin_id,
        "contribution_id": contribution_id,
        "status": status,
        "duration_ms": duration_ms,
        "count": count,
    }
    if reason_code and _STABLE_CODE.fullmatch(reason_code):
        event["reason_code"] = reason_code
    try:
        sink(event)
    except Exception:  # noqa: BLE001 — telemetry must never break the request
        pass


__all__ = ["GapConsultHost"]
