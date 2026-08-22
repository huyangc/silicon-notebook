"""Startup-frozen single Provider host for authorized Report batch export."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

from app.domain.report_export import (
    REPORT_EXPORT_FORMAT_MARKDOWN,
    REPORT_EXPORT_FORMATS,
    ReportExportBoundaryError,
    ReportExportError,
    ReportExporterCall,
    ReportExportRendered,
    ReportExportSource,
)
from app.extension_sdk import (
    REPORT_EXPORTER_POINT,
    AvailabilityStatus,
    ContributionKind,
    ExtensionResultStatus,
    ProviderResult,
    ReportExportedItem,
    ReportExporterAvailabilityContext,
    ReportExporterContext,
    ReportExportItemRef,
    ReportExportView,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


@dataclass(frozen=True, slots=True)
class _FrozenProvider:
    plugin_id: str
    contribution_id: str
    export: Callable[[ReportExporterContext], object]


class ReportExporterHost:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        trusted_plugin_ids: frozenset[str] = frozenset(),
    ) -> None:
        if type(trusted_plugin_ids) is not frozenset or any(
            type(item) is not str for item in trusted_plugin_ids
        ):
            raise ExtensionRegistryError("invalid trusted Report exporter set")
        registered = registry.contributions(REPORT_EXPORTER_POINT)
        if len(registered) > 1:
            raise ExtensionRegistryError("report exporter point has multiple providers")
        self._registry = registry
        self._provider: _FrozenProvider | None = None
        if not registered:
            return
        item = registered[0]
        declaration = item.contribution.declaration
        manifest = next(
            candidate
            for candidate in registry.manifests()
            if candidate.id == item.plugin_id
        )
        if declaration.kind is not ContributionKind.PROVIDER:
            raise ExtensionRegistryError("report exporter must be a provider")
        if (
            item.plugin_id not in trusted_plugin_ids
            or type(manifest.trust) is not str
            or manifest.trust != "builtin"
        ):
            raise ExtensionRegistryError(
                "report exporter must be an explicitly trusted built-in"
            )
        implementation = item.contribution.implementation
        try:
            formats = getattr(implementation, "formats", None)
            export = getattr(implementation, "export", None)
            valid_callables = callable(formats) and callable(export)
            coroutine_callable = (
                inspect.iscoroutinefunction(formats)
                or inspect.iscoroutinefunction(export)
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ExtensionRegistryError("invalid report exporter") from exc
        if not valid_callables or coroutine_callable:
            raise ExtensionRegistryError("invalid report exporter")
        try:
            declared_formats = formats()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ExtensionRegistryError("report exporter discovery failed") from exc
        if (
            type(declared_formats) is not tuple
            or any(type(value) is not str for value in declared_formats)
            or declared_formats != REPORT_EXPORT_FORMATS
        ):
            raise ExtensionRegistryError("invalid report exporter formats")
        self._provider = _FrozenProvider(
            item.plugin_id, declaration.id, export
        )

    @property
    def has_provider(self) -> bool:
        return self._provider is not None

    def export_application(
        self, call: ReportExporterCall
    ) -> tuple[ReportExportRendered, ...]:
        provider = self._provider
        if provider is None:
            raise ReportExportError("report_exporter_unavailable")
        self._validate_call(call)
        sources = tuple(
            (source.report_id, source.question, source.content_md)
            for source in call.sources
        )
        format_id = call.format_id
        connection_probe = call.connection_probe
        self._assert_connection_clear(connection_probe)
        try:
            availability = self._registry.availability(
                provider.contribution_id,
                ReportExporterAvailabilityContext(
                    provider.plugin_id,
                    provider.contribution_id,
                    format_id,
                ),
            )
        except BaseException as exc:
            self._assert_connection_clear(connection_probe)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReportExportError("report_exporter_unavailable") from None
        self._assert_connection_clear(connection_probe)
        if availability.status is not AvailabilityStatus.AVAILABLE:
            raise ReportExportError("report_exporter_unavailable")

        refs = tuple(ReportExportItemRef() for _ in sources)
        views = tuple(
            ReportExportView(ref, source[2])
            for ref, source in zip(refs, sources, strict=True)
        )
        source_contents = tuple(source[2] for source in sources)
        context = ReportExporterContext(format_id, views)
        self._assert_connection_clear(connection_probe)
        try:
            result = provider.export(context)
        except BaseException as exc:
            self._assert_connection_clear(connection_probe)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReportExportError("report_exporter_failed") from None
        self._assert_connection_clear(connection_probe)
        return self._validate_result(result, refs, source_contents, format_id)

    @staticmethod
    def _validate_call(call: object) -> None:
        if (
            type(call) is not ReportExporterCall
            or type(call.sources) is not tuple
            or not call.sources
            or type(call.format_id) is not str
            or call.format_id not in REPORT_EXPORT_FORMATS
        ):
            raise ReportExportError("invalid_report_export_call")
        for source in call.sources:
            if (
                type(source) is not ReportExportSource
                or type(source.report_id) is not str
                or not source.report_id
                or type(source.question) is not str
                or type(source.content_md) is not str
                or not source.content_md
            ):
                raise ReportExportError("invalid_report_export_call")

    @staticmethod
    def _validate_result(
        result: object,
        refs: tuple[ReportExportItemRef, ...],
        source_contents: tuple[str, ...],
        format_id: str,
    ) -> tuple[ReportExportRendered, ...]:
        if (
            type(result) is not ProviderResult
            or result.status is not ExtensionResultStatus.AVAILABLE
            or result.failure is not None
            or type(result.value) is not tuple
            or len(result.value) != len(refs)
        ):
            raise ReportExportError("invalid_report_export_result")
        rendered: list[ReportExportRendered] = []
        for index, value in enumerate(result.value):
            if (
                type(value) is not ReportExportedItem
                or value.ref is not refs[index]
            ):
                raise ReportExportError("invalid_report_export_result")
            content = value.content
            if type(content) not in {str, bytes}:
                raise ReportExportError("invalid_report_export_result")
            if format_id == REPORT_EXPORT_FORMAT_MARKDOWN and (
                type(content) is not str or content != source_contents[index]
            ):
                raise ReportExportError("invalid_report_export_result")
            rendered.append(ReportExportRendered(content))
        return tuple(rendered)

    @staticmethod
    def _assert_connection_clear(probe: object) -> None:
        try:
            method = getattr(probe, "is_connection_held")
            held = method()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReportExportBoundaryError(
                "invalid_report_export_connection_probe"
            ) from None
        if type(held) is not bool or held:
            raise ReportExportBoundaryError("report_export_connection_held")


__all__ = [
    "ReportExportBoundaryError",
    "ReportExportError",
    "ReportExporterHost",
]
