"""The module-level object a deployment's ``EXTENSIONS_CONFIG`` points at.

    bundle = "silicon_notebook_circuit_diagram.bundle:BUNDLE"

Note the module path: ``BUNDLE`` is deliberately **not** re-exported from the
package ``__init__``, so that the plain ``import
silicon_notebook_circuit_diagram`` — which a packaging check or a version
probe might do — does not drag the extension SDK in behind it.

**One contribution, and no capability.**  This plugin has no HTTP routes and
no UI package: everything it produces lands in the element metadata core
already persists and renders.  ``manifest.provides`` is therefore empty, and
so is the ``capability_decisions`` mapping a bundle would otherwise need —
there is no workspace entry to gate.  ``requires`` is empty for the reason the
arXiv sample's is: it is manifest-wide, so it would be silently inherited by
any contribution added later, and "the operator exported the API key" is a
per-contribution fact, not a precondition for the plugin instance.

**Availability is where the switch actually lives**, and it has two positions
rather than one:

* ``not_configured`` — ``configure`` never ran, so there are no settings.  A
  deployment that names this plugin in its TOML never sees it.
* ``api_key_missing`` — the plugin is configured, but the environment variable
  named by ``api_key_env`` is unset or blank.  This is the ordinary "installed
  but off" state, and it is the second of the two steps the README asks for:
  naming the plugin in ``EXTENSIONS_CONFIG`` is step one, exporting the
  credential is step two.

Core evaluates that probe on the same deadline-bound worker thread as
``enrich`` itself, so it is I/O-free by construction: it reads settings and
one environment variable, and stops.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    SOURCE_ELEMENT_ENRICHER_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)

from .enricher import CircuitDiagramEnricher
from .settings import CircuitDiagramSettings

PLUGIN_ID = "examples.circuit_diagram"

_ENRICHER = ContributionDeclaration(
    id=f"{PLUGIN_ID}.enricher",
    point=SOURCE_ELEMENT_ENRICHER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)


@dataclass
class CircuitDiagramBundle:
    """A plain object with the right shape — nothing is subclassed."""

    manifest: ExtensionManifest
    settings_model: type[CircuitDiagramSettings] = CircuitDiagramSettings
    settings: CircuitDiagramSettings | None = None
    enricher: CircuitDiagramEnricher = field(init=False)

    def __post_init__(self) -> None:
        """Wire the derived member every instance must have.

        Built here rather than assigned to the module-level ``BUNDLE``
        afterwards so that *any* instance is complete — a test that constructs
        a second bundle gets a working one, and the member cannot be forgotten
        by a future edit that adds an instantiation somewhere else.

        The enricher reads settings through ``lambda: self.settings`` rather
        than taking the value: ``configure`` has not run yet at this point, so
        a snapshot would be ``None`` for the life of the process.

        Its provenance, by contrast, IS taken by value, and from this
        instance's own manifest rather than from module constants: the
        enricher sizes each candidate with core's own
        ``persisted_element_enrichment_size``, which measures the envelope
        core writes around the payload, so a version restated by hand here
        would make the budget silently wrong the first time the manifest's
        moved on.
        """

        self.enricher = CircuitDiagramEnricher(
            lambda: self.settings,
            plugin_id=self.manifest.id,
            plugin_version=self.manifest.version,
            contribution_id=_ENRICHER.id,
        )

    def configure(self, settings: CircuitDiagramSettings) -> None:
        """Store the validated settings.  Nothing else — see the SOP §3.2.

        This runs inside startup composition, before the registry freezes and
        before the service is ready, so it must not start a thread, open a
        connection, or perform any I/O.  The transport is stateless and the
        credential is read from the environment at dial time, so there is
        nothing here to build anyway.
        """

        self.settings = settings

    def register(self, registrar) -> None:
        """Register exactly the contributions the manifest declares.

        Core compares the registered id set against ``manifest.contributions``
        and stops the process on any difference, so this method and the tuple
        below are one statement written twice; the test that compares them is
        there to keep the second copy from drifting.
        """

        registrar.add_contributor(
            ExtensionContribution(
                declaration=_ENRICHER,
                implementation=self.enricher,
                availability=self._available,
            )
        )

    # -- availability probe -------------------------------------------------

    def _available(self, _context: object | None) -> Availability:
        """Is this deployment actually able to classify an image right now?

        The context core passes (``ElementEnrichmentAvailabilityContext``)
        carries the element and image counts, and this probe deliberately
        ignores them: "there are no images in this source" is not
        unavailability, it is a source with no images, and answering
        ``DISABLED`` for it would make the operator's event log claim a
        configuration problem that does not exist.
        """

        if self.settings is None:
            return Availability(AvailabilityStatus.DISABLED, "not_configured")
        if not os.environ.get(self.settings.api_key_env, "").strip():
            return Availability(AvailabilityStatus.DISABLED, "api_key_missing")
        return Availability.available()


BUNDLE = CircuitDiagramBundle(
    ExtensionManifest(
        id=PLUGIN_ID,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="电路图识别（样板）",
        trust="deployment",
        contributions=(_ENRICHER,),
        # All three empty on purpose, and load-bearing.  See the module
        # docstring.
        requires=(),
        provides=(),
        ui_contributions=(),
    )
)
