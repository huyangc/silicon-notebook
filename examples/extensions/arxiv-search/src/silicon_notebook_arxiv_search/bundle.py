"""The module-level object a deployment's ``EXTENSIONS_CONFIG`` points at.

    bundle = "silicon_notebook_arxiv_search.bundle:BUNDLE"

Note the module path: ``BUNDLE`` is deliberately **not** re-exported from the
package ``__init__``.  This module imports FastAPI (through :mod:`.routes`) and
the extension SDK, so re-exporting it would make the plain
``import silicon_notebook_arxiv_search`` — which a packaging check or a version
probe might do — drag the whole backend in behind it.

**Two capability gates, and they are not interchangeable.**  This is the one
piece of the plugin whose shape is dictated by how core evaluates availability
rather than by what arXiv needs:

* ``manifest.requires`` only gates a contribution that some consumer looks up
  through ``registry.availability(contribution_id, ...)`` — the one accessor
  that walks ``manifest.requires`` before a contribution's own probe
  (``registry.py``).  HTTP route mounting never calls that accessor: routers
  are mounted unconditionally at startup from the registered contribution
  set, with no availability check anywhere in that path.  The workspace
  entry doesn't go through it either — a UI declaration's own ``capability``
  is evaluated directly via ``registry.capability_availability()``,
  bypassing ``manifest.requires`` entirely.  So putting "gap consultation is
  enabled" there would *not* have taken the router or the panel down with
  it, contrary to what an earlier version of this comment claimed.
* It is still left empty, for two reasons that hold regardless of the above:
  precision — ``requires`` is manifest-wide, so it would be silently
  inherited by any contribution this plugin adds later that a future
  consumer *does* look up through ``registry.availability()``, which is not
  what a single feature's on/off switch should do — and semantics:
  ``requires`` reads as an overall precondition for the plugin instance, not
  a per-feature toggle, and ``consult_enabled`` is the latter.  That
  emptiness is asserted by a test rather than left to be re-derived.
* ``ExtensionContribution.availability`` is evaluated **per contribution**,
  by whichever consumer calls ``registry.availability()`` for that specific
  contribution id (core's gap-consult host and its reflect-action host, for
  this plugin's ``ASK_GAP_CONSULT_POINT`` and ``ASK_REFLECT_ACTION_POINT``
  registrations).  That is where each outbound feature is actually gated, so
  turning one off leaves the search panel, the import route **and the other
  outbound feature** exactly as they were — because each contribution is
  gated on its own, not because ``manifest.requires`` would otherwise have
  reached them.  With two outbound contributions the precision argument above
  stops being hypothetical: a manifest-wide gate would have to mean
  "consultation is on" and "the reflect action is on" at once, and those are
  deliberately two separate decisions a deployment makes one at a time.

``manifest.provides`` then carries a third, separate thing: the capability the
*workspace UI entry* is gated on.  "This plugin is configured" is the honest
question for a side-panel button; "may this deployment consult arXiv on its
own" is not, and conflating them would hide the search panel from a deployment
that deliberately keeps consultation off.

All three probes are I/O-free, and the two outbound ones have a second reason
to be: core runs each on the same deadline-bound worker thread as the call it
gates, so a probe that dialled arXiv would spend the reader's own latency
budget deciding whether it was allowed to spend the reader's latency budget.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ASK_GAP_CONSULT_POINT,
    ASK_REFLECT_ACTION_POINT,
    Availability,
    AvailabilityProbe,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT
from app.extension_sdk.ui import UiContributionDeclaration

from .consult import ArxivGapConsultContributor
from .reflect_search import ArxivReflectSearchAction
from .routes import build_router
from .settings import ArxivSearchSettings

PLUGIN_ID = "examples.arxiv_search"
# The capability the workspace side-panel entry is gated on.  ``:`` is core's
# separator for its own ``point:name`` capabilities and is not legal here.
AVAILABLE_CAPABILITY = f"{PLUGIN_ID}.available"

_ROUTER = ContributionDeclaration(
    id=f"{PLUGIN_ID}.router",
    point=PLUGIN_HTTP_ROUTER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)
_CONSULT = ContributionDeclaration(
    id=f"{PLUGIN_ID}.gap_consult",
    point=ASK_GAP_CONSULT_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)
# One contribution is one reflect action — the point is defined that way, so a
# plugin offering a second function would declare a second contribution here
# with its own id, its own probe and its own budget.
_SEARCH = ContributionDeclaration(
    id=f"{PLUGIN_ID}.reflect_search",
    point=ASK_REFLECT_ACTION_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)
# Metadata only: the browser half lives in ``ui/arxiv-search`` and is copied in
# at build time.  ``id``/``capability`` here must match that package's
# ``ui-plugin.json`` character for character, or the browser's
# (plugin_id, version, contribution_id) lookup finds nothing and the entry
# silently does not render.
_PANEL = UiContributionDeclaration(
    id=f"{PLUGIN_ID}.panel",
    slot="workspace.side_panel",
    capability=AVAILABLE_CAPABILITY,
)


@dataclass
class ArxivSearchBundle:
    """A plain object with the right shape — nothing is subclassed."""

    manifest: ExtensionManifest
    settings_model: type[ArxivSearchSettings] = ArxivSearchSettings
    settings: ArxivSearchSettings | None = None
    contributor: ArxivGapConsultContributor = field(init=False)
    search_action: ArxivReflectSearchAction = field(init=False)
    capability_decisions: Mapping[str, AvailabilityProbe] = field(init=False)

    def __post_init__(self) -> None:
        """Wire the three derived members every instance must have.

        Both are built here rather than assigned to the module-level ``BUNDLE``
        afterwards so that *any* instance is complete — a test that constructs
        a second bundle gets a working one, and neither member can be forgotten
        by a future edit that adds an instantiation somewhere else.

        Both contributors read settings through ``lambda: self.settings``
        rather than taking the value: ``configure`` has not run yet at this
        point, so a snapshot would be ``None`` for the life of the process.
        """

        self.contributor = ArxivGapConsultContributor(lambda: self.settings)
        self.search_action = ArxivReflectSearchAction(lambda: self.settings)
        self.capability_decisions = {AVAILABLE_CAPABILITY: self._configured}

    def configure(self, settings: ArxivSearchSettings) -> None:
        """Store the validated settings.  Nothing else — see the SOP §3.6.

        This runs inside startup composition, before the registry freezes and
        before the service is ready, so it must not start a thread, open a
        connection, or perform any I/O.  The arXiv client is stateless and the
        throttle is module-level, so there is nothing here to build anyway.
        """

        self.settings = settings

    def register(self, registrar) -> None:
        """Register exactly the contributions the manifest declares.

        Core compares the registered id set against ``manifest.contributions``
        and stops the process on any difference, so this method and the tuple
        below are one statement written twice; the test that compares them is
        there to keep the second copy from drifting.  ``_PANEL`` is *not* in
        either: UI declarations are metadata and travel on
        ``ui_contributions``.
        """

        registrar.add_contributor(
            ExtensionContribution(
                declaration=_ROUTER, implementation=build_router
            )
        )
        registrar.add_contributor(
            ExtensionContribution(
                declaration=_CONSULT,
                implementation=self.contributor,
                availability=self._consult_available,
            )
        )
        registrar.add_contributor(
            ExtensionContribution(
                declaration=_SEARCH,
                implementation=self.search_action,
                availability=self._reflect_search_available,
            )
        )

    # -- availability probes ------------------------------------------------

    def _configured(self, _context: object | None) -> Availability:
        """Gates the workspace entry: is this plugin usable at all?"""

        if self.settings is None or not self.settings.base_url:
            return Availability(AvailabilityStatus.DISABLED, "not_configured")
        return Availability.available()

    def _consult_available(self, _context: object | None) -> Availability:
        """Gates outbound consultation, and only that.

        Reached through ``ExtensionContribution.availability`` rather than
        ``manifest.requires`` — see the module docstring for why that
        distinction is the whole design of this file.
        """

        if not self.contributor.consult_enabled():
            return Availability(AvailabilityStatus.DISABLED, "consult_disabled")
        return Availability.available()

    def _reflect_search_available(self, context: object | None) -> Availability:
        """Gates the reflect action the retrieval agent may call, and only that.

        A separate probe from ``_consult_available`` rather than a shared one:
        the two features leave the deployment at different moments and put
        their results in different places (a suggestion beside the answer
        versus quoted material inside it), so a deployment says yes to them
        one at a time.  Core evaluates this on its own deadline-bound worker
        immediately before every call, so it must stay I/O-free.

        Unlike the consult probe this one is handed a ``context``, and it uses
        it: ``unavailable_reason`` also answers "could a call under this
        deadline finish at all?".  A deployment whose timeouts do not fit core's
        reflect-action budget therefore never has the action offered to the
        model, rather than having it offered and refused every time.
        """

        reason = self.search_action.unavailable_reason(context)
        if reason is not None:
            return Availability(AvailabilityStatus.DISABLED, reason)
        return Availability.available()


BUNDLE = ArxivSearchBundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        # 0.2.0: the reflect-action contribution is a new capability of the
        # same package, so both manifests (this one and `ui-plugin.json`) move
        # together — the browser looks the panel up by
        # (plugin_id, version, contribution_id) and a half-bumped pair simply
        # stops rendering.
        version="0.2.0",
        api_version=EXTENSION_API_VERSION,
        display_name="arXiv 文献检索（样板）",
        trust="deployment",
        contributions=(_ROUTER, _CONSULT, _SEARCH),
        # Empty on purpose, and load-bearing.  See the module docstring.
        requires=(),
        provides=(AVAILABLE_CAPABILITY,),
        ui_contributions=(_PANEL,),
    )
)
