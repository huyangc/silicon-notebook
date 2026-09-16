"""Notebook welcome suggestions: bounded generated output, never authored input."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

QUESTION_SUGGESTION_COUNT = 4
QUESTION_SUGGESTION_LABEL_CHARS = 32
QUESTION_SUGGESTION_QUESTION_CHARS = 300
# Infrastructure page width, independent of the model's source sampling budget.
QUESTION_SUGGESTION_REVISION_PAGE_SIZE = 2048


class QuestionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: StrictStr = Field(min_length=1, max_length=QUESTION_SUGGESTION_LABEL_CHARS)
    question: StrictStr = Field(min_length=1, max_length=QUESTION_SUGGESTION_QUESTION_CHARS)


class QuestionSuggestionsResponse(BaseModel):
    questions: list[QuestionSuggestion] = Field(default_factory=list, max_length=QUESTION_SUGGESTION_COUNT)
    status: Literal["ready", "fallback"] = "fallback"
    sampled: bool = False
    source_count: int = 0
    sampled_source_count: int = 0
