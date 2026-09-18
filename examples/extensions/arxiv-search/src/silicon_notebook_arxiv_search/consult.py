"""The ``ask.gap_consult`` contributor: arXiv pointers when a run came up thin.

What core hands this contributor is one bounded :class:`GapConsultQuery` — a
question string, at most two gap phrases, and queries selected for arXiv —
plus a monotonic deadline.  What
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
4. no Latin search terms in the selected source phrases;
5. not enough of the deadline left to finish inside it;
6. the request itself.

**Step 5 is the one that is easy to get wrong**, and it is not written here:
``settings.deadline_budget`` owns it, because the reflect-action contributor
(:mod:`.reflect_search`) runs under a second core host with the same join-loop
shape and has to answer the same question.  Read that function for why a
budget of "everything that is left" is an answer nobody reads.

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

import time
from collections.abc import Callable

from app.extension_sdk import (
    ContributorResult,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_ACTUAL_QUERY_MAX_CHARS,
    GapConsultExtensionContext,
    GapConsultQuery,
    GapSuggestion,
    SourceDescriptor,
)

from . import client as arxiv_client
from .atom import SOURCE_LABEL, ArxivPaper
from .settings import (
    ArxivSearchSettings,
    deadline_budget,
    egress_allowed,
    search_kwargs,
)
from .terms import latin_terms


ARXIV_SOURCE_ID = "arxiv"


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

    def describe_sources(self) -> tuple[SourceDescriptor, ...]:
        """Offer the arXiv index to core's source-selection model."""

        return (SourceDescriptor(
            source_id=ARXIV_SOURCE_ID,
            display_name=SOURCE_LABEL,
            languages=("en",),
            content_type="research papers",
            query_advice="Use English scientific terms suited to arXiv papers.",
        ),)

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
        search_query = _search_query(terms)
        if not search_query:
            # arXiv is a Latin-keyword index.  A selected query with no Latin
            # terms would spend a politeness slot and a round trip for nothing.
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_no_latin_terms"
            )

        budget = deadline_budget(
            settings, context.deadline_monotonic, now=time.monotonic()
        )
        if budget is None:
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_budget_too_small"
            )

        try:
            papers = arxiv_client.search(
                search_query,
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
            _suggestion(paper, actual_query=search_query)
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


def _suggestion(paper: ArxivPaper, *, actual_query: str = "") -> GapSuggestion:
    """Map one record onto core's bounded suggestion fields.

    ``url`` is the PDF direct link, never the abstract page: core does not
    fetch the URL to find out what it is, and the import endpoint a reader
    might press probes exactly the address it is given.

    ``title``/``summary`` are cut here to core's own
    ``GAP_SUGGESTION_TITLE_MAX_CHARS``/``GAP_SUGGESTION_SUMMARY_MAX_CHARS`` —
    imported (re-exported by ``app.extension_sdk`` from their defining
    module, ``app.domain.gap_consult``), never hand-copied, so the two can
    never drift apart. This is **not** the same bound ``paper.title``/
    ``paper.summary`` were already cut to in :mod:`.atom`: that layer's
    ``TITLE_MAX_CHARS``/``SUMMARY_MAX_CHARS`` are a wider, display-oriented
    in-memory ceiling for the interactive ``/search`` page (see that
    module's docstring) and are no longer sized to match core's gap-
    suggestion limits, so a record reaching this function can still be wider
    than what a suggestion may carry. Core's own admission host
    (``_clean_text`` in ``app.extensions.gap_consult``) would cut an
    over-long value anyway — this plugin is not relying on that as its only
    line of defence, it is choosing to hand over an already-compliant value
    rather than let an unbidden truncation happen a layer away, on a plugin
    output core cannot label with *why* it was shortened. No ellipsis, for
    the same reason :func:`.atom._collapse` uses none: an appended marker
    would be indistinguishable from the record's own text.
    """

    return GapSuggestion(
        title=paper.title[:GAP_SUGGESTION_TITLE_MAX_CHARS],
        url=paper.pdf_url,
        summary=paper.summary[:GAP_SUGGESTION_SUMMARY_MAX_CHARS],
        source_label=SOURCE_LABEL,
        actual_query=actual_query,
    )


def _query_terms(query: GapConsultQuery) -> tuple[str, ...]:
    """Latin terms from selected arXiv phrases, or legacy direct-call input.

    New host calls carry source-specific phrases.  Direct SDK calls that omit
    ``source_queries`` retain the previous question-plus-gaps behavior.

    Which strings to scan is this contributor's decision and stays here; *how*
    a string yields terms belongs to :func:`.terms.latin_terms`, shared with
    the reflect-action contributor — see that module for why the scan itself
    must not exist twice.
    """

    if query.source_queries is not None:
        return latin_terms(*query.source_queries.get(ARXIV_SOURCE_ID, ()))
    return latin_terms(query.question, *query.gaps)


def _search_query(terms: tuple[str, ...]) -> str:
    """Keep the plugin-reported phrase identical to its actual search input."""

    selected: list[str] = []
    length = 0
    for term in terms:
        extra = len(term) + bool(selected)
        if length + extra > GAP_SUGGESTION_ACTUAL_QUERY_MAX_CHARS:
            break
        selected.append(term)
        length += extra
    return " ".join(selected)
