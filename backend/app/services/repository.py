from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol

from app.models.schemas import (
    ArticleCreate,
    ArticleResearchBrief,
    ArticleSummary,
    AskRequest,
    AskResponse,
    Candidate,
    CandidateUpdate,
    CaseCard,
    CaseSearchRequest,
    ChecklistItem,
    ChecklistRequest,
    ConflictPair,
    DuplicateGroup,
    FeedbackRequest,
    FeedbackResponse,
    GlossaryTermCard,
    KnowledgeUpdate,
    MergeRequest,
    MethodCard,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookUpdate,
    RiskItemCard,
    RuleCard,
    ScenarioQueryRequest,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)


@dataclass
class UploadedSourceFile:
    file_name: str
    content_type: str
    content: bytes


class NotebookRepository(Protocol):
    def current_user(self) -> UserProfile: ...

    def list_notebooks(self) -> List[NotebookSummary]: ...

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...

    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...

    def update_notebook(self, notebook_id: str, payload: NotebookUpdate) -> NotebookSummary: ...

    def delete_notebook(self, notebook_id: str) -> None: ...

    def list_sources(self, notebook_id: str) -> List[SourceSummary]: ...

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]: ...

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> List[SourceSummary]: ...

    def get_source(self, source_id: str) -> SourceDetail: ...

    def parse_source(self, source_id: str) -> SourceSummary: ...

    def process_source(self, source_id: str) -> SourceSummary: ...

    def source_elements(self, source_id: str) -> List[SourceElement]: ...

    def delete_source(self, source_id: str) -> None: ...

    def extract_source(self, source_id: str) -> List[Candidate]: ...

    def list_candidates(self, notebook_id: str, candidate_type: str | None = None) -> List[Candidate]: ...

    def update_candidate(self, candidate_id: str, payload: CandidateUpdate) -> Candidate: ...

    def approve_candidate(self, candidate_id: str) -> Candidate: ...

    def reject_candidate(self, candidate_id: str) -> Candidate: ...

    def list_rules(self, notebook_id: str) -> List[RuleCard]: ...

    def list_methods(self, notebook_id: str) -> List[MethodCard]: ...

    def list_risks(self, notebook_id: str) -> List[RiskItemCard]: ...

    def list_glossary(self, notebook_id: str) -> List[GlossaryTermCard]: ...

    def update_knowledge(
        self, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard | MethodCard | RiskItemCard | GlossaryTermCard: ...

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]: ...

    def find_conflicts(self, notebook_id: str) -> List[ConflictPair]: ...

    def merge_knowledge(
        self, source_id: str, payload: MergeRequest
    ) -> RuleCard | MethodCard | RiskItemCard | GlossaryTermCard: ...

    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse: ...

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse: ...

    def scenario_query(self, notebook_id: str, payload: ScenarioQueryRequest) -> AskResponse: ...

    def case_search(self, notebook_id: str, payload: CaseSearchRequest) -> List[CaseCard]: ...

    def checklist(self, notebook_id: str, payload: ChecklistRequest) -> List[ChecklistItem]: ...

    def list_articles(self, notebook_id: str) -> List[ArticleSummary]: ...

    def create_article(self, notebook_id: str, payload: ArticleCreate) -> ArticleSummary: ...

    def delete_article(self, article_id: str) -> None: ...

    def research_article(self, article_id: str) -> ArticleResearchBrief: ...

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...
