"""The ``ask.gap_consult`` contributor: arXiv pointers when a run came up thin.

What core hands this contributor is one bounded :class:`GapConsultQuery` — a
question string and at most two gap phrases — and a monotonic deadline.  What
it may not assume is anything about the thread it runs on: core runs the
availability probe and :meth:`ArxivGapConsultContributor.consult` together on a
private daemon thread with no copied context, so ``ContextVar`` and
thread-local state are both empty here, by design.

**Everything in :meth:`consult` is ordered by what it costs to refuse.**  The
cheapest refusals come first, so a deployment that has not enabled outbound
consultation, or a call with no suggestion slots left, pays nothing at all —
not a politeness slot, not a round trip, not a term scan.  In order:

1. not configured / ``consult_enabled`` false;
2. already cancelled;
3. no suggestion slots left for this call;
4. no Latin search terms in the question or the gap phrases;
5. not enough of the deadline left to finish inside it;
6. the request itself.

**Step 5 is the one that is easy to get wrong.**  ``acquire_slot`` may sleep up
to the budget it is given, and the HTTP call may then take up to
``timeout_seconds``.  A budget of "everything that is left" therefore returns an
answer exactly *on* the deadline — and core's join loop re-reads the deadline on
every 50 ms slice, so an answer that lands on it is read by nobody.  The gate
below refuses unless a *worst case* fits: a full politeness interval, a full
timeout, and a margin for the return trip.  The budget handed to the throttle
then subtracts the timeout and the margin back out, so the two agree by
construction.

The visible consequence, and it is the intended one: with this plugin's own
defaults (3 s politeness, 10 s timeout) and core's default 4 s gap-consult
deadline, this contributor never fires.  A deployment that wants gap
consultation must say so twice — ``consult_enabled = true`` *and* a
``ASK_GAP_CONSULT_TIMEOUT_SECONDS`` large enough for arXiv's own politeness
terms.  Refusing loudly at configuration time beats sending a request that
cannot be waited for.

**Cancellation returns; it does not raise.**  Core wraps the whole call in
``except BaseException`` and files anything thrown as ``gap_consult_failed``, so
raising here would relabel "the user pressed stop" as "the plugin broke".  Core
checks cancellation itself on every join slice, so returning is both honest and
sufficient.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable

from app.extension_sdk import (
    ContributorResult,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    GapConsultExtensionContext,
    GapConsultQuery,
    GapSuggestion,
)

from . import client as arxiv_client
from .atom import ArxivPaper
from .settings import ArxivSearchSettings, egress_allowed, search_kwargs

# Plugin-private bounds; registered for operators in the package README.
#
# The margin covers everything between "the response bytes arrived" and "core
# read the return value": parsing the feed, mapping it, and the up-to-50 ms
# slice core's join loop is sleeping in when the worker finishes.
CONSULT_RETURN_MARGIN_SECONDS = 0.25
SOURCE_LABEL = "arXiv"

# A Latin word of two or more characters, allowing the punctuation that shows
# up inside real identifiers (``GPT-4``, ``C++``, ``e.g``).
_LATIN_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+.#-]+")
_TERM_EDGE = ".-+#"

# English function words carry no retrieval signal and would crowd out the
# terms that do, since the query is capped at ``MAX_QUERY_TERMS``.  Kept small
# and plugin-private on purpose: this is a keyword extractor for one upstream,
# not a linguistics contribution.
_STOPWORDS = frozenset(
    {
        "about", "after", "all", "also", "and", "any", "are", "based", "been",
        "before", "being", "between", "both", "but", "can", "could", "did",
        "does", "doing", "done", "each", "even", "for", "from", "had", "has",
        "have", "how", "into", "its", "just", "may", "might", "more", "most",
        "much", "must", "not", "now", "one", "only", "other", "our", "out",
        "over", "same", "should", "since", "some", "such", "than", "that",
        "the", "their", "them", "then", "there", "these", "they", "this",
        "those", "through", "under", "use", "used", "using", "very", "was",
        "were", "what", "when", "where", "which", "while", "who", "why",
        "will", "with", "would", "you", "your",
    }
)


class ArxivGapConsultContributor:
    """One ``ask.gap_consult`` contributor bound to this plugin's settings.

    Settings arrive through a zero-argument callable rather than being captured
    at construction: ``configure`` runs after the bundle object exists, so a
    snapshot taken in ``__init__`` would be ``None`` forever.
    """

    def __init__(
        self, settings_source: Callable[[], ArxivSearchSettings | None]
    ) -> None:
        self._settings_source = settings_source

    # -- availability -------------------------------------------------------

    def settings(self) -> ArxivSearchSettings | None:
        """The bound settings, or ``None`` when this plugin has none.

        Defensive about the callable's answer for the same reason core is
        defensive about a plugin's: a bundle whose ``configure`` never ran
        yields ``None``, and anything that is not this model is not settings.
        """

        try:
            settings = self._settings_source()
        except Exception:  # noqa: BLE001 — an unconfigured plugin, not a crash
            return None
        return settings if isinstance(settings, ArxivSearchSettings) else None

    def consult_enabled(self) -> bool:
        """I/O-free, and the probe's whole question.  See :mod:`.bundle`."""

        settings = self.settings()
        return settings is not None and settings.consult_enabled

    # -- the contribution ---------------------------------------------------

    def consult(
        self, context: GapConsultExtensionContext
    ) -> ContributorResult[GapSuggestion]:
        settings = self.settings()
        if settings is None or not settings.consult_enabled:
            # Defence in depth: the availability probe already refused this
            # call.  Keeping the check means a host that ever stopped
            # consulting the probe cannot turn a disabled plugin into an
            # outbound request.
            return _unavailable(ExtensionFailureKind.DISABLED, "consult_disabled")

        cancellation = context.cancellation
        if cancellation is not None and cancellation.is_set():
            return _unavailable(ExtensionFailureKind.CANCELLED, "arxiv_cancelled")

        limit = min(context.max_suggestions, settings.consult_max_suggestions)
        if limit <= 0:
            # Nothing to give even on success.  ``search`` would short-circuit
            # on a non-positive limit anyway, but leaving early here means the
            # term scan and the deadline arithmetic below are not paid for a
            # result that has no room to land.
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_no_suggestion_budget"
            )

        terms = _query_terms(context.query)
        if not terms:
            # arXiv is a Latin-keyword index.  A question written entirely in
            # Chinese, with gap phrases to match, would return nothing at all —
            # so sending it would spend a politeness slot and a round trip to
            # learn what is already known here.  Both the question wording and
            # every gap phrase are scanned, because a gap phrase is often the
            # technical term the question itself paraphrased away.
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_no_latin_terms"
            )

        remaining = context.deadline_monotonic - time.monotonic()
        floor = (
            settings.politeness_interval_seconds
            + settings.timeout_seconds
            + CONSULT_RETURN_MARGIN_SECONDS
        )
        if remaining < floor:
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_budget_too_small"
            )
        budget = remaining - settings.timeout_seconds - CONSULT_RETURN_MARGIN_SECONDS

        try:
            papers = arxiv_client.search(
                " ".join(terms),
                **search_kwargs(settings, limit=limit, budget_seconds=budget),
            )
        except arxiv_client.ArxivThrottled:
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_throttled"
            )
        except Exception:  # noqa: BLE001 — one stable code, never the text
            return _unavailable(
                ExtensionFailureKind.FAILED, "arxiv_upstream_failed"
            )

        suggestions = tuple(
            _suggestion(paper)
            for paper in papers
            if egress_allowed(paper.pdf_url, settings.base_url)
        )
        return ContributorResult(
            items=suggestions, status=ExtensionResultStatus.AVAILABLE
        )


def _unavailable(
    kind: ExtensionFailureKind, code: str
) -> ContributorResult[GapSuggestion]:
    """No suggestions, plus the stable reason core writes to its own event."""

    return ContributorResult(
        items=(),
        status=ExtensionResultStatus.UNAVAILABLE,
        failure=ExtensionFailure(kind=kind, code=code),
    )


def _suggestion(paper: ArxivPaper) -> GapSuggestion:
    """Map one record onto core's four-field suggestion.

    ``url`` is the PDF direct link, never the abstract page: core does not
    fetch the URL to find out what it is, and the import endpoint a reader
    might press probes exactly the address it is given.
    """

    return GapSuggestion(
        title=paper.title,
        url=paper.pdf_url,
        summary=paper.summary,
        source_label=SOURCE_LABEL,
    )


def _query_terms(query: GapConsultQuery) -> tuple[str, ...]:
    """Latin search terms from the question *and* every gap phrase.

    Both halves are scanned because they fail in opposite directions: a
    question can be entirely in Chinese while its gap phrase is the English
    term of art the retrieval never covered, and a question full of ordinary
    English words can be carried by a single gap phrase naming the method.
    Dropping either half throws away the case the other one cannot serve.
    """

    seen: set[str] = set()
    terms: list[str] = []
    for text in (query.question, *query.gaps):
        if not isinstance(text, str):
            continue
        for match in _LATIN_TERM.finditer(text):
            term = match.group(0).strip(_TERM_EDGE).lower()
            if len(term) < 2 or term in _STOPWORDS or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= arxiv_client.MAX_QUERY_TERMS:
                return tuple(terms)
    return tuple(terms)
