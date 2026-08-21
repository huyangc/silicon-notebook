"""Thin built-in plugin for the generated-question recall capability."""
from __future__ import annotations

from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    GENERATED_QUESTION_ACCESS_CAPABILITY,
    RETRIEVAL_CONTRIBUTOR_POINT,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
    RetrievalExtensionContext,
)


GENERATED_QUESTION_CONTRIBUTION_ID = "builtin.generated_question"
_DECLARATION = ContributionDeclaration(
    GENERATED_QUESTION_CONTRIBUTION_ID,
    RETRIEVAL_CONTRIBUTOR_POINT,
    ContributionKind.CONTRIBUTOR,
)


class GeneratedQuestionContributor:
    """Delegate only to the request-bound core capability."""

    invocations = frozenset({"chunk_candidates"})

    @staticmethod
    def contribute(context: RetrievalExtensionContext) -> ContributorResult:
        access = context.generated_question
        if access is None:
            return ContributorResult((), ExtensionResultStatus.UNAVAILABLE)
        return access.contribute()


@dataclass(frozen=True)
class GeneratedQuestionBundle:
    manifest: ExtensionManifest = ExtensionManifest(
        id="builtin.generated_question",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Generated-question recall",
        trust="builtin",
        contributions=(_DECLARATION,),
        requires=(GENERATED_QUESTION_ACCESS_CAPABILITY,),
    )

    @staticmethod
    def register(registrar: ExtensionRegistrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(_DECLARATION, GeneratedQuestionContributor())
        )


GENERATED_QUESTION_BUNDLE = GeneratedQuestionBundle()
