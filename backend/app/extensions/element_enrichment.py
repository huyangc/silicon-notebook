"""Hard-deadline host for ``source.element_enricher`` contributors.

This host is the only place a plugin-authored element enrichment can become
core-visible, and it owns four things no contributor can influence:

1. **The egress surface.**  A contributor receives core's own read-only
   projection of the parsed elements and nothing else — no parser metadata
   mapping, no repository, no settings, no connection probe.
2. **Image bytes.**  They travel through a reader scoped to *one contributor's
   turn*, over locations the calling thread resolved before any plugin ran, so
   the worker thread a contributor runs on performs no database access, one
   image can never exceed ``max_asset_bytes``, and every read after that turn
   ends — the contributor having returned, timed out, or been abandoned —
   answers ``None``.
3. **The deadline.**  A contribution's availability probe *and* its ``enrich``
   call run together on one daemon thread joined in slices, so a plugin that
   never returns — in either half — costs this parse its remaining budget and
   ends the point rather than the whole job.
4. **Admission.**  Element identity, metadata shape and depth, description
   characters, proposal count and persisted bytes are all decided here, after
   the plugin returns.  A single violation discards that contribution's whole
   batch: a partially validated contribution is never persisted, and every
   other contribution is unaffected.

Everything else fails open: a raising, hanging, or malformed contributor
enriches nothing and the source it was consulted for is ingested unchanged.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Callable

from app.domain.cancellation import CoreCancellation
from app.domain.element_enrichment import (
    persisted_element_enrichment_size,
    thaw_element_enrichment_metadata,
    valid_element_enrichment_owner,
)
from app.domain.extensions import (
    ElementAssetLocation,
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ParsedElementEnvelope,
)
from app.extension_sdk import (
    AvailabilityStatus,
    ContributionKind,
    ContributorResult,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
)
from app.extension_sdk.element_enrichment import (
    SOURCE_ELEMENT_ENRICHER_POINT,
    ElementEnrichmentAvailabilityContext,
    ElementEnrichmentBudget,
    ElementEnrichmentCandidate,
    ElementEnrichmentContext,
    ElementRef,
    ElementView,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_EVENT_KIND = "source_element_enricher_attempt"
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
# Length precedes the regex wherever a plugin-authored code is validated, for
# the same reason as in ``gap_consult``: rejecting an arbitrarily long
# preconstructed code must cost O(1), not a full-string scan.
_STABLE_CODE_MAX_CHARS = 64
# The main thread never blocks longer than this without re-reading cancellation
# and the deadline, so both stay responsive against an uncooperative plugin.
_JOIN_SLICE_SECONDS = 0.05
# Characters a description may carry besides printable ones.  Fenced code
# blocks are the point of this field, so newlines and tabs are in; every other
# control character is not.
_DESCRIPTION_EXTRA_CHARS = frozenset("\n\t")
# Why a contribution's whole batch was discarded.  Distinct codes because
# "the plugin addressed an element it was not shown" and "the plugin's
# metadata was not JSON" are different operator problems with different fixes,
# and a single ``invalid`` code makes them indistinguishable in the log.
_REASON_SHAPE = "invalid_element_enrichment_result"
_REASON_REF = "invalid_element_ref"
_REASON_DUPLICATE = "duplicate_element_ref"
_REASON_METADATA = "invalid_enrichment_metadata"
_REASON_DESCRIPTION = "invalid_enrichment_description"
_REASON_BUDGET = "enrichment_budget_exceeded"


def _stable_code(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _STABLE_CODE_MAX_CHARS
        and bool(_STABLE_CODE.fullmatch(value))
    )


@dataclass(frozen=True, slots=True)
class _Registration:
    """One startup-frozen contributor with its provenance already validated."""

    plugin_id: str
    plugin_version: str
    contribution_id: str
    implementation: object


@dataclass
class _WorkerCell:
    """Private mailbox for one contributor attempt.

    Nothing here can stop a worker that refuses to return; when the main
    thread gives up it simply stops reading this cell, which is what makes a
    late write by an abandoned worker inert.
    """

    done: bool = False
    failed: bool = False
    reason: str | None = None
    ends_budget: bool = False
    result: object = None


@dataclass(frozen=True, slots=True)
class _Attempt:
    """What one contributor attempt produced.

    ``reason`` is ``None`` exactly when the contributor ran to completion; its
    (still unvalidated) return value is then in ``result``.  ``ends_budget`` is
    core-owned and never derived from a plugin-supplied reason string, so a
    plugin cannot cut the remaining contributors' turns short by naming its own
    unavailability reason ``element_enricher_timeout``.
    """

    reason: str | None = None
    result: object = None
    ends_budget: bool = False


class _SdkCancellation:
    """Project the caller's raw cancel signal onto the SDK's full token face.

    Same adapter shape as ``gap_consult._SdkCancellation`` and
    ``generated_question_contribution._CancellationToken``, with one deliberate
    difference: this point runs under a parse job, not an Ask request, so it
    raises the cancellation *base* class rather than ``AskCancelled``.  The
    monotonic cache is kept for the same reason as there — an
    ``is_set()``/``raise_if_cancelled()`` sequence must not be turnable into a
    hostile second-read type change.
    """

    __slots__ = ("_token", "_observed_cancelled")

    def __init__(self, token: object) -> None:
        self._token = token
        self._observed_cancelled = False

    def is_set(self) -> bool:
        if self._observed_cancelled:
            return True
        if self._token is None:
            return False
        cancelled = self._token.is_set()
        if type(cancelled) is not bool:
            raise TypeError("malformed cancellation state")
        if cancelled:
            self._observed_cancelled = True
        return cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            _raise_cancelled(self._token)


class _AssetReader:
    """Turn-scoped, size-capped reader over pre-resolved element images.

    It holds file *paths* the calling thread already resolved, never a
    repository or an asset service, so a contributor's worker thread cannot
    reach the database through it.  Every failure mode — an unknown or forged
    ref, an element with no image, an over-sized or unreadable file, a read
    after this contributor's turn ended — is the same answer, ``None``, and it
    never raises.

    One instance per contributor turn.  The shared location mapping is what
    every turn has in common; the ``_closed`` flag is what it must not share,
    because "the call this reader was issued for has ended" has to become true
    for contributor A the moment A's turn is over — including when A left a
    background thread behind and B is now running.
    """

    __slots__ = ("_by_identity", "_max_bytes", "_closed")

    def __init__(
        self, by_identity: Mapping[int, tuple[ElementRef, str]], max_bytes: int
    ) -> None:
        self._by_identity = by_identity
        self._max_bytes = max_bytes
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def read(self, ref: object) -> bytes | None:
        try:
            if self._closed:
                return None
            entry = self._by_identity.get(id(ref))
            # The second half is defensive and unreachable by construction —
            # every ref in this mapping is held alive by the host for the whole
            # call, so an id cannot be recycled underneath it — and therefore
            # carries no test of its own.  It stays because the cost is one
            # pointer comparison and the failure it guards against (handing a
            # plugin an image belonging to a different element) is silent.
            if entry is None or entry[0] is not ref:
                return None
            with open(entry[1], "rb") as handle:
                # One byte past the cap: enough to *detect* an over-sized image
                # without ever holding more than the cap plus one byte.
                payload = handle.read(self._max_bytes + 1)
            if type(payload) is not bytes or len(payload) > self._max_bytes:
                return None
            # Re-read after the I/O: the turn may have ended while this read
            # was in flight, and "valid only during the turn" has to hold on
            # the far side of the read too.
            return None if self._closed else payload
        except Exception:  # noqa: BLE001 — the reader answers, it never raises
            return None


class SourceElementEnricherHost:
    """Run element-enrichment contributors under a hard wall-clock deadline."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Source element enricher host requires a frozen registry"
            )
        versions = {
            manifest.id: manifest.version for manifest in registry.manifests()
        }
        registrations: list[_Registration] = []
        for item in registry.contributions(SOURCE_ELEMENT_ENRICHER_POINT):
            declaration = item.contribution.declaration
            implementation = item.contribution.implementation
            version = versions.get(item.plugin_id, "")
            if (
                declaration.kind is not ContributionKind.CONTRIBUTOR
                or not callable(getattr(implementation, "enrich", None))
            ):
                raise ExtensionRegistryError(
                    f"Source element enricher {declaration.id!r} does not "
                    "implement the contributor contract"
                )
            # Provenance is frozen here rather than checked at admission time
            # because it is pure topology: an id or version that cannot be
            # persisted safely is a deployment mistake an operator can fix, and
            # failing loudly at composition is how they find out — rather than
            # every parsed source silently dropping this whole batch.
            if not valid_element_enrichment_owner(
                item.plugin_id, version, declaration.id
            ):
                raise ExtensionRegistryError(
                    f"Source element enricher {declaration.id!r} has "
                    "provenance that cannot be persisted safely"
                )
            registrations.append(
                _Registration(
                    item.plugin_id, version, declaration.id, implementation
                )
            )
        self._registry = registry
        self._registrations = tuple(registrations)
        self._event_sink = event_sink
        self._clock = clock

    def has_contributions(self) -> bool:
        """Read the startup-frozen topology.  No clock, no I/O, no request state."""

        return bool(self._registrations)

    def enrich_application(
        self,
        call_context: ElementEnrichmentCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[ElementEnrichmentPatch, ...]:
        # Empty topology is a strict no-op: no validation, no clock read, no
        # probe call, no event, no file opened.  A deployment without element
        # enrichers pays exactly nothing here.
        if not self._registrations:
            return ()
        if type(call_context) is not ElementEnrichmentCallContext:
            return ()
        if not _valid_call(call_context):
            return ()
        sink = event_sink if event_sink is not None else self._event_sink
        # The CALLER's own lease is checked here, on the calling thread — the
        # only thread that can observe it, because both backends answer this
        # probe from thread-local (SQLite) or ContextVar (PostgreSQL) state.
        # Holding one while a plugin runs would let a contributor's latency sit
        # on top of an open database connection for the whole point.
        if not _connection_clear(call_context.connection_probe):
            _emit(
                sink,
                plugin_id="",
                contribution_id="",
                status="unavailable",
                reason_code="connection_lease_held",
                duration_ms=0,
            )
            return ()
        prepared = _prepare(call_context)
        if prepared is None:
            return ()
        refs, views, assets, image_count = prepared
        return self._run(call_context, sink, refs, views, assets, image_count)

    def _run(
        self,
        call: ElementEnrichmentCallContext,
        sink: Callable[[dict[str, object]], None] | None,
        refs: tuple[ElementRef, ...],
        views: tuple[ElementView, ...],
        assets: Mapping[int, tuple[ElementRef, str]],
        image_count: int,
    ) -> tuple[ElementEnrichmentPatch, ...]:
        accepted: list[ElementEnrichmentPatch] = []
        remaining = call.max_proposals
        for item in self._registrations:
            if remaining <= 0:
                break
            _raise_if_cancelled(call.cancellation)
            started = _safe_clock(self._clock)
            if not _deadline_open(started, call.deadline_monotonic):
                _emit(
                    sink,
                    plugin_id=item.plugin_id,
                    contribution_id=item.contribution_id,
                    status="unavailable",
                    reason_code="element_enricher_budget_exhausted",
                    duration_ms=0,
                )
                break
            # A fresh reader per turn, sharing only the resolved locations.
            # The ``finally`` is what makes the lifetime real: it closes this
            # reader on every exit — a clean return, a timeout, an abandoned
            # worker, a propagating cancellation — so a contributor that leaves
            # a thread running cannot keep reading images while the *next*
            # contributor has the floor.
            reader = _AssetReader(assets, call.max_asset_bytes)
            context = ElementEnrichmentContext(
                views,
                reader,
                _SdkCancellation(call.cancellation),
                ElementEnrichmentBudget(
                    remaining,
                    call.max_metadata_bytes,
                    call.max_description_chars,
                    call.max_asset_bytes,
                    call.deadline_monotonic,
                ),
            )
            try:
                attempt = self._execute(item, call, context, image_count)
            finally:
                reader.close()
            if attempt.reason is not None:
                _emit(
                    sink,
                    plugin_id=item.plugin_id,
                    contribution_id=item.contribution_id,
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
            admitted: tuple[ElementEnrichmentPatch, ...] | None = None
            reason = _REASON_SHAPE
            if _valid_result(result):
                if result.status is ExtensionResultStatus.UNAVAILABLE:
                    # An UNAVAILABLE result contributes nothing, items or not:
                    # that status is the contributor's own statement that it
                    # could not serve this call, so persisting the payload it
                    # disclaimed would put material into the notebook the
                    # plugin itself says is not an answer.  PARTIAL stays
                    # admitted — it means "some of it", not "none of it".
                    admitted, reason = (), ""
                else:
                    admitted, reason = self._admitted(
                        result.items, item, refs, remaining, call
                    )
            if admitted is None:
                _emit(
                    sink,
                    plugin_id=item.plugin_id,
                    contribution_id=item.contribution_id,
                    status="invalid",
                    reason_code=reason,
                    duration_ms=_elapsed_ms(self._clock, started),
                )
                continue
            accepted.extend(admitted)
            remaining -= len(admitted)
            _emit(
                sink,
                plugin_id=item.plugin_id,
                contribution_id=item.contribution_id,
                status=result.status.value,
                reason_code=_failure_code(result),
                duration_ms=_elapsed_ms(self._clock, started),
                count=len(admitted),
            )
        return tuple(accepted)

    def _execute(
        self,
        item: _Registration,
        call: ElementEnrichmentCallContext,
        context: ElementEnrichmentContext,
        image_count: int,
    ) -> _Attempt:
        """Decide availability and run one contributor on one daemon thread.

        The availability decision runs on the worker, inside the deadline, for
        the same reason ``enrich`` does: a plugin at this point supplies its
        own probe, so a slow or hung probe spends the parse's budget exactly as
        effectively as a slow or hung ``enrich``.  The post-decision connection
        re-check moves onto the worker with it, and must: ``is_connection_held``
        answers from thread-local (SQLite) or ContextVar (PostgreSQL) state, so
        the only thread that can observe a lease *the decision* took is the
        thread the decision ran on.

        The thread is started WITHOUT ``contextvars.copy_context()`` and that
        omission is load-bearing, not an oversight — do not "fix" it.  A fresh
        empty Context is precisely what keeps a plugin from inheriting this
        job's ambient state simply by virtue of running underneath it.

        A thread pool is likewise wrong here: one hung plugin would occupy a
        shared worker forever and turn a single plugin's fault into a
        deployment-wide outage.  The registered cost of a private daemon thread
        is that a genuinely hung plugin leaks one thread per affected parse.
        """
        deadline = call.deadline_monotonic
        cell = _WorkerCell()

        def _target() -> None:
            try:
                availability = self._registry.availability(
                    item.contribution_id,
                    ElementEnrichmentAvailabilityContext(
                        item.plugin_id,
                        item.contribution_id,
                        len(context.elements),
                        image_count,
                        deadline,
                    ),
                )
                if not _connection_clear(call.connection_probe):
                    cell.reason = "connection_lease_held"
                    cell.ends_budget = True
                elif availability.status is not AvailabilityStatus.AVAILABLE:
                    cell.reason = availability.reason_code
                else:
                    cell.result = item.implementation.enrich(context)
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
            # finish included: "past the deadline" is a fact about the clock,
            # not about which join slice happened to notice.
            if _is_cancelled(call.cancellation):
                _raise_cancelled(call.cancellation)
            if not _deadline_open(_safe_clock(self._clock), deadline):
                return _Attempt("element_enricher_timeout", ends_budget=True)
            if finished:
                break
        if cell.failed or not cell.done:
            return _Attempt("element_enricher_failed")
        if cell.reason is not None:
            return _Attempt(cell.reason, ends_budget=cell.ends_budget)
        return _Attempt(result=cell.result)

    def _admitted(
        self,
        items: tuple[object, ...],
        item: _Registration,
        refs: tuple[ElementRef, ...],
        remaining: int,
        call: ElementEnrichmentCallContext,
    ) -> tuple[tuple[ElementEnrichmentPatch, ...] | None, str]:
        """Core-owned admission for one contribution: all of it, or none of it.

        Returns ``(patches, "")`` or ``(None, reason_code)``.  ``None`` means
        "discard this contribution's whole batch".  Unlike the gap-consult
        point, which drops individual malformed suggestions, this one persists
        into a notebook's own element metadata under the contribution's name,
        so a half-admitted batch would be provenance that claims more than the
        plugin actually produced.
        """

        if len(items) > remaining:
            return None, _REASON_BUDGET
        by_identity = {id(ref): index for index, ref in enumerate(refs, start=1)}
        seen: set[int] = set()
        patches: list[ElementEnrichmentPatch] = []
        # Per contribution, not per point: the budget answers "how much may
        # THIS contribution add to this source", which is also what the plugin
        # was told in its own ``ElementEnrichmentBudget``.
        byte_count = 0
        for candidate in items:
            if type(candidate) is not ElementEnrichmentCandidate:
                return None, _REASON_SHAPE
            ordinal = by_identity.get(id(candidate.element))
            if ordinal is None or refs[ordinal - 1] is not candidate.element:
                return None, _REASON_REF
            if ordinal in seen:
                return None, _REASON_DUPLICATE
            description = _normalized_description(
                candidate.description, call.max_description_chars
            )
            if description is None:
                return None, _REASON_DESCRIPTION
            try:
                metadata = thaw_element_enrichment_metadata(
                    candidate.metadata,
                    # Every persisted node costs at least one byte, so the byte
                    # budget is also an upper bound on how many nodes could
                    # possibly fit — which makes it a bound on that walk.
                    max_nodes=call.max_metadata_bytes,
                )
                if type(metadata) is not dict:
                    raise TypeError("element enrichment metadata is not a mapping")
                byte_count += persisted_element_enrichment_size(
                    plugin_id=item.plugin_id,
                    plugin_version=item.plugin_version,
                    contribution_id=item.contribution_id,
                    metadata=metadata,
                    description=description,
                )
            except Exception:  # noqa: BLE001 — malformed metadata is a drop
                return None, _REASON_METADATA
            if byte_count > call.max_metadata_bytes:
                return None, _REASON_BUDGET
            seen.add(ordinal)
            patches.append(
                ElementEnrichmentPatch(
                    ordinal,
                    item.plugin_id,
                    item.plugin_version,
                    item.contribution_id,
                    metadata,
                    description,
                )
            )
        return tuple(patches), ""


def _prepare(
    call: ElementEnrichmentCallContext,
) -> tuple[
    tuple[ElementRef, ...],
    tuple[ElementView, ...],
    Mapping[int, tuple[ElementRef, str]],
    int,
] | None:
    """Mint this call's refs and views, and resolve which of them have images.

    ``asset_id`` is exposed on a view only when the caller actually resolved a
    location for it, because the SDK's promise is "non-empty means there IS an
    image to read" — an unresolvable asset would make that promise false and
    send every contributor down a read path that can only answer ``None``.
    """

    refs: list[ElementRef] = []
    views: list[ElementView] = []
    assets: dict[int, tuple[ElementRef, str]] = {}
    for expected, element in enumerate(call.elements, start=1):
        if not _valid_envelope(element, expected):
            return None
        ref = ElementRef(object())
        location = _location(call.asset_locations, element.asset_id)
        if location is not None:
            assets[id(ref)] = (ref, location.path)
        refs.append(ref)
        views.append(
            ElementView(
                ref,
                element.element_type,
                element.location_label,
                element.text,
                element.caption,
                element.description,
                "" if location is None else element.asset_id,
                ""
                if location is None
                else (location.mime or element.asset_mime),
            )
        )
    return tuple(refs), tuple(views), assets, len(assets)


def _valid_envelope(element: object, expected: int) -> bool:
    return (
        type(element) is ParsedElementEnvelope
        and type(element.ordinal) is int
        # Positional identity: a patch carries an ordinal back, and the caller
        # applies it by position, so the two must already agree here.
        and element.ordinal == expected
        and all(
            type(value) is str
            for value in (
                element.element_type,
                element.location_label,
                element.text,
                element.caption,
                element.description,
                element.asset_id,
                element.asset_mime,
            )
        )
    )


def _location(
    locations: Mapping[str, ElementAssetLocation], asset_id: str
) -> ElementAssetLocation | None:
    if not asset_id:
        return None
    try:
        location = locations.get(asset_id)
    except Exception:  # noqa: BLE001 — an unreadable mapping means "no image"
        return None
    if (
        type(location) is not ElementAssetLocation
        or type(location.path) is not str
        or not location.path
        or type(location.mime) is not str
    ):
        return None
    return location


def _valid_call(call: ElementEnrichmentCallContext) -> bool:
    return (
        type(call.elements) is tuple
        and bool(call.elements)
        and isinstance(call.asset_locations, Mapping)
        and type(call.max_proposals) is int
        and call.max_proposals >= 1
        and type(call.max_metadata_bytes) is int
        and call.max_metadata_bytes >= 1
        and type(call.max_description_chars) is int
        and call.max_description_chars >= 1
        and type(call.max_asset_bytes) is int
        and call.max_asset_bytes >= 1
        and _valid_deadline(call.deadline_monotonic)
    )


def _normalized_description(value: object, limit: int) -> str | None:
    """Canonical line endings, then the character rail.  ``None`` rejects.

    ``\\r\\n`` becomes ``\\n`` and a lone ``\\r`` is dropped: a plugin that
    built its description on Windows, or pasted a model's CRLF output, is
    proposing the same text as one that did not, and the persisted value must
    not depend on which.  Normalization can only shorten, so the length rail
    is applied to the raw value first and stays an O(1) rejection.
    """

    if type(value) is not str or len(value) > limit:
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "")
    return normalized if _printable_description(normalized) else None


def _printable_description(value: str) -> bool:
    return all(
        char.isprintable() or char in _DESCRIPTION_EXTRA_CHARS for char in value
    )


def _valid_failure(value: object) -> bool:
    return value is None or (
        type(value) is ExtensionFailure
        and type(value.kind) is ExtensionFailureKind
        and _stable_code(value.code)
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


def _raise_cancelled(cancellation: object) -> None:
    """Propagate the caller's own cancellation exception when it has one.

    A token that cannot raise its own — missing the method, or raising
    something else entirely — still cancels: the one thing this host must
    never do with an observed cancellation is continue, and it must not turn
    one into an unrelated exception escaping a fail-open host either.
    """

    try:
        raiser = getattr(cancellation, "raise_if_cancelled", None)
        if callable(raiser):
            raiser()
    except CoreCancellation:
        raise
    except Exception:  # noqa: BLE001 — a malformed token is still cancellation
        pass
    raise CoreCancellation()


def _raise_if_cancelled(cancellation: object) -> None:
    if _is_cancelled(cancellation):
        _raise_cancelled(cancellation)


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

    Never an element's text, a caption, a description, an asset id, a file
    path, a settings value, or an exception message.  A reason code that is
    not stable — a plugin is free to return any string — is dropped to the
    empty string rather than forwarded.
    """

    if sink is None:
        return
    try:
        sink(
            {
                "kind": _EVENT_KIND,
                "plugin_id": plugin_id,
                "contribution_id": contribution_id,
                "status": status,
                "reason_code": reason_code if _stable_code(reason_code) else "",
                "duration_ms": duration_ms,
                "count": count,
            }
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the parse
        pass


__all__ = ["SourceElementEnricherHost"]
