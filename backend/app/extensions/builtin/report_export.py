"""Default Markdown implementation of the single Report exporter Provider."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    REPORT_EXPORTER_POINT,
    REPORT_EXPORT_FORMAT_MARKDOWN,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    ProviderResult,
    ReportExportedItem,
    ReportExporterContext,
)


REPORT_MARKDOWN_EXPORTER_PLUGIN_ID = "builtin.report_markdown_exporter"
REPORT_MARKDOWN_EXPORTER_CONTRIBUTION_ID = "builtin.report_markdown"


class _MarkdownReportExporter:
    @staticmethod
    def formats() -> tuple[str, ...]:
        return (REPORT_EXPORT_FORMAT_MARKDOWN,)

    @staticmethod
    def export(
        context: ReportExporterContext,
    ) -> ProviderResult[tuple[ReportExportedItem, ...]]:
        return ProviderResult(
            tuple(
                ReportExportedItem(report.ref, report.content_md)
                for report in context.reports
            ),
            ExtensionResultStatus.AVAILABLE,
        )


_DECLARATION = ContributionDeclaration(
    REPORT_MARKDOWN_EXPORTER_CONTRIBUTION_ID,
    REPORT_EXPORTER_POINT,
    ContributionKind.PROVIDER,
)


@dataclass(frozen=True)
class ReportMarkdownExporterBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id=REPORT_MARKDOWN_EXPORTER_PLUGIN_ID,
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Markdown Report exporter",
        trust="builtin",
        contributions=(_DECLARATION,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_provider(
            ExtensionContribution(_DECLARATION, _MarkdownReportExporter())
        )


REPORT_MARKDOWN_EXPORTER_BUNDLE = ReportMarkdownExporterBundle()


__all__ = [
    "REPORT_MARKDOWN_EXPORTER_BUNDLE",
    "REPORT_MARKDOWN_EXPORTER_CONTRIBUTION_ID",
    "REPORT_MARKDOWN_EXPORTER_PLUGIN_ID",
]
