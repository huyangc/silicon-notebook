from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional
from uuid import uuid4

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.core.llm import OpenAICompatibleClient
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
    Citation,
    ConflictPair,
    DerivedRuleCandidate,
    DuplicateGroup,
    Evidence,
    FeedbackRequest,
    FeedbackResponse,
    GlossaryTermCard,
    KnowledgeRef,
    KnowledgeUpdate,
    MergeRequest,
    MethodCard,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    RiskItemCard,
    RuleCard,
    RuleExplanation,
    ScenarioQueryRequest,
    SearchHit,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)
from app.services.demo_repository import (
    CASE_NOISE,
    DEMO_ARTICLE_ID,
    DEMO_NOTEBOOK_ID,
    DEMO_SOURCE_ID,
    RULE_ESD_PATH,
    RULE_WIREBOND_RETURN,
    _citation,
)
from app.services.extraction import bind_evidence, run_extraction
from app.services.mineru_client import MinerUClient
from app.services.notebook_templates import NOTEBOOK_TEMPLATES, get_template
from app.services.parsers import parse_source_file
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    ARTICLE_SCHEMA_HINT,
    answer_prompt,
    article_prompt,
)
from app.services.repository import UploadedSourceFile
from app.services.retrieval import (
    RetrievedElement,
    RetrievedKnowledge,
    _payload_text,
    cosine,
    keyword_score,
    score_elements,
    score_knowledge,
)


# Knowledge statuses that may be surfaced in answers/retrieval (§12 governance).
# 'deprecated' is excluded; 'conflict' is retrieved but flagged elsewhere.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")
KNOWLEDGE_STATUSES = ("approved", "reviewed", "deprecated", "conflict", "project_specific")


class SQLiteRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root_dir = Path(__file__).resolve().parents[3]
        self.db_path = self._resolve_path(settings.sqlite_path)
        self.storage_dir = self._resolve_path(settings.storage_dir)
        self.llm_client = OpenAICompatibleClient(settings)
        self.mineru_client = MinerUClient(settings)
        self.event_log = EventLogger(settings, channel="events")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._seed()

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root_dir / path
        return path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  display_name TEXT NOT NULL,
                  role TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  memory_mode TEXT NOT NULL DEFAULT 'manual',
                  domain_focus TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notebooks (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  purpose TEXT NOT NULL DEFAULT '',
                  primary_domain TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'draft',
                  created_by TEXT REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  title TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'uploaded',
                  parse_status TEXT NOT NULL DEFAULT 'uploaded',
                  file_name TEXT NOT NULL DEFAULT '',
                  file_path TEXT NOT NULL DEFAULT '',
                  file_size INTEGER NOT NULL DEFAULT 0,
                  file_hash TEXT NOT NULL DEFAULT '',
                  summary TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_elements (
                  id TEXT PRIMARY KEY,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  element_type TEXT NOT NULL,
                  location_label TEXT NOT NULL,
                  text TEXT NOT NULL,
                  metadata TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS articles (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'uploaded',
                  summary TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_runs (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
                  run_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_candidates (
                  id TEXT PRIMARY KEY,
                  extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
                  candidate_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'candidate',
                  payload TEXT NOT NULL DEFAULT '{}',
                  evidence TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS element_embeddings (
                  element_id TEXT PRIMARY KEY REFERENCES source_elements(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                  object_id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_objects (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  object_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'approved',
                  owner TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL DEFAULT '{}',
                  evidence TEXT NOT NULL DEFAULT '[]',
                  source_candidate_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS answers (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  question TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feedback (
                  id TEXT PRIMARY KEY,
                  answer_id TEXT NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  rating TEXT NOT NULL,
                  comment TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS article_claims (
                  id TEXT PRIMARY KEY,
                  article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  statement TEXT NOT NULL DEFAULT '',
                  claim_type TEXT NOT NULL DEFAULT '',
                  relation_type TEXT NOT NULL DEFAULT '',
                  related_rule_id TEXT NOT NULL DEFAULT '',
                  implication TEXT NOT NULL DEFAULT '',
                  evidence TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS derived_rule_candidates (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  article_id TEXT REFERENCES articles(id) ON DELETE CASCADE,
                  title TEXT NOT NULL DEFAULT '',
                  proposed_rule TEXT NOT NULL DEFAULT '',
                  rationale TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'draft',
                  evidence TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );
                """
            )
            # Lightweight column migrations for pre-existing databases.
            ko_cols = {r["name"] for r in db.execute("PRAGMA table_info(knowledge_objects)").fetchall()}
            if "last_reviewed" not in ko_cols:
                db.execute(
                    "ALTER TABLE knowledge_objects ADD COLUMN last_reviewed TEXT NOT NULL DEFAULT ''"
                )
            nb_cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)").fetchall()}
            for col in ("target_users", "expected_questions", "source_types", "taxonomy", "access_scope"):
                if col not in nb_cols:
                    db.execute(f"ALTER TABLE notebooks ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    def _seed(self) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO users
                (id, email, display_name, role, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "user-local",
                    self.settings.single_user_email,
                    self.settings.single_user_name,
                    "curator",
                    "active",
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO user_profiles
                (id, user_id, memory_mode, domain_focus, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "profile-local",
                    "user-local",
                    "manual",
                    json.dumps(["Analog IC", "Packaging", "Reliability"]),
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO notebooks
                (id, name, purpose, primary_domain, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    DEMO_NOTEBOOK_ID,
                    "Analog Packaging Knowhow",
                    (
                        "Scenario-aware package rules, cases, and checklists for "
                        "low-noise analog IC teams."
                    ),
                    "Analog IC Packaging",
                    "beta-demo",
                    "user-local",
                    "2026-05-28T00:00:00",
                    now,
                ),
            )
            db.execute(
                """
                INSERT OR IGNORE INTO sources
                (id, notebook_id, title, source_type, status, parse_status, file_name,
                 file_path, file_size, file_hash, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    DEMO_SOURCE_ID,
                    DEMO_NOTEBOOK_ID,
                    "Synthetic Analog Packaging Knowhow Guideline",
                    "guideline",
                    "parsed",
                    "parsed",
                    "analog_packaging_knowhow.md",
                    str(self.root_dir / "data/demo/analog_packaging_knowhow.md"),
                    4096,
                    "synthetic",
                    (
                        "Synthetic mixed Chinese/English source covering wirebond "
                        "pin assignment, quiet ground return, ESD path review, and "
                        "package parasitic risks for low-noise analog front-ends."
                    ),
                    "2026-05-28T00:00:00",
                    now,
                ),
            )
            if self._count(db, "source_elements", "source_id", DEMO_SOURCE_ID) == 0:
                demo_elements = [
                    (
                        "el-pdf-001",
                        "paragraph",
                        "PDF p.3 paragraph 2",
                        RULE_WIREBOND_RETURN.evidence[0].quoted_span,
                        {"parser": "synthetic", "language": "en"},
                    ),
                    (
                        "el-md-002",
                        "paragraph",
                        "Markdown section 'ESD and quiet pins'",
                        RULE_ESD_PATH.evidence[0].quoted_span,
                        {"parser": "synthetic", "language": "en"},
                    ),
                    (
                        "el-docx-004",
                        "paragraph",
                        "DOCX paragraph 14",
                        CASE_NOISE.evidence[0].quoted_span,
                        {"parser": "synthetic", "language": "en"},
                    ),
                    (
                        "el-pptx-006",
                        "slide_text_box",
                        "PPTX slide 6 text box 2",
                        "Package review checklist: quiet pins, return path, parasitic extraction, ESD path.",
                        {"parser": "synthetic", "language": "mixed"},
                    ),
                ]
                for element_id, element_type, location_label, text, metadata in demo_elements:
                    db.execute(
                        """
                        INSERT INTO source_elements
                        (id, source_id, element_type, location_label, text, metadata, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            element_id,
                            DEMO_SOURCE_ID,
                            element_type,
                            location_label,
                            text,
                            json.dumps(metadata),
                            now,
                        ),
                    )
            db.execute(
                """
                INSERT OR IGNORE INTO articles
                (id, notebook_id, source_id, title, status, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    DEMO_ARTICLE_ID,
                    DEMO_NOTEBOOK_ID,
                    DEMO_SOURCE_ID,
                    "Synthetic Paper: Bondwire Coupling in Low-Noise AFE Packages",
                    "brief-ready",
                    (
                        "A synthetic article used to demonstrate claim extraction "
                        "and implication mapping for package-level coupling."
                    ),
                    "2026-05-28T00:00:00",
                    now,
                ),
            )
        self._seed_demo_knowledge()

    def _seed_demo_knowledge(self) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM knowledge_objects WHERE notebook_id = ?",
                (DEMO_NOTEBOOK_ID,),
            ).fetchone()
            if int(row["count"]):
                return
            candidate_count = self._count(db, "extraction_candidates", "source_id", DEMO_SOURCE_ID)
        if candidate_count == 0:
            self._run_extraction(DEMO_SOURCE_ID)
        for candidate in self.list_candidates(DEMO_NOTEBOOK_ID):
            if candidate.source_id == DEMO_SOURCE_ID:
                self.approve_candidate(candidate.id)

    def _count(self, db: sqlite3.Connection, table: str, column: str, value: str) -> int:
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",
            (value,),
        ).fetchone()
        return int(row["count"])

    def _count_knowledge(self, db: sqlite3.Connection, notebook_id: str, object_type: str) -> int:
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM knowledge_objects "
            f"WHERE notebook_id = ? AND object_type = ? AND status IN ({placeholders})",
            (notebook_id, object_type, *USABLE_STATUSES),
        ).fetchone()
        return int(row["count"])

    def _clear_source_extraction_state(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        candidate_rows = db.execute(
            "SELECT id FROM extraction_candidates WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        candidate_ids = [row["id"] for row in candidate_rows]

        stale_knowledge_ids: List[str] = []
        knowledge_rows = db.execute(
            "SELECT id, source_candidate_id, evidence FROM knowledge_objects WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        candidate_id_set = set(candidate_ids)
        for row in knowledge_rows:
            if row["source_candidate_id"] in candidate_id_set:
                stale_knowledge_ids.append(row["id"])
                continue
            try:
                evidence_items = json.loads(row["evidence"] or "[]")
            except json.JSONDecodeError:
                evidence_items = []
            if any(item.get("source_id") == source_id for item in evidence_items if isinstance(item, dict)):
                stale_knowledge_ids.append(row["id"])

        if stale_knowledge_ids:
            placeholders = ",".join("?" for _ in stale_knowledge_ids)
            db.execute(
                f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
                stale_knowledge_ids,
            )
        db.execute("DELETE FROM extraction_candidates WHERE source_id = ?", (source_id,))
        db.execute("DELETE FROM extraction_runs WHERE source_id = ?", (source_id,))
        if clear_embeddings:
            db.execute("DELETE FROM element_embeddings WHERE source_id = ?", (source_id,))

    def _knowledge_objects(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]] = USABLE_STATUSES,
    ) -> List[dict]:
        query = (
            "SELECT * FROM knowledge_objects WHERE notebook_id = ? AND object_type = ?"
        )
        params: List[object] = [notebook_id, object_type]
        if statuses is not None:
            status_list = list(statuses)
            placeholders = ",".join("?" for _ in status_list)
            query += f" AND status IN ({placeholders})"
            params.extend(status_list)
        query += " ORDER BY created_at ASC, id ASC"
        rows = db.execute(query, params).fetchall()
        objects: List[dict] = []
        for row in rows:
            keys = row.keys()
            objects.append(
                {
                    "id": row["id"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": [
                        Evidence(**item)
                        for item in json.loads(row["evidence"] or "[]")
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": row["last_reviewed"] if "last_reviewed" in keys else "",
                }
            )
        return objects

    def current_user(self) -> UserProfile:
        with self._connect() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", ("user-local",)).fetchone()
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?",
                ("user-local",),
            ).fetchone()
        return UserProfile(
            id=user["id"],
            email=user["email"],
            display_name=user["display_name"],
            role=user["role"],
            memory_mode=profile["memory_mode"] if profile else "manual",
            domain_focus=json.loads(profile["domain_focus"]) if profile else [],
        )

    def list_notebooks(self) -> List[NotebookSummary]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM notebooks ORDER BY created_at ASC"
            ).fetchall()
            return [self._notebook_from_row(db, row) for row in rows]

    def list_notebook_templates(self) -> List[NotebookTemplate]:
        return [NotebookTemplate(**t) for t in NOTEBOOK_TEMPLATES]

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        notebook_id = f"nb-{uuid4().hex[:10]}"
        now = _now()
        template = get_template(payload.template) if payload.template else None

        def fill(value, key, default):
            if value:
                return value
            if template and template.get(key):
                return template[key]
            return default

        purpose = fill(payload.purpose, "purpose", "")
        primary_domain = fill(
            payload.primary_domain if payload.primary_domain != "Semiconductor" else "",
            "primary_domain",
            "Semiconductor",
        )
        target_users = fill(payload.target_users, "target_users", "")
        access_scope = payload.access_scope
        expected_questions = fill(payload.expected_questions, "expected_questions", [])
        source_types = fill(payload.source_types, "source_types", [])
        taxonomy = fill(payload.taxonomy, "taxonomy", [])

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO notebooks
                (id, name, purpose, primary_domain, status, created_by, created_at, updated_at,
                 target_users, expected_questions, source_types, taxonomy, access_scope)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notebook_id,
                    payload.name,
                    purpose,
                    primary_domain,
                    "draft",
                    "user-local",
                    now,
                    now,
                    target_users,
                    json.dumps(expected_questions, ensure_ascii=False),
                    json.dumps(source_types, ensure_ascii=False),
                    json.dumps(taxonomy, ensure_ascii=False),
                    access_scope,
                ),
            )
            row = db.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            return self._notebook_from_row(db, row)

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        with self._connect() as db:
            row = db.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            if row is None:
                raise KeyError(notebook_id)
            return self._notebook_from_row(db, row)

    def update_notebook(self, notebook_id: str, payload: NotebookUpdate) -> NotebookSummary:
        self.get_notebook(notebook_id)
        updates: List[str] = []
        values: List[str] = []
        if payload.name is not None:
            updates.append("name = ?")
            values.append(payload.name.strip() or "Untitled notebook")
        if payload.purpose is not None:
            updates.append("purpose = ?")
            values.append(payload.purpose.strip())
        if payload.primary_domain is not None:
            updates.append("primary_domain = ?")
            values.append(payload.primary_domain.strip() or "Semiconductor")
        if payload.status is not None:
            updates.append("status = ?")
            values.append(payload.status.strip() or "draft")
        if payload.target_users is not None:
            updates.append("target_users = ?")
            values.append(payload.target_users.strip())
        if payload.access_scope is not None:
            updates.append("access_scope = ?")
            values.append(payload.access_scope.strip())
        for field in ("expected_questions", "source_types", "taxonomy"):
            value = getattr(payload, field)
            if value is not None:
                updates.append(f"{field} = ?")
                values.append(json.dumps(value, ensure_ascii=False))
        if updates:
            updates.append("updated_at = ?")
            values.append(_now())
            values.append(notebook_id)
            with self._connect() as db:
                db.execute(
                    f"UPDATE notebooks SET {', '.join(updates)} WHERE id = ?",
                    values,
                )
        return self.get_notebook(notebook_id)

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            source_rows = db.execute(
                "SELECT file_path FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        for row in source_rows:
            self._delete_file(row["file_path"])

    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? ORDER BY created_at ASC",
                (notebook_id,),
            ).fetchall()
            return [self._source_from_row(db, row) for row in rows]

    def get_source(self, source_id: str) -> SourceDetail:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise KeyError(source_id)
            summary = self._source_from_row(db, row)
            return SourceDetail(
                **summary.model_dump(),
                file_path=row["file_path"],
                error_message=row["error_message"],
            )

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]:
        self.get_notebook(notebook_id)
        source_ids: List[str] = []
        now = _now()
        with self._connect() as db:
            for file in payload.files:
                source_id = f"src-{uuid4().hex[:10]}"
                db.execute(
                    """
                    INSERT INTO sources
                    (id, notebook_id, title, source_type, status, parse_status, file_name,
                     file_size, summary, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        notebook_id,
                        file.file_name,
                        self._source_type_from_name(file.file_name),
                        "imported",
                        "metadata-only",
                        file.file_name,
                        file.file_size,
                        "File metadata imported. Upload the file to parse source elements.",
                        now,
                        now,
                    ),
                )
                source_ids.append(source_id)
        return [self.get_source(source_id) for source_id in source_ids]

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> List[SourceSummary]:
        """Register uploaded files and kick off processing.

        With a ``scheduler`` (e.g. ``BackgroundTasks.add_task``) the heavy
        parse/embed/extract pipeline runs out of band and each source is
        returned in the ``queued`` state. Without one (tests, scripts) the
        pipeline runs synchronously before returning.
        """
        self.get_notebook(notebook_id)
        imported: List[SourceSummary] = []
        for file in files:
            source_id = f"src-{uuid4().hex[:10]}"
            file_name = _safe_filename(file.file_name)
            digest = hashlib.sha256(file.content).hexdigest()
            source_dir = self.storage_dir / "notebooks" / notebook_id
            source_dir.mkdir(parents=True, exist_ok=True)
            stored_path = source_dir / f"{source_id}_{file_name}"
            stored_path.write_bytes(file.content)
            now = _now()
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO sources
                    (id, notebook_id, title, source_type, status, parse_status, file_name,
                     file_path, file_size, file_hash, summary, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        notebook_id,
                        file_name,
                        self._source_type_from_name(file_name),
                        "queued",
                        "queued",
                        file_name,
                        str(stored_path),
                        len(file.content),
                        digest,
                        "Uploaded; parsing is queued.",
                        now,
                        now,
                    ),
                )
            if scheduler is not None:
                scheduler(source_id)
            else:
                self.process_source(source_id)
            imported.append(self.get_source(source_id))
        return imported

    def _set_source_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        error_message: str = "",
    ) -> None:
        fields = ["status = ?", "parse_status = ?", "error_message = ?", "updated_at = ?"]
        params: List[object] = [status, status, error_message, _now()]
        if summary is not None:
            fields.insert(2, "summary = ?")
            params.insert(2, summary)
        with self._connect() as db:
            db.execute(
                f"UPDATE sources SET {', '.join(fields)} WHERE id = ?",
                (*params, source_id),
            )
        # Emit every status-machine transition so it is visible in the event log.
        self.event_log.emit(
            {
                "kind": "status",
                "source_id": source_id,
                "status": status,
                "error": error_message or "",
            }
        )

    def process_source(self, source_id: str) -> SourceSummary:
        """Run the full parse -> embed -> extract pipeline with a status machine.

        States: queued -> parsing -> parsed -> extracting -> extracted (or failed).

        Each stage is timed and logged to the `events` channel so a "stuck"
        upload can be traced to the exact step (parse / embed / extract) and how
        long it has been running.
        """
        source = self.get_source(source_id)
        notebook_id = source.notebook_id
        now = _now()
        pipeline_started = time.perf_counter()

        def stage(name: str, status: str, started: float, **extra) -> None:
            self.event_log.emit(
                {
                    "kind": "pipeline",
                    "source_id": source_id,
                    "notebook_id": notebook_id,
                    "file_name": source.file_name,
                    "stage": name,
                    "status": status,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    **extra,
                }
            )

        self._set_source_status(source_id, "parsing")
        try:
            t = time.perf_counter()
            stage("parse", "start", t)
            elements = parse_source_file(
                source_id, source.file_path, source.file_name, self.mineru_client
            )
            stage("parse", "done", t, elements=len(elements), parser_mode=str(getattr(self.mineru_client, "mode", "")))
            summary = self._summarize_source(source.title, elements)
            with self._connect() as db:
                self._clear_source_extraction_state(
                    db,
                    source_id,
                    source.notebook_id,
                    clear_embeddings=True,
                )
                db.execute("DELETE FROM source_elements WHERE source_id = ?", (source_id,))
                for index, element in enumerate(elements, start=1):
                    element_id = f"el-{source_id}-{index:04d}"
                    db.execute(
                        """
                        INSERT INTO source_elements
                        (id, source_id, element_type, location_label, text, metadata, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            element_id,
                            source_id,
                            element.element_type,
                            element.location_label,
                            element.text,
                            json.dumps(element.metadata),
                            now,
                        ),
                    )
            self._set_source_status(source_id, "parsed", summary=summary)

            t = time.perf_counter()
            stage("embed", "start", t)
            self._embed_source(source_id)
            stage("embed", "done", t)

            self._set_source_status(source_id, "extracting")
            t = time.perf_counter()
            stage("extract", "start", t)
            self._run_extraction(source_id)
            stage("extract", "done", t)
            # Surface "parsed to empty" (e.g. scanned/image PDF with no text layer)
            # instead of a silent success that looks like a real result.
            empty_hint = ""
            if not elements and source.file_name.lower().endswith(".pdf"):
                empty_hint = (
                    "No extractable text — likely a scanned/image PDF. "
                    "Enable MinerU (MINERU_MODE) or add OCR to parse it."
                )
            self._set_source_status(source_id, "extracted", error_message=empty_hint)
            stage("pipeline", "done", pipeline_started, elements=len(elements))
        except Exception as exc:
            stage("pipeline", "error", pipeline_started, error=f"{type(exc).__name__}: {exc}")
            self.event_log.logger.exception("process_source failed for %s", source_id)
            self._set_source_status(
                source_id,
                "failed",
                summary="Parsing failed; see source error.",
                error_message=str(exc),
            )
        return self.get_source(source_id)

    def parse_source(self, source_id: str) -> SourceSummary:
        # Manual (re)parse is always synchronous so the response reflects the result.
        return self.process_source(source_id)

    def source_elements(self, source_id: str) -> List[SourceElement]:
        self.get_source(source_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM source_elements WHERE source_id = ? ORDER BY created_at ASC, id ASC",
                (source_id,),
            ).fetchall()
        return [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json.loads(row["metadata"] or "{}"),
            )
            for row in rows
        ]

    def delete_source(self, source_id: str) -> None:
        source = self.get_source(source_id)
        now = _now()
        with self._connect() as db:
            article_rows = db.execute(
                "SELECT id FROM articles WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            article_ids = [row["id"] for row in article_rows]
            if article_ids:
                placeholders = ",".join("?" for _ in article_ids)
                db.execute(
                    f"DELETE FROM article_claims WHERE article_id IN ({placeholders})",
                    article_ids,
                )
                db.execute(
                    f"DELETE FROM derived_rule_candidates WHERE article_id IN ({placeholders})",
                    article_ids,
                )
                db.execute(
                    "UPDATE articles SET source_id = NULL, status = ?, updated_at = ? WHERE source_id = ?",
                    ("uploaded", now, source_id),
                )
            self._clear_source_extraction_state(
                db,
                source_id,
                source.notebook_id,
                clear_embeddings=True,
            )
            db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self._delete_file(source.file_path)

    def _run_extraction(self, source_id: str) -> None:
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        now = _now()
        run_id = f"run-{uuid4().hex[:10]}"
        with self._connect() as db:
            self._clear_source_extraction_state(
                db,
                source_id,
                source.notebook_id,
                clear_embeddings=False,
            )
            db.execute(
                """
                INSERT INTO extraction_runs
                (id, notebook_id, source_id, run_type, status, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, source.notebook_id, source_id, "candidate", "running", "", now, now),
            )
        try:
            records = run_extraction(self.llm_client, elements, source.title)
            extraction_mode = (
                "llm"
                if any(record.extraction_mode == "llm" for record in records)
                else "heuristic"
            )
            with self._connect() as db:
                for index, record in enumerate(records, start=1):
                    candidate_payload = dict(record.payload)
                    candidate_payload["_extraction_mode"] = record.extraction_mode
                    db.execute(
                        """
                        INSERT INTO extraction_candidates
                        (id, extraction_run_id, notebook_id, source_id, candidate_type,
                         status, payload, evidence, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"cand-{run_id}-{index:04d}",
                            run_id,
                            source.notebook_id,
                            source_id,
                            record.candidate_type,
                            record.status,
                            json.dumps(candidate_payload, ensure_ascii=False),
                            json.dumps(
                                [item.model_dump() for item in record.evidence],
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                        ),
                    )
                db.execute(
                    "UPDATE extraction_runs SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                    ("completed", f"extraction_mode={extraction_mode}", _now(), run_id),
                )
        except Exception as exc:
            with self._connect() as db:
                db.execute(
                    "UPDATE extraction_runs SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                    ("failed", str(exc), _now(), run_id),
                )
            raise

    def _embed_source(self, source_id: str) -> None:
        if not self.settings.embedding_configured:
            return
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        now = _now()
        for element in elements:
            text = element.text.strip()
            if not text:
                continue
            try:
                vector = self.llm_client.embed(text[:2000])
            except Exception:
                return
            with self._connect() as db:
                db.execute(
                    """
                    INSERT OR REPLACE INTO element_embeddings
                    (element_id, source_id, notebook_id, vector, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        element.id,
                        source_id,
                        source.notebook_id,
                        json.dumps(vector),
                        now,
                    ),
                )

    def _embed_knowledge(
        self,
        object_id: str,
        notebook_id: str,
        payload: Dict[str, object],
    ) -> None:
        """Embed a knowledge object's own payload text (WS4: payload-level
        vectors, not just evidence-element vectors). No-op without embeddings."""
        if not self.settings.embedding_configured:
            return
        text = _payload_text(payload).strip()
        if not text:
            return
        try:
            vector = self.llm_client.embed(text[:2000])
        except Exception:
            return
        with self._connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO knowledge_embeddings
                (object_id, notebook_id, vector, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (object_id, notebook_id, json.dumps(vector), _now()),
            )

    def _knowledge_vectors(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        objects: List[dict],
    ) -> Dict[str, List[float]]:
        """Map object_id -> payload embedding. Lazily backfills missing vectors
        for the given objects (one-time per object) so pre-existing / seed
        knowledge also gains payload-level semantic recall."""
        rows = db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        vectors: Dict[str, List[float]] = {
            row["object_id"]: json.loads(row["vector"])
            for row in rows
            if row["vector"]
        }
        if not self.settings.embedding_configured:
            return vectors
        now = _now()
        for obj in objects:
            object_id = obj["id"]
            if object_id in vectors:
                continue
            text = _payload_text(obj.get("payload", {})).strip()
            if not text:
                continue
            try:
                vector = self.llm_client.embed(text[:2000])
            except Exception:
                continue
            vectors[object_id] = vector
            db.execute(
                """
                INSERT OR REPLACE INTO knowledge_embeddings
                (object_id, notebook_id, vector, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (object_id, notebook_id, json.dumps(vector), now),
            )
        return vectors

    def extract_source(self, source_id: str) -> List[Candidate]:
        source = self.get_source(source_id)
        self._run_extraction(source_id)
        return self.list_candidates(source.notebook_id, None)

    def list_candidates(
        self,
        notebook_id: str,
        candidate_type: Optional[str] = None,
    ) -> List[Candidate]:
        self.get_notebook(notebook_id)
        query = (
            "SELECT c.*, s.title AS source_title "
            "FROM extraction_candidates c "
            "LEFT JOIN sources s ON s.id = c.source_id "
            "WHERE c.notebook_id = ? AND c.status != 'rejected' AND c.status != 'approved'"
        )
        params: List[str] = [notebook_id]
        if candidate_type:
            query += " AND c.candidate_type = ?"
            params.append(candidate_type)
        query += " ORDER BY c.created_at ASC, c.id ASC"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def _candidate_from_row(self, row: sqlite3.Row) -> Candidate:
        evidence = [Evidence(**item) for item in json.loads(row["evidence"] or "[]")]
        return Candidate(
            id=row["id"],
            notebook_id=row["notebook_id"],
            source_id=row["source_id"] or "",
            source_title=row["source_title"] or "",
            candidate_type=row["candidate_type"],
            status=row["status"],
            payload=json.loads(row["payload"] or "{}"),
            evidence=evidence,
            created_label=_created_label(row["created_at"]),
        )

    def _candidate_row_by_id(self, db: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = db.execute(
            """
            SELECT c.*, s.title AS source_title
            FROM extraction_candidates c
            LEFT JOIN sources s ON s.id = c.source_id
            WHERE c.id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return row

    def update_candidate(self, candidate_id: str, payload: CandidateUpdate) -> Candidate:
        with self._connect() as db:
            row = self._candidate_row_by_id(db, candidate_id)
            new_payload = (
                json.dumps(payload.payload, ensure_ascii=False)
                if payload.payload is not None
                else row["payload"]
            )
            new_status = payload.status if payload.status is not None else row["status"]
            db.execute(
                "UPDATE extraction_candidates SET payload = ?, status = ?, updated_at = ? WHERE id = ?",
                (new_payload, new_status, _now(), candidate_id),
            )
            row = self._candidate_row_by_id(db, candidate_id)
            return self._candidate_from_row(row)

    def approve_candidate(self, candidate_id: str) -> Candidate:
        now = _now()
        with self._connect() as db:
            row = self._candidate_row_by_id(db, candidate_id)
            notebook_id = row["notebook_id"]
            payload_json = row["payload"]
            existing = db.execute(
                "SELECT id FROM knowledge_objects WHERE source_candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if existing is None:
                object_id = f"ko-{uuid4().hex[:10]}"
                db.execute(
                    """
                    INSERT INTO knowledge_objects
                    (id, notebook_id, object_type, status, owner, payload, evidence,
                     source_candidate_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        object_id,
                        notebook_id,
                        row["candidate_type"],
                        "approved",
                        "",
                        payload_json,
                        row["evidence"],
                        candidate_id,
                        now,
                        now,
                    ),
                )
            else:
                object_id = existing["id"]
                db.execute(
                    "UPDATE knowledge_objects SET payload = ?, evidence = ?, status = ?, updated_at = ? WHERE source_candidate_id = ?",
                    (payload_json, row["evidence"], "approved", now, candidate_id),
                )
            db.execute(
                "UPDATE extraction_candidates SET status = ?, updated_at = ? WHERE id = ?",
                ("approved", now, candidate_id),
            )
            result_row = self._candidate_row_by_id(db, candidate_id)
            candidate = self._candidate_from_row(result_row)
        # WS4: build the payload-level vector for the newly approved object.
        try:
            self._embed_knowledge(object_id, notebook_id, json.loads(payload_json or "{}"))
        except Exception:
            pass
        return candidate

    def reject_candidate(self, candidate_id: str) -> Candidate:
        now = _now()
        with self._connect() as db:
            self._candidate_row_by_id(db, candidate_id)
            db.execute(
                "UPDATE extraction_candidates SET status = ?, updated_at = ? WHERE id = ?",
                ("rejected", now, candidate_id),
            )
            db.execute(
                "DELETE FROM knowledge_objects WHERE source_candidate_id = ?",
                (candidate_id,),
            )
            row = self._candidate_row_by_id(db, candidate_id)
            return self._candidate_from_row(row)

    def list_rules(self, notebook_id: str) -> List[RuleCard]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            objects = self._knowledge_objects(db, notebook_id, "rule", statuses=None)
        return [self._rule_card(self._as_retrieved(obj, "rule")) for obj in objects]

    def list_methods(self, notebook_id: str) -> List[MethodCard]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            objects = self._knowledge_objects(db, notebook_id, "method", statuses=None)
        return [self._method_card(self._as_retrieved(obj, "method")) for obj in objects]

    def list_risks(self, notebook_id: str) -> List[RiskItemCard]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            objects = self._knowledge_objects(db, notebook_id, "risk", statuses=None)
        return [self._risk_card(self._as_retrieved(obj, "risk")) for obj in objects]

    def list_glossary(self, notebook_id: str) -> List[GlossaryTermCard]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            objects = self._knowledge_objects(db, notebook_id, "glossary", statuses=None)
        return [self._glossary_card(self._as_retrieved(obj, "glossary")) for obj in objects]

    def explain_rule(self, notebook_id: str, rule_id: str) -> RuleExplanation:
        """Trace a rule back to its origin evidence and surface related
        cases / risks / checklist items (the "why is this rule here?" view, §6.10)."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM knowledge_objects "
                "WHERE id = ? AND notebook_id = ? AND object_type = 'rule'",
                (rule_id, notebook_id),
            ).fetchone()
            if row is None:
                raise KeyError(rule_id)
            rule_obj = {
                "id": row["id"],
                "payload": json.loads(row["payload"] or "{}"),
                "evidence": [Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
                "status": row["status"],
                "owner": row["owner"],
                "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
            }
            cases = self._knowledge_objects(db, notebook_id, "case")
            risks = self._knowledge_objects(db, notebook_id, "risk")
            checklist = self._knowledge_objects(db, notebook_id, "checklist")
            valid_ids = {element["element_id"] for element in self._gather_elements(db, notebook_id)}

        rule_card = self._rule_card(self._as_retrieved(rule_obj, "rule"))
        payload = rule_obj["payload"]
        query = " ".join(
            [rule_card.title, rule_card.statement, " ".join(rule_card.applies_to)]
        ).strip()
        related_cases = [self._case_card(item) for item in score_knowledge(query, cases, "case")[:3]]
        related_risks = [self._risk_card(item) for item in score_knowledge(query, risks, "risk")[:3]]
        related_checklist = [
            str(item.payload.get("question", "")).strip()
            for item in score_knowledge(query, checklist, "checklist")[:5]
            if str(item.payload.get("question", "")).strip()
        ]
        origin = [
            _citation("Rule origin", evidence)
            for evidence in rule_obj["evidence"]
            if not evidence.element_id or evidence.element_id in valid_ids
        ]
        return RuleExplanation(
            rule=rule_card,
            origin=origin,
            applicable_scenario=rule_card.applies_to,
            exception=str(payload.get("exception", "")),
            related_cases=related_cases,
            related_risks=related_risks,
            related_checklist=related_checklist,
        )

    def _derived_from_row(self, row: sqlite3.Row) -> DerivedRuleCandidate:
        return DerivedRuleCandidate(
            id=row["id"],
            notebook_id=row["notebook_id"],
            article_id=row["article_id"] or "",
            title=row["title"],
            proposed_rule=row["proposed_rule"],
            rationale=row["rationale"],
            status=row["status"],
            evidence=[Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
            created_label=_created_label(row["created_at"]),
        )

    def list_derived_rules(
        self, notebook_id: str, status: str | None = None
    ) -> List[DerivedRuleCandidate]:
        self.get_notebook(notebook_id)
        query = "SELECT * FROM derived_rule_candidates WHERE notebook_id = ?"
        params: List[object] = [notebook_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, id ASC"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._derived_from_row(row) for row in rows]

    def approve_derived_rule(self, candidate_id: str) -> RuleCard:
        """Promote a derived rule candidate into the formal rule library (§7.5)."""
        now = _now()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            payload = {
                "title": row["title"] or _first_sentence(row["proposed_rule"], 90),
                "statement": row["proposed_rule"],
                "applies_to": [],
                "recommendation": "",
                "risk_if_ignored": row["rationale"] or "",
                "severity": "medium",
            }
            ko_id = f"ko-{uuid4().hex[:10]}"
            db.execute(
                """
                INSERT INTO knowledge_objects
                (id, notebook_id, object_type, status, owner, payload, evidence,
                 source_candidate_id, created_at, updated_at)
                VALUES (?, ?, 'rule', 'approved', '', ?, ?, ?, ?, ?)
                """,
                (
                    ko_id,
                    row["notebook_id"],
                    json.dumps(payload, ensure_ascii=False),
                    row["evidence"] or "[]",
                    candidate_id,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE derived_rule_candidates SET status = 'approved' WHERE id = ?",
                (candidate_id,),
            )
            ko_row = db.execute("SELECT * FROM knowledge_objects WHERE id = ?", (ko_id,)).fetchone()
        obj = {
            "id": ko_row["id"],
            "payload": json.loads(ko_row["payload"] or "{}"),
            "evidence": [Evidence(**e) for e in json.loads(ko_row["evidence"] or "[]")],
            "status": ko_row["status"],
            "owner": ko_row["owner"],
            "last_reviewed": ko_row["last_reviewed"] if "last_reviewed" in ko_row.keys() else "",
        }
        return self._rule_card(self._as_retrieved(obj, "rule"))

    def reject_derived_rule(self, candidate_id: str) -> DerivedRuleCandidate:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            db.execute(
                "UPDATE derived_rule_candidates SET status = 'rejected' WHERE id = ?",
                (candidate_id,),
            )
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._derived_from_row(row)

    def update_knowledge(
        self, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard | MethodCard | RiskItemCard | GlossaryTermCard:
        now = _now()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ?", (knowledge_id,)
            ).fetchone()
            if row is None:
                raise KeyError(knowledge_id)
            if payload.status is not None and payload.status not in KNOWLEDGE_STATUSES:
                raise ValueError(f"invalid status: {payload.status}")
            new_payload = (
                json.dumps(payload.payload, ensure_ascii=False)
                if payload.payload is not None
                else row["payload"]
            )
            new_status = payload.status if payload.status is not None else row["status"]
            new_owner = payload.owner if payload.owner is not None else row["owner"]
            # Stamp last_reviewed whenever a curator changes status.
            last_reviewed = now if payload.status is not None else (
                row["last_reviewed"] if "last_reviewed" in row.keys() else ""
            )
            db.execute(
                "UPDATE knowledge_objects SET payload = ?, status = ?, owner = ?, "
                "last_reviewed = ?, updated_at = ? WHERE id = ?",
                (new_payload, new_status, new_owner, last_reviewed, now, knowledge_id),
            )
            row = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ?", (knowledge_id,)
            ).fetchone()
        # WS4: re-embed payload-level vector when the payload was edited.
        if payload.payload is not None:
            try:
                self._embed_knowledge(
                    knowledge_id, row["notebook_id"], json.loads(new_payload or "{}")
                )
            except Exception:
                pass
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        mapper = {
            "rule": self._rule_card,
            "method": self._method_card,
            "risk": self._risk_card,
            "glossary": self._glossary_card,
        }.get(row["object_type"], self._rule_card)
        return mapper(item)

    @staticmethod
    def _knowledge_headline(object_type: str, payload: dict) -> str:
        keys = {
            "rule": ("title", "statement"),
            "method": ("name", "use_when"),
            "risk": ("title", "description"),
            "glossary": ("term", "definition"),
            "case": ("symptom", "context"),
            "checklist": ("question",),
        }.get(object_type, ("title",))
        for key in keys:
            value = str(payload.get(key, "")).strip()
            if value:
                return value[:120]
        return object_type

    def _knowledge_ref(self, obj: dict, object_type: str) -> KnowledgeRef:
        return KnowledgeRef(
            id=obj["id"],
            object_type=object_type,
            headline=self._knowledge_headline(object_type, obj["payload"]),
            status=obj.get("status", "approved"),
        )

    @staticmethod
    def _payload_join(payload: dict) -> str:
        parts: List[str] = []
        for key, value in payload.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
        return " ".join(parts)

    def _knowledge_similarity(self, a: dict, b: dict, element_vectors: dict) -> float:
        text_a = self._payload_join(a["payload"])
        text_b = self._payload_join(b["payload"])
        keyword = max(keyword_score(text_a, text_b), keyword_score(text_b, text_a))
        semantic = 0.0
        vecs_a = [element_vectors[e.element_id] for e in a["evidence"] if e.element_id in element_vectors]
        vecs_b = [element_vectors[e.element_id] for e in b["evidence"] if e.element_id in element_vectors]
        for va in vecs_a:
            for vb in vecs_b:
                semantic = max(semantic, cosine(va, vb))
        return max(keyword, semantic * 0.95)

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            objs = self._knowledge_objects(db, notebook_id, object_type, statuses=None)
            elements = self._gather_elements(db, notebook_id)
        objs = [o for o in objs if o.get("status") != "deprecated"]
        element_vectors = self._element_vectors(elements)
        groups: List[DuplicateGroup] = []
        used: set = set()
        for i, base in enumerate(objs):
            if base["id"] in used:
                continue
            members = [base]
            best = 0.0
            for other in objs[i + 1:]:
                if other["id"] in used:
                    continue
                sim = self._knowledge_similarity(base, other, element_vectors)
                if sim >= 0.6:
                    members.append(other)
                    used.add(other["id"])
                    best = max(best, sim)
            if len(members) > 1:
                used.add(base["id"])
                groups.append(
                    DuplicateGroup(
                        object_type=object_type,
                        similarity=round(best, 3),
                        members=[self._knowledge_ref(m, object_type) for m in members],
                    )
                )
        return groups

    def merge_knowledge(self, source_id: str, payload: MergeRequest) -> RuleCard | MethodCard | RiskItemCard | GlossaryTermCard:
        into_id = payload.into_id
        if into_id == source_id:
            raise ValueError("cannot merge a knowledge object into itself")
        now = _now()
        with self._connect() as db:
            src = db.execute("SELECT * FROM knowledge_objects WHERE id = ?", (source_id,)).fetchone()
            tgt = db.execute("SELECT * FROM knowledge_objects WHERE id = ?", (into_id,)).fetchone()
            if src is None or tgt is None:
                raise KeyError(source_id if src is None else into_id)
            if src["object_type"] != tgt["object_type"]:
                raise ValueError("can only merge knowledge objects of the same type")
            merged: List[dict] = json.loads(tgt["evidence"] or "[]")
            seen = {(e.get("element_id"), e.get("quoted_span")) for e in merged}
            for item in json.loads(src["evidence"] or "[]"):
                key = (item.get("element_id"), item.get("quoted_span"))
                if key not in seen:
                    merged.append(item)
                    seen.add(key)
            db.execute(
                "UPDATE knowledge_objects SET evidence = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), now, into_id),
            )
            db.execute(
                "UPDATE knowledge_objects SET status = 'deprecated', last_reviewed = ?, updated_at = ? WHERE id = ?",
                (now, now, source_id),
            )
            row = db.execute("SELECT * FROM knowledge_objects WHERE id = ?", (into_id,)).fetchone()
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        mapper = {
            "rule": self._rule_card,
            "method": self._method_card,
            "risk": self._risk_card,
            "glossary": self._glossary_card,
        }.get(row["object_type"], self._rule_card)
        return mapper(item)

    def find_conflicts(self, notebook_id: str) -> List[ConflictPair]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rules = self._knowledge_objects(db, notebook_id, "rule")
        conflicts: List[ConflictPair] = []
        for i, a in enumerate(rules):
            pa = a["payload"]
            scope_a = f"{pa.get('title', '')} {' '.join(_as_str_list(pa.get('applies_to')))}"
            rec_a = str(pa.get("recommendation", "")).strip() or str(pa.get("statement", "")).strip()
            for b in rules[i + 1:]:
                pb = b["payload"]
                scope_b = f"{pb.get('title', '')} {' '.join(_as_str_list(pb.get('applies_to')))}"
                rec_b = str(pb.get("recommendation", "")).strip() or str(pb.get("statement", "")).strip()
                if not (rec_a and rec_b):
                    continue
                scope_sim = max(keyword_score(scope_a, scope_b), keyword_score(scope_b, scope_a))
                rec_sim = max(keyword_score(rec_a, rec_b), keyword_score(rec_b, rec_a))
                if scope_sim >= 0.5 and rec_sim < 0.2:
                    conflicts.append(
                        ConflictPair(
                            object_type="rule",
                            reason="Overlapping scope but divergent recommendation — needs owner review.",
                            a=self._knowledge_ref(a, "rule"),
                            b=self._knowledge_ref(b, "rule"),
                        )
                    )
        return conflicts

    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse:
        self.get_notebook(notebook_id)
        needle = query.strip().lower()
        hits: List[SearchHit] = []
        if not needle:
            return NotebookSearchResponse(query=query, hits=[])
        with self._connect() as db:
            notebook = db.execute(
                "SELECT * FROM notebooks WHERE id = ?",
                (notebook_id,),
            ).fetchone()
            source_rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            element_rows = db.execute(
                """
                SELECT source_elements.*, sources.title AS source_title
                FROM source_elements
                JOIN sources ON sources.id = source_elements.source_id
                WHERE sources.notebook_id = ?
                """,
                (notebook_id,),
            ).fetchall()
            article_rows = db.execute(
                "SELECT * FROM articles WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()

        candidates = [
            ("Notebook", notebook_id, notebook["name"], notebook["purpose"], "", ""),
            ("Domain", notebook_id, notebook["primary_domain"], notebook["primary_domain"], "", ""),
        ]
        for source in source_rows:
            candidates.extend(
                [
                    ("Source", notebook_id, source["title"], source["summary"], source["id"], ""),
                    ("Source", notebook_id, source["file_name"], source["file_name"], source["id"], ""),
                ]
            )
        for element in element_rows:
            candidates.append(
                (
                    "Element",
                    notebook_id,
                    f"{element['source_title']} · {element['location_label']}",
                    element["text"],
                    element["source_id"],
                    element["id"],
                )
            )
        for article in article_rows:
            candidates.append(
                ("Article", notebook_id, article["title"], article["summary"], "", "")
            )

        for scope, nb_id, label, text, source_id, element_id in candidates:
            haystack = f"{label} {text}".lower()
            if needle not in haystack:
                continue
            hits.append(
                SearchHit(
                    scope=scope,
                    notebook_id=nb_id,
                    label=label,
                    text=_snippet(text or label, needle),
                    source_id=source_id,
                    element_id=element_id,
                )
            )
        return NotebookSearchResponse(query=query, hits=hits[:20])

    def _embed_query(self, query: str) -> Optional[List[float]]:
        if not self.settings.embedding_configured:
            return None
        try:
            return self.llm_client.embed(query[:2000])
        except Exception:
            return None

    def _gather_elements(self, db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            """
            SELECT e.id, e.source_id, e.element_type, e.location_label, e.text,
                   s.title AS source_title, em.vector AS vector
            FROM source_elements e
            JOIN sources s ON s.id = e.source_id
            LEFT JOIN element_embeddings em ON em.element_id = e.id
            WHERE s.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()
        elements: List[dict] = []
        for row in rows:
            elements.append(
                {
                    "element_id": row["id"],
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "location_label": row["location_label"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "vector": json.loads(row["vector"]) if row["vector"] else None,
                }
            )
        return elements

    @staticmethod
    def _element_vectors(elements: List[dict]) -> dict:
        """Map element_id -> embedding vector for elements that have one."""
        return {
            element["element_id"]: element["vector"]
            for element in elements
            if element.get("vector")
        }

    def _rule_card(self, item: RetrievedKnowledge) -> RuleCard:
        payload = item.payload
        applies_to = payload.get("applies_to")
        if isinstance(applies_to, list):
            applies_list = [str(value) for value in applies_to if str(value).strip()]
        elif applies_to:
            applies_list = [str(applies_to)]
        else:
            applies_list = []
        return RuleCard(
            id=item.object_id,
            title=str(payload.get("title", "")),
            statement=str(payload.get("statement", "")),
            applies_to=applies_list,
            recommendation=str(payload.get("recommendation", "")),
            risk_if_ignored=str(payload.get("risk_if_ignored", "")),
            severity=str(payload.get("severity", "medium")),
            status=item.status or "approved",
            owner=item.owner,
            last_reviewed=item.last_reviewed,
            evidence=item.evidence,
        )

    def _method_card(self, item: RetrievedKnowledge) -> MethodCard:
        payload = item.payload
        return MethodCard(
            id=item.object_id,
            name=str(payload.get("name", "")),
            use_when=str(payload.get("use_when", "")),
            benefit=str(payload.get("benefit", "")),
            limitation=str(payload.get("limitation", "")),
            status=item.status or "approved",
            evidence=item.evidence,
        )

    def _risk_card(self, item: RetrievedKnowledge) -> RiskItemCard:
        payload = item.payload
        return RiskItemCard(
            id=item.object_id,
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            severity=str(payload.get("severity", "medium")),
            status=item.status or "approved",
            evidence=item.evidence,
        )

    def _glossary_card(self, item: RetrievedKnowledge) -> GlossaryTermCard:
        payload = item.payload
        return GlossaryTermCard(
            id=item.object_id,
            term=str(payload.get("term", "")),
            definition=str(payload.get("definition", "")),
            status=item.status or "approved",
            evidence=item.evidence,
        )

    @staticmethod
    def _as_retrieved(obj: dict, object_type: str) -> RetrievedKnowledge:
        return RetrievedKnowledge(
            object_id=obj["id"],
            object_type=object_type,
            payload=obj["payload"],
            evidence=obj["evidence"],
            status=obj.get("status", "approved"),
            owner=obj.get("owner", ""),
            last_reviewed=obj.get("last_reviewed", ""),
        )

    def _case_card(self, item: RetrievedKnowledge) -> CaseCard:
        payload = item.payload
        return CaseCard(
            id=item.object_id,
            symptom=str(payload.get("symptom", "")),
            context=str(payload.get("context", "")),
            root_cause=str(payload.get("root_cause", "")),
            resolution=str(payload.get("resolution", "")),
            lesson_learned=str(payload.get("lesson_learned", "")),
            evidence=item.evidence,
        )

    def _citations_from(
        self,
        items: List[RetrievedKnowledge],
        valid_element_ids: set,
        label: str,
    ) -> List[Citation]:
        citations: List[Citation] = []
        for item in items:
            for evidence in item.evidence:
                if evidence.element_id and evidence.element_id not in valid_element_ids:
                    continue
                citations.append(_citation(label, evidence))
        return citations

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        scenario_tags = [
            value
            for value in payload.scenario.values()
            if isinstance(value, str) and value.strip()
        ]
        query = " ".join([question, *scenario_tags]).strip()

        with self._connect() as db:
            rules = self._knowledge_objects(db, notebook_id, "rule")
            cases = self._knowledge_objects(db, notebook_id, "case")
            checklist = self._knowledge_objects(db, notebook_id, "checklist")
            methods = self._knowledge_objects(db, notebook_id, "method")
            risks = self._knowledge_objects(db, notebook_id, "risk")
            elements = self._gather_elements(db, notebook_id)
            query_vector = self._embed_query(query)
            knowledge_vectors = self._knowledge_vectors(
                db, notebook_id, rules + cases + checklist + methods + risks
            )

        element_vectors = self._element_vectors(elements)
        scenario = payload.scenario or {}
        scored_rules = score_knowledge(query, rules, "rule", query_vector, element_vectors, knowledge_vectors, scenario)[:5]
        scored_cases = score_knowledge(query, cases, "case", query_vector, element_vectors, knowledge_vectors, scenario)[:4]
        scored_checklist = score_knowledge(query, checklist, "checklist", query_vector, element_vectors, knowledge_vectors, scenario)[:6]
        scored_methods = score_knowledge(query, methods, "method", query_vector, element_vectors, knowledge_vectors, scenario)[:4]
        scored_risks = score_knowledge(query, risks, "risk", query_vector, element_vectors, knowledge_vectors, scenario)[:4]
        scored_elements = score_elements(query, elements, query_vector)

        valid_element_ids = {element["element_id"] for element in elements}
        related_rules = [self._rule_card(item) for item in scored_rules]
        related_cases = [self._case_card(item) for item in scored_cases]

        recommended_methods = [
            str(item.payload.get("name", "")).strip()
            or str(item.payload.get("use_when", "")).strip()
            for item in scored_methods
        ]
        recommended_methods = [text for text in recommended_methods if text]
        potential_risks = [
            str(item.payload.get("title", "")).strip()
            or str(item.payload.get("description", "")).strip()
            for item in scored_risks
        ]
        potential_risks = [text for text in potential_risks if text]
        checklist_questions = [
            str(item.payload.get("question", "")).strip() for item in scored_checklist
        ]
        checklist_questions = [text for text in checklist_questions if text]

        citations: List[Citation] = []
        citations.extend(self._citations_from(scored_rules, valid_element_ids, "Rule evidence"))
        citations.extend(self._citations_from(scored_cases, valid_element_ids, "Case evidence"))
        citations.extend(self._citations_from(scored_checklist, valid_element_ids, "Checklist evidence"))
        citations.extend(self._citations_from(scored_methods, valid_element_ids, "Method evidence"))
        citations.extend(self._citations_from(scored_risks, valid_element_ids, "Risk evidence"))

        has_knowledge = bool(
            scored_rules or scored_cases or scored_checklist or scored_methods or scored_risks
        )

        llm_mode = "deterministic"
        conclusion = ""
        applicable_scenario = scenario_tags
        missing_information: List[str] = []

        if self.llm_client.configured and (has_knowledge or scored_elements):
            try:
                conclusion, applicable_scenario, llm_methods, llm_risks, llm_checklist, missing_information = (
                    self._answer_with_llm(question, scenario_tags, scored_rules, scored_cases,
                                          scored_checklist, scored_methods, scored_risks, scored_elements)
                )
                recommended_methods = llm_methods or recommended_methods
                potential_risks = llm_risks or potential_risks
                checklist_questions = llm_checklist or checklist_questions
                llm_mode = "configured"
            except Exception:
                conclusion = ""

        if not conclusion:
            if has_knowledge:
                parts = []
                if related_rules:
                    parts.append(f"{len(related_rules)} approved rule(s) match this scenario")
                if related_cases:
                    parts.append(f"{len(related_cases)} related case(s)")
                if checklist_questions:
                    parts.append(f"{len(checklist_questions)} checklist item(s)")
                conclusion = (
                    "Notebook knowledge found: " + ", ".join(parts) + "."
                    if parts
                    else "Relevant notebook knowledge was retrieved for this question."
                )
            else:
                conclusion = (
                    "The notebook does not yet contain approved knowledge that "
                    "matches this question. Upload and review sources to build coverage."
                )
                missing_information = missing_information or [
                    "No approved rules, cases, or checklist items matched the query.",
                    "Upload sources, run extraction, and approve candidates to improve answers.",
                ]

        # §12 governance: flag any retrieved rule marked as conflicting.
        conflicted = [item for item in scored_rules if item.status == "conflict"]
        if conflicted:
            titles = ", ".join(
                str(item.payload.get("title", "")).strip() or item.object_id for item in conflicted
            )
            missing_information = missing_information + [
                f"Conflicting rule(s) flagged for owner review: {titles}."
            ]

        response = AskResponse(
            answer_id="",
            conclusion=conclusion,
            applicable_scenario=applicable_scenario,
            recommended_methods=recommended_methods,
            related_rules=related_rules,
            potential_risks=potential_risks,
            related_cases=related_cases,
            checklist=checklist_questions,
            missing_information=missing_information,
            citations=citations,
            llm_mode=llm_mode,
        )
        response.answer_id = self._save_answer(notebook_id, question, response)
        return response

    def _answer_with_llm(
        self,
        question: str,
        scenario_tags: List[str],
        rules: List[RetrievedKnowledge],
        cases: List[RetrievedKnowledge],
        checklist: List[RetrievedKnowledge],
        methods: List[RetrievedKnowledge],
        risks: List[RetrievedKnowledge],
        elements: List[RetrievedElement],
    ):
        def block(title: str, items: List[RetrievedKnowledge]) -> str:
            lines = []
            for item in items:
                text = "; ".join(
                    f"{key}: {value}"
                    for key, value in item.payload.items()
                    if not str(key).startswith("_") and str(value).strip()
                )
                lines.append(f"- {text}")
            return f"{title}:\n" + ("\n".join(lines) if lines else "- (none)")

        element_block = "\n".join(
            f"- [{element.location_label}] {element.text[:300]}" for element in elements[:8]
        )
        context_block = "\n\n".join(
            [
                block("Approved rules", rules),
                block("Cases", cases),
                block("Checklist items", checklist),
                block("Methods", methods),
                block("Risks", risks),
                "Source elements:\n" + (element_block or "- (none)"),
            ]
        )
        scenario_block = ", ".join(scenario_tags) if scenario_tags else "(not specified)"
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, scenario_block, context_block)}],
            ANSWER_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")

        def str_list(key: str) -> List[str]:
            value = data.get(key) or []
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        return (
            str(data.get("conclusion", "")).strip(),
            str_list("applicable_scenario") or scenario_tags,
            str_list("recommended_methods"),
            str_list("potential_risks"),
            str_list("checklist"),
            str_list("missing_information"),
        )

    def _save_answer(self, notebook_id: str, question: str, response: AskResponse) -> str:
        answer_id = f"ans-{uuid4().hex[:10]}"
        now = _now()
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self._connect() as db:
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    answer_id,
                    notebook_id,
                    question,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
        return answer_id

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse:
        if payload.rating not in {"useful", "not_useful"}:
            raise ValueError("rating must be useful or not_useful")
        now = _now()
        feedback_id = f"fb-{uuid4().hex[:10]}"
        with self._connect() as db:
            answer = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?",
                (answer_id,),
            ).fetchone()
            if answer is None:
                raise KeyError(answer_id)
            db.execute(
                "INSERT INTO feedback (id, answer_id, notebook_id, rating, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (feedback_id, answer_id, answer["notebook_id"], payload.rating, payload.comment, now),
            )
        return FeedbackResponse(
            id=feedback_id,
            answer_id=answer_id,
            rating=payload.rating,
            comment=payload.comment,
        )

    def scenario_query(self, notebook_id: str, payload: ScenarioQueryRequest) -> AskResponse:
        scenario = {key: value for key, value in payload.model_dump().items() if value}
        concern = payload.concern or "design review"
        question = (
            f"For {payload.domain or 'this domain'} {payload.block_type} "
            f"at {payload.design_stage or 'this stage'}, what should I check "
            f"regarding {concern}?"
        ).strip()
        return self.ask(notebook_id, AskRequest(question=question, scenario=scenario))

    def case_search(self, notebook_id: str, payload: CaseSearchRequest) -> List[CaseCard]:
        self.get_notebook(notebook_id)
        query_parts = [payload.query] + [
            value for value in payload.context.values() if value
        ]
        query = " ".join(query_parts).strip()
        with self._connect() as db:
            cases = self._knowledge_objects(db, notebook_id, "case")
            elements = self._gather_elements(db, notebook_id)
            query_vector = self._embed_query(query)
            knowledge_vectors = self._knowledge_vectors(db, notebook_id, cases)
        element_vectors = self._element_vectors(elements)
        scenario = payload.context or {}
        scored = (
            score_knowledge(query, cases, "case", query_vector, element_vectors, knowledge_vectors, scenario)
            if query
            else []
        )
        if not scored:
            scored = [
                RetrievedKnowledge(
                    object_id=case["id"],
                    object_type="case",
                    payload=case["payload"],
                    evidence=case["evidence"],
                )
                for case in cases
            ]
        return [self._case_card(item) for item in scored[:10]]

    def checklist(self, notebook_id: str, payload: ChecklistRequest) -> List[ChecklistItem]:
        self.get_notebook(notebook_id)
        query = payload.scenario.strip()
        with self._connect() as db:
            checklist_objs = self._knowledge_objects(db, notebook_id, "checklist")
            rules = self._knowledge_objects(db, notebook_id, "rule")
            elements = self._gather_elements(db, notebook_id)
            query_vector = self._embed_query(query)
            knowledge_vectors = self._knowledge_vectors(db, notebook_id, checklist_objs + rules)
        valid_element_ids = {element["element_id"] for element in elements}
        element_vectors = self._element_vectors(elements)

        scored = (
            score_knowledge(query, checklist_objs, "checklist", query_vector, element_vectors, knowledge_vectors)
            if query
            else []
        )
        if not scored:
            scored = [
                RetrievedKnowledge(
                    object_id=obj["id"],
                    object_type="checklist",
                    payload=obj["payload"],
                    evidence=obj["evidence"],
                )
                for obj in checklist_objs
            ]

        items: List[ChecklistItem] = []
        for item in scored[:12]:
            payload_obj = item.payload
            question = str(payload_obj.get("question", "")).strip()
            if not question:
                continue
            citations = [
                _citation("Checklist evidence", evidence)
                for evidence in item.evidence
                if not evidence.element_id or evidence.element_id in valid_element_ids
            ]
            items.append(
                ChecklistItem(
                    question=question,
                    severity=str(payload_obj.get("severity", "medium")),
                    required_evidence=str(payload_obj.get("required_evidence", "")),
                    related_rule_ids=[],
                    citations=citations,
                )
            )

        if not items:
            scored_rules = (
                score_knowledge(query, rules, "rule", query_vector, element_vectors, knowledge_vectors)
                if query
                else []
            )
            for item in scored_rules[:6]:
                statement = str(item.payload.get("statement", "")).strip()
                title = str(item.payload.get("title", "")).strip()
                if not (statement or title):
                    continue
                citations = [
                    _citation("Rule evidence", evidence)
                    for evidence in item.evidence
                    if not evidence.element_id or evidence.element_id in valid_element_ids
                ]
                items.append(
                    ChecklistItem(
                        question=f"Have you verified: {title or statement}?",
                        severity=str(item.payload.get("severity", "medium")),
                        required_evidence=str(item.payload.get("risk_if_ignored", "")),
                        related_rule_ids=[item.object_id],
                        citations=citations,
                    )
                )
        return items

    def list_articles(self, notebook_id: str) -> List[ArticleSummary]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM articles WHERE notebook_id = ? ORDER BY created_at ASC",
                (notebook_id,),
            ).fetchall()
        return [
            ArticleSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                source_id=row["source_id"] or "",
                title=row["title"],
                status=row["status"],
                summary=row["summary"],
            )
            for row in rows
        ]

    def create_article(self, notebook_id: str, payload: ArticleCreate) -> ArticleSummary:
        self.get_notebook(notebook_id)
        article_id = f"art-{uuid4().hex[:10]}"
        now = _now()
        source_id = payload.source_id.strip()
        if source_id:
            with self._connect() as db:
                source_row = db.execute(
                    "SELECT id FROM sources WHERE id = ? AND notebook_id = ?",
                    (source_id, notebook_id),
                ).fetchone()
            if source_row is None:
                raise ValueError("Article source must belong to the current notebook")
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO articles
                (id, notebook_id, source_id, title, status, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    notebook_id,
                    source_id or None,
                    payload.title,
                    "uploaded",
                    payload.abstract or "Article uploaded; research brief not generated yet.",
                    now,
                    now,
                ),
            )
        return self.list_articles(notebook_id)[-1]

    def delete_article(self, article_id: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT id FROM articles WHERE id = ?", (article_id,)).fetchone()
            if row is None:
                raise KeyError(article_id)
            db.execute("DELETE FROM articles WHERE id = ?", (article_id,))

    def research_article(self, article_id: str) -> ArticleResearchBrief:
        with self._connect() as db:
            row = db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            raise KeyError(article_id)
        article = ArticleSummary(
            id=row["id"],
            notebook_id=row["notebook_id"],
            source_id=row["source_id"] or "",
            title=row["title"],
            status=row["status"],
            summary=row["summary"],
        )
        notebook_id = row["notebook_id"]
        article_elements, article_source_title = self._article_elements(row)
        element_text = "\n".join(
            f"[{element.location_label}] {element.text}" for element in article_elements
        )
        article_text = f"{row['title']}\n{row['summary']}\n{element_text}".strip()
        with self._connect() as db:
            rules = self._knowledge_objects(db, notebook_id, "rule")

        brief_data = None
        if self.llm_client.configured and article_text:
            try:
                brief_data = self._research_article_with_llm(
                    article.title,
                    article_text,
                    rules,
                    article_elements,
                    article_source_title,
                )
            except Exception:
                brief_data = None
        if brief_data is None:
            brief_data = self._research_article_fallback(
                article.title,
                row["summary"],
                rules,
                article_elements,
                article_source_title,
            )

        self._persist_article_research(article_id, notebook_id, brief_data)
        article.status = "brief-ready"
        return ArticleResearchBrief(
            article=article,
            core_contribution=brief_data["core_contribution"],
            claims=[claim["statement"] for claim in brief_data["claims"]],
            limitations=brief_data["limitations"],
            notebook_relationships=brief_data["notebook_relationships"],
            derived_rule_candidates=[
                item["proposed_rule"] for item in brief_data["derived_rule_candidates"]
            ],
            validation_plan=brief_data["validation_plan"],
            citations=self._article_citations(brief_data),
        )

    def _research_article_with_llm(
        self,
        title: str,
        article_text: str,
        rules: List[dict],
        article_elements: List[SourceElement],
        article_source_title: str,
    ) -> dict:
        rules_block = "\n".join(
            f"- {str(rule['payload'].get('title', '')).strip()}: "
            f"{str(rule['payload'].get('statement', '')).strip()}"
            for rule in rules[:20]
        ) or "- (notebook has no approved rules yet)"
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": article_prompt(title, article_text[:8000], rules_block)}],
            ARTICLE_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("article analysis did not return a JSON object")

        claims: List[dict] = []
        for claim in data.get("claims") or []:
            if isinstance(claim, dict):
                statement = str(claim.get("statement", "")).strip()
                claim_type = str(claim.get("claim_type", "")).strip()
                quoted_span = str(claim.get("quoted_span", "")).strip()
            else:
                statement = str(claim).strip()
                claim_type = ""
                quoted_span = statement
            if statement:
                claims.append(
                    {
                        "statement": statement,
                        "claim_type": claim_type,
                        "evidence": self._article_evidence(
                            quoted_span or statement,
                            article_elements,
                            article_source_title,
                        ),
                    }
                )
        derived: List[dict] = []
        for item in data.get("derived_rule_candidates") or []:
            if isinstance(item, dict):
                text = str(item.get("proposed_rule", "")).strip()
                rationale = str(item.get("rationale", "")).strip()
                quoted_span = str(item.get("quoted_span", "")).strip()
            else:
                text = str(item).strip()
                rationale = ""
                quoted_span = text
            if text:
                derived.append(
                    {
                        "title": _first_sentence(text, 90),
                        "proposed_rule": text,
                        "rationale": rationale,
                        "evidence": self._article_evidence(
                            quoted_span or text,
                            article_elements,
                            article_source_title,
                        ),
                    }
                )

        def str_list(key: str) -> List[str]:
            value = data.get(key) or []
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        claims = self._attach_claim_relationships(claims, rules)
        return {
            "core_contribution": str(data.get("core_contribution", "")).strip()
            or "Article analyzed; see claims below.",
            "claims": claims,
            "limitations": str_list("limitations"),
            "validation_plan": str_list("validation_plan"),
            "derived_rule_candidates": derived,
            "notebook_relationships": self._article_rule_relationships(claims),
        }

    def _research_article_fallback(
        self,
        title: str,
        summary: str,
        rules: List[dict],
        article_elements: List[SourceElement],
        article_source_title: str,
    ) -> dict:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", summary or "") if s.strip()]
        claim_texts = sentences[:4] if sentences else ([title] if title else [])
        claims = [
            {
                "statement": statement,
                "claim_type": _claim_type(statement),
                "evidence": self._article_evidence(
                    statement,
                    article_elements,
                    article_source_title,
                ),
            }
            for statement in claim_texts
        ]
        claims = self._attach_claim_relationships(claims, rules)
        derived = self._derived_rules_from_claims(claims)
        core = sentences[0] if sentences else (title or "No abstract provided for this article.")
        return {
            "core_contribution": core,
            "claims": claims,
            "limitations": [
                "Analysis derived from the article title and abstract only "
                "(no full-text element parsing in this beta).",
            ],
            "validation_plan": [
                "Upload the full article text so claims can be bound to element-level evidence.",
                "Cross-check each claim against the notebook's approved rules.",
            ],
            "derived_rule_candidates": derived,
            "notebook_relationships": self._article_rule_relationships(claims),
        }

    def _article_elements(self, row: sqlite3.Row) -> tuple[List[SourceElement], str]:
        source_id = row["source_id"]
        if not source_id:
            return [], row["title"]
        try:
            source = self.get_source(source_id)
            return self.source_elements(source_id), source.title
        except KeyError:
            return [], row["title"]

    def _article_evidence(
        self,
        quoted_span: str,
        article_elements: List[SourceElement],
        article_source_title: str,
    ) -> List[Evidence]:
        if not quoted_span.strip() or not article_elements:
            return []
        evidence = bind_evidence(quoted_span, article_elements, article_source_title, 0.65)
        return [evidence] if evidence else []

    def _attach_claim_relationships(self, claims: List[dict], rules: List[dict]) -> List[dict]:
        for claim in claims:
            relation_type, rule_id, implication = self._best_rule_relationship(
                claim["statement"],
                rules,
            )
            claim["relation_type"] = relation_type
            claim["related_rule_id"] = rule_id
            claim["implication"] = implication
        return claims

    def _best_rule_relationship(self, statement: str, rules: List[dict]) -> tuple[str, str, str]:
        best_rule: Optional[dict] = None
        best_score = 0.0
        for rule in rules:
            payload = rule["payload"]
            rule_text = f"{payload.get('title', '')} {payload.get('statement', '')}"
            score = keyword_score(statement, rule_text)
            if score > best_score:
                best_score = score
                best_rule = rule
        if best_rule is None or best_score <= 0:
            return "", "", ""
        relation_type = _relation_type(statement)
        title = str(best_rule["payload"].get("title", "")).strip() or best_rule["id"]
        implication = f"{relation_type} approved rule: {title}"
        return relation_type, best_rule["id"], implication

    def _article_rule_relationships(self, claims: List[dict]) -> List[str]:
        relationships: List[str] = []
        for claim in claims:
            relation_type = claim.get("relation_type", "")
            related_rule_id = claim.get("related_rule_id", "")
            if relation_type and related_rule_id:
                relationships.append(f"{relation_type} {related_rule_id}: {claim['statement']}")
        return relationships

    def _derived_rules_from_claims(self, claims: List[dict]) -> List[dict]:
        derived: List[dict] = []
        for claim in claims:
            statement = claim["statement"]
            if not _rule_candidate_text(statement):
                continue
            proposed_rule = statement
            derived.append(
                {
                    "title": _first_sentence(proposed_rule, 90),
                    "proposed_rule": proposed_rule,
                    "rationale": claim.get("implication", "") or "Derived from article claim.",
                    "evidence": claim.get("evidence", []),
                }
            )
        return derived

    def _article_citations(self, brief_data: dict) -> List[Citation]:
        citations: List[Citation] = []
        seen: set[tuple[str, str, str]] = set()
        for item in [*brief_data["claims"], *brief_data["derived_rule_candidates"]]:
            for evidence in item.get("evidence", []):
                key = (evidence.source_id, evidence.element_id, evidence.quoted_span)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(_citation("Article evidence", evidence))
        return citations

    def _persist_article_research(
        self, article_id: str, notebook_id: str, brief_data: dict
    ) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("DELETE FROM article_claims WHERE article_id = ?", (article_id,))
            db.execute("DELETE FROM derived_rule_candidates WHERE article_id = ?", (article_id,))
            for index, claim in enumerate(brief_data["claims"], start=1):
                db.execute(
                    """
                    INSERT INTO article_claims
                    (id, article_id, notebook_id, statement, claim_type, relation_type,
                     related_rule_id, implication, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"clm-{article_id}-{index:03d}",
                        article_id,
                        notebook_id,
                        claim["statement"],
                        claim.get("claim_type", ""),
                        claim.get("relation_type", ""),
                        claim.get("related_rule_id", ""),
                        claim.get("implication", ""),
                        json.dumps(
                            [evidence.model_dump() for evidence in claim.get("evidence", [])],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
            for index, item in enumerate(brief_data["derived_rule_candidates"], start=1):
                db.execute(
                    """
                    INSERT INTO derived_rule_candidates
                    (id, notebook_id, article_id, title, proposed_rule, rationale,
                     status, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"drc-{article_id}-{index:03d}",
                        notebook_id,
                        article_id,
                        item.get("title", "") or _first_sentence(item["proposed_rule"], 90),
                        item["proposed_rule"],
                        item.get("rationale", ""),
                        "draft",
                        json.dumps(
                            [evidence.model_dump() for evidence in item.get("evidence", [])],
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
            db.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                ("brief-ready", now, article_id),
            )

    def _notebook_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> NotebookSummary:
        counts = {
            "sources": self._count(db, "sources", "notebook_id", row["id"]),
            "rules": self._count_knowledge(db, row["id"], "rule"),
            "cases": self._count_knowledge(db, row["id"], "case"),
            "checklist_items": self._count_knowledge(db, row["id"], "checklist"),
            "methods": self._count_knowledge(db, row["id"], "method"),
            "risks": self._count_knowledge(db, row["id"], "risk"),
            "glossary": self._count_knowledge(db, row["id"], "glossary"),
            "article_claims": self._count(db, "article_claims", "notebook_id", row["id"]),
        }
        keys = row.keys()

        def _list(field: str) -> List[str]:
            if field not in keys or not row[field]:
                return []
            try:
                value = json.loads(row[field])
                return [str(v) for v in value] if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        return NotebookSummary(
            id=row["id"],
            name=row["name"],
            purpose=row["purpose"],
            primary_domain=row["primary_domain"],
            status=row["status"],
            counts=counts,
            created_label=_created_label(row["created_at"]),
            target_users=row["target_users"] if "target_users" in keys else "",
            expected_questions=_list("expected_questions"),
            source_types=_list("source_types"),
            taxonomy=_list("taxonomy"),
            access_scope=row["access_scope"] if "access_scope" in keys else "",
        )

    def _source_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> SourceSummary:
        element_count = self._count(db, "source_elements", "source_id", row["id"])
        return SourceSummary(
            id=row["id"],
            notebook_id=row["notebook_id"],
            title=row["title"],
            type=row["source_type"],
            status=row["status"],
            summary=row["summary"],
            element_count=element_count,
            file_name=row["file_name"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parse_status=row["parse_status"],
            created_label=_created_label(row["created_at"]),
        )

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

    def _summarize_source(self, title: str, elements: List[SourceElement]) -> str:
        text = "\n".join(element.text for element in elements[:12])
        if self.llm_client.configured and text.strip():
            try:
                raw = self.llm_client.chat_json(
                    [
                        {
                            "role": "user",
                            "content": (
                                f"Summarize this semiconductor notebook source in one concise sentence.\n"
                                f"Title: {title}\n\n{text[:6000]}"
                            ),
                        }
                    ],
                    '{"summary": "one concise sentence"}',
                )
                parsed = json.loads(raw)
                summary = str(parsed.get("summary", "")).strip()
                if summary:
                    return summary
            except Exception:
                pass
        if not text.strip():
            return "Parsed source contains no extractable text elements."
        first = " ".join(text.split())[:260]
        return f"{len(elements)} parsed text element(s). {first}"

    def _delete_file(self, file_path: str) -> None:
        if not file_path or "data/demo" in file_path:
            return
        path = Path(file_path)
        if path.exists() and path.is_file():
            path.unlink()
        notebook_dir = path.parent
        if notebook_dir.exists() and not any(notebook_dir.iterdir()):
            shutil.rmtree(notebook_dir, ignore_errors=True)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _as_str_list(value: object) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


def _created_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _first_sentence(text: str, limit: int = 200) -> str:
    parts = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    sentence = parts[0] if parts else text
    return sentence.strip()[:limit]


def _claim_type(statement: str) -> str:
    lower = statement.lower()
    if re.search(r"\b(must|should|require|ensure|verify)\b|必须|应该|应当|需要|确认|验证", statement, re.IGNORECASE):
        return "recommendation"
    if re.search(r"\b(risk|fail|failure|damage|degrade|hazard)\b|风险|失效|损坏|退化", statement, re.IGNORECASE):
        return "warning"
    if any(token in lower for token in ("reduce", "increase", "improve", "measured", "observed")):
        return "result"
    return "mechanism"


def _relation_type(statement: str) -> str:
    if re.search(r"\b(challenge|contradict|conflict|however|limitation)\b|挑战|相反|冲突|局限", statement, re.IGNORECASE):
        return "challenges"
    if re.search(r"\b(refine|tune|adjust|balance|tradeoff)\b|权衡|细化|调整", statement, re.IGNORECASE):
        return "refines"
    if re.search(r"\b(extend|new|additional|also|further)\b|新增|扩展|进一步", statement, re.IGNORECASE):
        return "extends"
    return "supports"


def _rule_candidate_text(statement: str) -> bool:
    return bool(
        re.search(
            r"\b(must|shall|should|require[sd]?|ensure|verify|confirm|balance)\b|必须|应当|应该|需要|确认|验证|权衡",
            statement,
            re.IGNORECASE,
        )
    )


def _safe_filename(file_name: str) -> str:
    cleaned = Path(file_name).name.replace("/", "_").replace("\\", "_").strip()
    return cleaned or "source.bin"


def _snippet(text: str, needle: str) -> str:
    clean = " ".join(text.split())
    lower = clean.lower()
    index = lower.find(needle)
    if index < 0:
        return clean[:180]
    start = max(0, index - 48)
    end = min(len(clean), index + len(needle) + 120)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"
