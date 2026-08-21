from __future__ import annotations

from dataclasses import dataclass, replace

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
    calls = []
    registry = build_extension_registry(
        (_Bundle(_manifest("a", requires=("model:scheduled_access",))),),
        capability_decisions={
            "model:scheduled_access": lambda context: (
                calls.append(context) or Availability.available()
            )
        },
    )

    assert registry.manifests()[0].requires == ("model:scheduled_access",)
    assert calls == []
    assert registry.capability_availability("model:scheduled_access").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert calls == [None]


def test_missing_required_capability_decision_fails_freeze_but_optional_does_not():
    with pytest.raises(ExtensionRegistryError, match="decision entries"):
        build_extension_registry(
            (_Bundle(_manifest("required", requires=("missing",))),)
        )

    manifest = _manifest("optional")
    manifest = replace(manifest, optional_requires=("missing",))
    registry = build_extension_registry((_Bundle(manifest),))
    assert registry.manifests() == (manifest,)


def test_required_capability_availability_is_live_and_failure_is_sanitized():
    state = {"available": False, "raise": False}
    declaration = ContributionDeclaration(
        "probe", "retrieval.enrichment", ContributionKind.CONTRIBUTOR
    )

    def decide(_context):
        if state["raise"]:
            raise RuntimeError("secret endpoint")
        if state["available"]:
            return Availability.available()
        return Availability(AvailabilityStatus.DISABLED, "feature_disabled")

    registry = build_extension_registry(
        (_Bundle(_manifest("plugin", declaration, requires=("live",)), (object(),)),),
        capability_decisions={"live": decide},
    )

    assert registry.availability("probe").status is AvailabilityStatus.DISABLED
    state["available"] = True
    assert registry.availability("probe").status is AvailabilityStatus.AVAILABLE
    state["raise"] = True
    unavailable = registry.availability("probe")
    assert unavailable.status is AvailabilityStatus.UNAVAILABLE
    assert unavailable.reason_code == "capability_decision_failed"


def test_contribution_availability_failure_and_content_reason_are_sanitized():
    declaration = ContributionDeclaration(
        "probe", "retrieval.enrichment", ContributionKind.CONTRIBUTOR
    )

    raising = build_extension_registry((
        _Bundle(
            _manifest("raising", declaration),
            (object(),),
            lambda _context: (_ for _ in ()).throw(RuntimeError("secret")),
        ),
    ))
    result = raising.availability("probe")
    assert result.status is AvailabilityStatus.UNAVAILABLE
    assert result.reason_code == "availability_probe_failed"

    unsafe_reason = build_extension_registry((
        _Bundle(
            _manifest("unsafe", declaration),
            (object(),),
            lambda _context: Availability(
                AvailabilityStatus.UNAVAILABLE, "source title leaked"
            ),
        ),
    ))
    result = unsafe_reason.availability("probe")
    assert result.status is AvailabilityStatus.UNAVAILABLE
    assert result.reason_code == "invalid_availability_reason"
