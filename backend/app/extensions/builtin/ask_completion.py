"""Thin built-in observers for the existing Ask completion side effects."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_COMPLETED_OBSERVER_POINT,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    EXTENSION_API_VERSION,
    AskCompletedExtensionContext,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    ObserverReceipt,
)


ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID = "builtin.ask_agent_profile"
ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID = (
    "builtin.ask_retrieval_experience"
)
ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID = "builtin.ask_search_profile"


class _DelegatingCompletedObserver:
    @staticmethod
    def observe(context: AskCompletedExtensionContext) -> ObserverReceipt:
        if context.access is None:
            return ObserverReceipt(
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.UNAVAILABLE,
                    "ask_completed_access_unavailable",
                ),
            )
        return context.access.notify()


def _declaration(contribution_id: str) -> ContributionDeclaration:
    return ContributionDeclaration(
        contribution_id,
        ASK_COMPLETED_OBSERVER_POINT,
        ContributionKind.OBSERVER,
    )


_AGENT_PROFILE = _declaration(ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID)
_RETRIEVAL_EXPERIENCE = _declaration(
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID
)
_SEARCH_PROFILE = _declaration(ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID)


@dataclass(frozen=True)
class AgentProfileCompletedBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.ask_agent_profile",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Ask agent-profile completion",
        trust="builtin",
        contributions=(_AGENT_PROFILE,),
        requires=(ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_observer(
            ExtensionContribution(_AGENT_PROFILE, _DelegatingCompletedObserver())
        )


@dataclass(frozen=True)
class RetrievalExperienceCompletedBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.ask_retrieval_experience",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Ask retrieval-experience completion",
        trust="builtin",
        contributions=(_RETRIEVAL_EXPERIENCE,),
        requires=(ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_observer(
            ExtensionContribution(
                _RETRIEVAL_EXPERIENCE, _DelegatingCompletedObserver()
            )
        )


@dataclass(frozen=True)
class SearchProfileCompletedBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.ask_search_profile",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Ask search-profile completion",
        trust="builtin",
        contributions=(_SEARCH_PROFILE,),
        requires=(ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_observer(
            ExtensionContribution(_SEARCH_PROFILE, _DelegatingCompletedObserver())
        )


ASK_AGENT_PROFILE_COMPLETED_BUNDLE = AgentProfileCompletedBundle()
ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE = (
    RetrievalExperienceCompletedBundle()
)
ASK_SEARCH_PROFILE_COMPLETED_BUNDLE = SearchProfileCompletedBundle()


__all__ = [
    "ASK_AGENT_PROFILE_COMPLETED_BUNDLE",
    "ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID",
    "ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE",
    "ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID",
    "ASK_SEARCH_PROFILE_COMPLETED_BUNDLE",
    "ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID",
]
