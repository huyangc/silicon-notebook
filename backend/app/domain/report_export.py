"""Core-owned contracts for authorized Deep Report batch export."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


REPORT_EXPORT_FORMAT_MARKDOWN = "markdown"
REPORT_EXPORT_FORMATS = (REPORT_EXPORT_FORMAT_MARKDOWN,)
REPORT_EXPORT_FILENAME_STEM_MAX_CHARS = 40


class ReportExportError(RuntimeError):
    """Stable public-plane failure; never contains provider exception text."""


class ReportExportBoundaryError(ReportExportError):
    pass


class ReportExportConnectionProbe(Protocol):
    def is_connection_held(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReportExportSource:
    """One completed, creator-authorized report selected by the core store."""

    report_id: str
    question: str
    content_md: str


@dataclass(frozen=True, slots=True)
class ReportExportRendered:
    """Provider-rendered content in the exact input position."""

    content: str | bytes


@dataclass(frozen=True, slots=True)
class ReportExportArtifact:
    filename: str
    content: str | bytes


@dataclass(frozen=True, slots=True)
class ReportExporterCall:
    sources: tuple[ReportExportSource, ...]
    format_id: str
    connection_probe: ReportExportConnectionProbe


class ReportExporterHostPort(Protocol):
    @property
    def has_provider(self) -> bool: ...

    def export_application(
        self, call: ReportExporterCall
    ) -> tuple[ReportExportRendered, ...]: ...


__all__ = [
    "REPORT_EXPORT_FILENAME_STEM_MAX_CHARS",
    "REPORT_EXPORT_FORMAT_MARKDOWN",
    "REPORT_EXPORT_FORMATS",
    "ReportExportArtifact",
    "ReportExportBoundaryError",
    "ReportExportConnectionProbe",
    "ReportExportError",
    "ReportExporterCall",
    "ReportExporterHostPort",
    "ReportExportRendered",
    "ReportExportSource",
]
