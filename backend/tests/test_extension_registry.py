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
    UiContributionDeclaration,
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
    ui_contributions: tuple[UiContributionDeclaration, ...] = (),
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
        ui_contributions=ui_contributions,
    )


def test_empty_registry_is_frozen_and_has_no_contributions():
    registry = build_extension_registry()

    assert registry.frozen is True
    assert registry.manifests() == ()
    assert registry.contributions("retrieval.enrichment") == ()
    assert registry.ui_contributions() == ()

    with pytest.raises(ExtensionRegistryError, match="frozen"):
        registry.register(_Bundle(_manifest("late")))


def test_ui_contribution_topology_is_frozen_ordered_and_live():
    state = {"enabled": False}

    def decision(_context):
        return (
            Availability.available()
            if state["enabled"]
            else Availability(AvailabilityStatus.DISABLED, "disabled_by_test")
        )

    registry = build_extension_registry(
        (
            _Bundle(_manifest(
                "plugin-z",
                ui_contributions=(UiContributionDeclaration(
                    "z-panel", "workspace.side_panel", "ui.z"
                ),),
            )),
            _Bundle(_manifest(
                "plugin-a",
                ui_contributions=(UiContributionDeclaration(
                    "a-detail", "source.detail_section", "ui.a"
                ),),
            )),
        ),
        capability_decisions={"ui.z": decision, "ui.a": decision},
    )

    assert [row.id for _manifest_row, row in registry.ui_contributions()] == [
        "a-detail", "z-panel"
    ]
    assert registry.capability_availability("ui.z").status is AvailabilityStatus.DISABLED
    state["enabled"] = True
    assert registry.capability_availability("ui.z").status is AvailabilityStatus.AVAILABLE


@pytest.mark.parametrize(
    ("ui_contributions", "capabilities", "message"),
    [
        (
            (UiContributionDeclaration("bad", "side_panel", "ui.good"),),
            {"ui.good": lambda _context: Availability.available()},
            "canonical slots",
        ),
        (
            (UiContributionDeclaration("good", "workspace.side_panel", "ui.missing"),),
            {},
            "without a decision entry",
        ),
        (
            (
                UiContributionDeclaration("same", "workspace.side_panel", "ui.good"),
                UiContributionDeclaration("same", "source.detail_section", "ui.good"),
            ),
            {"ui.good": lambda _context: Availability.available()},
            "duplicate UI contribution",
        ),
    ],
)
def test_ui_contributions_reject_alias_missing_decision_and_duplicates(
    ui_contributions, capabilities, message
):
    with pytest.raises(ExtensionRegistryError, match=message):
        build_extension_registry(
            (_Bundle(_manifest("plugin", ui_contributions=ui_contributions)),),
            capability_decisions=capabilities,
        )


def test_runtime_and_ui_contributions_cannot_reuse_one_global_id():
    runtime = ContributionDeclaration(
        "same", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    with pytest.raises(ExtensionRegistryError, match="reuses one id"):
        build_extension_registry(
            (_Bundle(_manifest(
                "plugin",
                runtime,
                ui_contributions=(UiContributionDeclaration(
                    "same", "workspace.side_panel", "ui.good"
                ),),
            ), (object(),)),),
            capability_decisions={"ui.good": lambda _context: Availability.available()},
        )


def test_contribution_ids_are_globally_unique_across_plugin_boundaries():
    runtime = ContributionDeclaration(
        "same", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    ui = UiContributionDeclaration(
        "same", "workspace.side_panel", "ui.good"
    )
    decision = {"ui.good": lambda _context: Availability.available()}
    cases = (
        (
            (
                _Bundle(_manifest("ui-a", ui_contributions=(ui,))),
                _Bundle(_manifest("ui-b", ui_contributions=(ui,))),
            ),
            "duplicate UI contribution",
        ),
        (
            (
                _Bundle(_manifest("runtime-a", runtime), (object(),)),
                _Bundle(_manifest("ui-b", ui_contributions=(ui,))),
            ),
            "duplicate UI contribution",
        ),
        (
            (
                _Bundle(_manifest("ui-a", ui_contributions=(ui,))),
                _Bundle(_manifest("runtime-b", runtime), (object(),)),
            ),
            "duplicate contribution",
        ),
    )
    for bundles, message in cases:
        with pytest.raises(ExtensionRegistryError, match=message):
            build_extension_registry(
                bundles,
                capability_decisions=decision,
            )


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


def test_provider_chain_uses_dag_edges_then_stable_id_ties():
    declarations = {
        "a": ContributionDeclaration(
            "a", "parser.chain", ContributionKind.PROVIDER_CHAIN
        ),
        "b": ContributionDeclaration(
            "b",
            "parser.chain",
            ContributionKind.PROVIDER_CHAIN,
            after=("z",),
        ),
        "z": ContributionDeclaration(
            "z",
            "parser.chain",
            ContributionKind.PROVIDER_CHAIN,
            before=("b",),
        ),
    }

    for registration_order in (("b", "a", "z"), ("z", "b", "a")):
        registry = build_extension_registry(tuple(
            _Bundle(
                _manifest(f"plugin-{name}", declarations[name]),
                (object(),),
            )
            for name in registration_order
        ))
        assert [
            item.contribution.declaration.id
            for item in registry.contributions("parser.chain")
        ] == ["a", "z", "b"]


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        (
            (ContributionDeclaration(
                "a", "parser.chain", ContributionKind.PROVIDER_CHAIN,
                after=("missing",),
            ),),
            "unknown",
        ),
        (
            (ContributionDeclaration(
                "a", "parser.chain", ContributionKind.PROVIDER_CHAIN,
                after=("a",),
            ),),
            "self dependency",
        ),
        (
            (
                ContributionDeclaration(
                    "a", "parser.chain", ContributionKind.PROVIDER_CHAIN,
                    after=("b",),
                ),
                ContributionDeclaration(
                    "b", "parser.chain", ContributionKind.PROVIDER_CHAIN,
                    after=("a",),
                ),
            ),
            "cycle",
        ),
    ],
)
def test_provider_chain_rejects_unknown_self_and_cyclic_edges(
    declarations, message
):
    bundles = tuple(
        _Bundle(_manifest(f"plugin-{item.id}", item), (object(),))
        for item in declarations
    )
    with pytest.raises(ExtensionRegistryError, match=message):
        build_extension_registry(bundles)


def test_provider_chain_ordering_is_exact_and_forbidden_on_other_kinds():
    malformed = replace(
        ContributionDeclaration(
            "a", "parser.chain", ContributionKind.PROVIDER_CHAIN
        ),
        after=["b"],
    )
    with pytest.raises(ExtensionRegistryError, match="stable metadata"):
        build_extension_registry((
            _Bundle(_manifest("malformed", malformed), (object(),)),
        ))

    ordered_contributor = ContributionDeclaration(
        "a",
        "retrieval.contributor",
        ContributionKind.CONTRIBUTOR,
        after=("b",),
    )
    with pytest.raises(ExtensionRegistryError, match="stable metadata"):
        build_extension_registry((
            _Bundle(
                _manifest("ordered-contributor", ordered_contributor),
                (object(),),
            ),
        ))


def test_provider_chain_cannot_mix_kinds_at_one_point():
    chain = ContributionDeclaration(
        "chain", "parser.chain", ContributionKind.PROVIDER_CHAIN
    )
    observer = ContributionDeclaration(
        "observer", "parser.chain", ContributionKind.OBSERVER
    )
    with pytest.raises(ExtensionRegistryError, match="mixes"):
        build_extension_registry((
            _Bundle(_manifest("chain-plugin", chain), (object(),)),
            _Bundle(_manifest("observer-plugin", observer), (object(),)),
        ))


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


def test_registry_rejects_isolated_trust_and_accepts_deployment():
    isolated = replace(_manifest("isolated_plugin"), trust="isolated")
    with pytest.raises(ExtensionRegistryError, match="invalid trust classification"):
        build_extension_registry((_Bundle(isolated),))

    deployment = replace(_manifest("deployment_plugin"), trust="deployment")
    registry = build_extension_registry((_Bundle(deployment),))

    assert registry.manifests() == (deployment,)


@pytest.mark.parametrize(
    "provides",
    [
        ["cap.a"],
        ("Bad Name",),
        ("cap.a", "cap.a"),
    ],
    ids=["not_a_tuple", "malformed_name", "duplicate_name"],
)
def test_registry_rejects_malformed_provided_capability_names(provides):
    manifest = replace(_manifest("provider_plugin"), provides=provides)

    with pytest.raises(
        ExtensionRegistryError, match="declares invalid provided capabilities"
    ):
        build_extension_registry((_Bundle(manifest),))


def test_default_manifest_provides_is_empty():
    assert _manifest("plain_plugin").provides == ()
