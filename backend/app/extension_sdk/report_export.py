"""Point-specific SDK contract for the single Deep Report exporter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.report_export import REPORT_EXPORT_FORMAT_MARKDOWN
from app.extension_sdk.contracts import ProviderResult


REPORT_EXPORTER_POINT = "report.exporter"


class ReportExportItemRef:
    """Request-local opaque identity; only exact object identity is authoritative."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ReportExportView:
    ref: ReportExportItemRef
    content_md: str


@dataclass(frozen=True, slots=True)
class ReportExportedItem:
    ref: ReportExportItemRef
    content: str | bytes


@dataclass(frozen=True, slots=True)
class ReportExporterContext:
    format_id: str
    reports: tuple[ReportExportView, ...]


@dataclass(frozen=True, slots=True)
class ReportExporterAvailabilityContext:
    plugin_id: str
    contribution_id: str
    format_id: str


class ReportExporterProvider(Protocol):
    def formats(self) -> tuple[str, ...]: ...

    def export(
        self, context: ReportExporterContext
    ) -> ProviderResult[tuple[ReportExportedItem, ...]]: ...


__all__ = [
    "REPORT_EXPORTER_POINT",
    "REPORT_EXPORT_FORMAT_MARKDOWN",
    "ReportExportedItem",
    "ReportExporterAvailabilityContext",
    "ReportExporterContext",
    "ReportExporterProvider",
    "ReportExportItemRef",
    "ReportExportView",
]
