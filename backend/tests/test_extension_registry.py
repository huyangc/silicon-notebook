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


def test_deployment_bundle_core_registry_errors_pass_through_unwrapped():
    """Core's own diagnostics must reach the caller verbatim for *every*
    trust tier, deployment included — only a plugin's own exceptions get
    sanitized into an opaque ``plugin_registration_failed``.

    Mirrors ``test_builtin_register_failure_keeps_its_verbatim_registry_error``
    (test_extension_discovery.py) for ``trust="deployment"``, proving the
    exemption is keyed on exception *type* (``ExtensionRegistryError``), not
    on the bundle's trust tier.
    """

    manifest = replace(
        _manifest(
            "deployment_plugin",
            ContributionDeclaration(
                "deployment_plugin.one",
                "ask.completed_observer",
                ContributionKind.OBSERVER,
            ),
            ContributionDeclaration(
                "deployment_plugin.two",
                "ask.completed_observer",
                ContributionKind.OBSERVER,
            ),
        ),
        trust="deployment",
    )
    # register() calls registrar.add() for only the first declared
    # contribution — the second is declared in the manifest but never
    # registered, tripping core's own post-register() consistency check.
    bundle = _Bundle(manifest, implementations=(object(),))

    with pytest.raises(
        ExtensionRegistryError, match="registrations do not match its manifest"
    ) as excinfo:
        build_extension_registry((bundle,))
    # The core diagnostic text is intact — not replaced by a stable reason
    # code, and not an ExtensionDiscoveryError.
    assert "deployment_plugin" in str(excinfo.value)


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


# --- admin admission gate (runtime toggle read side) ------------------------


@pytest.fixture
def admission():
    """The process-global disabled-plugin snapshot, empty on entry and exit."""

    from app.core import extension_admission

    extension_admission.reset_for_tests()
    yield extension_admission
    extension_admission.reset_for_tests()


def _deployment(plugin_id, *declarations, provides=(), **kwargs):
    return replace(
        _manifest(plugin_id, *declarations, **kwargs),
        trust="deployment",
        provides=provides,
    )


def _probe_recording(calls):
    def probe(_context):
        calls.append(_context)
        return Availability.available()

    return probe


def test_admission_holder_publishes_reads_back_resets_and_copies(admission):
    assert admission.disabled_plugin_ids() == frozenset()

    admission.publish_disabled_plugin_ids(frozenset({"corp.a", "corp.b"}))
    assert admission.disabled_plugin_ids() == frozenset({"corp.a", "corp.b"})

    # A publisher that hands over a mutable set must not keep a live handle on
    # the snapshot: the holder copies, so a later mutation is not visible here.
    mutable = {"corp.c"}
    admission.publish_disabled_plugin_ids(mutable)
    mutable.add("corp.d")
    assert admission.disabled_plugin_ids() == frozenset({"corp.c"})

    admission.reset_for_tests()
    assert admission.disabled_plugin_ids() == frozenset()


def test_disabled_deployment_contribution_is_gated_before_its_probe_runs(
    admission,
):
    calls = []
    declaration = ContributionDeclaration(
        "corp.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    registry = build_extension_registry(
        (_Bundle(
            _deployment("corp.plugin", declaration),
            (object(),),
            _probe_recording(calls),
        ),),
        disabled_ids_provider=admission.disabled_plugin_ids,
    )

    assert registry.contribution_availability("corp.probe").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert registry.availability("corp.probe").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert len(calls) == 2

    admission.publish_disabled_plugin_ids(frozenset({"corp.plugin"}))

    for gated in (
        registry.contribution_availability("corp.probe"),
        # ``availability`` gates on its own, ahead of its ``requires`` loop —
        # the two cases below pin what that ordering is for.
        registry.availability("corp.probe"),
    ):
        assert gated.status is AvailabilityStatus.DISABLED
        assert gated.reason_code == "admin_disabled"
    # The disabled plugin's own code never ran: a gate below the probe would
    # still be asking a switched-off plugin whether it is available.
    assert len(calls) == 2

    admission.reset_for_tests()
    assert registry.availability("corp.probe").status is (
        AvailabilityStatus.AVAILABLE
    )


def test_builtin_and_untouched_deployment_plugins_are_never_gated(admission):
    builtin = ContributionDeclaration(
        "builtin.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    other = ContributionDeclaration(
        "corp.other.probe", "ask.completed_observer", ContributionKind.OBSERVER
    )
    registry = build_extension_registry(
        (
            _Bundle(_manifest("corp.plugin", builtin), (object(),)),
            _Bundle(_deployment("corp.other", other), (object(),)),
        ),
        disabled_ids_provider=admission.disabled_plugin_ids,
    )
    # Same id in the disabled set as the built-in bundle above: trust, not the
    # id, decides what the gate may reach.
    admission.publish_disabled_plugin_ids(
        frozenset({"corp.plugin", "corp.absent"})
    )

    assert registry.availability("builtin.probe").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert registry.plugin_runtime_disabled("corp.plugin") is False
    assert registry.availability("corp.other.probe").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert registry.plugin_runtime_disabled("corp.other") is False


def test_capability_gate_covers_deployment_owners_and_spares_core_names(
    admission,
):
    calls = []

    def decision(context):
        calls.append(context)
        return Availability.available()

    registry = build_extension_registry(
        (
            _Bundle(_deployment("corp.plugin", provides=("corp.cap",))),
            _Bundle(replace(
                _manifest("builtin.plugin"), provides=("builtin.cap",)
            )),
        ),
        capability_decisions={
            "corp.cap": decision,
            "builtin.cap": decision,
            # A core decision entry no manifest claims. Composition refuses a
            # plugin that tries to claim one (discovery.py's
            # ``plugin_capability_conflicts_core``), so a core name never
            # acquires a gateable owner — not because of how it is spelled.
            "model:scheduled_access": decision,
        },
        disabled_ids_provider=admission.disabled_plugin_ids,
    )
    admission.publish_disabled_plugin_ids(frozenset({"corp.plugin"}))

    gated = registry.capability_availability("corp.cap")
    assert gated.status is AvailabilityStatus.DISABLED
    assert gated.reason_code == "admin_disabled"
    assert calls == []

    for ungated in ("builtin.cap", "model:scheduled_access"):
        assert registry.capability_availability(ungated).status is (
            AvailabilityStatus.AVAILABLE
        )
    assert len(calls) == 2


def test_a_disabled_plugin_is_gated_before_its_own_requires_are_evaluated(
    admission,
):
    """The gate outranks the contribution's ``requires``, and answers for it.

    A disabled plugin's contribution that requires a core capability must not
    get that capability's decision run on its behalf, and must not have the
    administrator's switch reported as some unrelated reason — which is what
    returning the requirement's own verdict would do the moment that verdict
    is not AVAILABLE.
    """

    calls = []

    def core_decision(context):
        calls.append(context)
        return Availability(
            AvailabilityStatus.UNAVAILABLE, "core_precondition_missing"
        )

    declaration = ContributionDeclaration(
        "corp.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    registry = build_extension_registry(
        (_Bundle(
            _deployment("corp.plugin", declaration, requires=("core.cap",)),
            (object(),),
        ),),
        capability_decisions={"core.cap": core_decision},
        disabled_ids_provider=admission.disabled_plugin_ids,
    )

    # Enabled: the requirement is evaluated and it is what decides.
    enabled = registry.availability("corp.probe")
    assert enabled.status is AvailabilityStatus.UNAVAILABLE
    assert enabled.reason_code == "core_precondition_missing"
    assert len(calls) == 1

    admission.publish_disabled_plugin_ids(frozenset({"corp.plugin"}))

    gated = registry.availability("corp.probe")
    assert gated.status is AvailabilityStatus.DISABLED
    assert gated.reason_code == "admin_disabled"
    assert len(calls) == 1


def test_a_required_capability_is_gated_by_its_owner_not_by_its_consumer(
    admission,
):
    """``availability``'s ``requires`` loop must ask the gated door.

    The consumer here is *built-in*, so nothing about it is gateable; the only
    thing an admin switched off is the deployment plugin that owns the
    capability it requires. Reading the decision catalog directly in that loop
    would answer AVAILABLE — while ``capability_availability`` answers DISABLED
    for the very same name — and would run the disabled plugin's decision to
    produce that answer.
    """

    calls = []

    def owner_decision(context):
        calls.append(context)
        return Availability.available()

    consumer = ContributionDeclaration(
        "builtin.consumer.probe",
        "retrieval.contributor",
        ContributionKind.CONTRIBUTOR,
    )
    registry = build_extension_registry(
        (
            _Bundle(_deployment("corp.owner", provides=("corp.cap",))),
            _Bundle(
                _manifest("builtin.consumer", consumer, requires=("corp.cap",)),
                (object(),),
            ),
        ),
        capability_decisions={"corp.cap": owner_decision},
        disabled_ids_provider=admission.disabled_plugin_ids,
    )

    assert registry.availability("builtin.consumer.probe").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert len(calls) == 1

    admission.publish_disabled_plugin_ids(frozenset({"corp.owner"}))

    for gated in (
        registry.capability_availability("corp.cap"),
        registry.availability("builtin.consumer.probe"),
    ):
        assert gated.status is AvailabilityStatus.DISABLED
        assert gated.reason_code == "admin_disabled"
    # The disabled owner's decision was not run to reach that verdict.
    assert len(calls) == 1


def test_any_owner_of_a_shared_capability_name_disables_it(admission):
    """Composition rejects two plugins claiming one capability name, but this
    class does not, so the ambiguity has a defined answer: any owner disabled
    disables the name."""

    calls = []
    registry = build_extension_registry(
        (
            _Bundle(_deployment("corp.one", provides=("shared.cap",))),
            _Bundle(_deployment("corp.two", provides=("shared.cap",))),
        ),
        capability_decisions={
            "shared.cap": _probe_recording(calls),
        },
        disabled_ids_provider=admission.disabled_plugin_ids,
    )
    # Only one of the two owners is switched off.
    admission.publish_disabled_plugin_ids(frozenset({"corp.two"}))

    gated = registry.capability_availability("shared.cap")
    assert gated.status is AvailabilityStatus.DISABLED
    assert gated.reason_code == "admin_disabled"
    assert calls == []


def test_a_capability_a_builtin_also_provides_is_never_gated(admission):
    """A name a built-in bundle provides stays out of the ownership map, so no
    deployment plugin's toggle can reach it."""

    calls = []
    registry = build_extension_registry(
        (
            _Bundle(replace(
                _manifest("builtin.owner"), provides=("shared.cap",)
            )),
            _Bundle(_deployment("corp.claimant", provides=("shared.cap",))),
        ),
        capability_decisions={
            "shared.cap": _probe_recording(calls),
        },
        disabled_ids_provider=admission.disabled_plugin_ids,
    )
    admission.publish_disabled_plugin_ids(frozenset({"corp.claimant"}))

    assert registry.capability_availability("shared.cap").status is (
        AvailabilityStatus.AVAILABLE
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "ids",
    [
        # The accident this check exists for: a bare id is iterable, so a
        # coercing publisher would store its *characters* and disable nothing.
        "corp.plugin",
        ["corp.plugin"],
        {"corp.plugin": True},
        (name for name in ("corp.plugin",)),
        frozenset({b"corp.plugin"}),
        {"corp.plugin", None},
    ],
    ids=["str", "list", "dict", "generator", "bytes_member", "none_member"],
)
def test_publish_rejects_anything_that_is_not_a_set_of_str(admission, ids):
    with pytest.raises(TypeError, match="disabled plugin ids must"):
        admission.publish_disabled_plugin_ids(ids)
    assert admission.disabled_plugin_ids() == frozenset()

    # A ``dict``'s key view, unlike the dict itself, is a set of str.
    admission.publish_disabled_plugin_ids({"corp.plugin": True}.keys())
    assert admission.disabled_plugin_ids() == frozenset({"corp.plugin"})


def test_a_broken_disabled_ids_provider_leaves_every_plugin_admitted():
    """Read-side failure is always fail-*open*.

    A provider owes the registry a set (the publisher enforces that shape
    loudly at its own end). One that raises or returns something else is a
    wiring bug the request path cannot fix and must not fail on, so the gate
    disappears rather than guessing — the same direction "no toggle row =
    enabled" points.
    """

    declaration = ContributionDeclaration(
        "corp.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )

    def raising():
        raise RuntimeError("refresher thread died")

    for provider in (
        raising,
        lambda: None,
        lambda: object(),
        lambda: "corp.plugin",
        lambda: ["corp.plugin"],
    ):
        registry = build_extension_registry(
            (_Bundle(
                _deployment(
                    "corp.plugin", declaration, provides=("corp.cap",)
                ),
                (object(),),
            ),),
            capability_decisions={
                "corp.cap": lambda _context: Availability.available()
            },
            disabled_ids_provider=provider,
        )

        assert registry.availability("corp.probe").status is (
            AvailabilityStatus.AVAILABLE
        )
        assert registry.capability_availability("corp.cap").status is (
            AvailabilityStatus.AVAILABLE
        )
        assert registry.plugin_runtime_disabled("corp.plugin") is False


def test_a_provider_returning_a_plain_set_still_gates():
    """Any ``collections.abc.Set`` is honoured, not just the ``frozenset`` the
    holder happens to store: the gate only ever tests membership."""

    declaration = ContributionDeclaration(
        "corp.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    registry = build_extension_registry(
        (_Bundle(_deployment("corp.plugin", declaration), (object(),)),),
        disabled_ids_provider=lambda: {"corp.plugin"},
    )

    assert registry.availability("corp.probe").reason_code == "admin_disabled"


def test_plugin_runtime_disabled_requires_freeze_and_knows_three_answers(
    admission,
):
    bundles = (
        _Bundle(_deployment("corp.plugin")),
        _Bundle(_manifest("builtin.plugin")),
    )
    unfrozen = ExtensionRegistry(
        disabled_ids_provider=admission.disabled_plugin_ids
    )
    for bundle in bundles:
        unfrozen.register(bundle)
    with pytest.raises(ExtensionRegistryError, match="not frozen"):
        unfrozen.plugin_runtime_disabled("corp.plugin")

    registry = unfrozen.freeze()
    admission.publish_disabled_plugin_ids(
        frozenset({"corp.plugin", "builtin.plugin", "corp.never_loaded"})
    )

    assert registry.plugin_runtime_disabled("corp.plugin") is True
    assert registry.plugin_runtime_disabled("builtin.plugin") is False
    assert registry.plugin_runtime_disabled("corp.never_loaded") is False


def test_registries_without_a_provider_ignore_the_published_snapshot(
    admission,
):
    declaration = ContributionDeclaration(
        "corp.probe", "retrieval.contributor", ContributionKind.CONTRIBUTOR
    )
    bundles = (_Bundle(
        _deployment("corp.plugin", declaration, provides=("corp.cap",)),
        (object(),),
    ),)
    decisions = {"corp.cap": lambda _context: Availability.available()}
    admission.publish_disabled_plugin_ids(frozenset({"corp.plugin"}))

    omitted = build_extension_registry(bundles, capability_decisions=decisions)
    explicit_none = build_extension_registry(
        bundles,
        capability_decisions=decisions,
        disabled_ids_provider=None,
    )

    for registry in (omitted, explicit_none):
        assert registry.availability("corp.probe") == Availability.available()
        assert registry.contribution_availability("corp.probe") == (
            Availability.available()
        )
        assert registry.capability_availability("corp.cap") == (
            Availability.available()
        )
        assert registry.plugin_runtime_disabled("corp.plugin") is False
