"""Core orchestration for authorized Deep Report batch export."""
from __future__ import annotations

import re

from app.domain.report_export import (
    REPORT_EXPORT_FILENAME_STEM_MAX_CHARS,
    REPORT_EXPORT_FORMAT_MARKDOWN,
    ReportExportArtifact,
    ReportExportBoundaryError,
    ReportExportConnectionProbe,
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
    connection_probe: ReportExportConnectionProbe,
    format_id: str = REPORT_EXPORT_FORMAT_MARKDOWN,
) -> tuple[ReportExportArtifact, ...]:
    """Render one authorized batch once; core retains filename authority."""

    snapshots = _snapshot_sources(sources)
    _assert_connection_clear(connection_probe)
    try:
        rendered = host.export_application(
            ReportExporterCall(sources, format_id, connection_probe)
        )
    except BaseException as exc:
        _assert_connection_clear(connection_probe)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ReportExportError):
            raise
        raise ReportExportError("report_exporter_failed") from None
    _assert_connection_clear(connection_probe)
    if type(rendered) is not tuple or len(rendered) != len(snapshots):
        raise ReportExportError("invalid_report_export_result")
    seen: dict[str, int] = {}
    artifacts: list[ReportExportArtifact] = []
    for snapshot, output in zip(snapshots, rendered, strict=True):
        if type(output) is not ReportExportRendered:
            raise ReportExportError("invalid_report_export_result")
        if (
            format_id == REPORT_EXPORT_FORMAT_MARKDOWN
            and (type(output.content) is not str or output.content != snapshot[2])
        ):
            raise ReportExportError("invalid_report_export_result")
        if type(output.content) not in {str, bytes}:
            raise ReportExportError("invalid_report_export_result")
        report_id, question, _content_md = snapshot
        stem = _UNSAFE_FILENAME.sub("_", question or "").strip()
        stem = stem[:REPORT_EXPORT_FILENAME_STEM_MAX_CHARS] or report_id
        filename = f"{stem}-{report_id}.md"
        collision = seen.get(filename, 0)
        if collision:
            filename = f"{stem}-{report_id}-{collision}.md"
        seen[f"{stem}-{report_id}.md"] = collision + 1
        artifacts.append(ReportExportArtifact(filename, output.content))
    return tuple(artifacts)


def _snapshot_sources(
    sources: object,
) -> tuple[tuple[str, str, str], ...]:
    if type(sources) is not tuple or not sources:
        raise ReportExportError("invalid_report_export_call")
    snapshots: list[tuple[str, str, str]] = []
    for source in sources:
        if (
            type(source) is not ReportExportSource
            or type(source.report_id) is not str
            or not source.report_id
            or type(source.question) is not str
            or type(source.content_md) is not str
            or not source.content_md
        ):
            raise ReportExportError("invalid_report_export_call")
        snapshots.append((source.report_id, source.question, source.content_md))
    return tuple(snapshots)


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
