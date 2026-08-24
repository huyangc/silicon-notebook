"""Hard-deadline host for ``ask.gap_consult`` contributors.

This host is the only place a gap suggestion can become core-visible, and it
owns three things no plugin can influence:

1. **The egress surface.**  Contributors receive the frozen
   :class:`~app.domain.gap_consult.GapConsultQuery` the caller built and
   nothing else.
2. **The deadline.**  Contributions run on a worker thread joined in slices, so
   a plugin that never returns costs this request its remaining budget and
   nothing more.
3. **Sanitization.**  Titles, URLs, summaries and labels are validated and
   truncated here, after the plugin returns, so a hostile or sloppy plugin
   cannot put an unbounded string or a ``javascript:`` URL in front of a user.

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


@dataclass
class _WorkerCell:
    """Private mailbox for one contributor call.

    ``abandoned`` is not a cancellation signal to the worker — nothing can stop
    a thread that refuses to return.  It records that the main thread has moved
    on, which is why a late write here is inert: no one reads the cell again.
    """

    done: bool = False
    failed: bool = False
    result: object = None
    abandoned: bool = False


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
        if not _connection_clear(call_context.connection_probe):
            return ()
        sink = event_sink if event_sink is not None else self._event_sink
        accepted: list[GapSuggestion] = []
        seen_urls: set[str] = set()
        for item in self._contributors:
            if len(accepted) >= query.max_suggestions:
                break
            _raise_if_cancelled(call_context.cancellation)
            contribution = item.contribution
            contribution_id = contribution.declaration.id
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
            availability = self._registry.availability(
                contribution_id,
                GapConsultAvailabilityContext(
                    contribution_id, call_context.deadline_monotonic
                ),
            )
            # A live decision must not have taken a core connection on the way.
            if not _connection_clear(call_context.connection_probe):
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code="connection_lease_held",
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                break
            if availability.status is not AvailabilityStatus.AVAILABLE:
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=availability.reason_code,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            context = GapConsultExtensionContext(
                query,
                call_context.cancellation,
                query.max_suggestions - len(accepted),
                call_context.deadline_monotonic,
            )
            outcome, result = self._execute(
                contribution.implementation, context, call_context
            )
            if outcome != "ok":
                _emit(
                    sink,
                    plugin_id=plugin_id,
                    contribution_id=contribution_id,
                    status="unavailable",
                    reason_code=outcome,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                # A timeout means the point budget is spent, so there is no
                # honest way to start another contributor; a plugin fault is
                # local and the next one still gets its turn.
                if outcome == "gap_consult_timeout":
                    break
                continue
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
            admitted = _sanitized(
                result.items,
                limit=query.max_suggestions - len(accepted),
                seen_urls=seen_urls,
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
        implementation: object,
        context: GapConsultExtensionContext,
        call_context: GapConsultCallContext,
    ) -> tuple[str, object]:
        """Run one contributor on a throwaway daemon thread.

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
        cell = _WorkerCell()

        def _target() -> None:
            try:
                value = implementation.consult(context)
            except BaseException:  # noqa: BLE001 — a plugin fault is fail-open
                cell.failed = True
            else:
                cell.result = value
            finally:
                cell.done = True

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        while True:
            worker.join(_JOIN_SLICE_SECONDS)
            if not worker.is_alive():
                break
            if _is_cancelled(call_context.cancellation):
                cell.abandoned = True
                raise AskCancelled()
            if not _deadline_open(
                _safe_clock(self._clock), call_context.deadline_monotonic
            ):
                cell.abandoned = True
                return "gap_consult_timeout", None
        if cell.failed or not cell.done:
            return "gap_consult_failed", None
        return "ok", cell.result


def _valid_query(value: object) -> bool:
    return (
        type(value) is GapConsultQuery
        and type(value.question) is str
        and bool(value.question)
        and type(value.gaps) is tuple
        and len(value.gaps) <= GAP_CONSULT_MAX_GAP_PHRASES
        and all(type(phrase) is str for phrase in value.gaps)
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
    return value.strip()[:limit]


def _clean_url(value: object) -> str | None:
    if type(value) is not str:
        return None
    url = value.strip()
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
    items: tuple[object, ...], *, limit: int, seen_urls: set[str],
) -> tuple[GapSuggestion, ...]:
    """Core-owned admission: validate, bound, de-duplicate, cap.

    Nothing here touches the network or the database — whether a URL is
    reachable, or is really a PDF, is answered by the import endpoint's own
    probe when the user asks for it, not by speculatively fetching it now.
    """
    admitted: list[GapSuggestion] = []
    for item in items:
        if len(admitted) >= limit:
            break
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
