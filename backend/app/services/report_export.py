"""Core orchestration for authorized Deep Report batch export."""
from __future__ import annotations

import re

from app.domain.report_export import (
    REPORT_EXPORT_FILENAME_STEM_MAX_CHARS,
    REPORT_EXPORT_FORMAT_MARKDOWN,
    ReportExportArtifact,
    ReportExportBoundaryError,
    ReportExportError,
    ReportExporterCall,
    ReportExporterHostPort,
    ReportExportRendered,
    ReportExportSource,
)


_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\r\n]')


def export_completed_reports(
    sources: tuple[ReportExportSource, ...],
    *,
    host: ReportExporterHostPort,
    connection_probe: object,
    format_id: str = REPORT_EXPORT_FORMAT_MARKDOWN,
) -> tuple[ReportExportArtifact, ...]:
    """Render one authorized batch once; core retains filename authority."""

    _assert_connection_clear(connection_probe)
    rendered = host.export_application(
        ReportExporterCall(sources, format_id, connection_probe)
    )
    _assert_connection_clear(connection_probe)
    if type(rendered) is not tuple or len(rendered) != len(sources):
        raise ReportExportError("invalid_report_export_result")
    seen: dict[str, int] = {}
    artifacts: list[ReportExportArtifact] = []
    for source, output in zip(sources, rendered, strict=True):
        if type(source) is not ReportExportSource or type(output) is not ReportExportRendered:
            raise ReportExportError("invalid_report_export_result")
        if type(output.content) not in {str, bytes}:
            raise ReportExportError("invalid_report_export_result")
        stem = _UNSAFE_FILENAME.sub("_", source.question or "").strip()
        stem = stem[:REPORT_EXPORT_FILENAME_STEM_MAX_CHARS] or source.report_id
        filename = f"{stem}-{source.report_id}.md"
        collision = seen.get(filename, 0)
        if collision:
            filename = f"{stem}-{source.report_id}-{collision}.md"
        seen[f"{stem}-{source.report_id}.md"] = collision + 1
        artifacts.append(ReportExportArtifact(filename, output.content))
    return tuple(artifacts)


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


__all__ = ["export_completed_reports"]
