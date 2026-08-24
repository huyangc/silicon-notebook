"""Politeness-throttled arXiv fetching.

Like :mod:`.atom` this module knows arXiv and nothing else — no ``app.*``
import, and no dependency on the plugin's own settings model either, so the
transport stays replaceable on its own.  Callers map deployment settings onto
the keyword arguments of :func:`search`.

**The throttle is the load-bearing part.**  arXiv's API terms ask for at least
three seconds between requests, so requests are serialised process-wide and
spaced by a caller-supplied interval.  What makes that safe to call from a
deadline-bound context is the second argument: :func:`acquire_slot` never
sleeps past the budget it was given.  If the wait needed to honour the interval
would not fit, it releases the lock and answers ``False`` immediately — the
caller then skips this round entirely rather than burning someone else's
latency budget.  The two callers hand it very different budgets:

* an interactive search route can afford ``timeout + interval``;
* gap consultation may only spend ``deadline − now − timeout``, and a refusal
  there means "no suggestions this time", at a cost of zero network calls.

**Registered limitation — the throttle is per process.**  Production pins the
backend to a single worker, so in that deployment it is a global throttle.  A
multi-worker or multi-replica deployment needs external coordination; this
sample deliberately does not ship one.

**Registered limitation — no outbound address policy.**  ``base_url`` is
deployment-configured rather than user input, so this module does not re-check
that the host resolves to a public address the way core's URL ingestion does.
A variant that lets users choose the endpoint must add that check.
"""
from __future__ import annotations

import threading
import time
import urllib.request
from collections.abc import Callable
from urllib.parse import urlencode

from .atom import ArxivPaper, parse_atom

# Plugin-private bounds; see the package README for the registered list.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_TERMS = 8

Fetch = Callable[[str, float, str], bytes]


class ArxivSearchError(RuntimeError):
    """Base class for the two outcomes a caller must tell apart."""


class ArxivThrottled(ArxivSearchError):
    """No politeness slot was available inside the caller's budget."""


class ArxivUpstreamError(ArxivSearchError):
    """The upstream call or its response failed.

    The message carries the originating exception's class and text because that
    is what makes a log line useful.  It is diagnostic only: callers must map
    this to their own fixed user-facing sentence and never put ``str(exc)`` on
    screen.
    """


_THROTTLE = threading.Lock()
_LAST_REQUEST_AT: float | None = None


def acquire_slot(interval_seconds: float, budget_seconds: float) -> bool:
    """Take the right to make one request, or refuse within ``budget_seconds``.

    Returns ``True`` with the throttle held — the caller must pair it with
    exactly one :func:`release_slot`, in a ``finally``.  Returns ``False``
    holding nothing.

    Refusal has two causes, and both are bounded by the budget rather than by
    the interval: another caller held the throttle for longer than the budget,
    or the remaining politeness wait would outlast it.
    """
    started = time.monotonic()
    if not _THROTTLE.acquire(timeout=max(0.0, float(budget_seconds))):
        return False
    try:
        last = _LAST_REQUEST_AT
        wait = 0.0 if last is None else interval_seconds - (time.monotonic() - last)
        if wait > 0.0:
            if budget_seconds <= 0.0:
                # Defensive: an already-exhausted budget must never wait, even
                # though the non-blocking acquire above (``max(0.0, ...)``)
                # can still succeed instantly when the throttle happens to be
                # free.  Spelled out explicitly rather than relying on the
                # ``remaining`` subtraction below to land negative.
                _THROTTLE.release()
                return False
            remaining = budget_seconds - (time.monotonic() - started)
            if wait > remaining:
                # The whole point: skip the request rather than oversleep.
                _THROTTLE.release()
                return False
            time.sleep(wait)
    except BaseException:
        _THROTTLE.release()
        raise
    return True


def release_slot() -> None:
    """Stamp this request's time and hand the throttle on.

    The stamp is taken here, on release, so the interval is measured from the
    end of one request to the start of the next: the lock is held across the
    whole HTTP call, so two requests can never overlap either.
    """
    global _LAST_REQUEST_AT
    _LAST_REQUEST_AT = time.monotonic()
    _THROTTLE.release()


def build_query_url(
    query: str,
    *,
    base_url: str,
    start: int,
    max_results: int,
) -> str:
    """Build one arXiv API query URL.  Pure; raises on an empty term list.

    ``max_results`` is not clamped here — the only ceiling on it is the
    settings model's ``le=20`` bound.  A caller that builds a URL directly
    from this function (bypassing :class:`~.settings.ArxivSearchSettings`
    validation) gets whatever value it passes, floored at 1 below.
    """
    terms = query.split()[:MAX_QUERY_TERMS]
    if not terms:
        raise ValueError("arXiv query has no searchable terms")
    separator = "&" if "?" in base_url else "?"
    parameters = urlencode(
        {
            "search_query": " AND ".join(f"all:{term}" for term in terms),
            "start": max(0, int(start)),
            "max_results": max(1, int(max_results)),
        }
    )
    return f"{base_url}{separator}{parameters}"


def search(
    query: str,
    *,
    base_url: str,
    limit: int,
    budget_seconds: float,
    timeout_seconds: float,
    politeness_interval_seconds: float,
    user_agent: str,
    start: int = 0,
    fetch: Fetch | None = None,
) -> tuple[ArxivPaper, ...]:
    """Run one arXiv query under the politeness throttle.

    ``fetch`` is injectable so callers can be tested without a network — the
    same shape core's own URL probe uses.

    A non-positive ``limit`` returns ``()`` immediately, before touching the
    network or the throttle: :func:`~.atom.parse_atom` would discard whatever
    came back anyway (its own ``limit <= 0`` guard), so spending a politeness
    slot and a round trip on a result nobody wants is pure waste — and, worse,
    it would burn part of a caller's throttle budget for nothing.
    """
    if limit <= 0:
        return ()
    url = build_query_url(
        query, base_url=base_url, start=start, max_results=limit
    )
    dial = fetch or _fetch
    if not acquire_slot(politeness_interval_seconds, budget_seconds):
        raise ArxivThrottled("no arXiv politeness slot inside the budget")
    try:
        payload = dial(url, timeout_seconds, user_agent)
    except Exception as exc:  # noqa: BLE001 — one stable outcome for the caller
        raise ArxivUpstreamError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        release_slot()
    try:
        return parse_atom(payload, limit=limit)
    except Exception as exc:  # noqa: BLE001 — an unparseable feed is upstream's
        raise ArxivUpstreamError(f"{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float, user_agent: str) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES`` of the response body.

    This ceiling bounds network cost and turns a response the endpoint never
    intended to send into an unparseable document, which the caller reports
    as an upstream failure rather than an unbounded read.  It is *not* a
    defence against XML entity expansion — a payload far under this ceiling
    can still declare an expansion factor in the millions once parsed.  See
    :mod:`.atom` for what actually guards against that attack class.
    """
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": user_agent}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_RESPONSE_BYTES)
