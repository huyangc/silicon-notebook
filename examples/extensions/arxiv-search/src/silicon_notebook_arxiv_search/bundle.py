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
  contribution id (core's gap-consult host, for this plugin's
  ``ASK_GAP_CONSULT_POINT`` registration).  That is where outbound
  consultation is actually gated, so turning it off leaves the search panel
  and the import route exactly as they were — because each contribution is
  gated on its own, not because ``manifest.requires`` would otherwise have
  reached them.

``manifest.provides`` then carries a third, separate thing: the capability the
*workspace UI entry* is gated on.  "This plugin is configured" is the honest
question for a side-panel button; "may this deployment consult arXiv on its
own" is not, and conflating them would hide the search panel from a deployment
that deliberately keeps consultation off.

Both probes are I/O-free, and the consult one has a second reason to be: core
runs it on the same deadline-bound worker thread as ``consult`` itself, so a
probe that dialled arXiv would spend the reader's own latency budget deciding
whether it was allowed to spend the reader's latency budget.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ASK_GAP_CONSULT_POINT,
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
    capability_decisions: Mapping[str, AvailabilityProbe] = field(init=False)

    def __post_init__(self) -> None:
        """Wire the two derived members every instance must have.

        Both are built here rather than assigned to the module-level ``BUNDLE``
        afterwards so that *any* instance is complete — a test that constructs
        a second bundle gets a working one, and neither member can be forgotten
        by a future edit that adds an instantiation somewhere else.

        The contributor reads settings through ``lambda: self.settings`` rather
        than taking the value: ``configure`` has not run yet at this point, so
        a snapshot would be ``None`` for the life of the process.
        """

        self.contributor = ArxivGapConsultContributor(lambda: self.settings)
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


BUNDLE = ArxivSearchBundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="arXiv 文献检索（样板）",
        trust="deployment",
        contributions=(_ROUTER, _CONSULT),
        # Empty on purpose, and load-bearing.  See the module docstring.
        requires=(),
        provides=(AVAILABLE_CAPABILITY,),
        ui_contributions=(_PANEL,),
    )
)
