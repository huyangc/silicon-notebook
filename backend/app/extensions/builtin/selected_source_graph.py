"""Thin built-in plugin for the selected-source graph capability."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    RETRIEVAL_CONTRIBUTOR_POINT,
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    RetrievalExtensionContext,
)


SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID = "builtin.selected_source_graph"
_DECLARATION = ContributionDeclaration(
    SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
    RETRIEVAL_CONTRIBUTOR_POINT,
    ContributionKind.CONTRIBUTOR,
)


class SelectedSourceGraphContributor:
    """Delegate only to the request-bound core capability."""

    invocations = frozenset({"selected_evidence"})

    @staticmethod
    def contribute(context: RetrievalExtensionContext) -> ContributorResult:
        access = context.selected_source_graph
        if access is None:
            return ContributorResult((), ExtensionResultStatus.UNAVAILABLE)
        return access.contribute()


@dataclass(frozen=True)
class SelectedSourceGraphBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.selected_source_graph",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Selected-source graph",
        trust="builtin",
        contributions=(_DECLARATION,),
        requires=(SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(_DECLARATION, SelectedSourceGraphContributor())
        )


SELECTED_SOURCE_GRAPH_BUNDLE = SelectedSourceGraphBundle()
