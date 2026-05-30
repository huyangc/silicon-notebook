from functools import lru_cache
from pathlib import Path
from typing import List

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, UploadFile

from app.core.config import get_settings
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
    DerivedRuleCandidate,
    DuplicateGroup,
    FeedbackRequest,
    FeedbackResponse,
    GlossaryTermCard,
    KnowledgeGraph,
    KnowledgeRecord,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    MergeRequest,
    MethodCard,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    RiskItemCard,
    RuleCard,
    RuleExplanation,
    ScenarioQueryRequest,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)
from app.services.repository import NotebookRepository, UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository

router = APIRouter()

SUPPORTED_SOURCE_SUFFIXES = {".pdf", ".md", ".markdown", ".docx", ".pptx", ".csv", ".xlsx", ".xlsm"}
MAX_SOURCE_UPLOAD_BYTES = 50 * 1024 * 1024


def _validate_source_file(file_name: str, content_size: int | None = None) -> None:
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source file type. Supported suffixes: {supported}",
        )
    if content_size is not None:
        if content_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded source file is empty")
        if content_size > MAX_SOURCE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded source file is too large")


@lru_cache
def repository() -> NotebookRepository:
    return SQLiteRepository(get_settings())


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": settings.llm_configured,
        "embedding_configured": settings.embedding_configured,
    }


@router.get("/me", response_model=UserProfile)
def me() -> UserProfile:
    return repository().current_user()


@router.get("/notebook-templates", response_model=List[NotebookTemplate])
def list_notebook_templates() -> List[NotebookTemplate]:
    return repository().list_notebook_templates()


@router.get("/notebooks", response_model=List[NotebookSummary])
def list_notebooks() -> List[NotebookSummary]:
    return repository().list_notebooks()


@router.post("/notebooks", response_model=NotebookSummary)
def create_notebook(payload: NotebookCreate) -> NotebookSummary:
    return repository().create_notebook(payload)


@router.get("/notebooks/{notebook_id}", response_model=NotebookSummary)
def get_notebook(notebook_id: str) -> NotebookSummary:
    try:
        return repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/analytics", response_model=NotebookAnalytics)
def notebook_analytics(notebook_id: str) -> NotebookAnalytics:
    try:
        return repository().notebook_analytics(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}", response_model=NotebookSummary)
def update_notebook(
    notebook_id: str,
    payload: NotebookUpdate,
) -> NotebookSummary:
    try:
        return repository().update_notebook(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}", status_code=204)
def delete_notebook(notebook_id: str) -> None:
    try:
        repository().delete_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/sources", response_model=List[SourceSummary])
def list_sources(notebook_id: str) -> List[SourceSummary]:
    return repository().list_sources(notebook_id)


@router.post("/notebooks/{notebook_id}/sources/import", response_model=List[SourceSummary])
def import_sources(
    notebook_id: str,
    payload: SourceImportRequest,
) -> List[SourceSummary]:
    try:
        for file in payload.files:
            _validate_source_file(file.file_name)
        return repository().import_sources(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources", response_model=List[SourceSummary])
async def upload_sources(
    notebook_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
) -> List[SourceSummary]:
    try:
        repo = repository()
        uploaded_files = []
        for file in files:
            file_name = file.filename or "source.bin"
            _validate_source_file(file_name)
            content = await file.read()
            _validate_source_file(file_name, len(content))
            uploaded_files.append(
                UploadedSourceFile(
                    file_name=file_name,
                    content_type=file.content_type or "",
                    content=content,
                )
            )
        return repo.upload_sources(
            notebook_id,
            uploaded_files,
            scheduler=lambda source_id: background_tasks.add_task(repo.process_source, source_id),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str) -> SourceDetail:
    try:
        return repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.post("/sources/{source_id}/parse", response_model=SourceSummary)
def parse_source(source_id: str) -> SourceSummary:
    try:
        return repository().parse_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get("/sources/{source_id}/elements", response_model=List[SourceElement])
def source_elements(source_id: str) -> List[SourceElement]:
    try:
        return repository().source_elements(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str) -> None:
    try:
        repository().delete_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


_CANDIDATE_TYPE_MAP = {
    "rules": "rule",
    "methods": "method",
    "risks": "risk",
    "cases": "case",
    "checklist": "checklist",
    "glossary": "glossary",
}


@router.post("/sources/{source_id}/extract", response_model=List[Candidate])
def extract_source(source_id: str) -> List[Candidate]:
    try:
        return repository().extract_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get("/notebooks/{notebook_id}/candidates", response_model=List[Candidate])
def list_candidates(notebook_id: str) -> List[Candidate]:
    try:
        return repository().list_candidates(notebook_id, None)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/candidates/{candidate_type}", response_model=List[Candidate])
def list_candidates_by_type(notebook_id: str, candidate_type: str) -> List[Candidate]:
    mapped = _CANDIDATE_TYPE_MAP.get(candidate_type)
    if mapped is None:
        raise HTTPException(status_code=400, detail="Unknown candidate type")
    try:
        return repository().list_candidates(notebook_id, mapped)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/candidates/{candidate_id}", response_model=Candidate)
def update_candidate(candidate_id: str, payload: CandidateUpdate) -> Candidate:
    try:
        return repository().update_candidate(candidate_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")


@router.post("/candidates/{candidate_id}/approve", response_model=Candidate)
def approve_candidate(candidate_id: str) -> Candidate:
    try:
        return repository().approve_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")


@router.post("/candidates/{candidate_id}/reject", response_model=Candidate)
def reject_candidate(candidate_id: str) -> Candidate:
    try:
        return repository().reject_candidate(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found")


@router.get("/notebooks/{notebook_id}/rules", response_model=List[RuleCard])
def list_rules(notebook_id: str) -> List[RuleCard]:
    try:
        return repository().list_rules(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/rules/{rule_id}/explain", response_model=RuleExplanation)
def explain_rule(notebook_id: str, rule_id: str) -> RuleExplanation:
    try:
        return repository().explain_rule(notebook_id, rule_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Rule not found")


@router.get("/notebooks/{notebook_id}/methods", response_model=List[MethodCard])
def list_methods(notebook_id: str) -> List[MethodCard]:
    try:
        return repository().list_methods(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/risks", response_model=List[RiskItemCard])
def list_risks(notebook_id: str) -> List[RiskItemCard]:
    try:
        return repository().list_risks(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/glossary", response_model=List[GlossaryTermCard])
def list_glossary(notebook_id: str) -> List[GlossaryTermCard]:
    try:
        return repository().list_glossary(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get(
    "/notebooks/{notebook_id}/knowledge-types",
    response_model=List[KnowledgeTypeCount],
)
def knowledge_types(notebook_id: str) -> List[KnowledgeTypeCount]:
    try:
        return repository().knowledge_types(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/knowledge", response_model=List[KnowledgeRecord])
def list_knowledge(
    notebook_id: str, type: str = Query(...)
) -> List[KnowledgeRecord]:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().list_knowledge(notebook_id, object_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Editable extraction-schema registry ---------------------------------
@router.get("/object-schemas", response_model=List[ObjectSchemaModel])
def list_object_schemas() -> List[ObjectSchemaModel]:
    return repository().list_object_schemas()


@router.post("/object-schemas", response_model=ObjectSchemaModel)
def create_object_schema(payload: ObjectSchemaCreate) -> ObjectSchemaModel:
    try:
        return repository().create_object_schema(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/object-schemas/{object_type}", response_model=ObjectSchemaModel)
def update_object_schema(
    object_type: str, payload: ObjectSchemaUpdate
) -> ObjectSchemaModel:
    try:
        return repository().update_object_schema(object_type, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/object-schemas/{object_type}")
def delete_object_schema(object_type: str):
    try:
        repository().delete_object_schema(object_type)
        return {"status": "deleted", "object_type": object_type}
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/schema-proposals",
    response_model=List[ObjectSchemaModel],
)
def propose_schemas(notebook_id: str) -> List[ObjectSchemaModel]:
    try:
        return repository().propose_schemas(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/knowledge/{knowledge_id}")
def update_knowledge(knowledge_id: str, payload: KnowledgeUpdate):
    try:
        return repository().update_knowledge(knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_KNOWLEDGE_TYPE_MAP = {
    "rules": "rule",
    "methods": "method",
    "risks": "risk",
    "cases": "case",
    "checklist": "checklist",
    "glossary": "glossary",
}


@router.get("/notebooks/{notebook_id}/duplicates", response_model=List[DuplicateGroup])
def find_duplicates(notebook_id: str, type: str = Query("rules")) -> List[DuplicateGroup]:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().find_duplicates(notebook_id, object_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/conflicts", response_model=List[ConflictPair])
def find_conflicts(notebook_id: str) -> List[ConflictPair]:
    try:
        return repository().find_conflicts(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/graph", response_model=KnowledgeGraph)
def knowledge_graph(notebook_id: str) -> KnowledgeGraph:
    try:
        return repository().knowledge_graph(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/knowledge/{knowledge_id}/merge")
def merge_knowledge(knowledge_id: str, payload: MergeRequest):
    try:
        return repository().merge_knowledge(knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/notebooks/{notebook_id}/search", response_model=NotebookSearchResponse)
def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    try:
        return repository().search_notebook(notebook_id, q)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse)
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    try:
        return repository().ask(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/scenario-query", response_model=AskResponse)
def scenario_query(notebook_id: str, payload: ScenarioQueryRequest) -> AskResponse:
    try:
        return repository().scenario_query(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/case-search", response_model=List[CaseCard])
def case_search(notebook_id: str, payload: CaseSearchRequest) -> List[CaseCard]:
    try:
        return repository().case_search(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/checklist", response_model=List[ChecklistItem])
def checklist(notebook_id: str, payload: ChecklistRequest) -> List[ChecklistItem]:
    try:
        return repository().checklist(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/articles", response_model=List[ArticleSummary])
def list_articles(notebook_id: str) -> List[ArticleSummary]:
    try:
        return repository().list_articles(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/articles", response_model=ArticleSummary)
def create_article(notebook_id: str, payload: ArticleCreate) -> ArticleSummary:
    try:
        return repository().create_article(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/articles/{article_id}", status_code=204)
def delete_article(article_id: str) -> None:
    try:
        repository().delete_article(article_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Article not found")


@router.post("/articles/{article_id}/research", response_model=ArticleResearchBrief)
def research_article(article_id: str) -> ArticleResearchBrief:
    try:
        return repository().research_article(article_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Article not found")


@router.get("/notebooks/{notebook_id}/derived-rules", response_model=List[DerivedRuleCandidate])
def list_derived_rules(notebook_id: str) -> List[DerivedRuleCandidate]:
    try:
        return repository().list_derived_rules(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/derived-rules/{candidate_id}/approve", response_model=RuleCard)
def approve_derived_rule(candidate_id: str) -> RuleCard:
    try:
        return repository().approve_derived_rule(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Derived rule candidate not found")


@router.post("/derived-rules/{candidate_id}/reject", response_model=DerivedRuleCandidate)
def reject_derived_rule(candidate_id: str) -> DerivedRuleCandidate:
    try:
        return repository().reject_derived_rule(candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Derived rule candidate not found")


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(answer_id: str, payload: FeedbackRequest) -> FeedbackResponse:
    try:
        return repository().submit_feedback(answer_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Answer not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
