"""Global source question-answering transport values."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.ask import (
    AnswerAnchor,
    AskIntentConfirmation,
    AskResponse,
    Citation,
    TraceStep,
    ASK_QUESTION_MAX_CHARS,
    CONVERSATION_TITLE_MAX_CHARS,
)
from app.core.ask_retrieval_policy import DEFAULT_RETRIEVAL_EFFORT, RetrievalEffort

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
    # D1-1: data-model-only groundwork for routing a global (cross-library) ask
    # through the same single-library ``AskService.ask`` engine used by
    # ``AskRequest``. Not consumed by ``_run`` yet -- see ``mode``/``trace``/
    # ``answer`` on ``GlobalAskJob`` for the matching read-side note.
    mode: str = "chunk"
    intent: AskIntentConfirmation | None = None
    retrieval_effort: RetrievalEffort = DEFAULT_RETRIEVAL_EFFORT

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
    # Legacy answer shape: the global-only synthesis response ``_run`` has
    # always written. Kept so already-persisted rows keep reading back
    # unchanged; new code should read ``answer`` (or the
    # ``global_answer_*`` projection helpers below) instead of this field
    # directly. Do not write new rows through this field once the engine
    # moves onto ``AskService.ask`` (a later task) -- it stays only to
    # replay history.
    response: GlobalAskAnswer | None = None
    # The engine that answers this turn and the resource level it runs at, both
    # frozen when the job is created and read back by the detached worker, so
    # neither request field can be "accepted and then ignored".
    #
    # ``retrieval_effort`` is CLAMPED to the default at admission rather than
    # carried from the request: see ``GlobalAskService.start``.
    #
    # The confirmed INTENT is deliberately not a field here. It is run input,
    # handed to the worker like the conversation history, and the finished turn
    # already publishes the understanding that actually answered on
    # ``answer.intent``. Putting it on the job as well would persist the same
    # contract twice AND -- because this model is a response body while
    # ``GlobalAskRequest`` is a request body -- split ``AskIntentConfirmation``
    # into two schema names across the whole public OpenAPI document.
    mode: str = "chunk"
    retrieval_effort: RetrievalEffort = DEFAULT_RETRIEVAL_EFFORT
    trace: list[TraceStep] = Field(default_factory=list)
    answer: AskResponse | None = None


# D1-1 read-side projections: prefer the new ``answer``/``trace`` shape and
# fall back to the legacy ``response`` shape a job written before the engine
# switch (a later task) so every existing persisted row keeps reading back
# with no migration. ``_run`` does not populate ``answer``/``trace`` yet, so
# today these always resolve through the legacy branch -- they exist ahead of
# that switch so the read side and the write side change on separate,
# independently reviewable commits.
def global_answer_text(job: "GlobalAskJob") -> str:
    if job.answer is not None:
        return job.answer.answer
    if job.response is not None:
        return job.response.answer
    return ""


def global_answer_citations(job: "GlobalAskJob") -> list[Citation]:
    if job.answer is not None:
        return job.answer.citations
    if job.response is not None:
        return job.response.citations
    return []


def global_answer_trace(job: "GlobalAskJob") -> list[TraceStep]:
    if job.answer is not None and job.answer.reasoning_trace:
        return job.answer.reasoning_trace
    return job.trace


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
