"""P0 profile stub: just the id + extraction targets used by chunks/packages.
The object/relation type vocabularies arrive in P1."""
from __future__ import annotations

ARTICLE_TARGETS = ["ArticleClaim", "ArticleMethod", "ScalingLaw",
                   "ExperimentResult", "MechanisticExplanation"]
TEXTBOOK_TARGETS = ["Concept", "Formula", "Derivation", "ProcessFlow",
                    "DesignPrinciple"]


def extraction_targets(profile: str):
    return ARTICLE_TARGETS if profile == "article_research" else TEXTBOOK_TARGETS
