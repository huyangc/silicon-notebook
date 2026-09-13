"""Deadline, cancellation and admission plumbing shared by extension hosts.

Extracted from ``extensions/gap_consult.py`` when ``ask.reflect_action`` got a
host of its own (design document
``docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md`` §五,
whose host correspondence table says "reuse verbatim, one shared module, two
imports").  Nothing here is new behaviour: every constant keeps its value and
every function keeps its body, so the gap-consult host reads exactly as it did
before this module existed.

Why share rather than copy: each of these is a *security* rail, not a
convenience.  A second, independently maintained copy of "cut before you
strip", "http/https only", "an unreadable clock is not a plugin verdict" or
"cancellation is the one signal a host never swallows" is precisely the shape
in which one of the two copies quietly loses a rule — and the loss would show
up as a hostile plugin payload reaching a reader, not as a failing test.

What deliberately stays behind in each host: the point-specific pieces — the
call-context validation, the result-shape check, the per-item admission loop
and the event receipt.  Those name their own types and their own limits, so
folding them together would mean a contract with axes neither point has.
"""
from __future__ import annotations

import math
import re
from typing import Callable
from urllib.parse import urlparse

from app.domain.cancellation import AskCancelled


STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
# Length precedes the regex wherever a plugin-authored code is validated: the
# scans run on the request thread after the worker already spent the deadline,
# so an arbitrarily long preconstructed `code` must cost O(1) to reject, not a
# full-string scan (codex #584 R9).  64 comfortably covers every stable code
# the hosts and the SDK emit.
STABLE_CODE_MAX_CHARS = 64

# The main thread never blocks longer than this without re-reading cancellation
# and the deadline, so both stay responsive against an uncooperative plugin.
JOIN_SLICE_SECONDS = 0.05
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
# How many items admission will EXAMINE, as a multiple of how many it could
# still accept.  A contributor's payload is unbounded input: without a cap, a
# tuple of a million rejects makes core walk all million of them on the
# request's critical path — *after* the deadline that was supposed to bound
# this contributor has already been honoured, so the budget buys nothing.
# The factor is deliberately generous rather than tight: the point is to deny
# an unbounded scan, not to police sloppiness, so a plugin whose good items sit
# behind a handful of malformed ones still gets them admitted.
ADMISSION_SCAN_FACTOR = 20
# Re-read cancellation every this many examined items.  Belt and braces: the
# scan is short, but cancellation is the one signal a host never fails open on,
# and it is set from another thread — it can flip after the join loop's own
# read and before the last item is examined.
ADMISSION_CANCEL_STRIDE = 8
# Slack allowed above a field's own limit before the raw contributor string is
# cut.  This cut lands on PLUGIN OUTPUT, never on user data — the "never
# silently truncate what a user typed" rail governs write and render paths —
# and it exists so `str.strip()` can never be pointed at a 50 MB string.  The
# headroom is wide enough that every realistic value stays byte-identical to an
# unbounded strip; only a value whose LEADING whitespace alone exceeds it now
# reads as empty, which drops the item — the safe direction.
ADMISSION_SLICE_HEADROOM_CHARS = 1024


class SdkCancellation:
    """Project the caller's raw cancel event onto the SDK's full token face.

    An extension call context types ``cancellation`` as the SDK
    ``CancellationToken`` protocol, whose face is ``is_set()`` **and**
    ``raise_if_cancelled()`` — but the production caller hands a host a raw
    ``threading.Event``, which only has the first half.  A compliant plugin
    calling ``raise_if_cancelled()`` on the raw event would AttributeError,
    which a worker's fail-open guard then records as a plugin failure: a
    well-behaved contributor loses its results for following the contract
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


def stable_code(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= STABLE_CODE_MAX_CHARS
        and bool(STABLE_CODE.fullmatch(value))
    )


def clean_text(value: object, limit: int) -> str | None:
    if type(value) is not str:
        return None
    # Cut BEFORE stripping.  `str.strip()` allocates a full copy of whatever it
    # is handed, so stripping first is an unbounded allocation driven by plugin
    # output; the bound has to come first for it to be a bound at all.
    return value[: limit + ADMISSION_SLICE_HEADROOM_CHARS].strip()[:limit]


def clean_url(value: object, limit: int) -> str | None:
    if type(value) is not str:
        return None
    # Same cut-before-strip rule as `clean_text`, and it does not weaken the
    # rejection below: anything longer than the limit plus the headroom is
    # still longer than the limit after the cut, so it is dropped exactly as it
    # was before — never shortened into a different destination.
    url = value[: limit + ADMISSION_SLICE_HEADROOM_CHARS].strip()
    # A URL is the one field that must NOT be truncated to fit: a shortened URL
    # is a different, silently wrong destination.  Over-long ones are rejected.
    if not url or len(url) > limit:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 — malformed input is a drop, not a crash
        return None
    if parsed.scheme not in ALLOWED_URL_SCHEMES or not parsed.netloc:
        return None
    return url


def is_cancelled(cancellation: object) -> bool:
    try:
        probe = getattr(cancellation, "is_set", None)
        if not callable(probe):
            return False
        return probe() is True
    except Exception:  # noqa: BLE001 — an unreadable token is not cancellation
        return False


def raise_if_cancelled(cancellation: object) -> None:
    if is_cancelled(cancellation):
        raise AskCancelled()


def safe_clock(clock: Callable[[], float]) -> float | None:
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


def valid_deadline(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and value > 0


def deadline_open(now: float | None, deadline: float) -> bool:
    # An unreadable clock is an observability problem, never a reason to
    # abandon work the deployment asked for.
    return now is None or now <= deadline


def elapsed_ms(clock: Callable[[], float], started: float | None) -> int:
    if started is None:
        return 0
    ended = safe_clock(clock)
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


__all__ = [
    "ADMISSION_CANCEL_STRIDE",
    "ADMISSION_SCAN_FACTOR",
    "ADMISSION_SLICE_HEADROOM_CHARS",
    "ALLOWED_URL_SCHEMES",
    "JOIN_SLICE_SECONDS",
    "STABLE_CODE",
    "STABLE_CODE_MAX_CHARS",
    "SdkCancellation",
    "clean_text",
    "clean_url",
    "deadline_open",
    "elapsed_ms",
    "is_cancelled",
    "raise_if_cancelled",
    "safe_clock",
    "stable_code",
    "valid_deadline",
]
