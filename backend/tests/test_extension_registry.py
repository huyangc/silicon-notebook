from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extensions import build_extension_registry
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    implementations: tuple[object, ...] = ()
    availability: object | None = None

    def register(self, registrar) -> None:
        for declaration, implementation in zip(
            self.manifest.contributions, self.implementations
        ):
            registrar.add(
                ExtensionContribution(
                    declaration,
                    implementation,
                    self.availability,
                )
            )


def _manifest(
    plugin_id: str,
    *declarations: ContributionDeclaration,
    requires: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> ExtensionManifest:
    return ExtensionManifest(
        id=plugin_id,
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name=plugin_id,
        trust="builtin",
        contributions=tuple(declarations),
        requires=requires,
        depends_on=depends_on,
    )


def test_empty_registry_is_frozen_and_has_no_contributions():
    registry = build_extension_registry()

    assert registry.frozen is True
    assert registry.manifests() == ()
    assert registry.contributions("retrieval.enrichment") == ()

    with pytest.raises(ExtensionRegistryError, match="frozen"):
        registry.register(_Bundle(_manifest("late")))


def test_topology_is_frozen_but_availability_is_resolved_live():
    state = {"available": False}
    declaration = ContributionDeclaration(
        "probe", "retrieval.enrichment", ContributionKind.CONTRIBUTOR
    )

    def probe(_context):
        if state["available"]:
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            reason_code="not_configured",
        )

    registry = build_extension_registry(
        (_Bundle(_manifest("plugin", declaration), (object(),), probe),)
    )

    assert registry.availability("probe").status is AvailabilityStatus.UNAVAILABLE
    state["available"] = True
    assert registry.availability("probe").status is AvailabilityStatus.AVAILABLE


def test_single_provider_conflict_is_rejected_but_chain_has_stable_id_order():
    provider_a = ContributionDeclaration(
        "provider-a", "export", ContributionKind.PROVIDER
    )
    provider_b = ContributionDeclaration(
        "provider-b", "export", ContributionKind.PROVIDER
    )
    with pytest.raises(ExtensionRegistryError, match="multiple single providers"):
        build_extension_registry(
            (
                _Bundle(_manifest("a", provider_a), (object(),)),
                _Bundle(_manifest("b", provider_b), (object(),)),
            )
        )

    late = ContributionDeclaration("late", "parser", ContributionKind.PROVIDER_CHAIN)
    early = ContributionDeclaration("early", "parser", ContributionKind.PROVIDER_CHAIN)
    registry = build_extension_registry(
        (
            _Bundle(_manifest("late-plugin", late), (object(),)),
            _Bundle(_manifest("early-plugin", early), (object(),)),
        )
    )
    assert [
        item.contribution.declaration.id
        for item in registry.contributions("parser")
    ] == ["early", "late"]


def test_unknown_dependencies_and_dependency_cycles_fail_at_startup():
    with pytest.raises(ExtensionRegistryError, match="unknown"):
        build_extension_registry((_Bundle(_manifest("a", depends_on=("missing",))),))

    registry = ExtensionRegistry()
    registry.register(_Bundle(_manifest("a", depends_on=("b",))))
    registry.register(_Bundle(_manifest("b", depends_on=("a",))))
    with pytest.raises(ExtensionRegistryError, match="cycle"):
        registry.freeze()


def test_capability_requirements_are_not_treated_as_plugin_dependencies():
    registry = build_extension_registry(
        (_Bundle(_manifest("a", requires=("model:scheduled_access",))),)
    )

    assert registry.manifests()[0].requires == ("model:scheduled_access",)
