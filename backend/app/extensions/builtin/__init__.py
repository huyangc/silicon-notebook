"""Trusted built-in extension bundles registered by the composition root."""

from app.extensions.builtin.generated_question import (
    GENERATED_QUESTION_BUNDLE,
    GENERATED_QUESTION_CONTRIBUTION_ID,
)

from app.extensions.builtin.selected_source_graph import (
    SELECTED_SOURCE_GRAPH_BUNDLE,
    SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
)

__all__ = [
    "GENERATED_QUESTION_BUNDLE",
    "GENERATED_QUESTION_CONTRIBUTION_ID",
    "SELECTED_SOURCE_GRAPH_BUNDLE",
    "SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID",
]
