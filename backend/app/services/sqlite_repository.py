from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.core.llm import OpenAICompatibleClient
from app.models.schemas import (
    AnswerAnchor,
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
    KnowledgeEdge,
    KnowledgeFieldValue,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeRecord,
    KnowledgeRef,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    MergeRequest,
    MethodCard,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    RiskItemCard,
    RuleCard,
    SearchHit,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)
from app.services import kg_ingest
from app.services.extraction_profiles import (
    LIST_FIELDS,
    OBJECT_SCHEMAS,
    OBJECT_TYPE_LABELS,
    PROFILES,
    ObjectSchema,
    get_profile,
    resolve_profile,
)


def _normalize_doc_type(doc_type: str) -> str:
    """Keep only known profile ids; everything else (incl. 'auto') means
    auto-detect, stored as ''."""
    value = (doc_type or "").strip().lower()
    return value if value in PROFILES else ""
from app.services.mineru_client import MinerUClient
from app.services.notebook_templates import NOTEBOOK_TEMPLATES
from app.services.parsers import parse_source_file
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    ARTICLE_SCHEMA_HINT,
    DESCRIPTION_SCHEMA_HINT,
    SCHEMA_INDUCTION_HINT,
    answer_prompt,
    article_prompt,
    notebook_description_prompt,
    schema_induction_prompt,
)
from app.services.repository import UploadedSourceFile
from app.services.retrieval import (
    RetrievedKnowledge,
    _TYPE_WEIGHT,
    _payload_text,
    cosine,
    keyword_score,
    score_knowledge,
)


# Knowledge statuses that may be surfaced in answers/retrieval (§12 governance).
# 'deprecated' is excluded; 'conflict' is retrieved but flagged elsewhere.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")

KNOWLEDGE_STATUSES = ("approved", "reviewed", "deprecated", "conflict", "project_specific")

# KG object types retrieved during ask(), in priority order.
_KG_TYPES = ("claim", "formula", "procedure", "concept")

# Global cap on KG hits returned by ask(): all types are scored, pooled, and
# ranked by relevance * type-weight (soft prior); the top _TOP_N are kept.
_TOP_N = 12

# Matches the `[k1]` provenance markers the answer LLM appends to grounded
# sentences; used to resolve markers -> citation anchors and to strip them when
# deriving the back-compat `conclusion` string.
_MARKER_RE = re.compile(r"\[(k\d+)\]")


class SQLiteRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root_dir = Path(__file__).resolve().parents[3]
        self.db_path = self._resolve_path(settings.sqlite_path)
        self.storage_dir = self._resolve_path(settings.storage_dir)
        self.llm_client = OpenAICompatibleClient(settings)
        from app.services.embedding import make_embedder
        self.embedder = make_embedder(self.settings)
        self.mineru_client = MinerUClient(settings)
        self.event_log = EventLogger(settings, channel="events")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._unified_cache: Dict[Any, Any] = {}
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
                  source_id TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_relations (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
                  source_object_id TEXT NOT NULL,
                  target_object_id TEXT NOT NULL,
                  edge_type TEXT NOT NULL,
                  evidence TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS answers (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  question TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL,
                  title TEXT DEFAULT '',
                  created_by TEXT DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
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

                CREATE TABLE IF NOT EXISTS object_schemas (
                  object_type TEXT PRIMARY KEY,
                  plural TEXT NOT NULL DEFAULT '',
                  fields TEXT NOT NULL DEFAULT '[]',
                  primary_field TEXT NOT NULL DEFAULT '',
                  description TEXT NOT NULL DEFAULT '',
                  label TEXT NOT NULL DEFAULT '',
                  list_fields TEXT NOT NULL DEFAULT '[]',
                  source TEXT NOT NULL DEFAULT 'builtin',
                  status TEXT NOT NULL DEFAULT 'active',
                  rationale TEXT NOT NULL DEFAULT '',
                  notebook_id TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS concept_clusters (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  canonical_id TEXT NOT NULL,
                  member_object_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clusters_nb ON concept_clusters(notebook_id);
                CREATE INDEX IF NOT EXISTS idx_clusters_member ON concept_clusters(member_object_id);

                CREATE TABLE IF NOT EXISTS concept_merge_candidates (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  canonical_a TEXT NOT NULL, canonical_b TEXT NOT NULL,
                  score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_nb_status ON concept_merge_candidates(notebook_id, status);
                """
            )
            # Lightweight column migrations for pre-existing databases.
            # SQLite has no `ADD COLUMN IF NOT EXISTS`; guard via PRAGMA so this
            # runs idempotently on every init.
            answer_cols = {r["name"] for r in db.execute("PRAGMA table_info(answers)").fetchall()}
            if "conversation_id" not in answer_cols:
                db.execute("ALTER TABLE answers ADD COLUMN conversation_id TEXT")
            ccols = {r["name"] for r in db.execute("PRAGMA table_info(conversations)").fetchall()}
            if "created_by" not in ccols:
                db.execute("ALTER TABLE conversations ADD COLUMN created_by TEXT DEFAULT ''")
            db.execute(
                "UPDATE conversations SET created_by='user-local' "
                "WHERE created_by IS NULL OR created_by=''"
            )
            ko_cols = {r["name"] for r in db.execute("PRAGMA table_info(knowledge_objects)").fetchall()}
            if "last_reviewed" not in ko_cols:
                db.execute(
                    "ALTER TABLE knowledge_objects ADD COLUMN last_reviewed TEXT NOT NULL DEFAULT ''"
                )
            if "source_id" not in ko_cols:
                db.execute(
                    "ALTER TABLE knowledge_objects ADD COLUMN source_id TEXT NOT NULL DEFAULT ''"
                )
            nb_cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)").fetchall()}
            for col in ("target_users", "expected_questions", "source_types", "taxonomy", "access_scope", "template"):
                if col not in nb_cols:
                    db.execute(f"ALTER TABLE notebooks ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            # purpose_auto=1 means the description is auto-derived from sources and
            # may be regenerated; set to 0 once the user edits it manually.
            if "purpose_auto" not in nb_cols:
                db.execute("ALTER TABLE notebooks ADD COLUMN purpose_auto INTEGER NOT NULL DEFAULT 0")
            # Per-source document type drives schema/profile selection at extraction.
            src_cols = {r["name"] for r in db.execute("PRAGMA table_info(sources)").fetchall()}
            if "doc_type" not in src_cols:
                db.execute("ALTER TABLE sources ADD COLUMN doc_type TEXT NOT NULL DEFAULT ''")
            # Seed the editable object-schema registry from the code defaults
            # (INSERT OR IGNORE keeps any curator edits / induced types intact).
            now = _now()
            for object_type, schema in OBJECT_SCHEMAS.items():
                list_fields = [f for f in schema.fields if f in LIST_FIELDS]
                db.execute(
                    """
                    INSERT OR IGNORE INTO object_schemas
                    (object_type, plural, fields, primary_field, description, label,
                     list_fields, source, status, rationale, notebook_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'builtin', 'active', '', '', ?, ?)
                    """,
                    (
                        object_type,
                        schema.plural,
                        json.dumps(schema.fields, ensure_ascii=False),
                        schema.primary,
                        schema.description,
                        OBJECT_TYPE_LABELS.get(object_type, object_type),
                        json.dumps(list_fields, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

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
        """Minimal creation: only name + description (purpose). When the user
        leaves the description blank it is flagged auto (purpose_auto=1) and
        later derived from the first batch of uploaded sources."""
        notebook_id = f"nb-{uuid4().hex[:10]}"
        now = _now()
        purpose = (payload.purpose or "").strip()
        purpose_auto = 0 if purpose else 1

        with self._connect() as db:
            db.execute(
                """
                INSERT INTO notebooks
                (id, name, purpose, primary_domain, status, created_by, created_at, updated_at,
                 purpose_auto)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notebook_id,
                    payload.name,
                    purpose,
                    "Semiconductor",
                    "draft",
                    "user-local",
                    now,
                    now,
                    purpose_auto,
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
            # A user-edited description is manual; stop auto-regenerating it.
            updates.append("purpose_auto = ?")
            values.append(0)
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
                     file_size, summary, doc_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _normalize_doc_type(file.doc_type),
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
                     file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        _normalize_doc_type(file.doc_type),
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
            mineru_error = str(getattr(self.mineru_client, "last_error", "") or "")
            element_parsers = sorted(
                {
                    str(element.metadata.get("parser", ""))
                    for element in elements
                    if element.metadata.get("parser")
                }
            )
            stage(
                "parse",
                "done",
                t,
                elements=len(elements),
                parser_mode=str(getattr(self.mineru_client, "mode", "")),
                actual_parsers=element_parsers,
                mineru_error=mineru_error[:500],
            )
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
            try:
                self.rebuild_unified_kg(self.get_source(source_id).notebook_id)
            except Exception:
                self.event_log.logger.exception("unified-KG rebuild failed for source %s", source_id)
            # Surface "parsed to empty" (e.g. scanned/image PDF with no text layer)
            # instead of a silent success that looks like a real result.
            empty_hint = ""
            if not elements and source.file_name.lower().endswith(".pdf"):
                empty_hint = (
                    "No extractable text — likely a scanned/image PDF. "
                    "Enable MinerU (MINERU_MODE) or add OCR to parse it."
                )
            fallback_hint = ""
            if (
                source.file_name.lower().endswith(".pdf")
                and self.mineru_client.configured
                and elements
                and "mineru" not in element_parsers
            ):
                fallback_hint = (
                    "MinerU did not produce usable elements; fell back to pypdf text extraction. "
                    "Check MinerU settings/logs if layout, formula, or table fidelity is expected."
                )
                if mineru_error:
                    fallback_hint = f"{fallback_hint} Last MinerU error: {mineru_error[:500]}"
            self._set_source_status(
                source_id,
                "extracted",
                error_message=empty_hint or fallback_hint,
            )
            # Derive the notebook description from its sources while it is still
            # auto (the user hasn't written one). Best-effort; never fails the pipeline.
            try:
                self._augment_notebook_description(source.notebook_id)
            except Exception:
                self.event_log.logger.exception(
                    "description augment failed for %s", source.notebook_id
                )
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

    def _augment_notebook_description(self, notebook_id: str) -> None:
        """Derive the notebook description from its sources, while it is still
        auto (purpose_auto=1). Regenerates as the first batch's sources finish so
        the final description reflects all of them. No-op once user-edited."""
        with self._connect() as db:
            nb = db.execute(
                "SELECT purpose_auto FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            if nb is None or ("purpose_auto" in nb.keys() and nb["purpose_auto"] != 1):
                return
            rows = db.execute(
                "SELECT title, doc_type, summary FROM sources "
                "WHERE notebook_id = ? AND status = 'extracted' ORDER BY created_at ASC",
                (notebook_id,),
            ).fetchall()
        if not rows:
            return
        titles = [r["title"] for r in rows]
        labels = []
        for r in rows:
            profile = PROFILES.get(_normalize_doc_type(r["doc_type"]))
            label = profile.label if profile else "自动检测"
            if label not in labels:
                labels.append(label)

        description = ""
        if self.llm_client.configured:
            block = "\n".join(
                f"- {r['title']} "
                f"[{(PROFILES.get(_normalize_doc_type(r['doc_type'])) or get_profile('academic_paper')).label}] "
                f"{(r['summary'] or '')[:200]}"
                for r in rows[:20]
            )
            try:
                raw = self.llm_client.chat_json(
                    [{"role": "user", "content": notebook_description_prompt(block)}],
                    DESCRIPTION_SCHEMA_HINT,
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    description = str(parsed.get("description", "")).strip()
            except Exception:
                description = ""
        if not description:
            shown = "、".join(titles[:5]) + ("等" if len(titles) > 5 else "")
            description = f"本笔记本收录了 {len(titles)} 个来源：{shown}。"
            if labels:
                description += f"文档类型涵盖 {'、'.join(labels)}。"

        with self._connect() as db:
            db.execute(
                "UPDATE notebooks SET purpose = ?, updated_at = ? "
                "WHERE id = ? AND purpose_auto = 1",
                (description[:1000], _now(), notebook_id),
            )

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
        self._invalidate_unified_cache(source.notebook_id)

    def _run_extraction(self, source_id: str) -> None:
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        now = _now()
        run_id = f"run-{uuid4().hex[:10]}"
        doc_type_id = _normalize_doc_type(getattr(source, "doc_type", "") or "") or "academic_paper"
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type_id, "academic")
        with self._connect() as db:
            self._clear_source_extraction_state(db, source_id, source.notebook_id, clear_embeddings=False)
            self._delete_relations_for_source(db, source_id)
            db.execute("DELETE FROM knowledge_objects WHERE source_id = ?", (source_id,))
            db.execute(
                """INSERT INTO extraction_runs
                   (id, notebook_id, source_id, run_type, status, error_message, created_at, updated_at)
                   VALUES (?, ?, ?, 'kg', 'running', '', ?, ?)""",
                (run_id, source.notebook_id, source_id, now, now))
        try:
            if not getattr(self.llm_client, "configured", False):
                with self._connect() as db:
                    db.execute("UPDATE extraction_runs SET status='completed', error_message='no-llm', updated_at=? WHERE id=?", (_now(), run_id))
                return
            raw_text = self._source_raw_text(source, elements)
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=self.settings.kg_window_target_chars,
                m=self.settings.kg_window_overlap_chars,
                workers=self.settings.kg_extract_workers,
            )
            warn = self.settings.kg_window_warn_threshold
            if graph.total_windows > warn:
                self.event_log.logger.warning(
                    "KG windows %s exceed warn threshold %s for source %s (%s) — "
                    "extracting in full, no truncation",
                    graph.total_windows, warn, source_id, source.file_name,
                )
            objects, relations = kg_ingest.build_records(graph, source.id, source.title, elements)
            n_obj, n_rel = self.store_kg(source.notebook_id, source.id, objects, relations)
            fw, tw = graph.failed_windows, graph.total_windows
            with self._connect() as db:
                db.execute("UPDATE extraction_runs SET status='completed', error_message=?, updated_at=? WHERE id=?",
                           (f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} windows_failed={fw}/{tw}", _now(), run_id))
        except Exception as exc:
            with self._connect() as db:
                db.execute("UPDATE extraction_runs SET status='failed', error_message=?, updated_at=? WHERE id=?",
                           (str(exc), _now(), run_id))
            raise

    def _source_raw_text(self, source, elements) -> str:
        """Raw document text for windowing: read the stored .md/.txt file when
        present, else reconstruct from element texts."""
        path = getattr(source, "file_path", "") or ""
        if path and (path.endswith(".md") or path.endswith(".markdown") or path.endswith(".txt")):
            try:
                resolved = self._resolve_path(path)
                return Path(resolved).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return "\n\n".join(e.text for e in elements)

    def _embed_source(self, source_id: str) -> None:
        if not self.settings.embedder_configured:
            return
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        pending = [el for el in elements if el.text.strip()]
        if not pending:
            return
        from app.services.embedding import embed_in_chunks
        trunc = self.settings.embed_truncate_chars
        texts = [el.text[:trunc] for el in pending]
        vectors = embed_in_chunks(
            self.embedder.embed_texts, texts,
            chunk_size=self.settings.embed_persist_chunk,
            logger=self.event_log.logger,
        )
        now = _now()
        stored = 0
        with self._connect() as db:
            for element, vector in zip(pending, vectors):
                if vector is None:
                    continue
                stored += 1
                db.execute(
                    """
                    INSERT OR REPLACE INTO element_embeddings
                    (element_id, source_id, notebook_id, vector, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (element.id, source_id, source.notebook_id, json.dumps(vector), now),
                )
        self.event_log.logger.info(
            "embedded %s/%s elements for source %s", stored, len(pending), source_id
        )

    def _embed_knowledge(
        self,
        object_id: str,
        notebook_id: str,
        payload: Dict[str, object],
    ) -> None:
        """Embed a knowledge object's own payload text (WS4: payload-level
        vectors, not just evidence-element vectors). No-op without embeddings."""
        if not self.settings.embedder_configured:
            return
        text = _payload_text(payload).strip()
        if not text:
            return
        try:
            vector = self.embedder.embed_query(text[:2000])
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

    def _embed_objects_batch(self, notebook_id: str, items: List[dict]) -> None:
        """Batch-embed object payload text into knowledge_embeddings (best-effort).

        Uses _payload_text (all string fields), matching the lazy backfill in
        _knowledge_vectors so a node embedded at ingest and one backfilled at
        search time get identical vectors. Name-only would diverge from the
        backfill path and silently skip nodes with no `name` (e.g. rule objects
        keyed on title/statement)."""
        if not self.settings.embedder_configured:
            return
        texts, ids = [], []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if not text:
                continue
            ids.append(it["_oid"]); texts.append(text[:2000])
        if not texts:
            return
        try:
            vectors = self.embedder.embed_texts(texts)
        except Exception:
            return  # embedding best-effort; never block ingestion
        now = _now()
        with self._connect() as db:
            for oid, vec in zip(ids, vectors):
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                    (oid, notebook_id, json.dumps(vec), now))

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
        if not self.settings.embedder_configured:
            return vectors
        pending_ids, pending_texts = [], []
        for obj in objects:
            object_id = obj["id"]
            if object_id in vectors:
                continue
            text = _payload_text(obj.get("payload", {})).strip()
            if not text:
                continue
            pending_ids.append(object_id); pending_texts.append(text[:2000])
        if not pending_texts:
            return vectors
        try:
            new_vectors = self.embedder.embed_texts(pending_texts)
        except Exception:
            return vectors  # backfill best-effort; never block search
        now = _now()
        for object_id, vector in zip(pending_ids, new_vectors):
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
        self._invalidate_unified_cache(notebook_id)
        return candidate

    def reject_candidate(self, candidate_id: str) -> Candidate:
        now = _now()
        with self._connect() as db:
            pre_row = self._candidate_row_by_id(db, candidate_id)
            notebook_id = pre_row["notebook_id"]
            db.execute(
                "UPDATE extraction_candidates SET status = ?, updated_at = ? WHERE id = ?",
                ("rejected", now, candidate_id),
            )
            db.execute(
                "DELETE FROM knowledge_objects WHERE source_candidate_id = ?",
                (candidate_id,),
            )
            row = self._candidate_row_by_id(db, candidate_id)
            candidate = self._candidate_from_row(row)
        self._invalidate_unified_cache(notebook_id)
        return candidate

    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]:
        """All object types present in this notebook with non-deprecated counts,
        so the UI can render a tab per type — including academic/textbook types
        that have no bespoke card."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT object_type, COUNT(*) AS c FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated' "
                "GROUP BY object_type",
                (notebook_id,),
            ).fetchall()
            label_rows = db.execute(
                "SELECT object_type, label FROM object_schemas"
            ).fetchall()
        labels = {r["object_type"]: (r["label"] or r["object_type"]) for r in label_rows}
        counts = {row["object_type"]: int(row["c"]) for row in rows}
        ordered = [t for t in OBJECT_SCHEMAS if t in counts]
        ordered += [t for t in counts if t not in OBJECT_SCHEMAS]
        return [
            KnowledgeTypeCount(
                object_type=t,
                label=labels.get(t, OBJECT_TYPE_LABELS.get(t, t)),
                count=counts[t],
            )
            for t in ordered
        ]

    def _knowledge_record(
        self, object_type: str, obj: dict, schema: Optional[ObjectSchema]
    ) -> KnowledgeRecord:
        payload = obj.get("payload") or {}
        keys = (
            schema.fields
            if schema
            else [k for k in payload if not str(k).startswith("_")]
        )
        fields: List[KnowledgeFieldValue] = []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                text = ", ".join(str(v) for v in value if str(v).strip())
            elif value is None:
                text = ""
            else:
                text = str(value)
            if text.strip():
                fields.append(KnowledgeFieldValue(key=key, value=text.strip()))
        return KnowledgeRecord(
            id=obj["id"],
            object_type=object_type,
            headline=self._knowledge_headline(object_type, payload),
            fields=fields,
            status=obj.get("status", "approved"),
            owner=obj.get("owner", ""),
            last_reviewed=obj.get("last_reviewed", ""),
            evidence=obj.get("evidence", []),
        )

    def list_knowledge(
        self, notebook_id: str, object_type: str
    ) -> List[KnowledgeRecord]:
        """Generic, type-agnostic listing for any object type (used to browse
        the academic/textbook types that have no dedicated card endpoint)."""
        self.get_notebook(notebook_id)
        schema = self.effective_schemas().get(object_type)
        with self._connect() as db:
            objects = self._knowledge_objects(db, notebook_id, object_type, statuses=None)
        return [self._knowledge_record(object_type, obj, schema) for obj in objects]

    # --- Editable extraction-schema registry ----------------------------
    @staticmethod
    def _object_schema_from_row(row) -> ObjectSchemaModel:
        return ObjectSchemaModel(
            object_type=row["object_type"],
            plural=row["plural"] or f"{row['object_type']}s",
            fields=json.loads(row["fields"] or "[]"),
            primary=row["primary_field"] or "",
            description=row["description"] or "",
            label=row["label"] or row["object_type"],
            list_fields=json.loads(row["list_fields"] or "[]"),
            source=row["source"] or "builtin",
            status=row["status"] or "active",
            rationale=row["rationale"] or "",
            notebook_id=row["notebook_id"] if "notebook_id" in row.keys() else "",
        )

    def effective_schemas(self) -> Dict[str, ObjectSchema]:
        """Active object schemas as an ObjectSchema registry for extraction —
        DB rows overlaid on the code defaults."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM object_schemas WHERE status = 'active'"
            ).fetchall()
        registry: Dict[str, ObjectSchema] = {}
        for row in rows:
            registry[row["object_type"]] = ObjectSchema(
                type=row["object_type"],
                plural=row["plural"] or f"{row['object_type']}s",
                fields=json.loads(row["fields"] or "[]"),
                primary=row["primary_field"] or "",
                description=row["description"] or "",
                list_fields=json.loads(row["list_fields"] or "[]"),
            )
        for object_type, schema in OBJECT_SCHEMAS.items():
            registry.setdefault(object_type, schema)
        return registry

    def list_object_schemas(self) -> List[ObjectSchemaModel]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM object_schemas").fetchall()
        models = [self._object_schema_from_row(row) for row in rows]
        order = {"active": 0, "disabled": 1, "proposed": 2}
        models.sort(key=lambda m: (order.get(m.status, 3), m.object_type))
        return models

    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel:
        object_type = payload.object_type.strip().lower().replace(" ", "_")
        if not object_type:
            raise ValueError("object_type is required")
        now = _now()
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM object_schemas WHERE object_type = ?", (object_type,)
            ).fetchone()
            if exists is not None:
                raise ValueError(f"object type '{object_type}' already exists")
            db.execute(
                """
                INSERT INTO object_schemas
                (object_type, plural, fields, primary_field, description, label,
                 list_fields, source, status, rationale, notebook_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'custom', 'active', '', '', ?, ?)
                """,
                (
                    object_type,
                    payload.plural.strip() or f"{object_type}s",
                    json.dumps(payload.fields, ensure_ascii=False),
                    payload.primary.strip() or (payload.fields[0] if payload.fields else ""),
                    payload.description.strip(),
                    payload.label.strip() or object_type,
                    json.dumps(payload.list_fields, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM object_schemas WHERE object_type = ?", (object_type,)
            ).fetchone()
        return self._object_schema_from_row(row)

    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel:
        updates: List[str] = []
        values: List[object] = []
        if payload.plural is not None:
            updates.append("plural = ?")
            values.append(payload.plural.strip())
        if payload.fields is not None:
            updates.append("fields = ?")
            values.append(json.dumps(payload.fields, ensure_ascii=False))
        if payload.primary is not None:
            updates.append("primary_field = ?")
            values.append(payload.primary.strip())
        if payload.description is not None:
            updates.append("description = ?")
            values.append(payload.description.strip())
        if payload.label is not None:
            updates.append("label = ?")
            values.append(payload.label.strip())
        if payload.list_fields is not None:
            updates.append("list_fields = ?")
            values.append(json.dumps(payload.list_fields, ensure_ascii=False))
        if payload.status is not None:
            status = payload.status.strip()
            if status not in {"active", "disabled", "proposed"}:
                raise ValueError(f"invalid schema status: {status}")
            updates.append("status = ?")
            values.append(status)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM object_schemas WHERE object_type = ?", (object_type,)
            ).fetchone()
            if row is None:
                raise KeyError(object_type)
            if updates:
                updates.append("updated_at = ?")
                values.append(_now())
                values.append(object_type)
                db.execute(
                    f"UPDATE object_schemas SET {', '.join(updates)} WHERE object_type = ?",
                    values,
                )
            row = db.execute(
                "SELECT * FROM object_schemas WHERE object_type = ?", (object_type,)
            ).fetchone()
        return self._object_schema_from_row(row)

    def delete_object_schema(self, object_type: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT source FROM object_schemas WHERE object_type = ?",
                (object_type,),
            ).fetchone()
            if row is None:
                raise KeyError(object_type)
            if row["source"] == "builtin":
                raise ValueError("builtin schemas can be disabled but not deleted")
            db.execute(
                "DELETE FROM object_schemas WHERE object_type = ?", (object_type,)
            )

    def propose_schemas(self, notebook_id: str) -> List[ObjectSchemaModel]:
        """Schema induction (suggestion mode): inspect the notebook's content and
        propose NEW object types the current schema does not cover. Proposals are
        stored with status='proposed' for curator approval; never auto-activated.
        Requires the LLM; offline this is a no-op that returns existing proposals."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            existing = {
                r["object_type"]
                for r in db.execute(
                    "SELECT object_type FROM object_schemas"
                ).fetchall()
            }
            elements = self._gather_elements(db, notebook_id)
        if self.llm_client.configured and elements:
            sample = "\n".join(
                f"[{e['location_label']}] {e['text']}" for e in elements
            )[:8000]
            data: dict = {}
            try:
                raw = self.llm_client.chat_json(
                    [
                        {
                            "role": "user",
                            "content": schema_induction_prompt(
                                sorted(existing), sample
                            ),
                        }
                    ],
                    SCHEMA_INDUCTION_HINT,
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
            now = _now()
            with self._connect() as db:
                for item in data.get("new_types") or []:
                    if not isinstance(item, dict):
                        continue
                    object_type = (
                        str(item.get("object_type", "")).strip().lower().replace(" ", "_")
                    )
                    fields = [
                        str(f).strip()
                        for f in (item.get("fields") or [])
                        if str(f).strip()
                    ]
                    if not object_type or object_type in existing or not fields:
                        continue
                    primary = str(item.get("primary", "")).strip() or fields[0]
                    db.execute(
                        """
                        INSERT INTO object_schemas
                        (object_type, plural, fields, primary_field, description, label,
                         list_fields, source, status, rationale, notebook_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, '[]', 'induced', 'proposed', ?, ?, ?, ?)
                        """,
                        (
                            object_type,
                            str(item.get("plural", "")).strip() or f"{object_type}s",
                            json.dumps(fields, ensure_ascii=False),
                            primary,
                            str(item.get("description", "")).strip(),
                            str(item.get("label", "")).strip() or object_type,
                            str(item.get("rationale", "")).strip(),
                            notebook_id,
                            now,
                            now,
                        ),
                    )
                    existing.add(object_type)
        return [m for m in self.list_object_schemas() if m.status == "proposed"]

    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph:
        """KG-native graph: nodes = non-deprecated knowledge objects (4 KG types),
        edges = knowledge_relations rows."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, object_type, status, payload FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated'", (notebook_id,)).fetchall()
        nodes = [
            KnowledgeNode(id=r["id"], object_type=r["object_type"],
                          headline=self._kg_headline(json.loads(r["payload"] or "{}")),
                          status=r["status"])
            for r in rows]
        valid = {n.id for n in nodes}
        edges = [
            KnowledgeEdge(from_id=rel["source_object_id"], to_id=rel["target_object_id"],
                          relation=rel["edge_type"], label=rel["edge_type"])
            for rel in self.relations_for_notebook(notebook_id)
            if rel["source_object_id"] in valid and rel["target_object_id"] in valid]
        return KnowledgeGraph(nodes=nodes, edges=edges)

    def _kg_headline(self, payload: dict) -> str:
        name = (payload.get("name") or "").strip()
        return name[:120] if len(name) > 120 else name

    def add_relations(self, notebook_id: str, source_id: str,
                      relations: List[dict]) -> int:
        now = _now()
        with self._connect() as db:
            for rel in relations:
                db.execute(
                    """
                    INSERT INTO knowledge_relations
                    (id, notebook_id, source_id, source_object_id, target_object_id,
                     edge_type, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"rel-{uuid4().hex[:10]}", notebook_id, source_id,
                        rel["source_object_id"], rel["target_object_id"],
                        rel["edge_type"],
                        json.dumps(rel.get("evidence", []), ensure_ascii=False),
                        now,
                    ),
                )
        return len(relations)

    def store_kg(self, notebook_id: str, source_id: Optional[str],
                 objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
        """Insert KG nodes as approved knowledge_objects and edges as
        knowledge_relations (remapping local ids to DB ids). Embeds node payload.

        Objects and relations are written atomically in a single transaction so
        a mid-write failure cannot leave orphan objects with no edges.
        Relations whose source_local_id or target_local_id is not present in
        the given objects are silently skipped.
        """
        now = _now()
        local_to_id: Dict[str, str] = {}
        # Pre-assign DB ids and remap relations before opening the connection.
        for obj in objects:
            local_to_id[obj["local_id"]] = f"ko-{uuid4().hex[:10]}"
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            db_relations.append({
                "source_object_id": s,
                "target_object_id": t,
                "edge_type": rel["edge_type"],
                "evidence": rel.get("evidence", []),
            })
        with self._connect() as db:
            for obj in objects:
                oid = local_to_id[obj["local_id"]]
                obj["_oid"] = oid
                db.execute(
                    """INSERT INTO knowledge_objects
                       (id, notebook_id, object_type, status, owner, payload, evidence,
                        source_candidate_id, source_id, created_at, updated_at)
                       VALUES (?, ?, ?, 'approved', '', ?, ?, NULL, ?, ?, ?)""",
                    (oid, notebook_id, obj["object_type"],
                     json.dumps(obj["payload"], ensure_ascii=False),
                     json.dumps(obj["evidence"], ensure_ascii=False),
                     source_id or '', now, now),
                )
            for rel in db_relations:
                db.execute(
                    """
                    INSERT INTO knowledge_relations
                    (id, notebook_id, source_id, source_object_id, target_object_id,
                     edge_type, evidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"rel-{uuid4().hex[:10]}", notebook_id, source_id,
                        rel["source_object_id"], rel["target_object_id"],
                        rel["edge_type"],
                        json.dumps(rel["evidence"], ensure_ascii=False),
                        now,
                    ),
                )
        self._embed_objects_batch(notebook_id, objects)
        self._invalidate_unified_cache(notebook_id)
        return len(objects), len(db_relations)

    def relations_for_notebook(self, notebook_id: str) -> List[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM knowledge_relations WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
        return [
            {
                "id": r["id"], "source_id": r["source_id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"], "edge_type": r["edge_type"],
                "evidence": json.loads(r["evidence"] or "[]"),
            }
            for r in rows
        ]

    def _delete_relations_for_source(self, db, source_id: str) -> None:
        db.execute("DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,))

    # --- Concept-cluster / merge-candidate CRUD (Task 5) -------------------

    def write_clusters(self, notebook_id: str, rows: List[dict]) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("DELETE FROM concept_clusters WHERE notebook_id=?", (notebook_id,))
            for r in rows:
                db.execute(
                    "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,created_at) VALUES (?,?,?,?,?,?)",
                    (f"cc-{uuid4().hex[:10]}", notebook_id, r["canonical_id"], r["member_object_id"], r["canonical_name"], now))

    def cluster_map(self, notebook_id: str) -> Dict[str, str]:
        with self._connect() as db:
            rows = db.execute("SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    def write_merge_candidate(self, notebook_id: str, a: str, b: str, score: float) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
                (f"mc-{uuid4().hex[:10]}", notebook_id, a, b, score, now, now))

    def pending_merges(self, notebook_id: str) -> List[dict]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

    def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        with self._connect() as db:
            db.execute("UPDATE concept_merge_candidates SET status=?, updated_at=? WHERE id=? AND notebook_id=?", (status, _now(), candidate_id, notebook_id))

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "confirmed")
        self._invalidate_unified_cache(notebook_id)

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "rejected")
        self._invalidate_unified_cache(notebook_id)

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self._connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    def _invalidate_unified_cache(self, notebook_id: str) -> None:
        for key in [k for k in self._unified_cache if k[0] == notebook_id]:
            self._unified_cache.pop(key, None)

    def unified_graph(self, notebook_id: str, level: str = "concept") -> dict:
        self.get_notebook(notebook_id)
        cached = self._unified_cache.get((notebook_id, level))
        if cached is not None:
            return cached
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = db.execute(
                "SELECT id, object_type, payload, status FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchall()
        nodes = [{"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}")} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"], "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        g = derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))
        if level == "concept":
            cids = {n["id"] for n in g["nodes"] if n["object_type"] == "concept"}
            g = {"nodes": [n for n in g["nodes"] if n["object_type"] == "concept"],
                 "edges": [e for e in g["edges"] if e["source_object_id"] in cids and e["target_object_id"] in cids]}
        self._unified_cache[(notebook_id, level)] = g
        return g

    def rebuild_unified_kg(self, notebook_id: str) -> int:
        """Cluster the notebook's Concepts; persist concept_clusters + refresh
        pending candidates (preserving confirmed/rejected). Returns #clusters."""
        self.get_notebook(notebook_id)
        from app.services.kg_merge import cluster_concepts
        with self._connect() as db:
            crows = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND object_type='concept' AND status!='deprecated'",
                (notebook_id,)).fetchall()
            vrows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)).fetchall()
        concepts = [{"object_id": r["id"], "name": json.loads(r["payload"] or "{}").get("name", "")} for r in crows]
        vectors = {r["object_id"]: json.loads(r["vector"]) for r in vrows}
        # Defensive: a pre-existing DB may hold vectors of a different dimension
        # (legacy embedder). Drop mismatched-length vectors so cluster_concepts'
        # numpy stack can't raise; name-seed merge still applies to those nodes.
        dim = self.settings.embed_dim
        vectors = {oid: v for oid, v in vectors.items() if len(v) == dim}
        # decided_pairs keys are canonical ids of the form "K-<normalized_seed_name>";
        # cluster_concepts wants confirmed/rejected keyed by normalized seed name -> strip "K-".
        def _seed(cid: str) -> str:
            return cid[2:] if cid.startswith("K-") else cid
        decided = self.decided_pairs(notebook_id)
        confirmed = {frozenset((_seed(a), _seed(b))) for (a, b), s in decided.items() if s == "confirmed"}
        rejected = {frozenset((_seed(a), _seed(b))) for (a, b), s in decided.items() if s == "rejected"}
        res = cluster_concepts(concepts, vectors, confirmed, rejected)
        rows = [{"canonical_id": res["cluster_map"][c["object_id"]], "member_object_id": c["object_id"],
                 "canonical_name": res["canonical_names"][c["object_id"]]} for c in concepts]
        self.write_clusters(notebook_id, rows)
        # Refresh pending candidates in ONE transaction (per-candidate inserts
        # were the rebuild hotspot at scale). confirmed/rejected rows untouched.
        now = _now()
        with self._connect() as db:
            db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,))
            db.executemany(
                "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?, 'pending', ?, ?)",
                [(f"mc-{uuid4().hex[:10]}", notebook_id, a, b, score, now, now) for a, b, score in res["pending"]])
        self._invalidate_unified_cache(notebook_id)
        return len(set(res["cluster_map"].values()))

    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict:
        self.get_notebook(notebook_id)
        cmap = self.cluster_map(notebook_id)
        members = [oid for oid, cid in cmap.items() if cid == canonical_id]
        mset = set(members)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,)).fetchall()
            nrow = db.execute(
                "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
                (notebook_id, canonical_id)).fetchone()
        name = nrow["canonical_name"] if nrow else ""
        by_id = {r["id"]: {"id": r["id"], "object_type": r["object_type"],
                           "payload": json.loads(r["payload"] or "{}"),
                           "evidence": json.loads(r["evidence"] or "[]")} for r in rows}
        attached = []
        seen_attached: set[str] = set()
        for rel in self.relations_for_notebook(notebook_id):
            s, t = rel["source_object_id"], rel["target_object_id"]
            if s in mset and t not in mset:
                other = t
            elif t in mset and s not in mset:
                other = s
            else:
                continue
            if other in by_id and by_id[other]["object_type"] != "concept" and other not in seen_attached:
                seen_attached.add(other)
                attached.append({**by_id[other], "edge_type": rel["edge_type"]})
        evidence = [ev for oid in members for ev in by_id.get(oid, {}).get("evidence", [])]
        with self._connect() as db:
            evidence = self._enrich_evidence(db, evidence)
        return {"canonical_id": canonical_id, "canonical_name": name,
                "members": [by_id[o] for o in members if o in by_id],
                "attached": attached, "evidence": evidence}

    def _element_texts(self, db, element_ids):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        # NOTE: assumes ids[0]'s element belongs to `notebook_id` (single-tenant;
        # element ids here always come from objects in the target notebook).
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC", (ids[0],)).fetchall()
        ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
        return texts, ordinal

    def _enrich_evidence(self, db, evidence):
        texts, _ = self._element_texts(db, [e.get("element_id") for e in evidence])
        out = []
        for e in evidence:
            out.append({"quoted_span": e.get("quoted_span", ""),
                        "source_title": e.get("source_title", "") or e.get("source_id", ""),
                        "element_text": texts.get(e.get("element_id", ""), e.get("quoted_span", ""))})
        return out

    def node_context(self, notebook_id, object_id):
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=? AND notebook_id=?", (object_id, notebook_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            obj_type = row["object_type"]
            payload = json.loads(row["payload"] or "{}")
            section = payload.get("section_path", "")
            occurrences = self._enrich_evidence(db, json.loads(row["evidence"] or "[]"))
            result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                      "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}
            if obj_type == "concept":
                drow = db.execute(
                    "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                    "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                if drow is not None:
                    dpay = json.loads(drow["payload"] or "{}")
                    den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
                    result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))
            if obj_type == "procedure":
                prows = db.execute(
                    "SELECT id, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated'", (notebook_id,)).fetchall()
                # v1: group steps by exact section_path (precedes edges are sparse).
                # Two distinct procedures sharing a section heading would merge —
                # acceptable for inspection.
                candidate_steps = []
                for pr in prows:
                    ppay = json.loads(pr["payload"] or "{}")
                    if ppay.get("section_path", "") != section:
                        continue
                    ev = json.loads(pr["evidence"] or "[]")
                    first_eid = ev[0].get("element_id") if ev else ""
                    candidate_steps.append((ppay.get("name", ""), first_eid))
                # Collect all first evidence element_ids, then call _element_texts once.
                all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                if all_step_first_eids:
                    texts, ordinal = self._element_texts(db, all_step_first_eids)
                else:
                    texts, ordinal = {}, {}
                steps = []
                for step_name, first_eid in candidate_steps:
                    steps.append({"name": step_name, "element_text": texts.get(first_eid, ""),
                                  "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
                steps.sort(key=lambda s: s["_ord"])
                for s in steps:
                    s.pop("_ord", None)
                result["steps"] = steps
            return result

    # test-only helper; later tasks may replace it with a public insert path
    def _test_insert_object(self, notebook_id: str, object_type: str, payload: dict, source_id: str = "") -> str:
        oid = f"ko-{uuid4().hex[:10]}"
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO knowledge_objects
                   (id, notebook_id, object_type, status, owner, payload, evidence,
                    source_candidate_id, source_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?, ?)""",
                (oid, notebook_id, object_type, json.dumps(payload, ensure_ascii=False), source_id, now, now),
            )
        return oid

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
        self._invalidate_unified_cache(row["notebook_id"])
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
            # KG node types: text lives in payload["name"]
            "claim": ("name", "statement"),
            "formula": ("name", "statement"),
            "procedure": ("name", "title"),
            "concept": ("name", "term", "definition"),
            "finding": ("name", "statement", "metric"),
            "principle": ("statement", "rationale"),
            "example": ("title", "problem"),
        }.get(object_type, ("name", "title", "statement", "term", "question"))
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
        self._invalidate_unified_cache(row["notebook_id"])
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
            knowledge_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated'",
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
        for ko in knowledge_rows:
            payload = json.loads(ko["payload"] or "{}")
            label = OBJECT_TYPE_LABELS.get(ko["object_type"], ko["object_type"])
            headline = self._knowledge_headline(ko["object_type"], payload)
            candidates.append(
                (label, notebook_id, headline, self._payload_join(payload), "", "")
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
        if not self.settings.embedder_configured:
            return None
        try:
            return self.embedder.embed_query(query[:2000])
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
        """KG-native ask: retrieves over the 4 KG object types (claim/formula/
        procedure/concept), performs 1-hop relation expansion, and synthesises
        a conclusion via the LLM (or deterministic fallback)."""
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        # Legacy `scenario` is accepted for frontend back-compat but no longer
        # woven into retrieval or the answer prompt.
        query = question

        # Resolve (create-or-append) the conversation and load prior turns. The
        # history shapes the answer wording only — retrieval still runs fresh
        # per question below.
        with self._connect() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question
            )
            history = self._conversation_history(db, conversation_id)

        with self._connect() as db:
            kg_objs: Dict[str, List[dict]] = {
                t: self._knowledge_objects(db, notebook_id, t) for t in _KG_TYPES
            }
            elements = self._gather_elements(db, notebook_id)
            query_vector = self._embed_query(query)
            all_kg = [o for objs in kg_objs.values() for o in objs]
            knowledge_vectors = self._knowledge_vectors(db, notebook_id, all_kg)

        element_vectors = self._element_vectors(elements)

        from app.services.retrieval import cosine_sims
        element_sims = cosine_sims(query_vector, element_vectors) if query_vector else None
        knowledge_sims = cosine_sims(query_vector, knowledge_vectors) if query_vector else None

        # Score each KG type (so the right per-type vectors are used), then pool
        # all hits and rank globally by relevance * type-weight (soft prior).
        # No fixed per-type quota: highly-relevant types can dominate the top-N.
        scored_all: List[RetrievedKnowledge] = []
        for t in _KG_TYPES:
            objs = kg_objs[t]
            if not objs:
                continue
            scored_all.extend(
                score_knowledge(
                    query, objs, t, query_vector, element_vectors, knowledge_vectors, None,
                    element_sims=element_sims, knowledge_sims=knowledge_sims,
                )
            )
        scored_all.sort(
            key=lambda it: it.score * _TYPE_WEIGHT.get(it.object_type, 0.5),
            reverse=True,
        )
        top_hits: List[RetrievedKnowledge] = scored_all[:self.settings.retrieval_top_n]

        # 1-hop expansion: for each top-hit object, pull its graph neighbours.
        hit_ids = {item.object_id for item in top_hits}
        relations = self.relations_for_notebook(notebook_id)
        neighbour_ids: set = set()
        for rel in relations:
            src, tgt = rel["source_object_id"], rel["target_object_id"]
            if src in hit_ids and tgt not in hit_ids:
                neighbour_ids.add(tgt)
            elif tgt in hit_ids and src not in hit_ids:
                neighbour_ids.add(src)

        # Fetch neighbour objects if any.
        neighbour_objs: List[dict] = []
        if neighbour_ids:
            with self._connect() as db:
                placeholders = ",".join("?" for _ in neighbour_ids)
                rows = db.execute(
                    f"SELECT * FROM knowledge_objects WHERE id IN ({placeholders})",
                    list(neighbour_ids),
                ).fetchall()
            for row in rows:
                keys = row.keys()
                neighbour_objs.append({
                    "id": row["id"],
                    "object_type": row["object_type"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": [
                        Evidence(**item)
                        for item in json.loads(row["evidence"] or "[]")
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": row["last_reviewed"] if "last_reviewed" in keys else "",
                })

        # Build related_knowledge from hits + neighbours (hits first, no dups).
        registry = self.effective_schemas()
        seen_ids: set = set()
        related_knowledge: List[KnowledgeRecord] = []

        for item in top_hits:
            if item.object_id in seen_ids:
                continue
            seen_ids.add(item.object_id)
            obj = {
                "id": item.object_id,
                "payload": item.payload,
                "status": item.status,
                "owner": getattr(item, "owner", ""),
                "last_reviewed": getattr(item, "last_reviewed", ""),
                "evidence": item.evidence,
            }
            related_knowledge.append(
                self._knowledge_record(item.object_type, obj, registry.get(item.object_type))
            )

        for nobj in neighbour_objs:
            if nobj["id"] in seen_ids:
                continue
            seen_ids.add(nobj["id"])
            related_knowledge.append(
                self._knowledge_record(
                    nobj["object_type"], nobj, registry.get(nobj["object_type"])
                )
            )

        related_knowledge = related_knowledge[:12]

        # Citations from top hits.
        valid_element_ids = {element["element_id"] for element in elements}
        citations: List[Citation] = []
        citations.extend(self._citations_from(top_hits, valid_element_ids, "KG evidence"))

        # related_knowledge is always derived from top_hits (hits + 1-hop
        # neighbours), so top_hits being non-empty implies related_knowledge is
        # non-empty; the converse is also true — no need to check both.
        has_knowledge = bool(top_hits)
        llm_mode = "deterministic"
        conclusion = ""
        answer = ""
        grounded = False
        anchors: List[AnswerAnchor] = []

        # When an LLM is configured we always synthesise — grounding on KG hits
        # where they exist, and reasoning from general knowledge otherwise
        # (ungrounded). Never dead-ends with the canned refusal.
        if self.llm_client.configured:
            try:
                answer, grounded, llm_mode, anchors = self._answer_kg(
                    notebook_id, question, top_hits, history
                )
            except Exception:
                answer, grounded, anchors, llm_mode = "", False, [], "deterministic"

        if answer:
            # Back-compat `conclusion` = answer with provenance markers stripped.
            conclusion = _MARKER_RE.sub("", answer).strip()
        else:
            # No LLM configured (or it failed): deterministic fallback.
            llm_mode = "deterministic"
            if has_knowledge:
                n = len(top_hits)
                conclusion = (
                    f"Found {n} relevant KG knowledge object(s) for this question."
                    if n
                    else "Relevant notebook knowledge was retrieved for this question."
                )
            else:
                conclusion = (
                    "The notebook does not yet contain approved knowledge that "
                    "matches this question. Upload and review sources to build coverage."
                )

        response = AskResponse(
            answer_id="",
            conclusion=conclusion,
            answer=answer,
            grounded=grounded,
            anchors=anchors,
            related_knowledge=related_knowledge,
            citations=citations,
            llm_mode=llm_mode,
            conversation_id=conversation_id,
        )
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id
        )
        return response

    def _concept_cluster_id(self, notebook_id: str, object_id: str) -> str:
        """Canonical unified-cluster id for a concept `object_id`, reusing the
        same `concept_clusters` membership map `concept_detail` relies on
        (`cluster_map` -> {member_object_id: canonical_id}). When clustering is
        not populated for the notebook (no cluster row for this object), fall
        back to `object_id` so dedup degrades gracefully (no merge, no crash)."""
        return self.cluster_map(notebook_id).get(object_id, object_id)

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge]) -> tuple:
        """Build the id-tagged enriched context block + id_map for the answer
        LLM. Each surviving hit gets a stable `k{i}` id; enrichment (definition /
        first-occurrence snippet / procedure steps) is pulled via node_context.
        Concept hits belonging to the same unified cluster (D4) are collapsed —
        the first (highest-scored) per cluster is kept, later duplicates dropped.
        Returns (context_block_str, id_map)."""
        lines, id_map = [], {}
        seen_concept_clusters: set = set()
        i = 0
        for hit in top_hits:
            if hit.object_type == "concept":
                cid = self._concept_cluster_id(notebook_id, hit.object_id)
                if cid in seen_concept_clusters:
                    continue
                seen_concept_clusters.add(cid)
            try:
                ctx = self.node_context(notebook_id, hit.object_id)
            except KeyError:
                continue
            i += 1
            key = f"k{i}"
            name = str(hit.payload.get("name", "")).strip()
            occ = ctx.get("occurrences") or []
            snippet = occ[0].get("element_text") if occ else ""
            definition = ctx.get("definition") or snippet
            extra = f" — def: {definition[:200]}" if definition else ""
            if ctx.get("steps"):
                extra += "; steps: " + " -> ".join(
                    s.get("name", "") for s in ctx["steps"][:8]
                )
            lines.append(f"{key}: [{hit.object_type}] {name}{extra}")
            id_map[key] = {
                "object_id": hit.object_id, "object_type": hit.object_type,
                "name": name, "definition": definition, "snippet": snippet,
                "source_title": (occ[0].get("source_title", "") if occ else ""),
                "location_label": (occ[0].get("section_path", "") if occ else ""),
            }
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _answer_kg(
        self,
        notebook_id: str,
        question: str,
        top_hits: List[RetrievedKnowledge],
        history: str = "",
    ) -> tuple:
        """Synthesise a (possibly reasoned) answer from KG hits using the LLM.
        Returns (answer_text, grounded, llm_mode, anchors). grounded requires
        both the LLM's self-report and at least one retrieved hit. `history`
        (prior conversation turns) shapes the wording but not the retrieval."""
        context_block, id_map = self._answer_context(notebook_id, top_hits)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        grounded = bool(data.get("grounded", False)) and bool(top_hits)
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, grounded, ("grounded" if grounded else "ungrounded"), anchors

    def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
        """Resolve the `[k_i]` markers present in `answer` into AnswerAnchor
        objects (deduped, in first-seen order). Markers not in `id_map` and
        items never cited are dropped."""
        from app.models.schemas import AnswerAnchor
        cited = []
        seen = set()
        for key in _MARKER_RE.findall(answer or ""):
            if key in seen or key not in id_map:
                continue
            seen.add(key)
            ctx = id_map[key]
            name = str(ctx.get("name", ""))
            cited.append(AnswerAnchor(
                key=key, object_id=ctx["object_id"], object_type=ctx["object_type"],
                label=(name[:40] or key), name=name,
                definition=ctx.get("definition"), snippet=ctx.get("snippet"),
                source_title=ctx.get("source_title", ""), location_label=ctx.get("location_label", ""),
            ))
        return cited

    def _save_answer(
        self,
        notebook_id: str,
        question: str,
        response: AskResponse,
        conversation_id: Optional[str] = None,
    ) -> str:
        answer_id = f"ans-{uuid4().hex[:10]}"
        now = _now()
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self._connect() as db:
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    answer_id,
                    notebook_id,
                    question,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    conversation_id,
                ),
            )
        return answer_id

    def _ensure_conversation(
        self, db, notebook_id: str, conversation_id: Optional[str], question: str
    ) -> str:
        """Return the conversation id for this turn: append to an existing
        conversation in this notebook (touching `updated_at`), or create a new
        one (id `conv-<hex>`, title from the first question)."""
        now = _now()
        if conversation_id:
            row = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND notebook_id = ?",
                (conversation_id, notebook_id),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                return conversation_id
        new_id = f"conv-{uuid4().hex[:10]}"
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, notebook_id, question[:60], self.current_user().id, now, now),
        )
        return new_id

    def _conversation_history(self, db, conversation_id: str, limit: int = 5) -> str:
        """Build the prior-turns history block (oldest->newest, last `limit`
        turns) from stored answer payloads. Uses each turn's `conclusion`
        (provenance markers already stripped). Returns "" when no prior turns."""
        rows = db.execute(
            "SELECT question, payload FROM answers WHERE conversation_id = ? "
            "ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
        rows = rows[-limit:]
        lines = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            conclusion = str(payload.get("conclusion", "")).strip()
            lines.append(f"User: {row['question']}\nAssistant: {conclusion}")
        return "\n".join(lines)

    def get_conversation(self, conversation_id: str) -> "ConversationDetail":
        """Rebuild a ConversationDetail from the conversations row + its answer
        turns. Raises KeyError if the conversation does not exist."""
        from app.models.schemas import (
            ConversationDetail,
            ConversationTurn,
        )
        with self._connect() as db:
            conv = db.execute(
                "SELECT id, notebook_id, title, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            rows = db.execute(
                "SELECT id, question, payload, created_at FROM answers "
                "WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        turns = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            turns.append(
                ConversationTurn(
                    answer_id=row["id"],
                    question=row["question"],
                    response=AskResponse(**payload),
                    created_at=row["created_at"],
                )
            )
        return ConversationDetail(
            id=conv["id"],
            notebook_id=conv["notebook_id"],
            title=conv["title"] or "",
            updated_at=conv["updated_at"] or "",
            turn_count=len(turns),
            turns=turns,
        )

    def list_conversations(self, notebook_id: str) -> "List[ConversationSummary]":
        """List conversations for a notebook (most-recently-updated first) with
        a per-conversation turn count. Raises KeyError if the notebook is gone."""
        from app.models.schemas import ConversationSummary
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.id, c.notebook_id, c.title, c.updated_at, "
                "(SELECT COUNT(*) FROM answers a WHERE a.conversation_id = c.id) AS turn_count "
                "FROM conversations c WHERE c.notebook_id = ? AND c.created_by = ? "
                "ORDER BY c.updated_at DESC",
                (notebook_id, self.current_user().id),
            ).fetchall()
        return [
            ConversationSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"] or "",
                updated_at=row["updated_at"] or "",
                turn_count=row["turn_count"],
            )
            for row in rows
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, _now(), conversation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        with self._connect() as db:
            cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            if cur.rowcount == 0:
                raise KeyError(conversation_id)
            db.execute("DELETE FROM answers WHERE conversation_id=?", (conversation_id,))

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

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        """Answer-quality + curation + coverage metrics for a notebook (§16)."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            answers_total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM answers WHERE notebook_id = ?", (notebook_id,)
                ).fetchone()["c"]
            )
            useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            not_useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'not_useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            low_rated = [
                row["question"]
                for row in db.execute(
                    "SELECT DISTINCT a.question FROM feedback f "
                    "JOIN answers a ON a.id = f.answer_id "
                    "WHERE f.notebook_id = ? AND f.rating = 'not_useful' "
                    "ORDER BY f.created_at DESC LIMIT 10",
                    (notebook_id,),
                ).fetchall()
            ]
            candidate_counts = {
                row["status"]: int(row["c"])
                for row in db.execute(
                    "SELECT status, COUNT(*) AS c FROM extraction_candidates "
                    "WHERE notebook_id = ? GROUP BY status",
                    (notebook_id,),
                ).fetchall()
            }
            knowledge_counts = {
                row["object_type"]: int(row["c"])
                for row in db.execute(
                    "SELECT object_type, COUNT(*) AS c FROM knowledge_objects "
                    "WHERE notebook_id = ? AND status != 'deprecated' GROUP BY object_type",
                    (notebook_id,),
                ).fetchall()
            }
            source_status_counts = {
                row["parse_status"]: int(row["c"])
                for row in db.execute(
                    "SELECT parse_status, COUNT(*) AS c FROM sources "
                    "WHERE notebook_id = ? GROUP BY parse_status",
                    (notebook_id,),
                ).fetchall()
            }
        rated = useful + not_useful
        return NotebookAnalytics(
            answers_total=answers_total,
            feedback_useful=useful,
            feedback_not_useful=not_useful,
            usefulness_rate=round(useful / rated, 3) if rated else 0.0,
            low_rated_questions=low_rated,
            candidate_counts=candidate_counts,
            knowledge_counts=knowledge_counts,
            source_status_counts=source_status_counts,
        )

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
        """Bind a quoted span to the best matching source element (substring check)."""
        if not quoted_span.strip() or not article_elements:
            return []
        needle = " ".join((quoted_span or "").split()).lower()
        if len(needle) < 6:
            return []
        for element in article_elements:
            haystack = " ".join((element.text or "").split()).lower()
            if needle in haystack or haystack in needle:
                return [Evidence(
                    source_id=element.source_id,
                    source_title=article_source_title,
                    element_id=element.id,
                    element_type=element.element_type,
                    location_label=element.location_label,
                    quoted_span=quoted_span.strip()[:400],
                    confidence=0.65,
                )]
        return []

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
            doc_type=row["doc_type"] if "doc_type" in row.keys() else "",
            extraction_warning=self._extraction_warning(db, row["id"]),
        )

    def _extraction_warning(self, db: sqlite3.Connection, source_id: str) -> Optional[str]:
        """Surface a user-facing warning when the latest KG extraction left
        network-failed windows (degraded run). Parsed from the run's
        `windows_failed=N/T` token rather than stored on the source row."""
        run = db.execute(
            "SELECT error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
        if run is None:
            return None
        m = re.search(r"windows_failed=(\d+)/(\d+)", run["error_message"] or "")
        if not m:
            return None
        fw = int(m.group(1))
        if fw <= 0:
            return None
        tw = int(m.group(2))
        return f"部分内容因网络问题未抽取（{fw}/{tw} 段失败），建议重新上传或重试抽取。"

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
        if not file_path:
            return
        path = Path(file_path)
        if path.exists() and path.is_file():
            path.unlink()
        notebook_dir = path.parent
        if notebook_dir.exists() and not any(notebook_dir.iterdir()):
            shutil.rmtree(notebook_dir, ignore_errors=True)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _citation(label: str, evidence: Evidence) -> Citation:
    return Citation(
        label=label,
        source_id=evidence.source_id,
        element_id=evidence.element_id,
        location_label=evidence.location_label,
        quoted_span=evidence.quoted_span,
    )


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
