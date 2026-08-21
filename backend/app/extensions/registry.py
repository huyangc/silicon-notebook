"""Startup-built extension topology with request-time availability checks."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    Availability,
    AvailabilityStatus,
    ContributionKind,
    ExtensionBundle,
    ExtensionContribution,
    ExtensionManifest,
)


class ExtensionRegistryError(ValueError):
    """Invalid extension topology discovered during startup."""


@dataclass(frozen=True)
class RegisteredContribution:
    plugin_id: str
    contribution: ExtensionContribution


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

    def add_auditor(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.AUDITOR)

    def add_observer(self, contribution: ExtensionContribution) -> None:
        self._add_kind(contribution, ContributionKind.OBSERVER)


class ExtensionRegistry:
    """Mutable only during composition; topology is immutable after ``freeze``.

    Availability probes remain live and are called for each request/context.
    Freezing topology therefore never snapshots provider health, permissions,
    configuration, or other request-time state.
    """

    def __init__(self) -> None:
        self._frozen = False
        self._manifests: dict[str, ExtensionManifest] = {}
        self._contributions: dict[str, RegisteredContribution] = {}
        self._points: dict[str, list[RegisteredContribution]] = defaultdict(list)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, bundle: ExtensionBundle) -> None:
        if self._frozen:
            raise ExtensionRegistryError("extension registry is frozen")
        manifest = bundle.manifest
        if not manifest.id or not manifest.version or not manifest.display_name:
            raise ExtensionRegistryError("extension manifest identifiers must be non-empty")
        if manifest.api_version != EXTENSION_API_VERSION:
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} uses unsupported API {manifest.api_version!r}"
            )
        if manifest.trust not in {"builtin", "isolated"}:
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} has invalid trust classification"
            )
        if manifest.id in self._manifests:
            raise ExtensionRegistryError(f"duplicate extension id {manifest.id!r}")
        declaration_ids = [item.id for item in manifest.contributions]
        if len(declaration_ids) != len(set(declaration_ids)):
            raise ExtensionRegistryError(
                f"extension {manifest.id!r} declares duplicate contribution ids"
            )
        self._manifests[manifest.id] = manifest
        before = set(self._contributions)
        try:
            bundle.register(_BundleRegistrar(self, manifest))
            registered = set(self._contributions) - before
            if registered != set(declaration_ids):
                raise ExtensionRegistryError(
                    f"extension {manifest.id!r} registrations do not match its manifest"
                )
        except Exception:
            self._rollback_manifest(manifest.id, before)
            raise

    def _rollback_manifest(self, plugin_id: str, prior_ids: set[str]) -> None:
        self._manifests.pop(plugin_id, None)
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
        declared = {item.id: item for item in manifest.contributions}
        if declared.get(declaration.id) != declaration:
            raise ExtensionRegistryError(
                f"contribution {declaration.id!r} differs from its manifest declaration"
            )
        if not declaration.id or not declaration.point:
            raise ExtensionRegistryError("contribution id and point must be non-empty")
        if declaration.id in self._contributions:
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
        for point, registrations in self._points.items():
            providers = [
                item
                for item in registrations
                if item.contribution.declaration.kind is ContributionKind.PROVIDER
            ]
            if len(providers) > 1:
                raise ExtensionRegistryError(
                    f"extension point {point!r} has multiple single providers"
                )
            registrations.sort(key=lambda item: item.contribution.declaration.id)
        self._manifests = MappingProxyType(dict(self._manifests))  # type: ignore[assignment]
        self._contributions = MappingProxyType(  # type: ignore[assignment]
            dict(self._contributions)
        )
        self._points = MappingProxyType(  # type: ignore[assignment]
            {point: tuple(items) for point, items in self._points.items()}
        )
        self._frozen = True
        return self

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

    def manifests(self) -> tuple[ExtensionManifest, ...]:
        self._require_frozen()
        return tuple(self._manifests.values())

    def contributions(self, point: str) -> tuple[RegisteredContribution, ...]:
        self._require_frozen()
        return tuple(self._points.get(point, ()))

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
        probe = registered.contribution.availability
        if probe is None:
            return Availability.available()
        return probe(context)

    def _require_frozen(self) -> None:
        if not self._frozen:
            raise ExtensionRegistryError("extension registry is not frozen")


def frozen_registry(bundles: Iterable[ExtensionBundle] = ()) -> ExtensionRegistry:
    registry = ExtensionRegistry()
    for bundle in bundles:
        registry.register(bundle)
    return registry.freeze()
