"""Global source question-answering transport values."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.ask import AnswerAnchor, Citation, ASK_QUESTION_MAX_CHARS, CONVERSATION_TITLE_MAX_CHARS

GLOBAL_ASK_PAGE_SIZE = 50
GLOBAL_ASK_PAGE_MAX = 100
GLOBAL_ASK_ID_MAX_CHARS = 200

class GlobalNotebookScope(BaseModel):
    mode: Literal["all", "include"] = "all"
    notebook_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize(self):
        self.notebook_ids = list(dict.fromkeys(self.notebook_ids))
        if self.mode == "all" or not self.notebook_ids:
            self.mode, self.notebook_ids = "all", []
        return self

class GlobalAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_CHARS)
    notebook_scope: GlobalNotebookScope | None = None
    conversation_id: str | None = Field(default=None, max_length=GLOBAL_ASK_ID_MAX_CHARS)
    client_request_id: str | None = Field(default=None, max_length=GLOBAL_ASK_ID_MAX_CHARS)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入问题。")
        return value

class GlobalAskSkippedNotebook(BaseModel):
    notebook_id: str
    reason: str


class GlobalAskAnswer(BaseModel):
    answer_id: str
    question: str
    answer: str
    grounded: bool = False
    anchors: list[AnswerAnchor] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    created_at: str
    notebook_scope: GlobalNotebookScope
    resolved_notebook_ids: list[str]
    searched_notebook_ids: list[str]
    cited_notebook_ids: list[str]
    skipped_notebooks: list[GlobalAskSkippedNotebook] = Field(default_factory=list)
    degraded_notebook_ids: list[str] = Field(default_factory=list)
    completeness_notice: str = "回答仅使用本次命中的有限原文，不代表逐篇穷尽检查。"

class GlobalAskJob(BaseModel):
    job_id: str
    conversation_id: str
    status: Literal["running", "done", "failed", "cancelled", "interrupted"]
    question: str
    created_at: str
    notebook_scope: GlobalNotebookScope
    resolved_notebook_ids: list[str]
    searched_notebook_ids: list[str] = Field(default_factory=list)
    cited_notebook_ids: list[str] = Field(default_factory=list)
    skipped_notebooks: list[GlobalAskSkippedNotebook] = Field(default_factory=list)
    degraded_notebook_ids: list[str] = Field(default_factory=list)
    # Explicitly displayable Chinese guidance; the service never stores raw exceptions here.
    error: str | None = None
    response: GlobalAskAnswer | None = None

class GlobalConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    notebook_scope: GlobalNotebookScope
    submitted_via: Literal["web", "mcp"]

class GlobalConversationDetail(GlobalConversationSummary):
    turns: list[GlobalAskJob]
    has_more: bool = False
    next_offset: int | None = None

class GlobalConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=CONVERSATION_TITLE_MAX_CHARS)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入对话名称。")
        return value.strip()
