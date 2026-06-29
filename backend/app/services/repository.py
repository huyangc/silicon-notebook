from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol

from app.models.schemas import (
    AddUrlSourcesResult,
    ArticleCreate,
    ArticleResearchBrief,
    ArticleSummary,
    AskRequest,
    AskResponse,
    DerivedRuleCandidate,
    DuplicateGroup,
    FeedbackRequest,
    FeedbackResponse,
    KnowledgeGraph,
    KnowledgeRecord,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    MergeRequest,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    PaginatedSources,
    RuleCard,
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
    doc_type: str = ""  # "" = auto-detect; else an extraction profile id


class NotebookRepository(Protocol):
    def current_user(self) -> UserProfile: ...

    def list_notebooks(self) -> List[NotebookSummary]: ...

    def list_notebook_templates(self) -> List[NotebookTemplate]: ...

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...

    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...

    def update_notebook(self, notebook_id: str, payload: NotebookUpdate) -> NotebookSummary: ...

    def delete_notebook(self, notebook_id: str) -> None: ...

    def eval_insert_source_for_test(
        self, nb_id: str, name: str, text: str, tmpdir: str
    ) -> str: ...

    def list_sources(self, notebook_id: str) -> List[SourceSummary]: ...

    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = "") -> PaginatedSources: ...

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]: ...

    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> "AddUrlSourcesResult": ...

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

    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]: ...

    def list_knowledge(
        self, notebook_id: str, object_type: str
    ) -> List[KnowledgeRecord]: ...

    def list_object_schemas(self) -> List[ObjectSchemaModel]: ...

    def create_object_schema(
        self, payload: ObjectSchemaCreate
    ) -> ObjectSchemaModel: ...

    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel: ...

    def delete_object_schema(self, object_type: str) -> None: ...

    def propose_schemas(self, notebook_id: str) -> List[ObjectSchemaModel]: ...

    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph: ...

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard: ...

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]: ...

    def merge_knowledge(
        self, notebook_id: str, source_id: str, payload: MergeRequest
    ) -> RuleCard: ...

    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse: ...

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse: ...

    def list_articles(self, notebook_id: str) -> List[ArticleSummary]: ...

    def create_article(self, notebook_id: str, payload: ArticleCreate) -> ArticleSummary: ...

    def delete_article(self, article_id: str) -> None: ...

    def research_article(self, article_id: str) -> ArticleResearchBrief: ...

    def list_derived_rules(
        self, notebook_id: str, status: str | None = None
    ) -> List[DerivedRuleCandidate]: ...

    def approve_derived_rule(self, notebook_id: str, candidate_id: str) -> RuleCard: ...

    def reject_derived_rule(self, notebook_id: str, candidate_id: str) -> DerivedRuleCandidate: ...

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse: ...
