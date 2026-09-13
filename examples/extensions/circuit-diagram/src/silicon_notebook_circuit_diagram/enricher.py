"""The ``source.element_enricher`` contributor: netlists for schematics.

What core hands this contributor is a read-only projection of every element
one freshly parsed source produced, a call-scoped reader for the images among
them, and a monotonic deadline.  What it may not assume is anything about the
thread it runs on: core runs the availability probe and :meth:`enrich`
together on a private daemon thread with no copied context, so ``ContextVar``
and thread-local state are both empty here, by design.

**The per-image budget check is the part that is easy to get wrong.**  Core's
deadline covers the *whole point*, and a plugin that starts an image it cannot
finish does not merely waste a call: the host abandons the worker, files an
``element_enricher_timeout`` and ends the point, so every image after it is
lost too, and so is every later contributor's turn.  The check below therefore
refuses to start an image unless a worst case fits — a full request timeout
plus a margin for the return trip — and reports ``PARTIAL`` for the ones it
did not reach.  Stopping early with three of eight images classified is a
better answer than being abandoned with none persisted.

**One image's failure is one image's failure.**  A refused request, a
malformed answer, an unreadable asset: each is counted and skipped, and the
result is still ``AVAILABLE`` or ``PARTIAL``.  ``UNAVAILABLE`` is reserved for
"this contributor could not serve this call at all", because core discards an
``UNAVAILABLE`` batch whole.

**Accuracy is explicitly not claimed.**  This is a sample: it proves the link
from a parsed image to persisted, retrievable metadata.  Whether
``deepseek-flash`` reads a particular schematic correctly is the deployment's
own question, and the README says so to the operator as well.
"""
from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable

from app.domain.element_enrichment import persisted_element_enrichment_size
from app.extension_sdk import (
    ContributorResult,
    ElementEnrichmentBudget,
    ElementEnrichmentCandidate,
    ElementEnrichmentContext,
    ElementView,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
)

from . import client as circuit_client
from .settings import CircuitDiagramSettings, classify_kwargs

# Plugin-private bounds; registered for operators in the package README.
#
# The margin covers everything between "the response bytes arrived" and "core
# read the return value": parsing, candidate assembly, and the up-to-50 ms
# slice core's join loop is sleeping in when the worker finishes.
RETURN_MARGIN_SECONDS = 0.25
# A netlist is a document, not a sentence, and a function summary is the
# opposite.  Both are ceilings on model output, applied before anything is
# persisted, so a model that answers with a megabyte cannot push this
# contribution's batch past core's own byte budget on its own.
NETLIST_MAX_CHARS = 4000
FUNCTION_MAX_CHARS = 1000

_FUNCTION_HEADINGS = {"zh": "电路功能：", "en": "Circuit function: "}
# Characters a description may carry besides printable ones, and the
# replacement for everything else.  Same criterion core's admission uses
# (``app.extensions.element_enrichment._normalized_description``), applied
# here so ONE image's unprintable character cannot cost the batch: core
# discards a whole contribution over a single U+3000 or NBSP a model emitted,
# and a model that formats Chinese prose emits them routinely.
_DESCRIPTION_EXTRA_CHARS = frozenset("\n\t")


class CircuitDiagramEnricher:
    """One ``source.element_enricher`` contributor bound to this plugin's settings.

    Settings arrive through a zero-argument callable rather than being captured
    at construction: ``configure`` runs after the bundle object exists, so a
    snapshot taken in ``__init__`` would be ``None`` forever.
    """

    def __init__(
        self,
        settings_source: Callable[[], CircuitDiagramSettings | None],
        *,
        plugin_id: str,
        plugin_version: str,
        contribution_id: str,
    ) -> None:
        self._settings_source = settings_source
        # This contribution's own provenance, passed in from the manifest
        # rather than restated here, because core writes it INSIDE the subtree
        # its byte budget measures: sizing a candidate without it would
        # under-count by exactly the envelope core adds.  Required rather than
        # defaulted for the same reason — a silently empty envelope is a
        # silently wrong budget.
        self._plugin_id = plugin_id
        self._plugin_version = plugin_version
        self._contribution_id = contribution_id

    # -- availability -------------------------------------------------------

    def settings(self) -> CircuitDiagramSettings | None:
        """The bound settings, or ``None`` when this plugin has none.

        Defensive about the callable's answer for the same reason core is
        defensive about a plugin's: a bundle whose ``configure`` never ran
        yields ``None``, and anything that is not this model is not settings.
        """

        try:
            settings = self._settings_source()
        except Exception:  # noqa: BLE001 — an unconfigured plugin, not a crash
            return None
        return settings if isinstance(settings, CircuitDiagramSettings) else None

    def credential_present(self) -> bool:
        """I/O-free, and the probe's whole question beyond "is it configured".

        Reading ``os.environ`` is a dictionary lookup, not I/O, which is what
        makes this safe to run on core's deadline-bound worker: a probe that
        dialled the endpoint to find out whether it was allowed to dial the
        endpoint would spend the parse's budget deciding.
        """

        settings = self.settings()
        return settings is not None and bool(
            os.environ.get(settings.api_key_env, "").strip()
        )

    # -- the contribution ---------------------------------------------------

    def enrich(
        self, context: ElementEnrichmentContext
    ) -> ContributorResult[ElementEnrichmentCandidate]:
        settings = self.settings()
        if settings is None:
            # Defence in depth: the availability probe already refused this
            # call.  Keeping the check means a host that ever stopped
            # consulting the probe cannot turn an unconfigured plugin into an
            # outbound request.
            return _unavailable(ExtensionFailureKind.DISABLED, "not_configured")
        api_key = os.environ.get(settings.api_key_env, "").strip()
        if not api_key:
            return _unavailable(ExtensionFailureKind.DISABLED, "api_key_missing")

        budget = context.budget
        targets = _classifiable(context.elements)[
            : min(settings.max_images_per_source, max(0, budget.max_proposals))
        ]
        floor = settings.timeout_seconds + RETURN_MARGIN_SECONDS
        candidates: list[ElementEnrichmentCandidate] = []
        # One image's bytes, classified once per call.  A header logo repeated
        # on forty pages is forty identical assets, and paying for forty
        # identical answers would spend the deadline this plugin exists to
        # protect.  Only successes are remembered: a failed request says
        # nothing about the image, so retrying it on the next page is right.
        classified: dict[str, circuit_client.CircuitClassification] = {}
        attempted = 0
        skipped = 0
        spent_bytes = 0

        for view in targets:
            # Core checks cancellation on every join slice and raises on the
            # calling thread, so this raise is belt-and-braces: what it buys is
            # that a cancelled parse stops paying for outbound requests
            # immediately rather than at the end of the one in flight.
            context.cancellation.raise_if_cancelled()

            payload = context.assets.read(view.ref)
            if payload is None or len(payload) > settings.max_image_bytes:
                skipped += 1
                continue
            digest = hashlib.sha256(payload).hexdigest()
            classification = classified.get(digest)
            if classification is None:
                if budget.deadline_monotonic - time.monotonic() < floor:
                    # Not enough of core's deadline left for a worst-case
                    # request.  Stop rather than be abandoned mid-call — see
                    # the module docstring for why being abandoned is strictly
                    # worse.  The check sits after the cache lookup on purpose:
                    # a repeat of an image already classified costs nothing, so
                    # refusing it would throw away a free candidate.
                    return _partial(candidates, attempted)
                attempted += 1
                try:
                    classification = circuit_client.classify(
                        payload,
                        _media_type(view.asset_mime),
                        **classify_kwargs(settings, api_key=api_key),
                    )
                except Exception:  # noqa: BLE001 — one image's fault, not the batch's
                    skipped += 1
                    continue
                classified[digest] = classification
            if not classification.is_circuit:
                # Deliberately no candidate, not even a "checked, not a
                # circuit" marker: a marker would be persisted metadata whose
                # only content is this plugin's own opinion of an image core
                # already describes.
                continue

            candidate = _candidate(view, classification, settings, budget)
            if candidate is None:
                # A schematic the model described with neither a netlist nor a
                # function summary.  Persisting ``is_circuit`` alone would be
                # provenance with no content behind it.
                skipped += 1
                continue
            spent_bytes += self._persisted_cost(candidate)
            if spent_bytes > budget.max_metadata_bytes:
                # Core discards a contribution's WHOLE batch when it exceeds
                # the byte budget, so an over-full batch would throw away the
                # images that did fit.  Stop one short instead.
                return _partial(candidates, attempted)
            candidates.append(candidate)

        if skipped:
            return ContributorResult(
                items=tuple(candidates), status=ExtensionResultStatus.PARTIAL
            )
        return ContributorResult(
            items=tuple(candidates), status=ExtensionResultStatus.AVAILABLE
        )

    def _persisted_cost(self, candidate: ElementEnrichmentCandidate) -> int:
        """Exactly what core will charge this candidate against the budget.

        Core's own ``persisted_element_enrichment_size`` — the SOP allows a
        deployment plugin to import ``app.domain`` alongside the SDK, and this
        is why it is worth doing.  Two things a hand-rolled estimate gets
        wrong in the *dangerous* direction: the description is charged twice
        (once JSON-quoted inside the envelope, once as the raw bytes appended
        to the element's ``text``), and JSON escaping makes the quoted copy
        larger than the string.  Under-counting means proposing a batch core
        refuses whole, which throws away the images that did fit.
        """

        return persisted_element_enrichment_size(
            plugin_id=self._plugin_id,
            plugin_version=self._plugin_version,
            contribution_id=self._contribution_id,
            metadata=dict(candidate.metadata),
            description=candidate.description,
        )


def _classifiable(elements: tuple[ElementView, ...]) -> list[ElementView]:
    """The image elements this plugin can send, in the order core offered them.

    Filtering on ``asset_id`` as well as ``element_type`` is what the SDK's
    contract asks for: a non-empty ``asset_id`` is core's promise that there
    are bytes to read, and an image element without one is a caption core
    parsed out of a document whose picture never landed.
    """

    return [
        view
        for view in elements
        if view.element_type == "image"
        and view.asset_id
        and _media_type(view.asset_mime) in circuit_client.SUPPORTED_IMAGE_MIME_TYPES
    ]


def _media_type(mime: str) -> str:
    """``image/png`` out of ``image/PNG; charset=binary``."""

    return mime.split(";", 1)[0].strip().lower()


def _candidate(
    view: ElementView,
    classification: circuit_client.CircuitClassification,
    settings: CircuitDiagramSettings,
    budget: ElementEnrichmentBudget,
) -> ElementEnrichmentCandidate | None:
    """Shape one model answer into what core will persist, or ``None``.

    ``None`` means this answer has no content worth persisting: a model that
    called the image a schematic and then supplied neither a netlist nor a
    function summary has said nothing a reader could use.

    Both halves are cut to the same values the description carries, so the
    structured ``metadata`` and the retrievable ``description`` can never
    disagree about what the model said — a reader comparing the two would
    otherwise find a netlist in one that is absent from the other.

    **The description ceiling is core's, and the heading and fence are charged
    against it first.**  Core rejects a description longer than
    ``max_description_chars`` by discarding this contribution's WHOLE batch, so
    cutting ``function`` to the raw limit and only then adding a heading and a
    fence around it would overshoot by exactly that scaffolding.  A limit too
    small to hold even the empty scaffolding yields no description at all: the
    metadata still carries what the model said, and proposing a description
    certain to be refused would cost the images that did fit.
    """

    language = settings.prompt_language
    limit = budget.max_description_chars
    overhead = len(_description("", "", language))
    function = _normalized(classification.function)
    netlist = _unfenced(_normalized(classification.netlist))
    if not function and not netlist:
        return None
    if limit >= overhead:
        function = function[: min(FUNCTION_MAX_CHARS, limit - overhead)]
        netlist = netlist[
            : min(NETLIST_MAX_CHARS, limit - overhead - len(function))
        ]
        description = _description(function, netlist, language)
    else:
        function = function[:FUNCTION_MAX_CHARS]
        netlist = netlist[:NETLIST_MAX_CHARS]
        description = ""
    return ElementEnrichmentCandidate(
        element=view.ref,
        metadata={
            "is_circuit": True,
            "netlist": netlist,
            "function": function,
            # The model id, and deliberately nothing else from settings: the
            # SOP's red line for this point forbids credential and endpoint
            # settings in persisted metadata, and allows the id of the model
            # that produced the answer — without it, revisiting a netlist
            # later means guessing which model wrote it.
            "model": settings.model,
        },
        description=description,
    )


def _description(function: str, netlist: str, language: str) -> str:
    """The retrievable text core persists, with the netlist in a fenced block.

    The fence is the contract with the front end: core's own
    ``descriptionBlocks`` splits a description on triple-backtick fences and
    renders the inside as a code block, so a netlist written as prose would be
    reflowed into an unreadable paragraph.
    """

    heading = _FUNCTION_HEADINGS.get(language, _FUNCTION_HEADINGS["zh"])
    return f"{heading}{function}\n\n```spice\n{netlist}\n```"


def _normalized(value: str) -> str:
    """Canonical line endings, then core's own printable-character rail.

    Two different reasons to do this here rather than leave it to core:

    * ``\\r\\n``.  Core normalizes it in a *description*, but the same text is
      also persisted into ``metadata``, which core does not normalize, so
      doing it here is what keeps the two copies byte-identical.
    * **Unprintables.**  Core refuses a description carrying anything that is
      neither printable nor ``\\n``/``\\t``, and refusing it discards this
      contribution's whole batch.  A model writing Chinese prose emits U+3000
      and NBSP as a matter of course, and a copied datasheet carries zero-width
      joiners and a BOM.  Replacing them with a plain space — the same
      criterion core admits by — keeps one image's formatting habit from
      costing every other image in the source.
    """

    canonical = value.replace("\r\n", "\n").replace("\r", "")
    return "".join(
        character
        if character.isprintable() or character in _DESCRIPTION_EXTRA_CHARS
        else " "
        for character in canonical
    )


def _unfenced(netlist: str) -> str:
    """Drop any Markdown fence lines the model wrapped its netlist in.

    The netlist goes *inside* a fence this plugin writes, so a model that
    helpfully fenced its own answer would produce a nested fence — which the
    front end's splitter reads as a code block ending three lines early, with
    the rest of the netlist rendered as prose.
    """

    return "\n".join(
        line for line in netlist.splitlines() if not line.lstrip().startswith("```")
    ).strip("\n")


def _partial(
    candidates: list[ElementEnrichmentCandidate], attempted: int
) -> ContributorResult[ElementEnrichmentCandidate]:
    """Stopped early: some of it, or — having classified nothing — none of it.

    ``UNAVAILABLE`` when not a single image was attempted, because that is the
    honest statement: this contributor did not serve the call.  Once one image
    has been classified, ``PARTIAL`` is the honest one, and core admits it
    exactly like ``AVAILABLE``.
    """

    if attempted == 0:
        return _unavailable(ExtensionFailureKind.UNAVAILABLE, "budget_exhausted")
    return ContributorResult(
        items=tuple(candidates), status=ExtensionResultStatus.PARTIAL
    )


def _unavailable(
    kind: ExtensionFailureKind, code: str
) -> ContributorResult[ElementEnrichmentCandidate]:
    """No candidates, plus the stable reason core writes to its own event."""

    return ContributorResult(
        items=(),
        status=ExtensionResultStatus.UNAVAILABLE,
        failure=ExtensionFailure(kind=kind, code=code),
    )
