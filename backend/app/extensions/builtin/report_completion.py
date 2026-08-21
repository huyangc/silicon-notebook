"""Built-in adapter for the existing Report completion side effect."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    REPORT_COMPLETED_OBSERVER_POINT,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    ObserverReceipt,
    ReportCompletedExtensionContext,
)


REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID = (
    "builtin.report_agent_profile"
)


class _ReportAgentProfileObserver:
    @staticmethod
    def observe(context: ReportCompletedExtensionContext) -> ObserverReceipt:
        if context.access is None:
            return ObserverReceipt(
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.UNAVAILABLE,
                    "report_completed_access_unavailable",
                ),
            )
        return context.access.notify()


_DECLARATION = ContributionDeclaration(
    REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
    REPORT_COMPLETED_OBSERVER_POINT,
    ContributionKind.OBSERVER,
)


@dataclass(frozen=True)
class ReportAgentProfileCompletedBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.report_agent_profile",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Report agent-profile completion",
        trust="builtin",
        contributions=(_DECLARATION,),
        requires=(REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_observer(
            ExtensionContribution(_DECLARATION, _ReportAgentProfileObserver())
        )


REPORT_AGENT_PROFILE_COMPLETED_BUNDLE = ReportAgentProfileCompletedBundle()


__all__ = [
    "REPORT_AGENT_PROFILE_COMPLETED_BUNDLE",
    "REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID",
]
