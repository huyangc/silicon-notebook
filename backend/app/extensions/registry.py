"""Startup-built extension topology with request-time availability checks."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Set
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from app.domain.reflect_action import (
    EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS,
    REFLECT_ACTION_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_ENUM_VALUES_MAX,
    REFLECT_ACTION_ENUM_VALUE_RE,
    REFLECT_ACTION_NAME_RE,
    REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS,
    REFLECT_ACTION_PARAMETER_NAME_RE,
    REFLECT_ACTION_PARAMETERS_MAX,
    RESERVED_REFLECT_KEYS,
    RESERVED_REFLECT_PARAMETER_NAMES,
    ReflectActionDescriptor,
    ReflectActionParameter,
    ReflectActionSpec,
    contains_control_characters,
)
from app.extension_sdk import (
    ASK_ENGINE_POINT,
    ASK_REFLECT_ACTION_POINT,
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionBundle,
    ExtensionContribution,
    ExtensionManifest,
    UiContributionDeclaration,
)
from app.extensions.capabilities import (
    EMPTY_CAPABILITY_CATALOG,
    CapabilityDecisionCatalog,
)
from app.extensions.discovery import ExtensionDiscoveryError


class ExtensionRegistryError(ValueError):
    """Invalid extension topology discovered during startup."""


_STABLE_REASON = re.compile(r"^[a-z][a-z0-9_]*$")
_STABLE_METADATA_ID = re.compile(
    r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
)
_WORKSPACE_UI_SLOTS = frozenset({
    "workspace.side_panel",
    "source.detail_section",
})
# One shared immutable verdict for every admission-gated evaluation. It is a
# frozen dataclass, so handing the same instance to every caller is safe, and
# ``admin_disabled`` satisfies ``_STABLE_REASON`` like any probe-supplied code.
_ADMIN_DISABLED = Availability(
    AvailabilityStatus.DISABLED, reason_code="admin_disabled"
)


@dataclass(frozen=True)
class RegisteredContribution:
    plugin_id: str
    contribution: ExtensionContribution


def _reject_reflect_text(
    locator: str, field_name: str, value: object, limit: int
) -> None:
    """Loudly reject a descriptor string that is not one bounded, non-empty line.

    Empty, over-length and control characters are all startup FAILURES, not
    clamps or strips.  A descriptor is deployment configuration, not user data:
    silently trimming it would show the model a different description than the
    operator wrote, a surviving ``\\n`` would let plugin text pose as another
    rule in core's own prompt template, and an empty one would put a bare action
    name in front of the model with nothing saying what it does — a tool the
    model can only guess at is worse than a tool it does not have.

    Blank-after-strip counts as empty: ``"   "`` renders exactly as ``""`` does.

    The message names only ``locator`` (contribution id and plugin id, both
    core-validated stable identifiers) and the field name — never the offending
    text, which on a deployment plugin may carry an API key or an internal
    hostname.
    """

    if type(value) is not str:
        raise ExtensionRegistryError(
            f"reflect action {locator} has a non-string {field_name}"
        )
    if not value.strip():
        raise ExtensionRegistryError(
            f"reflect action {locator} has an empty {field_name}"
        )
    if len(value) > limit:
        raise ExtensionRegistryError(
            f"reflect action {locator} has a {field_name} longer than "
            f"{limit} characters"
        )
    if contains_control_characters(value):
        raise ExtensionRegistryError(
            f"reflect action {locator} has a {field_name} containing "
            "control characters"
        )


def _validate_reflect_parameters(
    locator: str, descriptor: ReflectActionDescriptor
) -> None:
    if type(descriptor.parameters) is not tuple:
        raise ExtensionRegistryError(
            f"reflect action {locator} must declare parameters as a tuple"
        )
    if len(descriptor.parameters) > REFLECT_ACTION_PARAMETERS_MAX:
        raise ExtensionRegistryError(
            f"reflect action {locator} declares more than "
            f"{REFLECT_ACTION_PARAMETERS_MAX} parameters"
        )
    seen: set[str] = set()
    for parameter in descriptor.parameters:
        if type(parameter) is not ReflectActionParameter:
            raise ExtensionRegistryError(
                f"reflect action {locator} declares a parameter that is "
                "not a ReflectActionParameter"
            )
        if (
            type(parameter.name) is not str
            or not REFLECT_ACTION_PARAMETER_NAME_RE.fullmatch(parameter.name)
        ):
            raise ExtensionRegistryError(
                f"reflect action {locator} declares a parameter whose "
                "name is not a stable lowercase identifier"
            )
        # Safe to quote from here on: the name matched the identifier pattern.
        if parameter.name in RESERVED_REFLECT_PARAMETER_NAMES:
            raise ExtensionRegistryError(
                f"reflect action {locator} declares reserved parameter "
                f"name {parameter.name!r}"
            )
        if parameter.name in seen:
            raise ExtensionRegistryError(
                f"reflect action {locator} declares parameter "
                f"{parameter.name!r} twice"
            )
        seen.add(parameter.name)
        # Exact ``bool``, not truthiness: ``required="false"`` is a plugin bug
        # that reads as *required* everywhere it is tested, so the run would
        # fail closed on a missing argument the author believed was optional.
        if type(parameter.required) is not bool:
            raise ExtensionRegistryError(
                f"reflect action {locator} parameter {parameter.name!r} "
                "declares a non-boolean required flag"
            )
        _reject_reflect_text(
            locator,
            f"parameter {parameter.name!r} description",
            parameter.description,
            REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS,
        )
        _validate_reflect_parameter_values(locator, parameter)


def _validate_reflect_parameter_values(
    locator: str, parameter: ReflectActionParameter
) -> None:
    """The kind/values pairing: an enum needs values, text must not carry any.

    A text parameter with values would render as a free-text slot in the prompt
    line and as an ``a|b`` template in the schema hint — two projections of one
    descriptor disagreeing, which is precisely the failure the "one description,
    three places" discipline exists to prevent.
    """

    if type(parameter.values) is not tuple:
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "must declare values as a tuple"
        )
    if parameter.kind == "text":
        if parameter.values:
            raise ExtensionRegistryError(
                f"reflect action {locator} parameter "
                f"{parameter.name!r} is text but declares enum values"
            )
        return
    if parameter.kind != "enum":
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "has a kind other than 'text' or 'enum'"
        )
    if not parameter.values:
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "is an enum with no values"
        )
    if len(parameter.values) > REFLECT_ACTION_ENUM_VALUES_MAX:
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            f"declares more than {REFLECT_ACTION_ENUM_VALUES_MAX} enum values"
        )
    if any(
        type(value) is not str or not REFLECT_ACTION_ENUM_VALUE_RE.fullmatch(value)
        for value in parameter.values
    ):
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "declares an enum value that is not a stable lowercase identifier"
        )
    if len(set(parameter.values)) != len(parameter.values):
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "declares a duplicate enum value"
        )
    # TWO values minimum, checked last so the more specific failures above keep
    # naming themselves. A one-value enum renders as a schema example with no
    # ``|`` in it -- and ``model_json._collect_shape_deviations`` decides "this
    # is an enum" by looking for exactly that character. So a single-value
    # parameter would be advertised to the model as a closed choice while the
    # shape walk treated it as free text and never reported it, which is
    # the one shape the projection's schema comment promises cannot happen. A
    # parameter with only one legal value is also not a choice: it belongs in
    # the action description, or the plugin should not ask for it at all.
    if len(parameter.values) < 2:
        raise ExtensionRegistryError(
            f"reflect action {locator} parameter {parameter.name!r} "
            "is an enum with fewer than two values"
        )


def _validate_reflect_descriptor(
    locator: str, descriptor: ReflectActionDescriptor
) -> None:
    if (
        type(descriptor.name) is not str
        or not REFLECT_ACTION_NAME_RE.fullmatch(descriptor.name)
    ):
        raise ExtensionRegistryError(
            f"reflect action {locator} has a name that is not a stable "
            "lowercase identifier"
        )
    # Both halves of the reserved set matter. Colliding with a CORE ACTION ID
    # would shadow a built-in action in the whitelist; colliding with a SCHEMA
    # FIELD name would make the model write this action's arguments into a core
    # slot, because a plugin action's parameters arrive as one nested object
    # keyed by the action name.
    if descriptor.name in RESERVED_REFLECT_KEYS:
        raise ExtensionRegistryError(
            f"reflect action {locator} uses reserved reflect key "
            f"{descriptor.name!r}"
        )
    # All three plugin-authored strings go through the one rail, so "empty",
    # "too long" and "carries a control character" cannot mean different things
    # depending on which field they happen to be in.
    _reject_reflect_text(
        locator,
        "description",
        descriptor.description,
        REFLECT_ACTION_DESCRIPTION_MAX_CHARS,
    )
    _reject_reflect_text(
        locator,
        "source_label",
        descriptor.source_label,
        EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS,
    )
    if type(descriptor.max_calls_per_run) is not int or descriptor.max_calls_per_run < 1:
        raise ExtensionRegistryError(
            f"reflect action {locator} must allow at least one call per run"
        )
    _validate_reflect_parameters(locator, descriptor)


class _BundleRegistrar:
    def __init__(self, registry: "ExtensionRegistry", manifest: ExtensionManifest):
        self._registry = registry
        self._manifest = manifest

    def add(self, contribution: ExtensionContribution) -> None:
        self._registry._add(self._manifest, contribution)

    def _add_kind(
        self, contribution: ExtensionContribution, expected: ContributionKind
    ) -> None:
        if contribution.declaration.kind is not expected:
            raise ExtensionRegistryError(
                f"contribution {contribution.declaration.id!r} is not {expected.value}"
            )
        self.add(contribution)

    def add_provider(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.PROVIDER)

    def add_provider_chain_link(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.PROVIDER_CHAIN)

    def add_contributor(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.CONTRIBUTOR)

    def add_observer(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.OBSERVER)


class ExtensionRegistry:
    """Mutable only during composition; topology is immutable after ``freeze``.

    Availability probes remain live and are called for each request/context.
    Freezing topology therefore never snapshots provider health, permissions,
    configuration, or other request-time state. The admin admission gate
    (``disabled_ids_provider``) is live in exactly the same sense: freezing
    settles *which* plugins the gate may apply to, never whether it currently
    applies to any of them — that verdict is re-read from the process snapshot
    on every evaluation, which is what lets an administrator switch a
    deployment plugin off without a restart.
    """

    def __init__(
        self,
        capability_catalog: CapabilityDecisionCatalog | None = None,
        *,
        disabled_ids_provider: Callable[[], Set[str]] | None = None,
    ) -> None:
        self._frozen = False
        self._capabilities = capability_catalog or EMPTY_CAPABILITY_CATALOG
        self._manifests: dict[str, ExtensionManifest] = {}
        self._contributions: dict[str, RegisteredContribution] = {}
        self._points: dict[str, list[RegisteredContribution]] = defaultdict(list)
        self._ui_contributions: dict[str, tuple[str, UiContributionDeclaration]] = {}
        # ``None`` means no gate at all: every evaluation below then takes the
        # same path it took before this parameter existed, down to the returned
        # objects. Registries built by tests and by any caller that has not
        # opted in stay in that state.
        self._disabled_ids_provider = disabled_ids_provider
        # Filled by ``freeze``; empty until then, and unreachable before then
        # because every consumer goes through ``_require_frozen``.
        self._gateable_plugin_ids: frozenset[str] = frozenset()
        self._gateable_capability_owners: Mapping[str, frozenset[str]] = (
            MappingProxyType({})
        )
        # Also filled by ``freeze``. Empty for every deployment with no
        # ``ask.reflect_action`` contribution, which is the byte-for-byte
        # "reflect never heard of plugin actions" state.
        self._reflect_action_specs: tuple[ReflectActionSpec, ...] = ()

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, bundle: ExtensionBundle) -> None:
        if self._frozen:
            raise ExtensionRegistryError("extension registry is frozen")
        manifest = bundle.manifest
        if (
            type(manifest.id) is not str
            or not _STABLE_METADATA_ID.fullmatch(manifest.id)
            or not manifest.version
            or not manifest.display_name
        ):
            raise ExtensionRegistryError("extension manifest identifiers must be non-empty")
        if manifest.api_version != EXTENSION_API_VERSION:
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} uses unsupported API {manifest.api_version!r}"
            )
        # "isolated" is a reserved value for a future process-isolated trust
        # tier — it must never enter this in-process registry. Only
        # "builtin" (shipped with this build) and "deployment"
        # (EXTENSIONS_CONFIG-loaded, in-process) bundles register here.
        if manifest.trust not in {"builtin", "deployment"}:
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} has invalid trust classification"
            )
        if manifest.id in self._manifests:
            raise ExtensionRegistryError(f"duplicate extension id {manifest.id!r}")
        declaration_ids: list[str] = []
        for declaration in manifest.contributions:
            if (
                type(declaration) is not ContributionDeclaration
                or type(declaration.id) is not str
                or not _STABLE_METADATA_ID.fullmatch(declaration.id)
                or type(declaration.point) is not str
                or not _STABLE_METADATA_ID.fullmatch(declaration.point)
                or type(declaration.kind) is not ContributionKind
                or not self._valid_ordering(declaration.after)
                or not self._valid_ordering(declaration.before)
                or (
                    declaration.kind is not ContributionKind.PROVIDER_CHAIN
                    and (declaration.after or declaration.before)
                )
            ):
                raise ExtensionRegistryError(
                    "contribution declaration must use stable metadata identifiers and kind"
                )
            declaration_ids.append(declaration.id)
        if len(declaration_ids) != len(set(declaration_ids)):
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} declares duplicate contribution ids"
            )
        if type(manifest.ui_contributions) is not tuple:
            raise ExtensionRegistryError("UI contributions must be an immutable tuple")
        ui_ids: list[str] = []
        for declaration in manifest.ui_contributions:
            if (
                type(declaration) is not UiContributionDeclaration
                or type(declaration.id) is not str
                or not _STABLE_METADATA_ID.fullmatch(declaration.id)
                or type(declaration.slot) is not str
                or declaration.slot not in _WORKSPACE_UI_SLOTS
                or type(declaration.capability) is not str
                or not _STABLE_METADATA_ID.fullmatch(declaration.capability)
            ):
                raise ExtensionRegistryError(
                    "UI contribution declarations require stable ids, capabilities, and canonical slots"
                )
            if declaration.id in self._ui_contributions or declaration.id in self._contributions:
                raise ExtensionRegistryError(
                    f"duplicate UI contribution id {declaration.id!r}"
                )
            ui_ids.append(declaration.id)
        if len(ui_ids) != len(set(ui_ids)):
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} declares duplicate UI contribution ids"
            )
        if set(ui_ids) & set(declaration_ids):
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} reuses one id across runtime and UI contributions"
            )
        if (
            type(manifest.provides) is not tuple
            or any(
                type(name) is not str or not _STABLE_METADATA_ID.fullmatch(name)
                for name in manifest.provides
            )
            or len(manifest.provides) != len(set(manifest.provides))
        ):
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} declares invalid provided capabilities"
            )
        self._manifests[manifest.id] = manifest
        for declaration in manifest.ui_contributions:
            self._ui_contributions[declaration.id] = (manifest.id, declaration)
        before = set(self._contributions)
        try:
            bundle.register(_BundleRegistrar(self, manifest))
            registered = set(self._contributions) - before
            if registered != set(declaration_ids):
                raise ExtensionRegistryError(
                    f"extension {manifest.id!r} registrations do not match its manifest"
                )
        except ExtensionRegistryError:
            # Core's own diagnostics — including the "registrations do not
            # match its manifest" check just above — are constructed entirely
            # from stable metadata identifiers this module already validated,
            # never from plugin-controlled text. They stay verbatim and
            # unwrapped for *every* trust tier, deployment included, because
            # sanitizing them would hide core's most actionable rejection
            # message behind an opaque reason code.
            #
            # ⚠ Residual risk, accepted and logged (plan §5): a plugin *can*
            # reach this branch on purpose. ``ExtensionRegistryError`` is not
            # on the SDK surface — ``app.extension_sdk`` does not re-export it
            # — but nothing stops out-of-repo code from doing
            # ``from app.extensions.registry import ExtensionRegistryError``
            # and raising it with its own message, which would then reach the
            # operator's log unsanitized. A plugin has no legitimate reason to
            # raise core's internal registry exception, and the blast radius is
            # a startup-time log line (registration is refused either way), so
            # this is not defended against here.
            self._rollback_manifest(manifest.id, before)
            raise
        except Exception as exc:
            self._rollback_manifest(manifest.id, before)
            if manifest.trust == "deployment":
                # An out-of-repo plugin's ``register()`` may raise anything, and
                # a deployment plugin routinely holds an API key or an internal
                # hostname on the bundle it is registering from — a plain
                # ``raise ValueError(f"... {self.settings.token} ...")`` would put
                # that value into the traceback and into every log that renders
                # it.  Registration is therefore sanitized exactly like every
                # other deployment-plugin rejection: plugin id, stable reason
                # code, exception *class* name, and ``from None`` so the original
                # never reaches a traceback.  Built-in bundles keep their
                # verbatim ``ExtensionRegistryError`` — that text is core's own
                # diagnostic, not plugin-controlled.
                raise ExtensionDiscoveryError(
                    manifest.id,
                    "plugin_registration_failed",
                    exception_type=type(exc).__name__,
                ) from None
            raise

    def _rollback_manifest(self, plugin_id: str, prior_ids: set[str]) -> None:
        self._manifests.pop(plugin_id, None)
        self._ui_contributions = {
            contribution_id: registered
            for contribution_id, registered in self._ui_contributions.items()
            if registered[0] != plugin_id
        }
        for contribution_id in set(self._contributions) - prior_ids:
            registered = self._contributions.pop(contribution_id)
            point = registered.contribution.declaration.point
            self._points[point] = [
                item for item in self._points[point] if item is not registered
            ]

    def _add(
        self, manifest: ExtensionManifest, contribution: ExtensionContribution
    ) -> None:
        if self._frozen:
            raise ExtensionRegistryError("extension registry is frozen")
        declaration = contribution.declaration
        if (
            type(declaration) is not ContributionDeclaration
            or type(declaration.id) is not str
            or not _STABLE_METADATA_ID.fullmatch(declaration.id)
            or type(declaration.point) is not str
            or not _STABLE_METADATA_ID.fullmatch(declaration.point)
            or type(declaration.kind) is not ContributionKind
            or not self._valid_ordering(declaration.after)
            or not self._valid_ordering(declaration.before)
            or (
                declaration.kind is not ContributionKind.PROVIDER_CHAIN
                and (declaration.after or declaration.before)
            )
        ):
            raise ExtensionRegistryError(
                "contribution declaration must use stable metadata identifiers and kind"
            )
        declared = {item.id: item for item in manifest.contributions}
        if declared.get(declaration.id) != declaration:
            raise ExtensionRegistryError(
                f"contribution {declaration.id!r} differs from its manifest declaration"
            )
        if declaration.id in self._contributions or declaration.id in self._ui_contributions:
            raise ExtensionRegistryError(
                f"duplicate contribution id {declaration.id!r}"
            )
        registered = RegisteredContribution(manifest.id, contribution)
        self._contributions[declaration.id] = registered
        self._points[declaration.point].append(registered)

    def freeze(self) -> "ExtensionRegistry":
        if self._frozen:
            return self
        self._validate_dependencies()
        self._validate_required_capabilities()
        self._validate_ui_capabilities()
        for point, registrations in self._points.items():
            kinds = {
                item.contribution.declaration.kind for item in registrations
            }
            if ContributionKind.PROVIDER_CHAIN in kinds and len(kinds) != 1:
                raise ExtensionRegistryError(
                    f"provider chain {point!r} mixes contribution kinds"
                )
            providers = [
                item
                for item in registrations
                if item.contribution.declaration.kind is ContributionKind.PROVIDER
            ]
            # ``ask.engine`` is a provider *set*: one provider contribution is
            # one independently selectable mode. Every other provider point
            # keeps the original exactly-one topology rule.
            if len(providers) > 1 and point != ASK_ENGINE_POINT:
                raise ExtensionRegistryError(
                    f"extension point {point!r} has multiple single providers"
                )
            if registrations and all(
                item.contribution.declaration.kind
                is ContributionKind.PROVIDER_CHAIN
                for item in registrations
            ):
                registrations[:] = self._ordered_provider_chain(
                    point, registrations
                )
            else:
                registrations.sort(
                    key=lambda item: item.contribution.declaration.id
                )
        # After the ordering loop, so the spec tuple is in exactly the order
        # ``contributions(ASK_REFLECT_ACTION_POINT)`` reports.
        self._validate_reflect_actions()
        self._freeze_admission_scope()
        self._manifests = MappingProxyType(dict(self._manifests))  # type: ignore[assignment]
        self._contributions = MappingProxyType(  # type: ignore[assignment]
            dict(self._contributions)
        )
        self._points = MappingProxyType(  # type: ignore[assignment]
            {point: tuple(items) for point, items in self._points.items()}
        )
        self._ui_contributions = MappingProxyType(  # type: ignore[assignment]
            dict(sorted(self._ui_contributions.items()))
        )
        self._frozen = True
        return self

    def _validate_reflect_actions(self) -> None:
        """Settle the ``ask.reflect_action`` topology, once, at freeze.

        Every rule here is a STARTUP failure rather than a runtime skip: a
        reflect action is explicit deployment configuration, frozen at boot
        (SOP §9.1), and a descriptor the model would be shown wrongly — or not
        at all — is not something to discover on the first question of the day.

        Action-name uniqueness is global across plugins, not per plugin. The
        model types a bare name, so two plugins offering ``search_papers`` are
        one ambiguous action, and namespacing them would mean showing the model
        a plugin id it must never see (design document §九 invariant 7).

        Every rejection here names the contribution id AND the plugin id. Both
        are stable metadata identifiers this class already validated — never
        plugin-authored text — and on a deployment running several plugins the
        contribution id alone does not say whose package to go and fix. The
        model never sees either; this is an operator-facing startup log.
        """

        specs: list[ReflectActionSpec] = []
        claimed: dict[str, str] = {}
        for registered in self._points.get(ASK_REFLECT_ACTION_POINT, ()):
            declaration = registered.contribution.declaration
            locator = f"{declaration.id!r} (plugin {registered.plugin_id!r})"
            if declaration.kind is not ContributionKind.CONTRIBUTOR:
                raise ExtensionRegistryError(
                    f"reflect action {locator} must be registered as "
                    f"{ContributionKind.CONTRIBUTOR.value}"
                )
            descriptor = getattr(
                registered.contribution.implementation, "descriptor", None
            )
            if type(descriptor) is not ReflectActionDescriptor:
                raise ExtensionRegistryError(
                    f"reflect action {locator} does not expose a "
                    "ReflectActionDescriptor"
                )
            _validate_reflect_descriptor(locator, descriptor)
            owner = claimed.get(descriptor.name)
            if owner is not None:
                raise ExtensionRegistryError(
                    f"reflect action name {descriptor.name!r} is claimed by both "
                    f"{owner} and {locator}"
                )
            claimed[descriptor.name] = locator
            specs.append(
                ReflectActionSpec(
                    declaration.id, registered.plugin_id, descriptor
                )
            )
        self._reflect_action_specs = tuple(specs)

    def reflect_action_specs(self) -> tuple[ReflectActionSpec, ...]:
        """The validated reflect-action topology, in frozen registration order.

        Descriptors only — no availability, no admin gate, no budget. Deciding
        which of these a given run may actually offer belongs to the host
        (design document §3.1), which re-reads the live probes every run.
        """

        self._require_frozen()
        return self._reflect_action_specs

    def _freeze_admission_scope(self) -> None:
        """Settle *what the admission gate is allowed to reach*, once.

        Two frozen facts, both pure topology: the set of deployment-trust
        plugin ids, and which of them owns each capability name. Nothing here
        asks whether anything is currently disabled — that stays live.

        Only ``trust == "deployment"`` plugins are gateable. Built-in bundles
        ship with this build and are not administrable, so their ids never
        enter the set and the capabilities they provide never enter the
        mapping; core's own capability names (the decisions the composition
        root mints, which no manifest ``provides``) are absent for the same
        reason, so a core decision can never be gated by a plugin's toggle.

        Ownership is stored as a *set* of ids per capability rather than a
        single id even though composition admits exactly one owner
        (``capability_decisions_from_bundles`` rejects a name claimed twice,
        by core or by another plugin, at startup). This class does not itself
        enforce that uniqueness — a registry assembled directly, as tests do,
        can hold two deployment manifests providing one name — and a
        ``dict[str, str]`` would silently resolve such a pair by registration
        order. A set answers it explicitly instead: any owner disabled
        disables the capability.
        """

        gateable: dict[str, set[str]] = {}
        builtin_provided: set[str] = set()
        deployment_ids: set[str] = set()
        for plugin_id, manifest in self._manifests.items():
            if manifest.trust != "deployment":
                builtin_provided.update(manifest.provides)
                continue
            deployment_ids.add(plugin_id)
            for capability in manifest.provides:
                gateable.setdefault(capability, set()).add(plugin_id)
        self._gateable_plugin_ids = frozenset(deployment_ids)
        self._gateable_capability_owners = MappingProxyType({
            capability: frozenset(owners)
            for capability, owners in gateable.items()
            if capability not in builtin_provided
        })

    def _disabled_ids(self) -> Set[str]:
        """The live disabled-plugin snapshot, or the empty set.

        A provider is out-of-registry code, and this runs inside request-path
        availability evaluation, so a raising provider must not become a
        request failure. A broken gate degrades to *no gate*, matching the
        storage layer's "no row = enabled" default: an operator who never
        disabled anything, and an operator whose refresh just broke, both keep
        every loaded plugin admitted.

        The snapshot is used, never rebuilt: membership and ``isdisjoint`` are
        all the gate needs, so nothing is allocated per evaluation. Shape is
        the publisher's contract — ``app.core.extension_admission`` rejects a
        non-set loudly when it is *published* — and a provider that returns
        some other shape anyway is a wiring bug this path can only fail open
        on, exactly as it does for one that raises.
        """

        provider = self._disabled_ids_provider
        if provider is None:
            return frozenset()
        try:
            disabled = provider()
        except Exception:
            return frozenset()
        return disabled if isinstance(disabled, Set) else frozenset()

    def _plugin_admission_blocked(self, plugin_id: str) -> bool:
        return (
            plugin_id in self._gateable_plugin_ids
            and plugin_id in self._disabled_ids()
        )

    def plugin_runtime_disabled(self, plugin_id: str) -> bool:
        """True when an admin has switched this deployment plugin off.

        False for a built-in plugin, for an id this registry never loaded, and
        for every plugin when no provider is wired — the three ways a caller
        can ask about something the gate does not reach.
        """

        self._require_frozen()
        return self._plugin_admission_blocked(plugin_id)

    def _validate_dependencies(self) -> None:
        known = set(self._manifests)
        graph: dict[str, set[str]] = {}
        for plugin_id, manifest in self._manifests.items():
            missing = set(manifest.depends_on) - known
            if missing:
                raise ExtensionRegistryError(
                    f"extension {plugin_id!r} depends on unknown extensions {sorted(missing)!r}"
                )
            graph[plugin_id] = set(manifest.depends_on)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(plugin_id: str) -> None:
            if plugin_id in visiting:
                raise ExtensionRegistryError("extension dependency cycle")
            if plugin_id in visited:
                return
            visiting.add(plugin_id)
            for dependency in graph[plugin_id]:
                visit(dependency)
            visiting.remove(plugin_id)
            visited.add(plugin_id)

        for plugin_id in graph:
            visit(plugin_id)

    def _validate_required_capabilities(self) -> None:
        for plugin_id, manifest in self._manifests.items():
            missing = sorted(
                capability
                for capability in manifest.requires
                if not self._capabilities.has(capability)
            )
            if missing:
                raise ExtensionRegistryError(
                    f"extension {plugin_id!r} requires capabilities without "
                    f"decision entries {missing!r}"
                )

    def _validate_ui_capabilities(self) -> None:
        for contribution_id, (_plugin_id, declaration) in self._ui_contributions.items():
            if not self._capabilities.has(declaration.capability):
                raise ExtensionRegistryError(
                    f"UI contribution {contribution_id!r} references a capability without a decision entry"
                )

    @staticmethod
    def _valid_ordering(value: object) -> bool:
        return (
            type(value) is tuple
            and all(
                type(item) is str and _STABLE_METADATA_ID.fullmatch(item)
                for item in value
            )
            and len(value) == len(set(value))
        )

    @staticmethod
    def _ordered_provider_chain(
        point: str,
        registrations: list[RegisteredContribution],
    ) -> list[RegisteredContribution]:
        by_id = {
            item.contribution.declaration.id: item for item in registrations
        }
        graph = {contribution_id: set() for contribution_id in by_id}
        indegree = {contribution_id: 0 for contribution_id in by_id}

        def edge(source: str, target: str) -> None:
            if source == target:
                raise ExtensionRegistryError(
                    f"provider chain {point!r} contains a self dependency"
                )
            if source not in by_id or target not in by_id:
                raise ExtensionRegistryError(
                    f"provider chain {point!r} references an unknown link"
                )
            if target not in graph[source]:
                graph[source].add(target)
                indegree[target] += 1

        for contribution_id, registered in by_id.items():
            declaration = registered.contribution.declaration
            for dependency in declaration.after:
                edge(dependency, contribution_id)
            for successor in declaration.before:
                edge(contribution_id, successor)

        ready = sorted(
            contribution_id
            for contribution_id, degree in indegree.items()
            if degree == 0
        )
        ordered: list[RegisteredContribution] = []
        while ready:
            contribution_id = ready.pop(0)
            ordered.append(by_id[contribution_id])
            for successor in sorted(graph[contribution_id]):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
                    ready.sort()
        if len(ordered) != len(registrations):
            raise ExtensionRegistryError(
                f"provider chain {point!r} contains an ordering cycle"
            )
        return ordered

    def manifests(self) -> tuple[ExtensionManifest, ...]:
        self._require_frozen()
        return tuple(self._manifests.values())

    def contributions(self, point: str) -> tuple[RegisteredContribution, ...]:
        self._require_frozen()
        return tuple(self._points.get(point, ()))

    def ui_contributions(
        self,
    ) -> tuple[tuple[ExtensionManifest, UiContributionDeclaration], ...]:
        """Return the frozen metadata-only UI topology in stable id order."""

        self._require_frozen()
        return tuple(
            (self._manifests[plugin_id], declaration)
            for plugin_id, declaration in self._ui_contributions.values()
        )

    def availability(
        self, contribution_id: str, context: object | None = None
    ) -> Availability:
        self._require_frozen()
        registered = self._contributions.get(contribution_id)
        if registered is None:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="unknown_contribution",
            )
        # Third gate point, and the earliest one this method could have: ahead
        # of the ``requires`` loop, not delegated to ``contribution_availability``
        # at the end. A disabled plugin's contribution that requires a core or
        # built-in capability would otherwise run that decision first — work on
        # behalf of a plugin nobody may use — and, whenever the decision came
        # back non-AVAILABLE, would return *its* reason instead of
        # ``admin_disabled``, hiding the administrator's switch behind an
        # unrelated one. All three entry points therefore answer identically
        # for a disabled plugin, and none of them evaluates anything first.
        if self._plugin_admission_blocked(registered.plugin_id):
            return _ADMIN_DISABLED
        manifest = self._manifests[registered.plugin_id]
        for capability in manifest.requires:
            # Through ``capability_availability``, not the catalog directly:
            # a required capability can be *another* plugin's, and that owner
            # may be the one an admin disabled. Reading the catalog here would
            # both answer AVAILABLE for a capability the very same registry
            # calls DISABLED one method over, and run the disabled plugin's
            # decision to get that answer. A capability with no gateable owner
            # — core's own, or a built-in's — costs one absent dict lookup on
            # the way through.
            decision = self.capability_availability(capability, context)
            if decision.status is not AvailabilityStatus.AVAILABLE:
                return decision
        return self.contribution_availability(contribution_id, context)

    def capability_availability(
        self, capability: str, context: object | None = None
    ) -> Availability:
        """Evaluate one frozen capability decision without a contribution probe.

        One of the three admission gate points, and the only one keyed on a
        capability's *owner* rather than on a contribution's plugin. This entry
        is not reachable only through ``availability`` — ``parser_chain`` and
        ``retrieval`` call it directly to ask "can this capability be used at
        all", with no contribution in hand — so gating it separately is what
        keeps a disabled plugin's capability from being consulted through that
        door. A capability with no gateable owner (core's own, or one a
        built-in provides) never matches and falls straight through to the
        catalog, unchanged.
        """

        self._require_frozen()
        owners = self._gateable_capability_owners.get(capability)
        # ``isdisjoint`` rather than ``&``: the question is whether *any* owner
        # is disabled, and intersecting builds a set to throw away.
        if owners and not owners.isdisjoint(self._disabled_ids()):
            return _ADMIN_DISABLED
        return self._capabilities.availability(capability, context)

    def contribution_availability(
        self, contribution_id: str, context: object | None = None
    ) -> Availability:
        """Evaluate only the contribution's I/O-free live probe.

        One of the three admission gate points, and the reason the gate is not
        installed in ``availability`` alone: ``retrieval`` and ``parser_chain``
        reach this method directly for every contributor and chain link they
        dispatch, so a gate only one level up would leave exactly those two
        hosts running a disabled plugin's code. Every host that arrives through
        ``availability`` instead (ask, ask_engine, indexing, report,
        report_export, gap_consult) is stopped by that method's own check
        before it ever gets here.

        The gate sits above the probe, not beside it: a disabled plugin's own
        code must not run, and it is not asked whether it is available.
        """

        self._require_frozen()
        registered = self._contributions.get(contribution_id)
        if registered is None:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="unknown_contribution",
            )
        if self._plugin_admission_blocked(registered.plugin_id):
            return _ADMIN_DISABLED
        probe = registered.contribution.availability
        if probe is None:
            return Availability.available()
        try:
            result = probe(context)
        except Exception:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="availability_probe_failed",
            )
        if type(result) is not Availability:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_availability_probe",
            )
        if not isinstance(result.status, AvailabilityStatus):
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_availability_status",
            )
        if type(result.reason_code) is not str:
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_availability_reason",
            )
        reason = result.reason_code
        if reason and not _STABLE_REASON.fullmatch(reason):
            return Availability(
                AvailabilityStatus.UNAVAILABLE,
                reason_code="invalid_availability_reason",
            )
        return result

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise ExtensionRegistryError("extension registry is not frozen")


def frozen_registry(
    bundles: Iterable[ExtensionBundle] = (),
    *,
    capability_catalog: CapabilityDecisionCatalog | None = None,
    disabled_ids_provider: Callable[[], Set[str]] | None = None,
) -> ExtensionRegistry:
    registry = ExtensionRegistry(
        capability_catalog, disabled_ids_provider=disabled_ids_provider
    )
    for bundle in bundles:
        registry.register(bundle)
    return registry.freeze()
