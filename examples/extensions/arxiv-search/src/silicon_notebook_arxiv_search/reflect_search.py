"""The ``ask.reflect_action`` contributor: an arXiv search the model may call.

This is the other kind of outbound contribution, and the difference from
:mod:`.consult` is not a detail of timing.  Gap consultation is core's own
decision, made once, after the answer draft is already written, and what it
returns is a *suggestion* beside the answer.  A reflect action is a function
this plugin **lends to the retrieval agent**: its name, its description and its
one parameter are projected into the reflect prompt and schema, and the model
itself decides — mid-run, after the in-library channels have come back empty —
whether to call it and what to type into it.  What comes back is evidence: it
enters synthesis, the answer may quote it with a ``[k]`` citation, and the
reader sees it labelled ``[external · arXiv]`` with an openable link.

**The egress surface is the model's own argument, and only that.**  ``invoke``
reads ``context.arguments["query"]`` and never falls back to
``context.question``.  Core would allow the question — it is in the context —
but a fallback would mean this plugin sending text the model did not choose to
send, on a call the model believed it was scoping itself.  A query with no
Latin word is refused with a stable code rather than quietly re-derived from
something wider.

**Everything in :meth:`invoke` is ordered by what it costs to refuse**, exactly
as in :mod:`.consult`, and for the same reason: a deployment that has not
enabled this action, or a call with no admission slots left, must pay nothing
at all — not a politeness slot, not a round trip, not a term scan.  In order:

1. not configured / ``reflect_search_enabled`` false;
2. already cancelled;
3. no admission slots left for this call;
4. no Latin search terms in the argument;
5. not enough of the deadline left to finish inside it;
6. the request itself.

The visible consequence of step 5, and it is the intended one: with this
plugin's own defaults (3 s politeness, 10 s timeout) and core's default 8 s
reflect-action deadline, this contributor never fires.  Enabling it takes two
settings, the second one core's — see the sample TOML's own warning block.
**That verdict is reached in the availability probe, not only here.**
:meth:`ArxivReflectSearchAction.unavailable_reason` runs the same arithmetic
against the probe's deadline, so a misconfigured deployment does not offer the
action to the model at all (core leaves an event carrying
``reflect_budget_too_small``) instead of putting a function in front of it that
refuses every call — which would spend the model's attention, and a slot of the
run's own action budget, on a channel that cannot work.  Step 5 stays as
defence in depth: ``invoke`` is a public entry point, and a caller that never
asked the probe must not get a request out of it.

**Cancellation returns; it does not raise.**  Core's host files anything
thrown as ``plugin_action_failed``, so raising here would relabel "the user
pressed stop" as "the plugin broke".  The host re-reads cancellation on every
join slice and the reflect loop re-reads its own token right after, so
returning is both honest and sufficient.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from app.extension_sdk import (
    EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS,
    EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS,
    EXTERNAL_EVIDENCE_TITLE_MAX_CHARS,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    ReflectActionAvailabilityContext,
    ReflectActionCallContext,
    ReflectActionDescriptor,
    ReflectActionItem,
    ReflectActionParameter,
    ReflectActionResult,
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

# The one parameter, and the whole of what may leave the deployment on this
# call besides the question core already holds.  ``text`` rather than an enum
# pair with a category filter: a second parameter would be a second thing the
# model can get wrong for no retrieval gain, and this sample is a shape to
# copy, not a feature to grow.
QUERY_PARAMETER = ReflectActionParameter(
    name="query",
    description=(
        "English keywords or a short English phrase describing the missing"
        " aspect. arXiv is a Latin-keyword index, so a Chinese query returns"
        " nothing and is refused before any request is made."
    ),
    kind="text",
    required=True,
)

# One contribution is one action, so one descriptor.  Everything the model
# reads about this function is here: core supplies the surrounding fixed
# sentences (that the material is external, that it is citable, when to reach
# for it, that arguments must not be copied out of the candidates) and a plugin
# cannot edit those.  Every string is one line — a newline or any other control
# character in a descriptor is a *startup* failure, not a clamp.
DESCRIPTOR = ReflectActionDescriptor(
    name="search_arxiv",
    description=(
        "Search arXiv, the open-access preprint index for physics,"
        " mathematics, computer science and related fields, and return the"
        " matching papers as a title, an excerpt of the abstract and a direct"
        " PDF link. It suits an aspect that turns on English technical"
        " terminology or on recent published work that a notebook may simply"
        " not contain yet. It cannot help with Chinese keywords (the index is"
        " Latin-only), with non-academic material such as product"
        " documentation or news, or with anything internal to this"
        " deployment."
    ),
    source_label=SOURCE_LABEL,
    parameters=(QUERY_PARAMETER,),
    # One call per run: a second search for the same missing aspect is nearly
    # always the same search reworded, and core's own run budget is spent
    # before the call is made rather than after it returns.
    max_calls_per_run=1,
)

# Shown to the model on the next reflect turn when the search ran and matched
# nothing.  Phrased as a next step rather than an apology: the useful thing the
# model can learn from an empty arXiv page is that these particular keywords
# are not the ones, and that giving up on outside material is a legitimate
# outcome.  Well inside ``REFLECT_ACTION_NOTE_MAX_CHARS`` (300).
EMPTY_RESULT_NOTE = (
    "arXiv returned no match for these keywords; try a different English"
    " phrasing or give up on external material."
)


class ArxivReflectSearchAction:
    """One ``ask.reflect_action`` contributor bound to this plugin's settings.

    Settings arrive through a zero-argument callable rather than being captured
    at construction, for the same reason as in :mod:`.consult`: ``configure``
    runs after the bundle object exists, so a snapshot taken in ``__init__``
    would be ``None`` forever.
    """

    descriptor = DESCRIPTOR

    def __init__(
        self, settings_source: Callable[[], ArxivSearchSettings | None]
    ) -> None:
        self._settings_source = settings_source

    # -- availability -------------------------------------------------------

    def settings(self) -> ArxivSearchSettings | None:
        """The bound settings, or ``None`` when this plugin has none."""

        try:
            settings = self._settings_source()
        except Exception:  # noqa: BLE001 — an unconfigured plugin, not a crash
            return None
        return settings if isinstance(settings, ArxivSearchSettings) else None

    def reflect_search_enabled(self) -> bool:
        """Has this deployment said yes to the reflect action at all?"""

        settings = self.settings()
        return settings is not None and settings.reflect_search_enabled

    def unavailable_reason(self, context: object | None = None) -> str | None:
        """``None`` when the action may be offered, else a stable reason code.

        I/O-free — a clock read and two comparisons — because core runs this on
        the same deadline-bound worker thread as the call it gates (see
        :mod:`.bundle`).

        **Two questions, not one.**  "Has this deployment agreed?" is the
        settings flag.  "Can a call under THIS deadline finish?" is the same
        worst-case arithmetic :meth:`invoke` uses, asked early — because the
        answer is already knowable from the deployment's own settings and core's
        own deadline, and a plugin that knew it would refuse should say so here
        rather than let the model spend a turn, an action slot and a wall-clock
        wait discovering it.  The refusal is visible: core records the reason
        code on its own event for that contribution.

        The deadline half is skipped when ``context`` is not core's
        reflect-action availability context — ``None`` from a test or another
        consumer means "no deadline was stated", and inventing one would turn a
        question about configuration into a guess about time.
        """

        settings = self.settings()
        if settings is None or not settings.reflect_search_enabled:
            return "reflect_search_disabled"
        if type(context) is ReflectActionAvailabilityContext and (
            deadline_budget(
                settings, context.deadline_monotonic, now=time.monotonic()
            )
            is None
        ):
            return "reflect_budget_too_small"
        return None

    # -- the contribution ---------------------------------------------------

    def invoke(self, context: ReflectActionCallContext) -> ReflectActionResult:
        settings = self.settings()
        if settings is None or not settings.reflect_search_enabled:
            # Defence in depth: core's host evaluates the availability probe on
            # the worker thread immediately before this call, so reaching here
            # disabled means that probe stopped being consulted.  A disabled
            # plugin must not become an outbound request either way.
            return _unavailable(
                ExtensionFailureKind.DISABLED, "reflect_search_disabled"
            )

        cancellation = context.cancellation
        if cancellation is not None and cancellation.is_set():
            return _unavailable(ExtensionFailureKind.CANCELLED, "arxiv_cancelled")

        if context.max_items <= 0:
            # Nothing this call brings back could be admitted, so the term scan
            # and the deadline arithmetic below are not paid for either.
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_no_item_budget"
            )

        # ONLY the argument the model wrote.  See the module docstring: the
        # question is in the context, and using it here would widen the egress
        # surface past what the model chose to send.
        terms = latin_terms(context.arguments.get("query", ""))
        if not terms:
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
                **search_kwargs(
                    settings,
                    limit=min(context.max_items, settings.reflect_search_max_items),
                    budget_seconds=budget,
                ),
            )
        except arxiv_client.ArxivThrottled:
            return _unavailable(
                ExtensionFailureKind.UNAVAILABLE, "arxiv_throttled"
            )
        except Exception:  # noqa: BLE001 — one stable code, never the text
            return _unavailable(
                ExtensionFailureKind.FAILED, "arxiv_upstream_failed"
            )

        items = tuple(
            _item(paper)
            for paper in papers
            if egress_allowed(paper.pdf_url, settings.base_url)
        )
        # An empty answer is not a failure — the model asked a question and got
        # a real answer to it — so it comes back AVAILABLE with a note rather
        # than as a skip the model cannot read anything into.  The note also
        # covers the case where every record was dropped by the egress filter:
        # from the model's side both are "these keywords brought nothing back",
        # and naming this deployment's mirror policy to it would be neither
        # actionable nor its business.
        return ReflectActionResult(
            items=items,
            note="" if items else EMPTY_RESULT_NOTE,
            status=ExtensionResultStatus.AVAILABLE,
        )


def _unavailable(kind: ExtensionFailureKind, code: str) -> ReflectActionResult:
    """No items, plus the stable reason core writes to its own event and trace."""

    return ReflectActionResult(
        items=(),
        note="",
        status=ExtensionResultStatus.UNAVAILABLE,
        failure=ExtensionFailure(kind=kind, code=code),
    )


def _item(paper: ArxivPaper) -> ReflectActionItem:
    """Map one record onto core's four-field external-evidence item.

    ``url`` is the PDF direct link rather than the abstract page, as in
    :mod:`.consult`: the reader's "open link" button and the "import as source"
    button both address exactly what they are given.

    ``title``/``excerpt`` are cut here to core's own
    ``EXTERNAL_EVIDENCE_TITLE_MAX_CHARS``/``EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS``
    — imported from the SDK, never hand-copied — for the same reason
    ``_suggestion`` does it one module over: :mod:`.atom`'s own ceilings are a
    wider display bound for the interactive ``/search`` page, so a record
    reaching here can still be wider than an item may carry.  Core's admission
    host would cut an over-long value anyway; handing over an already-compliant
    one keeps the truncation where it can be explained.  No ellipsis, as
    everywhere else in this package: an appended marker is indistinguishable
    from the record's own text.

    ``excerpt`` is the abstract, and that choice is the contract: core never
    re-summarizes it, so this exact text is what the citation card shows and
    what synthesis reads.  ``location_label`` carries the arXiv id, which is
    the one durable name a reader can look the paper up by — the version-bearing
    id, not a section number, because an external item has no section.

    **The label is the one field cut is wrong for.**  ``.atom`` allows an id up
    to ``ARXIV_ID_MAX_CHARS`` (64), so ``arXiv:<id>`` can pass core's 60, and a
    clipped identifier is not a shortened label — it is a *different*
    identifier, pointing at a paper that does not exist, printed beside a real
    title and a real link.  An over-long label is therefore dropped whole;
    ``location_label`` is optional and the URL still says exactly which paper
    this is.
    """

    label = f"arXiv:{paper.arxiv_id}"
    return ReflectActionItem(
        title=paper.title[:EXTERNAL_EVIDENCE_TITLE_MAX_CHARS],
        excerpt=paper.summary[:EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS],
        url=paper.pdf_url,
        location_label=(
            label if len(label) <= EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS
            else ""
        ),
    )
