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
    GapConsultExtensionContext,
    GapConsultQuery,
    GapSuggestion,
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

        budget = deadline_budget(
            settings, context.deadline_monotonic, now=time.monotonic()
        )
        if budget is None:
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_budget_too_small"
            )

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
    )


def _query_terms(query: GapConsultQuery) -> tuple[str, ...]:
    """Latin search terms from the question *and* every gap phrase.

    Both halves are scanned because they fail in opposite directions: a
    question can be entirely in Chinese while its gap phrase is the English
    term of art the retrieval never covered, and a question full of ordinary
    English words can be carried by a single gap phrase naming the method.
    Dropping either half throws away the case the other one cannot serve.

    Which strings to scan is this contributor's decision and stays here; *how*
    a string yields terms belongs to :func:`.terms.latin_terms`, shared with
    the reflect-action contributor — see that module for why the scan itself
    must not exist twice.
    """

    return latin_terms(query.question, *query.gaps)
