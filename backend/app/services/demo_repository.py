from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from app.core.config import Settings
from app.models.schemas import (
    ArticleCreate,
    ArticleResearchBrief,
    ArticleSummary,
    AskRequest,
    AskResponse,
    CaseCard,
    CaseSearchRequest,
    ChecklistItem,
    ChecklistRequest,
    Citation,
    Evidence,
    NotebookCreate,
    NotebookSummary,
    NotebookUpdate,
    RuleCard,
    ScenarioQueryRequest,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)


DEMO_NOTEBOOK_ID = "nb-analog-packaging"
DEMO_SOURCE_ID = "src-synthetic-wirebond-guideline"
DEMO_ARTICLE_ID = "art-synthetic-bondwire-coupling"


def _evidence(element_id: str, location_label: str, quoted_span: str) -> Evidence:
    return Evidence(
        source_id=DEMO_SOURCE_ID,
        source_title="Synthetic Analog Packaging Knowhow Guideline",
        element_id=element_id,
        element_type="paragraph",
        location_label=location_label,
        quoted_span=quoted_span,
        confidence=0.86,
    )


def _citation(label: str, evidence: Evidence) -> Citation:
    return Citation(
        label=label,
        source_id=evidence.source_id,
        element_id=evidence.element_id,
        location_label=evidence.location_label,
        quoted_span=evidence.quoted_span,
    )


RULE_WIREBOND_RETURN = RuleCard(
    id="RULE-PKG-001",
    title="Keep low-noise input bondwires away from switching return loops",
    statement=(
        "低噪声模拟输入引脚的 bondwire should be separated from high di/dt "
        "switching loops and noisy return paths."
    ),
    applies_to=["Analog IC", "Low-noise AFE", "Wirebond", "Package review"],
    recommendation=(
        "Place sensitive input pins next to quiet ground references and review "
        "the package return path before pinout freeze."
    ),
    risk_if_ignored=(
        "Coupled switching current can raise the measured noise floor and make "
        "lab performance worse than simulation."
    ),
    severity="high",
    status="approved",
    evidence=[
        _evidence(
            "el-pdf-001",
            "PDF p.3 paragraph 2",
            "Sensitive analog inputs should not share an immediate bondwire neighborhood with high-current switching returns.",
        )
    ],
)

RULE_ESD_PATH = RuleCard(
    id="RULE-PKG-002",
    title="Review ESD discharge path together with sensitive analog pins",
    statement=(
        "ESD path planning must be reviewed with pin assignment when the same "
        "package edge contains sensitive analog and external-facing pins."
    ),
    applies_to=["ESD", "Package review", "Analog front-end"],
    recommendation=(
        "Check discharge path, clamp location, local ground inductance, and "
        "victim pin adjacency in one review pass."
    ),
    risk_if_ignored=(
        "A legal schematic-level ESD solution may still inject transient stress "
        "or substrate noise into the analog front-end."
    ),
    severity="medium",
    status="reviewed",
    evidence=[
        _evidence(
            "el-md-002",
            "Markdown section 'ESD and quiet pins'",
            "ESD clamps and quiet analog pins need a shared package-level review because the return inductance is package dependent.",
        )
    ],
)

CASE_NOISE = CaseCard(
    id="CASE-2024-AFE-NOISE-001",
    symptom="Measured input-referred noise was 30 percent higher than pre-layout simulation.",
    context=(
        "Low-noise AFE in wirebond package; high-current digital return pins "
        "were assigned on the same package side."
    ),
    root_cause=(
        "Bondwire mutual coupling and shared return inductance created a noise "
        "path that was absent from the original simulation setup."
    ),
    resolution=(
        "Pin assignment was revised, quiet ground pins were added around the "
        "input pair, and package parasitics were included in signoff simulation."
    ),
    lesson_learned=(
        "Package review should happen before pinout freeze and should include "
        "return-path coupling checks."
    ),
    evidence=[
        _evidence(
            "el-docx-004",
            "DOCX paragraph 14",
            "The lab noise issue disappeared after the input pins were moved away from the switching return cluster.",
        )
    ],
)


class DemoRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.notebooks: Dict[str, NotebookSummary] = {
            DEMO_NOTEBOOK_ID: NotebookSummary(
                id=DEMO_NOTEBOOK_ID,
                name="Analog Packaging Knowhow",
                purpose=(
                    "Scenario-aware package rules, cases, and checklists for "
                    "low-noise analog IC teams."
                ),
                primary_domain="Analog IC Packaging",
                status="beta-demo",
                counts={
                    "sources": 1,
                    "rules": 2,
                    "cases": 1,
                    "checklist_items": 4,
                    "article_claims": 3,
                },
                created_label="2026年5月28日",
            )
        }
        self.sources: Dict[str, SourceSummary] = {
            DEMO_SOURCE_ID: SourceSummary(
                id=DEMO_SOURCE_ID,
                notebook_id=DEMO_NOTEBOOK_ID,
                title="Synthetic Analog Packaging Knowhow Guideline",
                type="guideline",
                status="parsed",
                summary=(
                    "Synthetic mixed Chinese/English source covering wirebond "
                    "pin assignment, quiet ground return, ESD path review, and "
                    "package parasitic risks for low-noise analog front-ends."
                ),
                element_count=4,
                file_name="analog_packaging_knowhow.md",
                file_size=4096,
            )
        }
        self.elements: Dict[str, List[SourceElement]] = {
            DEMO_SOURCE_ID: [
                SourceElement(
                    id="el-pdf-001",
                    source_id=DEMO_SOURCE_ID,
                    element_type="paragraph",
                    location_label="PDF p.3 paragraph 2",
                    text=RULE_WIREBOND_RETURN.evidence[0].quoted_span,
                    metadata={"parser": "synthetic", "language": "en"},
                ),
                SourceElement(
                    id="el-md-002",
                    source_id=DEMO_SOURCE_ID,
                    element_type="paragraph",
                    location_label="Markdown section 'ESD and quiet pins'",
                    text=RULE_ESD_PATH.evidence[0].quoted_span,
                    metadata={"parser": "synthetic", "language": "en"},
                ),
                SourceElement(
                    id="el-docx-004",
                    source_id=DEMO_SOURCE_ID,
                    element_type="paragraph",
                    location_label="DOCX paragraph 14",
                    text=CASE_NOISE.evidence[0].quoted_span,
                    metadata={"parser": "synthetic", "language": "en"},
                ),
                SourceElement(
                    id="el-pptx-006",
                    source_id=DEMO_SOURCE_ID,
                    element_type="slide_text_box",
                    location_label="PPTX slide 6 text box 2",
                    text="Package review checklist: quiet pins, return path, parasitic extraction, ESD path.",
                    metadata={"parser": "synthetic", "language": "mixed"},
                ),
            ]
        }
        self.articles: Dict[str, ArticleSummary] = {
            DEMO_ARTICLE_ID: ArticleSummary(
                id=DEMO_ARTICLE_ID,
                notebook_id=DEMO_NOTEBOOK_ID,
                source_id=DEMO_SOURCE_ID,
                title="Synthetic Paper: Bondwire Coupling in Low-Noise AFE Packages",
                status="brief-ready",
                summary=(
                    "A synthetic article used to demonstrate claim extraction "
                    "and implication mapping for package-level coupling."
                ),
            )
        }

    def current_user(self) -> UserProfile:
        return UserProfile(
            id="user-local",
            email=self.settings.single_user_email,
            display_name=self.settings.single_user_name,
            role="curator",
            memory_mode="manual",
            domain_focus=["Analog IC", "Packaging", "Reliability"],
        )

    def list_notebooks(self) -> List[NotebookSummary]:
        return list(self.notebooks.values())

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        notebook_id = f"nb-{uuid4().hex[:10]}"
        now = datetime.now()
        notebook = NotebookSummary(
            id=notebook_id,
            name=payload.name,
            purpose=payload.purpose,
            primary_domain=payload.primary_domain,
            status="draft",
            counts={
                "sources": 0,
                "rules": 0,
                "cases": 0,
                "checklist_items": 0,
                "article_claims": 0,
            },
            created_label=f"{now.year}年{now.month}月{now.day}日",
        )
        self.notebooks[notebook_id] = notebook
        return notebook

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self.notebooks[notebook_id]

    def update_notebook(
        self,
        notebook_id: str,
        payload: NotebookUpdate,
    ) -> NotebookSummary:
        notebook = self.get_notebook(notebook_id)
        if payload.name is not None:
            notebook.name = payload.name.strip() or notebook.name
        if payload.purpose is not None:
            notebook.purpose = payload.purpose.strip()
        if payload.primary_domain is not None:
            notebook.primary_domain = payload.primary_domain.strip() or notebook.primary_domain
        if payload.status is not None:
            notebook.status = payload.status.strip() or notebook.status
        return notebook

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        del self.notebooks[notebook_id]

        source_ids = [
            source_id
            for source_id, source in self.sources.items()
            if source.notebook_id == notebook_id
        ]
        for source_id in source_ids:
            del self.sources[source_id]
            self.elements.pop(source_id, None)

        article_ids = [
            article_id
            for article_id, article in self.articles.items()
            if article.notebook_id == notebook_id
        ]
        for article_id in article_ids:
            del self.articles[article_id]

    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        return [
            source
            for source in self.sources.values()
            if source.notebook_id == notebook_id
        ]

    def import_sources(
        self,
        notebook_id: str,
        payload: SourceImportRequest,
    ) -> List[SourceSummary]:
        self.get_notebook(notebook_id)
        imported_sources: List[SourceSummary] = []
        for file in payload.files:
            source_id = f"src-{uuid4().hex[:10]}"
            source_type = self._source_type_from_name(file.file_name)
            source = SourceSummary(
                id=source_id,
                notebook_id=notebook_id,
                title=file.file_name,
                type=source_type,
                status="imported",
                summary=(
                    "File metadata imported. Content parsing and element "
                    "extraction will run in the ingestion pipeline."
                ),
                element_count=0,
                file_name=file.file_name,
                file_size=file.file_size,
            )
            self.sources[source_id] = source
            self.elements[source_id] = []
            imported_sources.append(source)

        notebook = self.notebooks[notebook_id]
        notebook.counts["sources"] = len(self.list_sources(notebook_id))
        return imported_sources

    def _source_type_from_name(self, file_name: str) -> str:
        lower_name = file_name.lower()
        if lower_name.endswith(".pdf"):
            return "pdf"
        if lower_name.endswith(".md") or lower_name.endswith(".markdown"):
            return "markdown"
        if lower_name.endswith(".docx"):
            return "docx"
        if lower_name.endswith(".pptx"):
            return "pptx"
        return "other"

    def source_elements(self, source_id: str) -> List[SourceElement]:
        return self.elements.get(source_id, [])

    def delete_source(self, source_id: str) -> None:
        source = self.sources[source_id]
        del self.sources[source_id]
        self.elements.pop(source_id, None)
        notebook = self.notebooks[source.notebook_id]
        notebook.counts["sources"] = len(self.list_sources(source.notebook_id))

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        if notebook_id != DEMO_NOTEBOOK_ID:
            source_count = len(self.list_sources(notebook_id))
            return AskResponse(
                conclusion=(
                    "This notebook is ready for sources. Import PDF, Markdown, "
                    "DOCX, or PPTX files first; extraction will then populate "
                    "rules, cases, checklists, and citations."
                ),
                applicable_scenario=[],
                recommended_methods=[
                    "Import existing engineering documents from the Sources panel.",
                    "Run ingestion and extraction after files are available.",
                ],
                related_rules=[],
                potential_risks=[],
                related_cases=[],
                checklist=[],
                missing_information=[
                    f"{source_count} source file(s) imported; parsed knowledge is not available yet.",
                    "Notebook title and description will be inferred after source analysis.",
                ],
                citations=[],
                llm_mode="waiting-for-sources",
            )

        rule_evidence = RULE_WIREBOND_RETURN.evidence[0]
        case_evidence = CASE_NOISE.evidence[0]
        return AskResponse(
            conclusion=(
                "For a low-noise AFE in a wirebond package, review pin "
                "assignment around quiet input pins, ground return continuity, "
                "high-current loop adjacency, and ESD discharge path before "
                "pinout freeze."
            ),
            applicable_scenario=[
                "Analog IC",
                "Low-noise front-end",
                "Wirebond package",
                "Package or pinout review",
            ],
            recommended_methods=[
                "Place sensitive input pins near quiet ground references.",
                "Model bondwire and package return inductance in signoff simulation.",
                "Review ESD path and switching return path in the same package pass.",
            ],
            related_rules=[RULE_WIREBOND_RETURN, RULE_ESD_PATH],
            potential_risks=[
                "Measured noise can exceed simulation if package coupling is omitted.",
                "ESD discharge current can share an unintended local return path.",
            ],
            related_cases=[CASE_NOISE],
            checklist=[
                "Are sensitive input pins separated from high-current switching returns?",
                "Is there a quiet ground reference adjacent to the input pins?",
                "Were bondwire parasitics included in the latest simulation?",
                "Has the ESD path been reviewed at package level?",
            ],
            missing_information=[
                "Exact package type and pin map",
                "Switching current magnitude and edge rate",
                "Available package parasitic model",
            ],
            citations=[
                _citation("Rule evidence", rule_evidence),
                _citation("Case evidence", case_evidence),
            ],
            llm_mode="configured" if self.settings.llm_configured else "deterministic-demo",
        )

    def scenario_query(
        self,
        notebook_id: str,
        payload: ScenarioQueryRequest,
    ) -> AskResponse:
        question = (
            "Scenario query for "
            f"{payload.domain} {payload.block_type} {payload.design_stage} "
            f"{payload.package_type} {payload.concern}"
        )
        return self.ask(
            notebook_id,
            AskRequest(
                question=question,
                scenario=payload.dict(),
            ),
        )

    def case_search(
        self,
        notebook_id: str,
        payload: CaseSearchRequest,
    ) -> List[CaseCard]:
        if notebook_id != DEMO_NOTEBOOK_ID:
            self.get_notebook(notebook_id)
            return []
        return [CASE_NOISE]

    def checklist(
        self,
        notebook_id: str,
        payload: ChecklistRequest,
    ) -> List[ChecklistItem]:
        if notebook_id != DEMO_NOTEBOOK_ID:
            self.get_notebook(notebook_id)
            return []
        rule_evidence = RULE_WIREBOND_RETURN.evidence[0]
        citation = _citation("Checklist evidence", rule_evidence)
        return [
            ChecklistItem(
                question="Are low-noise input pins isolated from high di/dt return loops?",
                severity="high",
                required_evidence="Pin map with package-side adjacency and return path annotation.",
                related_rule_ids=["RULE-PKG-001"],
                citations=[citation],
            ),
            ChecklistItem(
                question="Was package parasitic coupling included in the latest noise simulation?",
                severity="high",
                required_evidence="Simulation deck or signoff note including bondwire parasitics.",
                related_rule_ids=["RULE-PKG-001"],
                citations=[citation],
            ),
            ChecklistItem(
                question="Has the ESD path been checked against quiet analog pins?",
                severity="medium",
                required_evidence="ESD path sketch and clamp location review.",
                related_rule_ids=["RULE-PKG-002"],
                citations=[_citation("ESD evidence", RULE_ESD_PATH.evidence[0])],
            ),
        ]

    def list_articles(self, notebook_id: str) -> List[ArticleSummary]:
        return [
            article
            for article in self.articles.values()
            if article.notebook_id == notebook_id
        ]

    def create_article(
        self,
        notebook_id: str,
        payload: ArticleCreate,
    ) -> ArticleSummary:
        article_id = f"art-{uuid4().hex[:10]}"
        article = ArticleSummary(
            id=article_id,
            notebook_id=notebook_id,
            source_id=payload.source_id,
            title=payload.title,
            status="uploaded",
            summary=payload.abstract or "Article uploaded; research brief not generated yet.",
        )
        self.articles[article_id] = article
        return article

    def delete_article(self, article_id: str) -> None:
        del self.articles[article_id]

    def research_article(self, article_id: str) -> ArticleResearchBrief:
        article = self.articles[article_id]
        evidence = RULE_WIREBOND_RETURN.evidence[0]
        return ArticleResearchBrief(
            article=article,
            core_contribution=(
                "The article highlights that bondwire geometry and return-path "
                "placement can dominate package-level coupling for low-noise AFE inputs."
            ),
            claims=[
                "Bondwire mutual coupling increases when sensitive inputs are routed near switching returns.",
                "Package-level return inductance can explain lab noise that is absent from schematic-only simulation.",
                "Pin assignment review should happen before package signoff.",
            ],
            limitations=[
                "Synthetic article for local beta only.",
                "No image, plot, or measurement-table parsing in MVP.",
            ],
            notebook_relationships=[
                "supports RULE-PKG-001",
                "extends checklist coverage for package parasitic simulation",
                "suggests validation action for pinout freeze reviews",
            ],
            derived_rule_candidates=[
                "Before pinout freeze, require a package coupling review between low-noise input pins, quiet grounds, and high-current switching returns."
            ],
            validation_plan=[
                "Run package parasitic extraction for candidate pin maps.",
                "Compare noise simulation with and without bondwire coupling.",
                "Ask packaging owner to approve the final pin assignment checklist.",
            ],
            citations=[_citation("Article implication evidence", evidence)],
        )
