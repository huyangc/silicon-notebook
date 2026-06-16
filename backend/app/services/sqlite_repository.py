from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
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
    Citation,
    DerivedRuleCandidate,
    DuplicateGroup,
    Evidence,
    FeedbackRequest,
    FeedbackResponse,
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
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    RuleCard,
    SearchHit,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)
from app.services import kg_ingest
from app.services.vector_cache import VectorCache
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
    FOLLOWUP_REWRITE_SCHEMA_HINT,
    NOTEBOOK_META_SCHEMA_HINT,
    RERANK_SCHEMA_HINT,
    SCHEMA_INDUCTION_HINT,
    answer_prompt,
    article_prompt,
    followup_rewrite_prompt,
    notebook_description_prompt,
    notebook_meta_prompt,
    rerank_prompt,
    schema_induction_prompt,
)
from app.services.repository import UploadedSourceFile
from app.services.retrieval import (
    RetrievedKnowledge,
    RetrievedElement,
    W_KEYWORD,
    W_SEMANTIC,
    _TYPE_WEIGHT,
    _fuse,
    _payload_text,
    bm25_scores,
    cosine,
    keyword_score,
    rrf_fuse,
    score_knowledge,
    score_elements,
    type_weight,
    tier_weight,
    is_process_query,
    ensure_procedure_quota,
    classify_evidence,
)


# Knowledge statuses that may be surfaced in answers/retrieval (§12 governance).
# 'deprecated' is excluded; 'conflict' is retrieved but flagged elsewhere.
USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")

# Default notebook-name placeholders the frontend creates; name auto-fill only
# overwrites these (never a user-chosen name).
_DEFAULT_NOTEBOOK_NAMES = {"", "未命名笔记本", "Untitled notebook"}

KNOWLEDGE_STATUSES = ("approved", "reviewed", "deprecated", "conflict", "project_specific")

# KG object types retrieved during ask(), in priority order.
_KG_TYPES = ("claim", "formula", "procedure", "concept")


# Matches the `[k1]` provenance markers the answer LLM appends to grounded
# sentences; used to resolve markers -> citation anchors and to strip them when
# deriving the back-compat `conclusion` string.
_MARKER_RE = re.compile(r"\[(k\d+)\]")

# Tolerant variant that ALSO matches malformed markers with internal whitespace
# (e.g. `[ k1]`). Used only to scrub citation-shaped tokens that did NOT bind to
# a real anchor, so no fabricated/malformed marker reaches the user. Kept
# separate from _MARKER_RE so the strict marker->anchor resolution is unchanged.
_LOOSE_MARKER_RE = re.compile(r"\[\s*k\d+\s*\]")


def _strip_unbound_markers(answer: str, bound_keys: set) -> str:
    """Normalise the `[k…]`-shaped tokens in `answer` against `bound_keys` (the
    keys that actually resolved to an anchor):
      - key in bound_keys  → rewrite to the canonical `[key]` form (repairs a
        malformed spaced `[ k1]` so it reads as a clean citation, not a fabricated
        one, while still pointing at its real anchor);
      - key not in bound_keys → drop the token (out-of-map ids like `[k99]`, or a
        spaced id with no anchor).
    Collapses the double space a removed mid-sentence marker would leave behind."""
    def _sub(m: re.Match) -> str:
        key = m.group(0).strip("[]").strip()
        return f"[{key}]" if key in bound_keys else ""
    cleaned = _LOOSE_MARKER_RE.sub(_sub, answer or "")
    # A stripped marker between words leaves "word  word"; normalise to one space
    # without disturbing newlines / other whitespace runs the model intended.
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


class SQLiteRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root_dir = Path(__file__).resolve().parents[3]
        self.db_path = self._resolve_path(settings.sqlite_path)
        self.storage_dir = self._resolve_path(settings.storage_dir)
        self.llm_client = OpenAICompatibleClient(settings)
        # 推理搜索专用 client：配齐 REASONING_LLM_* → 独立模型实例；否则 None，
        # 由 reasoning_llm_client 属性动态回退到 self.llm_client。
        self._reasoning_llm_client = (
            OpenAICompatibleClient(
                settings,
                base_url=settings.reasoning_llm_base_url,
                api_key=settings.reasoning_llm_api_key,
                model=settings.reasoning_llm_model,
            )
            if settings.reasoning_llm_configured
            else None
        )
        # 查询改写/扩展专用 client(DeepSeek v4-fast 之类快模型)：设了 REWRITE_LLM_MODEL
        # 即启用(base_url/api_key 缺省复用主端点)；否则 None，由 rewrite_llm_client 回退。
        self._rewrite_llm_client = (
            OpenAICompatibleClient(
                settings,
                base_url=settings.rewrite_llm_base_url or settings.openai_compat_base_url,
                api_key=settings.rewrite_llm_api_key or settings.openai_compat_api_key,
                model=settings.rewrite_llm_model,
            )
            if settings.rewrite_llm_configured
            else None
        )
        from app.services.embedding import make_embedder
        self.embedder = make_embedder(self.settings)
        self.mineru_client = MinerUClient(settings)
        self.event_log = EventLogger(settings, channel="events")
        if settings.reasoning_llm_partially_configured:
            self.event_log.logger.warning(
                "REASONING_LLM_* 仅部分配置(base_url/api_key/model 需全填)，"
                "推理搜索将回退到全局 OPENAI_COMPAT_* 模型。")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._unified_cache: Dict[Any, Any] = {}
        self._vector_cache = VectorCache()
        self._write_lock = threading.RLock()
        self._migrate()
        self._seed()

    @property
    def reasoning_llm_client(self):
        """推理路径专用 LLM client。配齐 REASONING_LLM_* → 独立模型；否则动态回退到
        当前 self.llm_client（含测试运行时替换的 fake），未配置时与全局行为完全一致。"""
        if self._reasoning_llm_client is not None:
            return self._reasoning_llm_client
        return self.llm_client

    @property
    def rewrite_llm_client(self):
        """查询改写/扩展专用 LLM client(快模型)。设了 REWRITE_LLM_MODEL → 独立实例；
        否则动态回退到当前 self.llm_client(含测试替换的 fake)，与全局行为一致。"""
        if self._rewrite_llm_client is not None:
            return self._rewrite_llm_client
        return self.llm_client

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root_dir / path
        return path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.settings.db_busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.settings.db_busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA cache_size = -65536")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("PRAGMA mmap_size = 268435456")
        return connection

    @contextmanager
    def _write(self):
        """串行化写事务：进程内同一时刻只有一个写者进 SQLite，并发写线程在
        Python 层排队而非裸抢 SQLite 写锁（后者即 `database is locked` 的根因）。
        纯读保持用 _connect()，不受影响（WAL 支持并发读）。"""
        with self._write_lock:
            with self._connect() as db:
                yield db

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

                CREATE TABLE IF NOT EXISTS chunks (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  text TEXT NOT NULL,
                  section_path TEXT NOT NULL DEFAULT '',
                  element_ids TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_nb ON chunks(notebook_id);

                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                  chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_nb ON chunk_embeddings(notebook_id);

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
                  object_type TEXT NOT NULL DEFAULT 'concept',
                  canonical_description TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS promotion_candidates (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  object_id TEXT NOT NULL,
                  object_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'proposed',
                  -- status values: proposed | under_review | approved | rejected
                  reason TEXT NOT NULL DEFAULT '',
                  reviewed_by TEXT NOT NULL DEFAULT '',
                  base_match_id TEXT NOT NULL DEFAULT '',
                  -- canonical_id in the base corpus if dedup found a match
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_promotion_status ON promotion_candidates(status);
                CREATE INDEX IF NOT EXISTS idx_promotion_nb ON promotion_candidates(notebook_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_object ON promotion_candidates(object_id)
                  WHERE status NOT IN ('approved', 'rejected');

                CREATE TABLE IF NOT EXISTS unified_kg_state (
                  notebook_id TEXT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                  dirty INTEGER NOT NULL DEFAULT 0,
                  last_rebuild_at TEXT NOT NULL DEFAULT '',
                  object_count INTEGER NOT NULL DEFAULT 0,
                  relation_count INTEGER NOT NULL DEFAULT 0,
                  cluster_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS concept_whitelist (
                  term TEXT PRIMARY KEY,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sources_notebook_status ON sources(notebook_id, status);
                CREATE INDEX IF NOT EXISTS idx_sources_notebook_created ON sources(notebook_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_source_elements_source ON source_elements(source_id);
                CREATE INDEX IF NOT EXISTS idx_source_elements_source_created ON source_elements(source_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_type_status ON knowledge_objects(notebook_id, object_type, status);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_status ON knowledge_objects(notebook_id, status);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_source ON knowledge_objects(source_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source ON knowledge_relations(notebook_id, source_object_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_target ON knowledge_relations(notebook_id, target_object_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_source ON knowledge_relations(source_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_nb ON knowledge_embeddings(notebook_id);
                CREATE INDEX IF NOT EXISTS idx_element_embeddings_nb ON element_embeddings(notebook_id);

                CREATE TABLE IF NOT EXISTS communities (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  level INTEGER NOT NULL DEFAULT 0,
                  member_ids TEXT NOT NULL,
                  size INTEGER NOT NULL,
                  title TEXT NOT NULL DEFAULT '',
                  summary TEXT NOT NULL DEFAULT '',
                  findings TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_communities_nb_level ON communities(notebook_id, level);
                DROP INDEX IF EXISTS idx_communities_nb;
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
            # tier column: 'personal' (default) or 'base' (analog-textbook KG).
            # Existing notebooks default to 'personal'; the base KG is marked via
            # mark_notebook_base(). PRAGMA guard keeps this idempotent.
            if "tier" not in nb_cols:
                db.execute("ALTER TABLE notebooks ADD COLUMN tier TEXT NOT NULL DEFAULT 'personal'")
            # Per-source document type drives schema/profile selection at extraction.
            src_cols = {r["name"] for r in db.execute("PRAGMA table_info(sources)").fetchall()}
            if "doc_type" not in src_cols:
                db.execute("ALTER TABLE sources ADD COLUMN doc_type TEXT NOT NULL DEFAULT ''")
            # LLM review metadata columns for concept_merge_candidates.
            cm_cols = {r["name"] for r in db.execute("PRAGMA table_info(concept_merge_candidates)").fetchall()}
            if "confidence" not in cm_cols:
                db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN confidence REAL NOT NULL DEFAULT 0")
            if "rationale" not in cm_cols:
                db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN rationale TEXT NOT NULL DEFAULT ''")
            if "reviewed_by" not in cm_cols:
                db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT ''")
            # object_type column for concept_clusters (per-type isolation).
            cc_cols = {r["name"] for r in db.execute("PRAGMA table_info(concept_clusters)").fetchall()}
            if "object_type" not in cc_cols:
                db.execute("ALTER TABLE concept_clusters ADD COLUMN object_type TEXT NOT NULL DEFAULT 'concept'")
            if "canonical_description" not in cc_cols:
                db.execute("ALTER TABLE concept_clusters ADD COLUMN canonical_description TEXT NOT NULL DEFAULT ''")
            # Community report columns (title/summary/findings).
            comm_cols = {r["name"] for r in db.execute("PRAGMA table_info(communities)").fetchall()}
            for col, default in (("title", "''"), ("summary", "''"), ("findings", "'[]'")):
                if col not in comm_cols:
                    db.execute(f"ALTER TABLE communities ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
            # Track E: edge review_status column (curation feedback loop).
            kr_cols = {r["name"] for r in db.execute(
                "PRAGMA table_info(knowledge_relations)").fetchall()}
            if "review_status" not in kr_cols:
                db.execute(
                    "ALTER TABLE knowledge_relations "
                    "ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
                )
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
            from app.services.kg.filters import _norm as _wl_norm
            builtin_whitelist = [
                "VCO", "PLL", "LNA", "BJT", "MOS", "MOSFET", "CMOS", "FET",
                "NMOS", "PMOS", "BiCMOS", "JFET", "op amp", "ADC", "DAC",
                "CMRR", "PSRR", "ESD", "PVT",
                "AC", "DC", "IC", "RF", "IF", "LO",
            ]
            for term in builtin_whitelist:
                db.execute(
                    "INSERT OR IGNORE INTO concept_whitelist (term, note, created_at) VALUES (?, 'builtin', ?)",
                    (_wl_norm(term), now),
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

    def _has_kg(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    def _any_base_notebook_has_kg(self, db: "sqlite3.Connection | None" = None) -> bool:
        """True iff some tier='base' notebook has any knowledge_objects."""
        sql = ("SELECT EXISTS(SELECT 1 FROM knowledge_objects ko "
               "JOIN notebooks nb ON nb.id = ko.notebook_id WHERE nb.tier = 'base')")
        if db is not None:
            return bool(db.execute(sql).fetchone()[0])
        with self._connect() as conn:
            return bool(conn.execute(sql).fetchone()[0])

    def _clear_source_extraction_state(
        self,
        db: sqlite3.Connection,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        # KG writes directly to knowledge_objects; find stale objects by evidence source_id.
        stale_knowledge_ids: List[str] = []
        knowledge_rows = db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        for row in knowledge_rows:
            try:
                evidence_items = json.loads(row["evidence"] or "[]")
            except json.JSONDecodeError:
                evidence_items = []
            if any(item.get("source_id") == source_id for item in evidence_items if isinstance(item, dict)):
                stale_knowledge_ids.append(row["id"])

        if stale_knowledge_ids:
            placeholders = ",".join("?" for _ in stale_knowledge_ids)
            db.execute(
                f"DELETE FROM knowledge_embeddings WHERE object_id IN ({placeholders})",
                stale_knowledge_ids,
            )
            db.execute(
                f"DELETE FROM knowledge_objects WHERE id IN ({placeholders})",
                stale_knowledge_ids,
            )
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

        with self._write() as db:
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
            with self._write() as db:
                db.execute(
                    f"UPDATE notebooks SET {', '.join(updates)} WHERE id = ?",
                    values,
                )
        return self.get_notebook(notebook_id)

    def mark_notebook_base(self, notebook_id: str) -> None:
        """Mark a notebook as the authoritative base KG (tier='base').
        Idempotent; raises KeyError if the notebook does not exist."""
        self.get_notebook(notebook_id)  # raises KeyError if missing
        with self._write() as db:
            db.execute(
                "UPDATE notebooks SET tier='base', updated_at=? WHERE id=?",
                (_now(), notebook_id),
            )

    def set_notebook_personal(self, notebook_id: str) -> None:
        """Reset a notebook back to the personal tier (tier='personal').
        Symmetric inverse of mark_notebook_base; idempotent.
        Raises KeyError if the notebook does not exist."""
        self.get_notebook(notebook_id)  # raises KeyError if missing
        with self._write() as db:
            db.execute(
                "UPDATE notebooks SET tier='personal', updated_at=? WHERE id=?",
                (_now(), notebook_id),
            )

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        with self._write() as db:
            source_rows = db.execute(
                "SELECT file_path FROM sources WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            # knowledge_embeddings has no FK to notebooks (see DDL ~line 300), so
            # deleting the notebooks row does NOT cascade to it. Delete it here so
            # every public delete caller leaves zero orphan embedding rows.
            # (element_embeddings DOES cascade transitively via
            # source_elements -> sources -> notebooks, so it needs no explicit delete.)
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
                (notebook_id,),
            )
            db.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))
        for row in source_rows:
            self._delete_file(row["file_path"])

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        """Delete all KG artifacts for a notebook (objects, relations, clusters,
        merge candidates, embeddings, extraction runs, unified state) while KEEPING
        sources and source_elements so it can be re-extracted from already-parsed
        elements. Returns {table: rows_deleted}."""
        self.get_notebook(notebook_id)
        counts: dict = {}
        with self._write() as db:
            for table in ("knowledge_objects", "knowledge_relations", "concept_clusters",
                          "concept_merge_candidates", "knowledge_embeddings",
                          "extraction_runs", "unified_kg_state"):
                cur = db.execute(f"DELETE FROM {table} WHERE notebook_id = ?", (notebook_id,))
                counts[table] = cur.rowcount
        self._invalidate_unified_cache(notebook_id)
        return counts

    def eval_insert_source_for_test(
        self, nb_id: str, name: str, text: str, tmpdir: str
    ) -> str:
        """Insert a parsed source directly for eval speed tests.
        Uses the repo's write path; avoids raw _connect access in eval scripts."""
        import pathlib
        import uuid
        from app.services.kg.parsing import parse_elements
        f = pathlib.Path(tmpdir) / f"{name}.md"
        f.write_text(text, encoding="utf-8")
        sid = f"src-{uuid.uuid4().hex[:10]}"
        now = _now()
        els = parse_elements(text, source_file=str(f))
        with self._write() as db:
            db.execute(
                """INSERT INTO sources
                   (id, notebook_id, title, source_type, status, parse_status,
                    file_name, file_path, file_size, file_hash, summary, doc_type,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed', ?, ?, 0, '', '', ?, ?, ?)""",
                (sid, nb_id, name, f"{name}.md", str(f), "textbook", now, now))
            for el in els:
                db.execute(
                    """INSERT INTO source_elements
                       (id, source_id, element_type, location_label, text, metadata, created_at)
                       VALUES (?, ?, ?, ?, ?, '{}', ?)""",
                    (f"el-{uuid.uuid4().hex[:10]}", sid, el.type,
                     f"L{el.line_start}-{el.line_end}", el.text, now))
        return sid

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
        with self._write() as db:
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
            with self._write() as db:
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
        with self._write() as db:
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

    def _notebook_has_kg(self, notebook_id: str) -> bool:
        """True iff this notebook has any knowledge_objects row."""
        with self._connect() as db:
            row = db.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
                (notebook_id,),
            ).fetchone()
        return bool(row[0])

    def _should_extract_kg(self, notebook_id: str) -> bool:
        """摄取期是否抽 KG:全局开关开,或该 notebook 已有 KG(续抽保持完整)。"""
        return self.settings.kg_auto_extract or self._notebook_has_kg(notebook_id)

    def build_notebook_kg(self, notebook_id: str) -> dict:
        """按需对该 notebook 下"尚无 KG"的 source 逐个抽取(复用 _run_extraction)。
        幂等:已有 knowledge_objects 的 source 跳过。无 LLM → RuntimeError。
        单 source 失败隔离,不连累其余、不回退其终态,错误入 event log。"""
        self.get_notebook(notebook_id)  # KeyError if missing
        if not getattr(self.llm_client, "configured", False):
            raise RuntimeError("LLM not configured; cannot build KG")
        with self._connect() as db:
            src_ids = [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id = ?", (notebook_id,)).fetchall()]
            # source_id is NOT NULL DEFAULT '' — use != '' to find sources that truly have KG
            kgful = {r["source_id"] for r in db.execute(
                "SELECT DISTINCT source_id FROM knowledge_objects "
                "WHERE notebook_id = ? AND source_id != ''", (notebook_id,)).fetchall()}
        targets = [sid for sid in src_ids if sid not in kgful]
        done, failed = [], []
        for sid in targets:
            try:
                self._set_source_status(sid, "extracting")
                self._run_extraction(sid)
                self._set_source_status(sid, "extracted")
                done.append(sid)
            except Exception:  # noqa: BLE001 — 隔离单 source 失败
                failed.append(sid)
                self.event_log.logger.exception("build_notebook_kg failed for %s", sid)
        try:
            self._mark_unified_kg_dirty(notebook_id)
        except Exception:
            self.event_log.logger.exception("unified-KG dirty mark failed for %s", notebook_id)
        return {"built": done, "failed": failed, "skipped": sorted(kgful)}

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
            with self._write() as db:
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

            # chunk-native 基础: 合并 element 成检索 chunk(纯写库无网络, query 立即可用)。
            # best-effort: 失败不阻塞既有 parse->extract 流水线。
            try:
                self._build_chunks_for_source(source_id)
            except Exception:
                self.event_log.logger.exception("chunk build failed for %s", source_id)

            # Element embedding (best-effort semantic recall) runs in the BACKGROUND,
            # concurrent with KG extraction, so a large doc's slow embed never blocks
            # the KG result. 'extracted'/green below is gated on EXTRACTION only.
            import threading

            embed_started = time.perf_counter()
            stage("embed", "start", embed_started)

            def _embed_bg() -> None:
                try:
                    self._embed_source(source_id)
                    self._embed_chunks_for_source(source_id)   # chunk 向量后台补, 不阻塞流水线
                    stage("embed", "done", embed_started)
                except Exception as exc:  # noqa: BLE001 — best-effort; never fail the pipeline
                    stage("embed", "error", embed_started,
                          error=f"{type(exc).__name__}: {exc}")
                    self.event_log.logger.exception(
                        "background embed failed for %s", source_id
                    )

            embed_thread = threading.Thread(
                target=_embed_bg, name=f"embed-{source_id}", daemon=True
            )
            embed_thread.start()

            if self._should_extract_kg(notebook_id):
                self._set_source_status(source_id, "extracting")
                t = time.perf_counter()
                stage("extract", "start", t)
                self._run_extraction(source_id)
                stage("extract", "done", t)
                try:
                    self._mark_unified_kg_dirty(source.notebook_id)
                except Exception:
                    self.event_log.logger.exception("unified-KG dirty mark failed for source %s", source_id)
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
            # Auto-fill notebook name/description from sources (only while name is a
            # default placeholder / purpose is still auto). Persist BEFORE marking the
            # source 'extracted' so the frontend's extracted-triggered refetch shows
            # the fresh name/description live. Best-effort: never fail the pipeline.
            try:
                self._augment_notebook_meta(source.notebook_id, pending_source_id=source_id)
            except Exception:
                self.event_log.logger.exception(
                    "notebook meta augmentation failed for %s", source_id
                )
            self._set_source_status(
                source_id,
                "extracted",
                error_message=empty_hint or fallback_hint,
            )
            # KG ('extracted'/green) set above; wait for the background element
            # embedding to finish before declaring the whole pipeline done.
            embed_thread.join()
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

    def _augment_notebook_meta(self, notebook_id: str, pending_source_id: str = "") -> None:
        """Auto-fill the notebook name (while it is still a default placeholder)
        and/or description (while purpose_auto=1) from its processed sources.
        No-op for fields the user has set. `pending_source_id` is the source whose
        pipeline is finishing (still 'extracting'); it is counted so the FIRST
        source already produces a name/description."""
        with self._connect() as db:
            nb = db.execute(
                "SELECT name, purpose_auto FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
            if nb is None:
                return
            cur_name = (nb["name"] or "").strip()
            need_name = cur_name in _DEFAULT_NOTEBOOK_NAMES
            need_desc = ("purpose_auto" in nb.keys() and nb["purpose_auto"] == 1)
            if not (need_name or need_desc):
                return
            rows = db.execute(
                "SELECT title, doc_type, summary FROM sources "
                "WHERE notebook_id = ? AND (status = 'extracted' OR id = ?) "
                "ORDER BY created_at ASC",
                (notebook_id, pending_source_id),
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

        name_val, desc_val = "", ""
        if self.llm_client.configured:
            block = "\n".join(
                f"- {r['title']} "
                f"[{(PROFILES.get(_normalize_doc_type(r['doc_type'])) or get_profile('academic_paper')).label}] "
                f"{(r['summary'] or '')[:200]}"
                for r in rows[:20]
            )
            try:
                raw = self.llm_client.chat_json(
                    [{"role": "user", "content": notebook_meta_prompt(block)}],
                    NOTEBOOK_META_SCHEMA_HINT,
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    name_val = str(parsed.get("name", "")).strip()
                    desc_val = str(parsed.get("description", "")).strip()
            except Exception:
                name_val, desc_val = "", ""

        # Deterministic fallbacks (LLM off or failed).
        if need_desc and not desc_val:
            shown = "、".join(titles[:5]) + ("等" if len(titles) > 5 else "")
            desc_val = f"本笔记本收录了 {len(titles)} 个来源：{shown}。"
            if labels:
                desc_val += f"文档类型涵盖 {'、'.join(labels)}。"
        if need_name and not name_val:
            name_val = (titles[0] or "").strip()[:40]

        with self._write() as db:
            if need_name and name_val:
                # Optimistic guard: only overwrite if the name is still the
                # placeholder we read (no clobber of a concurrent rename).
                db.execute(
                    "UPDATE notebooks SET name = ?, updated_at = ? WHERE id = ? AND name = ?",
                    (name_val[:120], _now(), notebook_id, nb["name"]),
                )
            if need_desc and desc_val:
                db.execute(
                    "UPDATE notebooks SET purpose = ?, updated_at = ? "
                    "WHERE id = ? AND purpose_auto = 1",
                    (desc_val[:1000], _now(), notebook_id),
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
        with self._write() as db:
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
        self._mark_unified_kg_dirty(source.notebook_id)

    def extract_source(self, source_id: str) -> None:
        """Public entry to (re-)extract a single source's KG in place. Delegates to
        _run_extraction, which deletes the source's prior KG objects/relations/
        embeddings and re-runs extraction (idempotent per source). Used by
        reextract_notebook and maintenance scripts."""
        self._run_extraction(source_id)

    def _run_extraction(self, source_id: str) -> None:
        source = self.get_source(source_id)
        elements = self.source_elements(source_id)
        now = _now()
        run_id = f"run-{uuid4().hex[:10]}"
        doc_type_id = _normalize_doc_type(getattr(source, "doc_type", "") or "") or "academic_paper"
        kg_doc_type = kg_ingest.DOC_TYPE_MAP.get(doc_type_id, "academic")
        with self._write() as db:
            self._clear_source_extraction_state(db, source_id, source.notebook_id, clear_embeddings=False)
            self._delete_relations_for_source(db, source_id)
            db.execute(
                "DELETE FROM knowledge_embeddings WHERE object_id IN "
                "(SELECT id FROM knowledge_objects WHERE source_id = ?)",
                (source_id,),
            )
            db.execute("DELETE FROM knowledge_objects WHERE source_id = ?", (source_id,))
            db.execute(
                """INSERT INTO extraction_runs
                   (id, notebook_id, source_id, run_type, status, error_message, created_at, updated_at)
                   VALUES (?, ?, ?, 'kg', 'running', '', ?, ?)""",
                (run_id, source.notebook_id, source_id, now, now))
        try:
            if not getattr(self.llm_client, "configured", False):
                with self._write() as db:
                    db.execute("UPDATE extraction_runs SET status='completed', error_message='no-llm', updated_at=? WHERE id=?", (_now(), run_id))
                return
            raw_text = self._source_raw_text(source, elements)
            n_chars = kg_ingest.plan_window_size(
                len(raw_text), self.settings.kg_extract_workers,
                self.settings.kg_window_min_chars, self.settings.kg_window_max_chars,
                override=self.settings.kg_window_target_chars,
            )
            whitelist = self.concept_whitelist_terms()
            with self._connect() as db:
                _nb = db.execute(
                    "SELECT tier FROM notebooks WHERE id=?", (source.notebook_id,)
                ).fetchone()
            base_filter = bool(_nb and _nb["tier"] == "base")
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                whitelist=whitelist,
                refine=self.settings.kg_refine_enabled,
                gleaning_rounds=(self.settings.kg_gleaning_rounds if self.settings.kg_gleaning_enabled else 0),
                base_filter=base_filter,
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
            with self._write() as db:
                db.execute("UPDATE extraction_runs SET status='completed', error_message=?, updated_at=? WHERE id=?",
                           (f"kg objects={n_obj} relations={n_rel} doc_type={kg_doc_type} "
                            f"windows_failed={fw}/{tw} windows_skipped={graph.windows_skipped} "
                            f"concepts_dropped={graph.concepts_dropped} claims_dropped={graph.claims_dropped}", _now(), run_id))
        except Exception as exc:
            with self._write() as db:
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
        notebook_id = source.notebook_id
        elements = self.source_elements(source_id)
        pending = [el for el in elements if el.text.strip()]
        if not pending:
            return
        import concurrent.futures as _cf

        trunc = self.settings.embed_truncate_chars
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]

        # Pre-create the embedder's HTTP client single-threaded to avoid a lazy-init
        # race when many worker threads first touch it (no-op for fakes).
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

        def _embed_only(els: list) -> list:
            texts = [el.text[:trunc] for el in els]
            try:
                vectors = self.embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed batch failed (%d elements) for source %s: %s",
                    len(els), source_id, exc,
                )
                return []
            return [(el.id, vector) for el, vector in zip(els, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-el") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        now = _now()
        if rows:
            with self._write() as db:
                db.executemany(
                    "INSERT OR REPLACE INTO element_embeddings "
                    "(element_id, source_id, notebook_id, vector, created_at) VALUES (?,?,?,?,?)",
                    [(eid, source_id, notebook_id, json.dumps(vec), now) for eid, vec in rows],
                )
        self.event_log.logger.info(
            "embedded %s/%s elements for source %s", len(rows), len(pending), source_id
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
        with self._write() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO knowledge_embeddings
                (object_id, notebook_id, vector, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (object_id, notebook_id, json.dumps(vector), _now()),
            )

    def _embed_objects_batch(self, notebook_id: str, items: List[dict]) -> None:
        """并发 COMPUTE payload 向量, 再用一次写事务持久化到 knowledge_embeddings。
        每批计算失败照旧 log + 跳过(best-effort)。"""
        if not self.settings.embedder_configured:
            return
        pending = []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            return
        import concurrent.futures as _cf

        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

        def _embed_only(batch) -> list:
            texts = [t for _, t in batch]
            try:
                vectors = self.embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed kg-objects batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [(oid, vec) for (oid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-kg") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        if not rows:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(oid, notebook_id, json.dumps(vec), now) for oid, vec in rows],
            )

    def _build_chunks_for_source(self, source_id: str) -> None:
        """合并一个 source 的 source_elements 成检索 chunk(纯写库, 无网络)。
        幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。元素 id 形如
        el-<sid>-0001 零补位, 故 ORDER BY id == 插入顺序。"""
        from app.services.chunking import build_chunks
        from uuid import uuid4
        src = self.get_source(source_id)
        notebook_id = src.notebook_id
        with self._connect() as db:
            erows = db.execute(
                "SELECT id, element_type, text FROM source_elements "
                "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
        elements = [{"id": r["id"], "element_type": r["element_type"], "text": r["text"]} for r in erows]
        chunks = build_chunks(elements,
                              target_chars=self.settings.chunk_target_chars,
                              overlap_chars=self.settings.chunk_overlap_chars)
        now = _now()
        rows = [(f"ck-{uuid4().hex[:12]}", notebook_id, source_id, c["text"],
                 c["section_path"], json.dumps(c["element_ids"]), now) for c in chunks]
        with self._write() as db:
            db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))  # 级联删 embeddings
            db.executemany(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)

    def _embed_chunks_for_source(self, source_id: str) -> None:
        """给一个 source 已写入的 chunk 补向量(并发+429退避)。无网络则 no-op。"""
        if not self.settings.embedder_configured:
            return
        notebook_id = self.get_source(source_id).notebook_id
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, text FROM chunks WHERE source_id=?", (source_id,)).fetchall()
        items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
        self._embed_chunks_batch(notebook_id, items)

    def _chunk_and_embed_source(self, source_id: str) -> None:
        """build + embed(供回填脚本/测试同步调用)。"""
        self._build_chunks_for_source(source_id)
        self._embed_chunks_for_source(source_id)

    def _embed_chunks_batch(self, notebook_id: str, items: List[dict]) -> None:
        """并发调用 embedder(resilience 由 DashscopeEmbedder 层负责), 落 chunk_embeddings。
        每批失败时 log + 跳过(best-effort)。"""
        if not self.settings.embedder_configured or not items:
            return
        pending = []
        for it in items:
            text = (it["payload"].get("text") or "").strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            return
        import concurrent.futures as _cf
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i+size] for i in range(0, len(pending), size)]
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

        def _emb(batch):
            try:
                vecs = self.embedder.embed_texts([t for _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning("embed chunks batch failed (%d) for %s: %s",
                                              len(batch), notebook_id, exc)
                return []
            return [(cid, v) for (cid, _), v in zip(batch, vecs)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        out = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-ck") as pool:
            for part in pool.map(_emb, batches):
                out.extend(part)
        if not out:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                [(cid, notebook_id, json.dumps(v), now) for cid, v in out])

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

    def _backfill_knowledge_embeddings(self, db: sqlite3.Connection,
                                       notebook_id: str, objects: List[dict]) -> None:
        """Embed + persist any knowledge objects missing a vector, concurrently
        (reuses the concurrent _embed_objects_batch). No-op when all are embedded
        or no embedder. Lets ask() build a complete knowledge matrix without
        materializing a Python-list dict of every vector."""
        if not self.settings.embedder_configured:
            return
        have = {
            r["object_id"]
            for r in db.execute(
                "SELECT object_id FROM knowledge_embeddings "
                "WHERE notebook_id = ? AND vector IS NOT NULL",
                (notebook_id,),
            ).fetchall()
        }
        missing = [
            {"_oid": o["id"], "payload": o.get("payload", {})}
            for o in objects
            if o["id"] not in have and _payload_text(o.get("payload", {})).strip()
        ]
        if missing:
            self._embed_objects_batch(notebook_id, missing)

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
        with self._write() as db:
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
        with self._write() as db:
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
        with self._write() as db:
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
            with self._write() as db:
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
        with self._write() as db:
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
        """Insert KG nodes/edges (remapping local ids to DB ids), embeds payload.

        分块写入(每块 CHUNK 行, 各自一个 _write() 事务), 避免单源 2.6万行塞一个
        事务长时间持锁。本地 id->DB id 在分块前一次性预分配, 跨块关系仍能正确
        remap。代价: 失整源原子性(崩溃可能留半本); _run_extraction 逐源自清 +
        可重跑兜底。Relations 引用不到的 local id 静默跳过。"""
        CHUNK = 1000
        now = _now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = f"ko-{uuid4().hex[:10]}"
            obj["_oid"] = local_to_id[obj["local_id"]]   # _embed_objects_batch 依赖
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            db_relations.append({
                "source_object_id": s, "target_object_id": t,
                "edge_type": rel["edge_type"], "evidence": rel.get("evidence", []),
            })

        # Base strong-review gate (Track F): objects written directly to a base
        # notebook land as 'reviewed' (still in USABLE_STATUSES, so retrievable)
        # rather than 'approved' — the curator confirms via update_knowledge
        # before they are treated as canonical. Personal notebooks keep
        # 'approved' (no behavior change).
        with self._connect() as db:
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
        auto_status = 'reviewed' if (nb_row and nb_row["tier"] == 'base') else 'approved'

        for i in range(0, len(objects), CHUNK):
            chunk = objects[i:i + CHUNK]
            with self._write() as db:
                db.executemany(
                    "INSERT INTO knowledge_objects "
                    "(id, notebook_id, object_type, status, owner, payload, evidence, "
                    "source_candidate_id, source_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)",
                    [(o["_oid"], notebook_id, o["object_type"], auto_status,
                      json.dumps(o["payload"], ensure_ascii=False),
                      json.dumps(o["evidence"], ensure_ascii=False),
                      source_id or '', now, now) for o in chunk],
                )
        for i in range(0, len(db_relations), CHUNK):
            chunk = db_relations[i:i + CHUNK]
            with self._write() as db:
                db.executemany(
                    "INSERT INTO knowledge_relations "
                    "(id, notebook_id, source_id, source_object_id, target_object_id, "
                    "edge_type, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(f"rel-{uuid4().hex[:10]}", notebook_id, source_id,
                      r["source_object_id"], r["target_object_id"], r["edge_type"],
                      json.dumps(r["evidence"], ensure_ascii=False), now) for r in chunk],
                )
        self._embed_objects_batch(notebook_id, objects)
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)
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

    # --- Track E: edge trust review queue + curation feedback loop ----------
    _REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

    def review_queue(self, notebook_id: str, limit: int = 200) -> List[dict]:
        """Return edges ranked by review priority = edge_centrality * (1 - trust_score).

        Only edges with review_status != 'rejected' are included (rejected edges are
        excluded from reasoning and need no further review).
        Centrality is computed over the FULL graph (including non-rejected edges).
        trust_score combines evidence anchoring + cross-doc corroboration + type validity.
        """
        import json as _json
        from app.services.kg.edge_trust import (
            compute_trust_score, corroboration_counts,
            corroboration_score_from_count,
        )
        from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality

        self.get_notebook(notebook_id)
        with self._connect() as db:
            rel_rows = db.execute(
                "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
                "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
                "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
                "FROM knowledge_relations kr "
                "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
                "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
                "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            # Build node types + names for trust signals
            obj_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()

        node_types: dict = {}
        node_names: dict = {}
        for r in obj_rows:
            node_types[r["id"]] = r["object_type"]
            p = _json.loads(r["payload"] or "{}")
            node_names[r["id"]] = p.get("name", "")

        rels = []
        for r in rel_rows:
            rels.append({
                "id": r["id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"],
                "edge_type": r["edge_type"],
                "evidence": _json.loads(r["evidence"] or "[]"),
                "source_id": r["source_id"],
                "review_status": r["review_status"],
                "_src_type": r["src_type"] or "",
                "_tgt_type": r["tgt_type"] or "",
                "_src_name": node_names.get(r["source_object_id"], ""),
                "_tgt_name": node_names.get(r["target_object_id"], ""),
            })

        # Corroboration counts (batched over all edges)
        corr_counts = corroboration_counts(rels, node_names)

        # Edge centrality from the live graph (non-rejected edges only)
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            {oid: {"type": t, "name": node_names.get(oid, "")}
             for oid, t in node_types.items()},
            rels,
        )
        edge_centrality = compute_edge_centrality(G)

        items = []
        for rel in rels:
            rid = rel["id"]
            corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
            trust = compute_trust_score(rel, node_types, corr_score)
            ec = edge_centrality.get(rid, 0.0)
            # review_priority = high centrality × low trust
            priority = ec * (1.0 - trust)
            items.append({
                "rel_id": rid,
                "notebook_id": notebook_id,
                "edge_type": rel["edge_type"],
                "source_object_id": rel["source_object_id"],
                "target_object_id": rel["target_object_id"],
                "source_name": rel["_src_name"],
                "target_name": rel["_tgt_name"],
                "source_type": rel["_src_type"],
                "target_type": rel["_tgt_type"],
                "trust_score": trust,
                "edge_centrality": ec,
                "review_priority": priority,
                "review_status": rel["review_status"],
            })

        items.sort(key=lambda x: x["review_priority"], reverse=True)
        return items[:limit]

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation.

        Allowed statuses: 'pending', 'verified', 'rejected'.
        Raises ValueError for unknown statuses.
        Raises KeyError if the relation does not exist in this notebook.
        Invalidates the _rx_graph cache so the next graph-reasoning call sees
        the updated set of active edges.
        """
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"review_status must be one of {sorted(self._REVIEW_STATUSES)}, got {status!r}")
        with self._write() as db:
            cur = db.execute(
                "UPDATE knowledge_relations SET review_status=? "
                "WHERE id=? AND notebook_id=?",
                (status, rel_id, notebook_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"relation {rel_id!r} not found in notebook {notebook_id!r}")
        # Invalidate cached graph so _rx_graph rebuilds on next access
        # (belt-and-braces: _rx_graph's per-status-count version key would
        # also catch the flip on its own).
        self._invalidate_unified_cache(notebook_id)

    def _delete_relations_for_source(self, db, source_id: str) -> None:
        db.execute("DELETE FROM knowledge_relations WHERE source_id = ?", (source_id,))

    # --- Concept-cluster / merge-candidate CRUD (Task 5) -------------------

    def write_clusters(self, notebook_id: str, rows: List[dict],
                       object_type: str = "concept") -> None:
        now = _now()
        with self._write() as db:
            db.execute("DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=?",
                       (notebook_id, object_type))
            for r in rows:
                db.execute(
                    "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"cc-{uuid4().hex[:10]}", notebook_id, r["canonical_id"],
                     r["member_object_id"], r["canonical_name"], object_type,
                     r.get("canonical_description", ""), now))

    def cluster_map(self, notebook_id: str) -> Dict[str, str]:
        with self._connect() as db:
            rows = db.execute("SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    def write_merge_candidate(self, notebook_id: str, a: str, b: str, score: float) -> None:
        now = _now()
        with self._write() as db:
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
        with self._write() as db:
            db.execute("UPDATE concept_merge_candidates SET status=?, updated_at=? WHERE id=? AND notebook_id=?", (status, _now(), candidate_id, notebook_id))

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "confirmed")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "rejected")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    def review_pending_merges(self, notebook_id: str, limit: int = 50, auto_confirm_threshold: float = 0.95) -> dict:
        self.get_notebook(notebook_id)
        pending = self.pending_merges(notebook_id)[: max(1, min(limit, 200))]
        from app.services.concept_merge_review import review_merge_candidates
        decisions = review_merge_candidates(self.llm_client, pending)
        confirmed = rejected = unsure = 0
        now = _now()
        with self._write() as db:
            for decision in decisions:
                candidate_id = decision["candidate_id"]
                confidence = decision["confidence"]
                status = "pending"
                if decision["decision"] == "merge" and confidence >= auto_confirm_threshold:
                    status = "confirmed"
                    confirmed += 1
                elif decision["decision"] == "keep_separate" and confidence >= auto_confirm_threshold:
                    status = "rejected"
                    rejected += 1
                else:
                    unsure += 1
                db.execute(
                    """
                    UPDATE concept_merge_candidates
                    SET status=?, confidence=?, rationale=?, reviewed_by='llm', updated_at=?
                    WHERE id=? AND notebook_id=?
                    """,
                    (status, confidence, decision["rationale"], now, candidate_id, notebook_id),
                )
        if confirmed or rejected:
            self._mark_unified_kg_dirty(notebook_id)
            self._invalidate_unified_cache(notebook_id)
        return {"reviewed": len(decisions), "confirmed": confirmed, "rejected": rejected, "unsure": unsure}

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self._connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    def concept_whitelist_terms(self) -> set:
        with self._connect() as db:
            return {r["term"] for r in db.execute("SELECT term FROM concept_whitelist").fetchall()}

    def concept_whitelist_list(self) -> List[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT term, note, created_at FROM concept_whitelist ORDER BY term").fetchall()
        return [{"term": r["term"], "note": r["note"], "created_at": r["created_at"]} for r in rows]

    def concept_whitelist_add(self, term: str, note: str = "") -> dict:
        from app.services.kg.filters import _norm
        t = _norm(term)
        if not t:
            raise ValueError("empty term")
        now = _now()
        with self._write() as db:
            db.execute(
                "INSERT OR REPLACE INTO concept_whitelist (term, note, created_at) VALUES (?, ?, ?)",
                (t, note, now),
            )
        return {"term": t, "note": note, "created_at": now}

    def concept_whitelist_remove(self, term: str) -> None:
        from app.services.kg.filters import _norm
        with self._write() as db:
            db.execute("DELETE FROM concept_whitelist WHERE term = ?", (_norm(term),))

    def _invalidate_unified_cache(self, notebook_id: str) -> None:
        for key in [k for k in self._unified_cache if k[0] == notebook_id]:
            self._unified_cache.pop(key, None)
        # Matrices are stored under "{nb}:matrix:{table}" (see _vector_matrix). The old
        # "{nb}:knowledge" key never matched (dead no-op). Invalidate BOTH embedding
        # tables so an in-place re-embed (same row count + same-second created_at, i.e.
        # an unchanged version tuple) cannot serve a stale vector.
        for table in ("knowledge_embeddings", "element_embeddings"):
            self._vector_cache.invalidate(f"{notebook_id}:matrix:{table}")
        self._vector_cache.invalidate(f"{notebook_id}:kwtok")
        # In-memory reasoning graph (built from knowledge_relations) — evict so a
        # re-ingest/delete with an unchanged version tuple cannot serve a stale graph.
        self._vector_cache.invalidate(f"{notebook_id}:rxgraph")
        # Federated graph caches are keyed "{active_id}:fed_rxgraph" — the ACTIVE
        # (personal) notebook's id, NOT this notebook's. A change in THIS notebook
        # (e.g. a base notebook) may affect any federated graph that includes it,
        # so evict every fed_rxgraph entry; tracking participants per key is
        # overkill for the POC. This explicit eviction also guards against
        # same-second in-place edits that leave the version tuple unchanged.
        for key in [k for k in self._vector_cache._store if k.endswith(":fed_rxgraph")]:
            self._vector_cache.invalidate(key)

    def _mark_unified_kg_dirty(self, notebook_id: str) -> None:
        now = _now()
        with self._write() as db:
            db.execute(
                """
                INSERT INTO unified_kg_state (notebook_id, dirty, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(notebook_id) DO UPDATE SET dirty=1, updated_at=excluded.updated_at
                """,
                (notebook_id, now),
            )

    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)).fetchone()
            clusters = db.execute(
                "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
        if row is None:
            return {"dirty": False, "last_rebuild_at": "", "objects": 0, "relations": 0, "clusters": int(clusters)}
        return {
            "dirty": bool(row["dirty"]),
            "last_rebuild_at": row["last_rebuild_at"],
            "objects": int(row["object_count"]),
            "relations": int(row["relation_count"]),
            "clusters": int(row["cluster_count"] or clusters),
        }

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
        # LLM 兜底: ≥hi 的 auto_candidates 经复核确认后并入 confirmed, 重聚一次
        from app.services.concept_merge_review import review_merge_candidates
        autoc = res.get("auto_candidates", [])
        if autoc and getattr(self.llm_client, "configured", False):
            cand_dicts = [{"id": f"ac{i}", "canonical_a": a, "canonical_b": b, "score": s}
                          for i, (a, b, s) in enumerate(autoc)]
            decisions = review_merge_candidates(self.llm_client, cand_dicts)
            by_id = {d["candidate_id"]: d for d in decisions}
            extra = set()
            for i, (a, b, s) in enumerate(autoc):
                d = by_id.get(f"ac{i}")
                if d and d["decision"] == "merge" and d["confidence"] >= 0.90:
                    extra.add(frozenset((a[2:] if a.startswith("K-") else a,
                                        b[2:] if b.startswith("K-") else b)))
            if extra:
                confirmed = set(confirmed) | extra
                res = cluster_concepts(concepts, vectors, confirmed, rejected)
        rows = [{"canonical_id": res["cluster_map"][c["object_id"]], "member_object_id": c["object_id"],
                 "canonical_name": res["canonical_names"][c["object_id"]]} for c in concepts]
        if self.settings.kg_concept_desc_enabled and getattr(self.llm_client, "configured", False):
            from app.services.prompts import concept_description_prompt, CONCEPT_DESC_SCHEMA_HINT
            # members per canonical cluster
            members_by_cid: Dict[str, List[str]] = {}
            for r in rows:
                members_by_cid.setdefault(r["canonical_id"], []).append(r["member_object_id"])
            desc_by_cid: Dict[str, str] = {}
            for cid, mids in members_by_cid.items():
                if len(mids) < 2:
                    continue   # only fuse cross-doc merged clusters (cost bound)
                # gather member evidence quotes
                ph = ",".join("?" for _ in mids)
                with self._connect() as db:
                    erows = db.execute(
                        f"SELECT evidence FROM knowledge_objects WHERE id IN ({ph})", mids).fetchall()
                quotes = []
                for er in erows:
                    for ev in json.loads(er["evidence"] or "[]"):
                        q = (ev.get("quoted_span") or "").strip()
                        if q:
                            quotes.append(q)
                quotes = list(dict.fromkeys(quotes))[:8]   # dedup, cap
                if not quotes:
                    continue
                name = next((r["canonical_name"] for r in rows if r["canonical_id"] == cid), "")
                block = "\n".join(f"- {q}" for q in quotes)
                try:
                    raw = self.llm_client.chat_json(
                        [{"role": "user", "content": concept_description_prompt(name, block)}],
                        CONCEPT_DESC_SCHEMA_HINT)
                    desc = (json.loads(raw).get("description") or "").strip()
                except Exception:
                    desc = ""
                if desc:
                    desc_by_cid[cid] = desc
            if desc_by_cid:
                for r in rows:
                    if r["canonical_id"] in desc_by_cid:
                        r["canonical_description"] = desc_by_cid[r["canonical_id"]]
        self.write_clusters(notebook_id, rows)
        # Cross-doc exact-normalized merge for non-concept types (v1: seed-based;
        # vector near-dup remains concept-only). Each type isolated by id prefix.
        from app.services.kg_merge import (cluster_objects, seed_claim,
                                           seed_formula, seed_procedure)
        _TYPE_MERGE = {"claim": (seed_claim, "KL-"),
                       "formula": (seed_formula, "KF-"),
                       "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPE_MERGE.items():
            with self._connect() as db:
                trows = db.execute(
                    "SELECT id, payload FROM knowledge_objects "
                    "WHERE notebook_id=? AND object_type=? AND status!='deprecated'",
                    (notebook_id, t)).fetchall()
            tobjs = []
            for r in trows:
                pay = json.loads(r["payload"] or "{}")
                tobjs.append({"object_id": r["id"], "name": pay.get("name", ""),
                              "payload": pay})
            if not tobjs:
                self.write_clusters(notebook_id, [], object_type=t)   # clear stale rows
                continue
            res_t = cluster_objects(tobjs, {}, set(), set(), seed_fn=sfn,
                                    conflict_fn=None, id_prefix=prefix)
            trows_w = [{"canonical_id": res_t["cluster_map"][o["object_id"]],
                        "member_object_id": o["object_id"],
                        "canonical_name": res_t["canonical_names"][o["object_id"]]}
                       for o in tobjs]
            self.write_clusters(notebook_id, trows_w, object_type=t)
        # Refresh pending candidates in ONE transaction (per-candidate inserts
        # were the rebuild hotspot at scale). confirmed/rejected rows untouched.
        now = _now()
        with self._write() as db:
            db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,))
            db.executemany(
                "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?, 'pending', ?, ?)",
                [(f"mc-{uuid4().hex[:10]}", notebook_id, a, b, score, now, now) for a, b, score in res["pending"]])
        self._invalidate_unified_cache(notebook_id)
        cluster_count = len(set(res["cluster_map"].values()))
        with self._write() as db:
            object_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchone()["c"]
            relation_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
            db.execute(
                """
                INSERT INTO unified_kg_state
                (notebook_id, dirty, last_rebuild_at, object_count, relation_count, cluster_count, updated_at)
                VALUES (?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(notebook_id) DO UPDATE SET
                  dirty=0,
                  last_rebuild_at=excluded.last_rebuild_at,
                  object_count=excluded.object_count,
                  relation_count=excluded.relation_count,
                  cluster_count=excluded.cluster_count,
                  updated_at=excluded.updated_at
                """,
                (notebook_id, now, object_count, relation_count, cluster_count, now),
            )
        return cluster_count

    def rebuild_communities(self, notebook_id: str, level: int = 0) -> int:
        """Detect communities over the notebook's KG (objects linked by relations)
        via networkx Louvain; persist them (delete + reinsert). No LLM. Returns the
        community count. Deterministic (fixed seed)."""
        self.get_notebook(notebook_id)
        import networkx as nx
        from networkx.algorithms.community import louvain_communities
        with self._connect() as db:
            rels = db.execute(
                "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
                (notebook_id,)).fetchall()
        g = nx.Graph()
        for r in rels:
            s, t = r["source_object_id"], r["target_object_id"]
            if not s or not t or s == t:
                continue
            if g.has_edge(s, t):
                g[s][t]["weight"] += 1
            else:
                g.add_edge(s, t, weight=1)
        comms = (louvain_communities(g, weight="weight", seed=42)
                 if g.number_of_nodes() else [])
        now = _now()
        with self._write() as db:
            db.execute("DELETE FROM communities WHERE notebook_id=? AND level=?", (notebook_id, level))
            for comm in comms:
                members = sorted(comm)
                db.execute(
                    "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"cm-{uuid4().hex[:10]}", notebook_id, level, json.dumps(members), len(members), now))
        return len(comms)

    def list_communities(self, notebook_id: str, level: int = 0) -> List[List[str]]:
        """Member-id lists of each detected community (for summaries / global search)."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT member_ids FROM communities WHERE notebook_id=? AND level=? ORDER BY size DESC, id ASC",
                (notebook_id, level)).fetchall()
        return [json.loads(r["member_ids"]) for r in rows]

    def summarize_communities(self, notebook_id: str, level: int = 0) -> int:
        """For each detected community, generate an LLM report (title/summary/
        findings) from its members + internal relations; persist on the community
        row. No-op (returns 0) when disabled or LLM unconfigured. Returns the
        number of communities summarized."""
        self.get_notebook(notebook_id)
        if not self.settings.kg_community_summary_enabled or not getattr(self.llm_client, "configured", False):
            return 0
        from app.services.prompts import community_report_prompt, COMMUNITY_REPORT_SCHEMA_HINT
        with self._connect() as db:
            crows = db.execute(
                "SELECT id, member_ids FROM communities WHERE notebook_id=? AND level=?",
                (notebook_id, level)).fetchall()
        done = 0
        for cr in crows:
            members = json.loads(cr["member_ids"] or "[]")
            if not members:
                continue
            ph = ",".join("?" for _ in members)
            with self._connect() as db:
                orows = db.execute(
                    f"SELECT id, object_type, payload FROM knowledge_objects WHERE id IN ({ph})", members).fetchall()
                rrows = db.execute(
                    f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
                    f"WHERE notebook_id=? AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                    [notebook_id, *members, *members]).fetchall()
            name_by_id = {}
            mlines = []
            for o in orows:
                nm = json.loads(o["payload"] or "{}").get("name", "")
                name_by_id[o["id"]] = nm
                mlines.append(f"- [{o['object_type']}] {nm}")
            rlines = [f"{name_by_id.get(r['source_object_id'],'?')} -[{r['edge_type']}]-> {name_by_id.get(r['target_object_id'],'?')}"
                      for r in rrows]
            members_block = "\n".join(mlines)
            relations_block = "\n".join(rlines) if rlines else "(none)"
            try:
                raw = self.llm_client.chat_json(
                    [{"role": "user", "content": community_report_prompt(members_block, relations_block)}],
                    COMMUNITY_REPORT_SCHEMA_HINT)
                data = json.loads(raw)
            except Exception:
                continue
            title = str(data.get("title", "")).strip()
            summary = str(data.get("summary", "")).strip()
            findings = data.get("findings") if isinstance(data.get("findings"), list) else []
            if not summary:
                continue
            with self._write() as db:
                db.execute("UPDATE communities SET title=?, summary=?, findings=? WHERE id=?",
                           (title, summary, json.dumps(findings), cr["id"]))
            done += 1
        return done

    def get_community_reports(self, notebook_id: str, level: int = 0) -> List[dict]:
        """Persisted community reports (only those summarized). For global search."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT member_ids, title, summary, findings FROM communities "
                "WHERE notebook_id=? AND level=? AND summary!='' ORDER BY size DESC, id ASC",
                (notebook_id, level)).fetchall()
        return [{"member_ids": json.loads(r["member_ids"] or "[]"), "title": r["title"],
                 "summary": r["summary"], "findings": json.loads(r["findings"] or "[]")} for r in rows]

    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            # Get cluster members and canonical name in one query
            cluster_rows = db.execute(
                "SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ko.payload, ko.evidence "
                "FROM concept_clusters cc "
                "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
                "WHERE cc.notebook_id=? AND cc.canonical_id=? AND ko.status!='deprecated'",
                (notebook_id, canonical_id),
            ).fetchall()
            # canonical_name comes from the cluster table (same for all rows)
            name_row = db.execute(
                "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
                (notebook_id, canonical_id),
            ).fetchone()
            name = name_row["canonical_name"] if name_row else ""

        members = []
        member_ids = []
        for r in cluster_rows:
            obj = {
                "id": r["member_object_id"],
                "object_type": r["object_type"],
                "payload": json.loads(r["payload"] or "{}"),
                "evidence": json.loads(r["evidence"] or "[]"),
            }
            members.append(obj)
            member_ids.append(r["member_object_id"])

        mset = set(member_ids)

        if not mset:
            # No members found; still return valid shape
            return {"canonical_id": canonical_id, "canonical_name": name,
                    "members": [], "attached": [], "evidence": []}

        ph = ",".join("?" for _ in mset)
        mlist = list(mset)

        with self._connect() as db:
            # Targeted relation queries using member placeholders
            rels_out = db.execute(
                f"SELECT source_object_id, target_object_id, edge_type "
                f"FROM knowledge_relations WHERE notebook_id=? AND source_object_id IN ({ph})",
                [notebook_id] + mlist,
            ).fetchall()
            rels_in = db.execute(
                f"SELECT source_object_id, target_object_id, edge_type "
                f"FROM knowledge_relations WHERE notebook_id=? AND target_object_id IN ({ph})",
                [notebook_id] + mlist,
            ).fetchall()

            # Collect attached object ids (non-member side of relations)
            attached_ids: set[str] = set()
            rel_edges: list[dict] = []
            for rel in rels_out:
                other = rel["target_object_id"]
                if other not in mset:
                    attached_ids.add(other)
                    rel_edges.append({"other": other, "edge_type": rel["edge_type"]})
            for rel in rels_in:
                other = rel["source_object_id"]
                if other not in mset:
                    attached_ids.add(other)
                    rel_edges.append({"other": other, "edge_type": rel["edge_type"]})

            # Batch-read attached objects
            by_other: dict[str, dict] = {}
            if attached_ids:
                aph = ",".join("?" for _ in attached_ids)
                arows = db.execute(
                    f"SELECT id, object_type, payload, evidence FROM knowledge_objects "
                    f"WHERE id IN ({aph}) AND status!='deprecated'",
                    list(attached_ids),
                ).fetchall()
                by_other = {
                    r["id"]: {
                        "id": r["id"],
                        "object_type": r["object_type"],
                        "payload": json.loads(r["payload"] or "{}"),
                        "evidence": json.loads(r["evidence"] or "[]"),
                    }
                    for r in arows
                }

        attached = []
        seen_attached: set[str] = set()
        for edge in rel_edges:
            other = edge["other"]
            if other in by_other and by_other[other]["object_type"] != "concept" and other not in seen_attached:
                seen_attached.add(other)
                attached.append({**by_other[other], "edge_type": edge["edge_type"]})

        member_by_id = {m["id"]: m for m in members}
        evidence = [ev for oid in member_ids for ev in member_by_id.get(oid, {}).get("evidence", [])]
        with self._connect() as db:
            evidence = self._enrich_evidence(db, evidence)

        return {"canonical_id": canonical_id, "canonical_name": name,
                "members": [member_by_id[o] for o in member_ids if o in member_by_id],
                "attached": attached, "evidence": evidence}

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC",
            (ids[0],),
        ).fetchall()
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
                # prefer the unified cluster's fused description when present
                crow = db.execute(
                    "SELECT canonical_description FROM concept_clusters "
                    "WHERE notebook_id=? AND member_object_id=? AND canonical_description!='' LIMIT 1",
                    (notebook_id, object_id)).fetchone()
                if crow and crow["canonical_description"]:
                    result["definition"] = crow["canonical_description"]
                else:
                    drow = db.execute(
                        "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                        "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                    if drow is not None:
                        dpay = json.loads(drow["payload"] or "{}")
                        den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
                        result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))
            if obj_type == "procedure":
                steps_payload = payload.get("steps")
                if isinstance(steps_payload, list) and steps_payload:
                    # New self-contained shape: ordered steps live in the object's payload.
                    eids = [s.get("element_id") for s in steps_payload if s.get("element_id")]
                    texts, _ord = self._element_texts(db, eids) if eids else ({}, {})
                    result["steps"] = [
                        {"name": s.get("name", ""),
                         "element_text": texts.get(s.get("element_id") or "", s.get("quote", "")),
                         "section_path": section}
                        for s in steps_payload
                    ]
                else:
                    # Legacy fallback: group sibling procedure nodes by exact section_path
                    # (precedes edges are sparse). Two distinct procedures sharing a heading
                    # would merge — acceptable for inspection.
                    prows = db.execute(
                        "SELECT id, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated'", (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json.loads(pr["payload"] or "{}")
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json.loads(pr["evidence"] or "[]")
                        first_eid = ev[0].get("element_id") if ev else ""
                        candidate_steps.append((ppay.get("name", ""), first_eid))
                    all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                    if all_step_first_eids:
                        texts, ordinal = self._element_texts(db, all_step_first_eids, with_ordinal=True)
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
        with self._write() as db:
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

    def _rule_card_from_row(self, row: sqlite3.Row) -> RuleCard:
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        return self._rule_card(self._as_retrieved(obj, row["object_type"]))

    def approve_derived_rule(self, notebook_id: str, candidate_id: str) -> RuleCard:
        """Promote a derived rule candidate into the formal rule library (§7.5)."""
        now = _now()
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ? AND notebook_id = ?",
                (candidate_id, notebook_id),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            existing = db.execute(
                """
                SELECT * FROM knowledge_objects
                WHERE notebook_id = ? AND object_type = 'rule'
                  AND source_candidate_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (notebook_id, candidate_id),
            ).fetchone()
            if existing is not None:
                if row["status"] != "approved":
                    db.execute(
                        "UPDATE derived_rule_candidates SET status = 'approved' WHERE id = ?",
                        (candidate_id,),
                    )
                return self._rule_card_from_row(existing)
            if row["status"] == "rejected":
                raise ValueError("cannot approve a rejected derived rule candidate")
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
                    notebook_id,
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
        return self._rule_card_from_row(ko_row)

    def reject_derived_rule(self, notebook_id: str, candidate_id: str) -> DerivedRuleCandidate:
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ? AND notebook_id = ?",
                (candidate_id, notebook_id),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            if row["status"] == "approved":
                raise ValueError("cannot reject an approved derived rule candidate")
            db.execute(
                "UPDATE derived_rule_candidates SET status = 'rejected' WHERE id = ?",
                (candidate_id,),
            )
            row = db.execute(
                "SELECT * FROM derived_rule_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._derived_from_row(row)

    # --- Governance: promotion state machine (Track F) -------------------

    @staticmethod
    def _promotion_row_to_dict(row: sqlite3.Row, *, payload=None, evidence=None) -> dict:
        """Map a promotion_candidates row to the PromotionCandidate-shaped dict.
        payload/evidence are denormalised from knowledge_objects when listing."""
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "object_id": row["object_id"],
            "object_type": row["object_type"],
            "status": row["status"],
            "reason": row["reason"],
            "reviewed_by": row["reviewed_by"],
            "base_match_id": row["base_match_id"],
            "created_at": row["created_at"],
            "payload": payload if payload is not None else {},
            "evidence": evidence if evidence is not None else [],
        }

    def propose_promotion(self, notebook_id: str, object_id: str) -> dict:
        """Propose a personal-KG object for promotion into the base corpus.

        Idempotent for an already-active proposal of the same object. Raises
        KeyError if the notebook or object is missing; ValueError if the
        notebook is itself a base notebook (use the review gate there instead).
        """
        self.get_notebook(notebook_id)  # KeyError if notebook missing
        now = _now()
        with self._write() as db:
            obj = db.execute(
                "SELECT object_type FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (object_id, notebook_id),
            ).fetchone()
            if obj is None:
                raise KeyError(object_id)
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
            if nb_row and nb_row["tier"] == "base":
                raise ValueError("cannot propose from a base notebook — use the review gate")
            # Idempotency: return any active (non-approved, non-rejected) proposal.
            existing = db.execute(
                "SELECT * FROM promotion_candidates "
                "WHERE object_id=? AND status NOT IN ('approved','rejected')",
                (object_id,),
            ).fetchone()
            if existing is not None:
                return self._promotion_row_to_dict(existing)
            cand_id = f"promo-{uuid4().hex[:10]}"
            db.execute(
                """
                INSERT INTO promotion_candidates
                (id, notebook_id, object_id, object_type, status, reason,
                 reviewed_by, base_match_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'proposed', '', '', '', ?, ?)
                """,
                (cand_id, notebook_id, object_id, obj["object_type"], now, now),
            )
            row = db.execute(
                "SELECT * FROM promotion_candidates WHERE id=?", (cand_id,)
            ).fetchone()
        return self._promotion_row_to_dict(row)

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks (the curator sees
        everything). Defaults to the active queue (proposed + under_review);
        pass status_filter to view a single status. Denormalises payload +
        evidence from knowledge_objects for display."""
        with self._connect() as db:
            if status_filter:
                rows = db.execute(
                    "SELECT * FROM promotion_candidates WHERE status=? "
                    "ORDER BY created_at ASC, id ASC",
                    (status_filter,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM promotion_candidates "
                    "WHERE status IN ('proposed','under_review') "
                    "ORDER BY created_at ASC, id ASC"
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                obj = db.execute(
                    "SELECT payload, evidence FROM knowledge_objects WHERE id=?",
                    (row["object_id"],),
                ).fetchone()
                payload = json.loads(obj["payload"] or "{}") if obj else {}
                evidence = (
                    [Evidence(**e) for e in json.loads(obj["evidence"] or "[]")]
                    if obj
                    else []
                )
                out.append(
                    self._promotion_row_to_dict(row, payload=payload, evidence=evidence)
                )
        return out

    def approve_promotion(self, candidate_id: str) -> dict:
        """Approve a promotion: copy the personal object into the base corpus,
        deduplicating against existing base objects of the same type via the
        kg_merge seed clustering. Idempotent. Raises KeyError if the candidate
        is missing; ValueError if it is rejected or there is no base notebook.
        """
        now = _now()
        with self._write() as db:
            cand = db.execute(
                "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "rejected":
                raise ValueError("cannot approve a rejected promotion candidate")
            object_type = cand["object_type"]

            base_row = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if base_row is None:
                raise ValueError("no base notebook — mark one with mark_notebook_base() first")
            base_nb_id = base_row["id"]

            # Idempotency: if already approved, return the existing base object.
            if cand["status"] == "approved":
                existing = db.execute(
                    "SELECT id FROM knowledge_objects "
                    "WHERE notebook_id=? AND source_candidate_id=? "
                    "ORDER BY created_at ASC, id ASC LIMIT 1",
                    (base_nb_id, candidate_id),
                ).fetchone()
                base_object_id = existing["id"] if existing else (cand["base_match_id"] or "")
                return {
                    "candidate_id": candidate_id,
                    "base_object_id": base_object_id,
                    "merged_into": cand["base_match_id"] or "",
                }

            # Fetch the personal object being promoted.
            src = db.execute(
                "SELECT * FROM knowledge_objects WHERE id=?", (cand["object_id"],)
            ).fetchone()
            if src is None:
                raise KeyError(cand["object_id"])
            src_payload = json.loads(src["payload"] or "{}")
            src_evidence = json.loads(src["evidence"] or "[]")

            # Cross-corpus dedup against existing base objects of the same type.
            base_objs = db.execute(
                "SELECT id, payload, evidence FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                    ",".join("?" for _ in USABLE_STATUSES)
                ),
                (base_nb_id, object_type, *USABLE_STATUSES),
            ).fetchall()
            base_match_id = self._find_base_dedup_match(
                object_type, src_payload, base_objs
            )

            if base_match_id:
                # Merge: combine evidence into the matched base object; keep its id.
                matched = next(b for b in base_objs if b["id"] == base_match_id)
                merged_evidence = self._merge_evidence_lists(
                    json.loads(matched["evidence"] or "[]"), src_evidence
                )
                db.execute(
                    "UPDATE knowledge_objects SET evidence=?, updated_at=? WHERE id=?",
                    (json.dumps(merged_evidence, ensure_ascii=False), now, base_match_id),
                )
                base_object_id = base_match_id
                merged_into = base_match_id
            else:
                # No match: insert a fresh base object at status='approved'.
                base_object_id = f"ko-{uuid4().hex[:10]}"
                db.execute(
                    """
                    INSERT INTO knowledge_objects
                    (id, notebook_id, object_type, status, owner, payload, evidence,
                     source_candidate_id, source_id, created_at, updated_at)
                    VALUES (?, ?, ?, 'approved', '', ?, ?, ?, '', ?, ?)
                    """,
                    (
                        base_object_id,
                        base_nb_id,
                        object_type,
                        json.dumps(src_payload, ensure_ascii=False),
                        json.dumps(src_evidence, ensure_ascii=False),
                        candidate_id,
                        now,
                        now,
                    ),
                )
                merged_into = ""

            db.execute(
                "UPDATE promotion_candidates "
                "SET status='approved', base_match_id=?, reviewed_by='curator', updated_at=? "
                "WHERE id=?",
                (base_match_id, now, candidate_id),
            )

        # Embed the new base object's payload (best-effort; outside the txn so a
        # failing embedder never blocks approval). Only for freshly-inserted ones.
        if not base_match_id:
            self._embed_knowledge(base_object_id, base_nb_id, src_payload)
        self._invalidate_unified_cache(base_nb_id)
        self._mark_unified_kg_dirty(base_nb_id)
        return {
            "candidate_id": candidate_id,
            "base_object_id": base_object_id,
            "merged_into": merged_into,
        }

    @staticmethod
    def _seed_fn_for(object_type: str):
        """Return the kg_merge seed function for a KG object type."""
        from app.services.kg_merge import (
            seed_claim, seed_concept, seed_formula, seed_procedure,
        )
        return {
            "concept": seed_concept,
            "claim": seed_claim,
            "formula": seed_formula,
            "procedure": seed_procedure,
        }.get(object_type, seed_claim)

    def _find_base_dedup_match(
        self, object_type: str, src_payload: dict, base_objs: List[sqlite3.Row]
    ) -> str:
        """Exact-seed dedup (v1): return the id of an existing base object whose
        normalized seed matches the source payload, else ''. This works at cold
        start without vectors (the plan's v1 shortcut)."""
        seed_fn = self._seed_fn_for(object_type)
        src_seed = seed_fn({"name": src_payload.get("name", ""), "payload": src_payload})
        if not src_seed:
            return ""
        for b in base_objs:
            bp = json.loads(b["payload"] or "{}")
            b_seed = seed_fn({"name": bp.get("name", ""), "payload": bp})
            if b_seed and b_seed == src_seed:
                return b["id"]
        return ""

    @staticmethod
    def _merge_evidence_lists(base_ev: list, src_ev: list) -> list:
        """Union two evidence lists, deduping on (source_id, element_id, quoted_span)."""
        seen = set()
        merged: list = []
        for ev in [*base_ev, *src_ev]:
            if not isinstance(ev, dict):
                continue
            key = (ev.get("source_id"), ev.get("element_id"), ev.get("quoted_span"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(ev)
        return merged

    def reject_promotion(self, candidate_id: str, reason: str = "") -> dict:
        """Reject a promotion candidate. The personal object is left untouched.
        Raises KeyError if missing; ValueError if already approved."""
        now = _now()
        with self._write() as db:
            cand = db.execute(
                "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "approved":
                raise ValueError("cannot reject an approved promotion candidate")
            db.execute(
                "UPDATE promotion_candidates "
                "SET status='rejected', reason=?, reviewed_by='curator', updated_at=? "
                "WHERE id=?",
                (reason, now, candidate_id),
            )
            row = db.execute(
                "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        return self._promotion_row_to_dict(row)

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard:
        now = _now()
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
                (knowledge_id, notebook_id),
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
                "last_reviewed = ?, updated_at = ? WHERE id = ? AND notebook_id = ?",
                (new_payload, new_status, new_owner, last_reviewed, now, knowledge_id, notebook_id),
            )
            row = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
                (knowledge_id, notebook_id),
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
        return self._rule_card(item)

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

    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard:
        into_id = payload.into_id
        if into_id == source_id:
            raise ValueError("cannot merge a knowledge object into itself")
        now = _now()
        with self._write() as db:
            src = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
                (source_id, notebook_id),
            ).fetchone()
            tgt = db.execute(
                "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
                (into_id, notebook_id),
            ).fetchone()
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
        return self._rule_card(item)

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

    def _gather_elements(self, db: sqlite3.Connection, notebook_id: str,
                         with_vectors: bool = True) -> List[dict]:
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
                    "vector": (json.loads(row["vector"]) if row["vector"] else None)
                    if with_vectors else None,
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

    def _gather_chunks(self, db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            """
            SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids,
                   s.title AS source_title
            FROM chunks c JOIN sources s ON s.id = c.source_id
            WHERE c.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()
        return [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]

    def _vector_matrix(self, db: sqlite3.Connection, notebook_id: str,
                       table: str, id_col: str):
        """Cached (ids, normalized float32 matrix) for a notebook's embeddings.

        Streams JSON vectors straight into one float32 matrix (vector_index.
        build_matrix) so retrieval is a single matmul with bounded memory — vs
        materializing thousands of vectors as Python float lists (~1.3 GB on a
        large KG). Version-keyed on (count, max created_at) so it self-invalidates
        after (re)ingest. `table`/`id_col` are internal constants (not user input)."""
        from app.services.vector_index import build_matrix

        ver = db.execute(
            f"SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            f"FROM {table} WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        version = (table, ver["c"], ver["ts"])

        def _load():
            rows = db.execute(
                f"SELECT {id_col} AS vid, vector FROM {table} WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            return build_matrix((r["vid"], r["vector"]) for r in rows)

        return self._vector_cache.get(f"{notebook_id}:matrix:{table}", version, _load)

    def _keyword_token_sets(self, db, notebook_id: str, objects: list) -> dict:
        """Cached {object_id: frozenset(haystack_tokens)} for keyword scoring.

        Version-keyed on (COUNT, MAX(updated_at)) of knowledge_objects so any
        payload or evidence edit invalidates the cache. Evidence text is included
        so the token set is byte-equivalent to what score_knowledge builds live."""
        from app.services.retrieval import _tokens, _payload_text
        ver = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,)).fetchone()
        version = ("kwtok", ver["c"], ver["ts"])

        def _load():
            out = {}
            for o in objects:
                ev_text = " ".join(
                    e.quoted_span for e in o.get("evidence", [])
                )
                out[o["id"]] = frozenset(_tokens(f"{_payload_text(o['payload'])} {ev_text}"))
            return out

        return self._vector_cache.get(f"{notebook_id}:kwtok", version, _load)

    def _rx_graph(self, notebook_id: str):
        """Return the cached rustworkx PyDiGraph for `notebook_id`.

        Version-keyed on (COUNT, MAX created_at, per-review-status counts) of
        knowledge_relations (COUNT/MAX pattern as _vector_matrix at
        :3020-3045).  Graph is rebuilt on new ingest/delete and on any edge
        review flip.  Returned tuple: (G, idx_to_oid, oid_to_idx).
        Nodes are the USABLE_STATUSES knowledge_objects (same filter as the
        retrieval path); dangling edges (endpoint not a node) are dropped.
        """
        from app.services.kg.graph_reason import build_rx_graph
        with self._connect() as db:
            ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
                "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
                "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
                "FROM knowledge_relations WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone()
            # Track E: per-status counts make the version key sound — any
            # single-edge flip between pending/verified/rejected changes at
            # least one of (n_rejected, n_verified), so a curation verdict
            # invalidates the cached graph even when (COUNT, MAX created_at)
            # is unchanged. (MAX(review_status) was NOT sound: statuses
            # compare lexically, so e.g. verified→rejected with another
            # verified edge present left MAX='verified'.) set_edge_review's
            # explicit _invalidate_unified_cache remains as belt-and-braces.
            version = ("rxgraph", ver["c"], ver["ts"], ver["n_rej"], ver["n_ver"])

            def _load():
                ph = ",".join("?" for _ in USABLE_STATUSES)
                obj_rows = db.execute(
                    "SELECT id, object_type, payload FROM knowledge_objects "
                    f"WHERE notebook_id = ? AND status IN ({ph})",
                    (notebook_id, *USABLE_STATUSES),
                ).fetchall()
                nodes = {}
                for r in obj_rows:
                    p = json.loads(r["payload"] or "{}")
                    nodes[r["id"]] = {"type": r["object_type"], "name": p.get("name", "")}
                # Track E: rejected edges are excluded from the reasoning graph
                # (curation feedback loop). Bare filter, same as review_queue:
                # the column is NOT NULL DEFAULT 'pending' (migration runs in
                # __init__), so NULL is impossible.
                rel_rows = db.execute(
                    "SELECT id, source_object_id, target_object_id, edge_type, evidence "
                    "FROM knowledge_relations "
                    "WHERE notebook_id = ? AND review_status != 'rejected'",
                    (notebook_id,),
                ).fetchall()
                relations = [dict(r) for r in rel_rows]
                return build_rx_graph(nodes, relations)

            return self._vector_cache.get(f"{notebook_id}:rxgraph", version, _load)

    def _federated_rx_graph(self, active_notebook_id: str):
        """Return a federated PyDiGraph merging base notebook(s) + active notebook.

        Version-keyed, per participating notebook, on BOTH the relations
        (COUNT, MAX created_at, per-review-status counts) and the objects
        (COUNT, MAX updated_at), so an ingest into ANY of them — or an
        object-only change (status flip / payload edit bumps updated_at) — or
        an edge review flip (Track E) — triggers a rebuild even without an
        explicit eviction.  Cache key: "{active_id}:fed_rxgraph".

        Track E: relations with review_status = 'rejected' are excluded, same
        as _rx_graph — a curator's rejection must not flow into reasoning via
        the federated path either.

        Each relation row is tagged with its notebook_id before passing to
        build_rx_graph so per-edge tier stamping works.
        """
        from app.services.kg.graph_reason import build_rx_graph
        with self._connect() as db:
            # Participating notebooks: active + all base notebooks (excl. active
            # if active is itself base, to avoid duplication).
            base_rows = db.execute(
                "SELECT id, tier FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,),
            ).fetchall()
            active_row = db.execute(
                "SELECT id, tier FROM notebooks WHERE id=?",
                (active_notebook_id,),
            ).fetchone()
            active_tier = active_row["tier"] if active_row else "personal"

            # Build participating list: active first, then all base notebooks.
            participants = [(active_notebook_id, active_tier)] + [
                (r["id"], r["tier"]) for r in base_rows
            ]
            # Version key: per-notebook (nb_id, relations (count, max created_at,
            # per-review-status counts), objects (count, max updated_at)). Object
            # coverage makes object-only changes (deprecate / status flip /
            # payload edit) rebuild the graph. Track E: the per-status counts
            # (n_rej, n_ver) make a single edge flip between pending/verified/
            # rejected change the version even when (COUNT, MAX created_at) is
            # unchanged — same soundness argument as _rx_graph; set_edge_review's
            # explicit evict-all-fed_rxgraph remains as belt-and-braces.
            version_parts = []
            for nb_id, _ in participants:
                rel_ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
                    "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
                    "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
                    "FROM knowledge_relations WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                obj_ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
                    "FROM knowledge_objects WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                version_parts.append(
                    (nb_id, rel_ver["c"], rel_ver["ts"],
                     rel_ver["n_rej"], rel_ver["n_ver"],
                     obj_ver["c"], obj_ver["ts"]))
            version = ("fed_rxgraph", tuple(version_parts))

            tier_map = {nb_id: nb_tier for nb_id, nb_tier in participants}

            def _load():
                nodes: dict = {}
                all_relations: list = []
                ph = ",".join("?" for _ in USABLE_STATUSES)
                for nb_id, _ in participants:
                    obj_rows = db.execute(
                        "SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id = ? AND status IN ({ph})",
                        (nb_id, *USABLE_STATUSES),
                    ).fetchall()
                    for r in obj_rows:
                        p = json.loads(r["payload"] or "{}")
                        nodes[r["id"]] = {"type": r["object_type"], "name": p.get("name", "")}
                    # Track E: rejected edges are excluded from the federated
                    # reasoning graph too (curation feedback loop) — same bare
                    # filter as _rx_graph; the column is NOT NULL DEFAULT
                    # 'pending' (migration runs in __init__), so NULL is
                    # impossible.
                    rel_rows = db.execute(
                        "SELECT id, source_object_id, target_object_id, edge_type, evidence "
                        "FROM knowledge_relations "
                        "WHERE notebook_id = ? AND review_status != 'rejected'",
                        (nb_id,),
                    ).fetchall()
                    for r in rel_rows:
                        d = dict(r)
                        d["notebook_id"] = nb_id   # tag for tier_map lookup
                        all_relations.append(d)
                return build_rx_graph(nodes, all_relations, tier="personal", tier_map=tier_map)

            return self._vector_cache.get(
                f"{active_notebook_id}:fed_rxgraph", version, _load)

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

    def _retrieve_scored(self, notebook_id: str, query: str,
                         types: Optional[Iterable[str]] = None,
                         w_keyword: float = W_KEYWORD,
                         w_semantic: float = W_SEMANTIC) -> List[RetrievedKnowledge]:
        """Score KG objects of `types` (default all 4 _KG_TYPES) for `query`,
        returning RetrievedKnowledge sorted by fused relevance desc. Shared by
        the reasoning retriever's tools; `w_keyword`/`w_semantic` carry the
        per-sub-query `prefer` bias."""
        type_list = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        with self._connect() as db:
            kg_objs = {t: self._knowledge_objects(db, notebook_id, t) for t in type_list}
            query_vector = self._embed_query(query)
            elem_ids, elem_mat = self._vector_matrix(db, notebook_id, "element_embeddings", "element_id")
            kn_ids, kn_mat = self._vector_matrix(db, notebook_id, "knowledge_embeddings", "object_id")
            all_kg_objs = [o for objs in kg_objs.values() for o in objs]
            token_sets = self._keyword_token_sets(db, notebook_id, all_kg_objs)
        from app.services.vector_index import query_sims
        element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
        knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None
        scored: List[RetrievedKnowledge] = []
        for t in type_list:
            objs = kg_objs.get(t) or []
            if not objs:
                continue
            scored.extend(score_knowledge(
                query, objs, t, query_vector, None, None,
                element_sims=element_sims, knowledge_sims=knowledge_sims,
                w_keyword=w_keyword, w_semantic=w_semantic,
                keyword_token_sets=token_sets,
            ))
        scored.sort(key=lambda it: it.score, reverse=True)
        return scored

    def federated_retrieve(
        self,
        active_notebook_id: str,
        query: str,
        types: Optional[Iterable[str]] = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
    ) -> List[RetrievedKnowledge]:
        """Gather scored KG candidates from {base notebook(s)} ∪ {active personal
        notebook}, tagging each hit with .notebook_id and .tier.

        Each notebook's scoring path is IDENTICAL to _retrieve_scored — same
        _fuse, same dual-index best-of — so the [0,1]/tau and dual-index best-of
        invariants are preserved by construction. Hits are merged and sorted by
        score desc; no cross-notebook normalisation is applied (the same fused
        relevance scale applies to both tiers).
        """
        notebook_ids: List[str] = [active_notebook_id]
        with self._connect() as db:
            # Add base notebooks (excluding the active one if it is itself base).
            base_rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,),
            ).fetchall()
            notebook_ids.extend(r["id"] for r in base_rows)
            # Tier for each notebook_id (active + base) in one pass.
            tier_map: Dict[str, str] = {}
            for nid in notebook_ids:
                row = db.execute("SELECT tier FROM notebooks WHERE id=?", (nid,)).fetchone()
                tier_map[nid] = (row["tier"] if row else "personal")

        all_hits: List[RetrievedKnowledge] = []
        for nid in notebook_ids:
            hits = self._retrieve_scored(
                nid, query, types=types, w_keyword=w_keyword, w_semantic=w_semantic)
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)

        all_hits.sort(key=lambda it: it.score, reverse=True)
        return all_hits

    def _retrieve_neighbors(self, notebook_id: str, object_id: str,
                            edge_type: Optional[str] = None,
                            direction: str = "both") -> List[RetrievedKnowledge]:
        """1-hop graph neighbours of `object_id` as RetrievedKnowledge with
        placeholder relevance=0 (final relevance unified by run() via the
        original question). Honours edge_type filter; direction out=object as
        source, in=as target, both=either."""
        # Targeted index hits (idx_knowledge_relations_nb_source/_nb_target)
        # instead of loading every notebook edge: O(neighbours), not O(E).
        edge_clause = " AND edge_type=?" if edge_type else ""
        edge_param = [edge_type] if edge_type else []
        neighbour_ids: set = set()
        with self._connect() as db:
            if direction in ("out", "both"):
                neighbour_ids.update(
                    r["target_object_id"] for r in db.execute(
                        f"SELECT target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND source_object_id=?{edge_clause}",
                        [notebook_id, object_id, *edge_param],
                    ).fetchall()
                )
            if direction in ("in", "both"):
                neighbour_ids.update(
                    r["source_object_id"] for r in db.execute(
                        f"SELECT source_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND target_object_id=?{edge_clause}",
                        [notebook_id, object_id, *edge_param],
                    ).fetchall()
                )
            if not neighbour_ids:
                return []
            placeholders = ",".join("?" for _ in neighbour_ids)
            status_ph = ",".join("?" for _ in USABLE_STATUSES)
            rows = db.execute(
                f"SELECT * FROM knowledge_objects WHERE id IN ({placeholders}) "
                f"AND status IN ({status_ph})",
                [*neighbour_ids, *USABLE_STATUSES],
            ).fetchall()
        out: List[RetrievedKnowledge] = []
        for row in rows:
            keys = row.keys()
            out.append(RetrievedKnowledge(
                object_id=row["id"], object_type=row["object_type"],
                payload=json.loads(row["payload"] or "{}"),
                evidence=[Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
                score=0.0, relevance=0.0, status=row["status"], owner=row["owner"],
                last_reviewed=row["last_reviewed"] if "last_reviewed" in keys else "",
            ))
        return out

    def _retrieve_elements(self, notebook_id: str, query: str,
                           limit: int = 8) -> List[RetrievedElement]:
        """Keyword+semantic search over raw source_elements (fallback layer 2)."""
        query_vector = self._embed_query(query)
        with self._connect() as db:
            elements = self._gather_elements(db, notebook_id, with_vectors=True)
        return score_elements(query, elements, query_vector, limit=limit)

    def _retrieve_chunks(self, notebook_id: str, query: str, recall: int = 0):
        """大召回 chunk 候选。返回 (scored, ids, matrix);后两者供 MMR 取两两余弦
        (matrix 行已 L2 归一化, 点积即余弦)。"""
        from app.services.retrieval import score_chunks
        from app.services.vector_index import query_sims
        recall = recall or self.settings.chunk_recall
        query_vector = self._embed_query(query)
        with self._connect() as db:
            chunks = self._gather_chunks(db, notebook_id)
            ids, mat = self._vector_matrix(db, notebook_id, "chunk_embeddings", "chunk_id")
        chunk_sims = query_sims(query_vector, ids, mat) if query_vector else None
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        return scored, ids, mat

    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        """对每个子查询并发跑 _retrieve_chunks;返回 (collected{chunk_id:best}, per_query, ids, mat)。
        ids/mat 取首个非空子查询的矩阵(同 notebook 矩阵一致,用于后续 MMR 兜底)。"""
        from concurrent.futures import ThreadPoolExecutor

        def _one(q):
            try:
                return self._retrieve_chunks(notebook_id, q)
            except Exception:
                return ([], [], None)

        results = []
        if sub_queries:
            with ThreadPoolExecutor(max_workers=min(len(sub_queries), 8)) as ex:
                results = list(ex.map(_one, sub_queries))
        per_query, collected, ids, mat = [], {}, [], None
        for scored, qids, qmat in results:
            per_query.append({c.chunk_id: c for c in scored})
            for c in scored:
                cur = collected.get(c.chunk_id)
                if cur is None or c.relevance > cur.relevance:
                    collected[c.chunk_id] = c
            if mat is None and len(qids):
                ids, mat = qids, qmat
        return collected, per_query, ids, mat

    def _mmr_select_chunks(self, scored, ids, mat, k: int, lambda_: float):
        """对大召回结果做 MMR 多样性精选。沿用归一化矩阵, pair_sim=行点积。"""
        from app.services.mmr import mmr_rerank
        if len(scored) <= k:
            return list(scored)
        id_to_row = {cid: i for i, cid in enumerate(ids)}
        relevance = {c.chunk_id: c.relevance for c in scored}

        def pair_sim(a: str, b: str) -> float:
            ia, ib = id_to_row.get(a), id_to_row.get(b)
            if ia is None or ib is None:
                return 0.0
            return float(mat[ia] @ mat[ib])

        chosen = mmr_rerank([c.chunk_id for c in scored], relevance, pair_sim, k, lambda_)
        by_id = {c.chunk_id: c for c in scored}
        return [by_id[cid] for cid in chosen]

    def _chunk_answer_context(self, chunks) -> tuple:
        """产出长上下文综合用的 id 标注块 + id_map。chunk.text 已含 [section] 前缀
        (P1 build_chunks),故每行直接 `k_i: <text>`。id_map 形状与 KG 版一致,
        使 _parse_answer_anchors 原样复用(object_id=chunk_id, object_type=chunk)。"""
        budget = self.settings.chunk_answer_budget_chars
        lines, id_map = [], {}
        used = 0
        for i, c in enumerate(chunks, 1):
            if used >= budget and len(lines) >= 1:
                break
            key = f"k{i}"
            line = f"{key}: {c.text}"
            lines.append(line)
            used += len(line)
            id_map[key] = {
                "object_id": c.chunk_id, "object_type": "chunk",
                "name": c.section_path or c.source_title, "definition": None,
                "snippet": c.text[:300], "source_title": c.source_title,
                "location_label": c.section_path, "tier": "personal",
            }
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _answer_chunks(self, question, chunks, history="") -> tuple:
        """长上下文综合:把 MMR 精选的 chunk 原文喂给答案 LLM。返回
        (answer, llm_grounded, anchors)。复用 answer_prompt 的 [k] 标注协议。"""
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        context_block, id_map = self._chunk_answer_context(chunks)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def ask_chunk(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """chunk-native 通用问答:大召回 → MMR 多样性精选 → 长上下文综合 →
        引用绑回 chunk。KG 不参与(严格推理走 ask_reasoning)。"""
        import time
        from app.services.retrieval import classify_evidence
        ask_started = time.perf_counter()

        def ask_stage(name: str, started: float, **extra) -> None:
            self.event_log.emit({
                "kind": "ask_stage", "notebook_id": notebook_id, "stage": name,
                "latency_ms": round((time.perf_counter() - started) * 1000), **extra,
            })

        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)
        retrieval_query = self._rewrite_followup_query(history, question)

        _t = time.perf_counter()
        from app.services.query_rewrite import expand_query
        from app.services.retrieval import quota_fuse
        if self.settings.query_rewrite_enabled:
            ex = expand_query(self.rewrite_llm_client, retrieval_query, history,
                              max_subqueries=self.settings.chunk_max_subqueries)
            sub_queries = [s.query for s in ex.sub_queries]
        else:
            sub_queries = [retrieval_query]
        ask_stage("expand_query", _t, n=len(sub_queries))

        _t = time.perf_counter()
        if len(sub_queries) >= 2:
            collected, per_query, _ids, _mat = self._retrieve_chunks_multi(notebook_id, sub_queries)
            selected, _counts = quota_fuse(collected, per_query, self.settings.chunk_mmr_k,
                                           relevance=lambda c: c.relevance)
            ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
        else:
            scored, ids, mat = self._retrieve_chunks(notebook_id, sub_queries[0])
            selected = self._mmr_select_chunks(
                scored, ids, mat, self.settings.chunk_mmr_k, self.settings.chunk_mmr_lambda)
            ask_stage("retrieve_mmr", _t, recall=len(scored), selected=len(selected))

        # 引用绑回 chunk:element_id 取 chunk 首个 element(前端既有 element 引用可解析)。
        citations: List[Citation] = []
        for c in selected:
            eid = c.element_ids[0] if c.element_ids else ""
            citations.append(Citation(
                label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                source_id=c.source_id, element_id=eid,
                location_label=c.section_path, quoted_span=c.text[:200]))

        answer, llm_grounded, anchors = "", False, []
        _t = time.perf_counter()
        if self.llm_client.configured and selected:
            try:
                answer, llm_grounded, anchors = self._answer_chunks(
                    question, selected, history)
            except Exception:
                self.event_log.logger.exception("_answer_chunks failed for %s", notebook_id)
                answer, llm_grounded, anchors = "", False, []
        ask_stage("answer_llm", _t)

        evidence_level, top_relevance = classify_evidence(
            selected, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        grounded = evidence_level == "grounded"

        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            llm_mode = "deterministic"
            conclusion = (
                f"Retrieved {len(selected)} relevant passage(s) for this question."
                if selected else
                "No indexed content matches this question yet. Upload sources or build chunks.")

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
            citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=retrieval_query, top_relevance=top_relevance)
        response.mode = "chunk"
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        ask_stage("total", ask_started)
        return response

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Dispatch to the retrieval handler named by payload.mode, resolved
        through the ask_modes registry. Unknown modes raise UnknownAskMode (the
        API layer returns 422) — never a silent fall-through to the legacy path."""
        from app.services.ask_modes import resolve_mode
        spec = resolve_mode(getattr(payload, "mode", None))
        return getattr(self, spec.handler)(notebook_id, payload)

    # ask_fast (legacy KG-native, P4-5退役) 和 _ask_global (GraphRAG map-reduce, P4-5退役)
    # 已删除。旧会话/书签中的 mode="fast"/"global" 通过 ask_modes._RETIRED_MODES 映射到
    # "chunk"，不会触发 422。被这两个方法调用的 helper 有意保留（部分被其他 ask_* 共用）。

    def _concept_cluster_id(self, notebook_id: str, object_id: str) -> str:
        """Canonical unified-cluster id for a concept `object_id`, reusing the
        same `concept_clusters` membership map `concept_detail` relies on
        (`cluster_map` -> {member_object_id: canonical_id}). When clustering is
        not populated for the notebook (no cluster row for this object), fall
        back to `object_id` so dedup degrades gracefully (no merge, no crash)."""
        return self.cluster_map(notebook_id).get(object_id, object_id)

    def _rrf_scored(
        self,
        query: str,
        kg_objs: Dict[str, List[dict]],
        knowledge_sims: Optional[Dict[str, float]],
        element_sims: Optional[Dict[str, float]] = None,
    ) -> List[RetrievedKnowledge]:
        """BM25 + 语义 RRF 融合排序,产出与 score_knowledge 池化同构的列表。

        - BM25: 对所有类型的对象文本计算 Okapi BM25 分。
        - 语义: 直接使用 knowledge_sims (object_id->cosine_sim)。
        - RRF: 两组排名融合得最终分。
        - 不套 RELEVANCE_FLOOR(RRF 分量级很小,floor 会清空结果)。
        - score=RRF 分(排序用); relevance=[0,1] 融合(keyword+语义)分,供
          classify_evidence 的 tau 阈值判 grounded(RRF 微分会让全部判 inferred)。
        - weight 取 _TYPE_WEIGHT(类型权威)。
        """
        # 汇集所有类型对象,构建 (id, text) 列表及 id->obj 映射
        docs: List[tuple] = []
        id_to_obj: Dict[str, dict] = {}
        id_to_type: Dict[str, str] = {}
        for t in _KG_TYPES:
            for obj in (kg_objs.get(t) or []):
                oid = obj["id"]
                payload = obj.get("payload", {})
                evidence = obj.get("evidence", [])
                ev_text = " ".join(e.quoted_span for e in evidence)
                text = _payload_text(payload) + (" " + ev_text if ev_text else "")
                docs.append((oid, text))
                id_to_obj[oid] = obj
                id_to_type[oid] = t

        bm25 = bm25_scores(query, docs)
        sims: Dict[str, float] = knowledge_sims or {}

        fused = rrf_fuse([bm25, sims], k=self.settings.retrieval_rrf_k)
        text_by_id = dict(docs)

        result: List[RetrievedKnowledge] = []
        for oid, rrf_score in fused.items():
            if rrf_score <= 0:
                continue
            obj = id_to_obj.get(oid)
            if obj is None:
                continue
            object_type = id_to_type[oid]
            weight = _TYPE_WEIGHT.get(object_type, 0.5)
            # score = RRF (ordering); relevance = [0,1] fused keyword+semantic so
            # classify_evidence's tau thresholds stay valid (RRF micro-scores would
            # otherwise classify every answer as "inferred").
            # Best-of: object-level sim OR max(element-level sims), same as
            # score_knowledge — protects the dual-index invariant so an object
            # grounded only via an evidence-element embedding is not downgraded.
            semantic = sims.get(oid, 0.0)
            has_vec = oid in sims
            if element_sims:
                for ev in obj.get("evidence", []):
                    eid = getattr(ev, "element_id", "") or ""
                    s = element_sims.get(eid)
                    if s is not None:
                        has_vec = True
                        semantic = max(semantic, s)
            relevance = _fuse(keyword_score(query, text_by_id.get(oid, "")),
                              semantic, has_vec)
            result.append(
                RetrievedKnowledge(
                    object_id=oid,
                    object_type=object_type,
                    payload=obj.get("payload", {}),
                    evidence=obj.get("evidence", []),
                    score=rrf_score,
                    relevance=relevance,
                    weight=weight,
                    status=str(obj.get("status", "approved")),
                    owner=str(obj.get("owner", "")),
                    last_reviewed=str(obj.get("last_reviewed", "")),
                )
            )
        result.sort(key=lambda it: it.score, reverse=True)
        return result

    def _rerank_hits(self, query: str, hits: List[RetrievedKnowledge]) -> List[RetrievedKnowledge]:
        """LLM relevance rerank over a candidate pool. No-op when disabled, the
        client is unconfigured, or the response is unusable (keeps input order)."""
        if not hits or not self.settings.rerank_enabled \
                or not getattr(self.llm_client, "configured", False):
            return hits
        block = "\n".join(
            f"[{i}] [{h.object_type}] {str(h.payload.get('name', ''))[:200]}"
            for i, h in enumerate(hits)
        )
        try:
            raw = self.llm_client.chat_json(
                [{"role": "user", "content": rerank_prompt(query, block)}],
                RERANK_SCHEMA_HINT,
                timeout=self.settings.rerank_timeout_seconds, max_retries=0,
            )
            data = json.loads(raw)
        except Exception:
            return hits
        scores: Dict[int, float] = {}
        for it in (data.get("items") if isinstance(data, dict) else None) or []:
            if isinstance(it, dict) and isinstance(it.get("index"), int):
                try:
                    scores[it["index"]] = float(it.get("score", 0.0))
                except (TypeError, ValueError):
                    pass
        if not scores:
            return hits
        order = sorted(range(len(hits)), key=lambda i: scores.get(i, -1.0), reverse=True)
        return [hits[i] for i in order]

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge]) -> tuple:
        """Build the id-tagged enriched context block + id_map for the answer
        LLM. Each surviving hit gets a stable `k{i}` id; enrichment (definition /
        first-occurrence snippet / procedure steps) is pulled via node_context.
        Hits belonging to the same unified cluster (any type) are collapsed —
        the first (highest-scored) per cluster is kept, later duplicates dropped.
        Objects without a cluster entry use their own object_id as cluster key,
        so they are never erroneously deduplicated.
        Returns (context_block_str, id_map)."""
        budget = self.settings.answer_context_budget_chars
        min_items = self.settings.answer_context_min_items
        lines, id_map = [], {}
        seen_clusters: set = set()
        cmap = self.cluster_map(notebook_id)
        used = 0
        i = 0
        for hit in top_hits:
            cid = cmap.get(hit.object_id, hit.object_id)
            if cid in seen_clusters:
                continue
            seen_clusters.add(cid)
            # Federation: enrich each hit against ITS OWN notebook (a base hit
            # lives in the base KG, not the active notebook). Falls back to the
            # active notebook_id for legacy/untagged hits.
            hit_nb = getattr(hit, "notebook_id", "") or notebook_id
            try:
                ctx = self.node_context(hit_nb, hit.object_id)
            except KeyError:
                continue
            # Stop once the budget is spent, but always keep at least min_items.
            if used >= budget and len(lines) >= min_items:
                break
            i += 1
            key = f"k{i}"
            name = str(hit.payload.get("name", "")).strip()
            occ = ctx.get("occurrences") or []
            snippet = occ[0].get("element_text") if occ else ""
            definition = ctx.get("definition") or snippet
            remaining = max(0, budget - used)
            def_cap = max(0, min(300, remaining))   # per-line cap shrinks as budget fills
            extra = f" — def: {definition[:def_cap]}" if (definition and def_cap) else ""
            if ctx.get("steps") and def_cap:   # steps share the per-line budget gate
                extra += "; steps: " + " -> ".join(
                    s.get("name", "") for s in ctx["steps"][:8]
                )
            tier = getattr(hit, "tier", "personal")
            # Tier prefix surfaces authority to the LLM ([base] vs [personal]) so
            # the conflict-precedence rule in answer_prompt has something to read.
            line = f"{key}: [{hit.object_type}][{tier}] {name}{extra}"
            lines.append(line)
            used += len(line)
            id_map[key] = {
                "object_id": hit.object_id, "object_type": hit.object_type,
                "name": name, "definition": definition, "snippet": snippet,
                "source_title": (occ[0].get("source_title", "") if occ else ""),
                "location_label": (occ[0].get("section_path", "") if occ else ""),
                "tier": tier,
            }
        # In-network relations: edges whose BOTH endpoints are in the context.
        # id_map values carry unique object_ids (one entry per surviving hit;
        # concept-cluster de-dup runs above), so this inversion drops no keys.
        oid_to_key = {v["object_id"]: k for k, v in id_map.items()}
        if len(oid_to_key) >= 2:
            ids = list(oid_to_key)
            ph = ",".join("?" for _ in ids)
            with self._connect() as db:
                rels = db.execute(
                    f"SELECT source_object_id, target_object_id, edge_type "
                    f"FROM knowledge_relations WHERE notebook_id=? "
                    f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                    [notebook_id, *ids, *ids],
                ).fetchall()
            rel_lines = []
            seen_rel = set()
            for r in rels:
                s = oid_to_key.get(r["source_object_id"])
                t = oid_to_key.get(r["target_object_id"])
                if s and t and s != t and (s, r["edge_type"], t) not in seen_rel:
                    seen_rel.add((s, r["edge_type"], t))
                    rel_lines.append(f"{s} -[{r['edge_type']}]-> {t}")
            if rel_lines:
                # Cap so a dense subgraph can't blow the answer context past budget.
                lines.append("relations: " + "; ".join(rel_lines[:30]))
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _rewrite_followup_query(self, history: str, question: str) -> str:
        """Resolve an elliptical follow-up into a standalone retrieval query using
        prior turns. Runs whenever there IS history (any non-first turn) — the
        rewrite model itself returns the question unchanged when it's already
        standalone, so we no longer pre-gate with a brittle keyword heuristic.
        Uses the dedicated fast rewrite model (rewrite_llm_client); always falls
        back to the raw question on any failure."""
        if not history.strip():
            return question
        client = self.rewrite_llm_client
        if not getattr(client, "configured", False):
            return question
        try:
            raw = client.chat_json(
                [{"role": "user", "content": followup_rewrite_prompt(history, question)}],
                FOLLOWUP_REWRITE_SCHEMA_HINT,
            )
            data = json.loads(raw)
            if not isinstance(data, dict):
                return question
            rewritten = str(data.get("query", "")).strip()
            return rewritten or question
        except Exception:
            return question

    def _answer_kg(
        self,
        notebook_id: str,
        question: str,
        top_hits: List[RetrievedKnowledge],
        history: str = "",
    ) -> tuple:
        """Synthesise a (possibly reasoned) answer from KG hits using the LLM.
        Returns (answer_text, llm_grounded, anchors). Returns the LLM's raw
        self-report; the relevance-aware grounding is decided by the caller via
        classify_evidence. `history`
        (prior conversation turns) shapes the wording but not the retrieval."""
        context_block, id_map = self._answer_context(notebook_id, top_hits)
        if (self.settings.kg_query_refine_enabled
                and getattr(self.llm_client, "configured", False) and id_map):
            from app.services.prompts import evidence_refine_prompt, EVIDENCE_REFINE_SCHEMA_HINT
            ev_lines = []
            for k, v in id_map.items():
                txt = (v.get("definition") or v.get("snippet") or "").strip()
                ev_lines.append(f"{k} {v.get('name','')}: {txt}".strip())
            ev_block = "\n".join(ev_lines)[: self.settings.query_refine_max_chars]
            try:
                raw_refine = self.llm_client.chat_json(
                    [{"role": "user", "content": evidence_refine_prompt(question, ev_block)}],
                    EVIDENCE_REFINE_SCHEMA_HINT,
                    timeout=self.settings.reasoning_timeout_seconds,
                    max_retries=self.settings.reasoning_max_retries)
                rel = json.loads(raw_refine).get("relevant")
                if not isinstance(rel, list):   # guard: LLM may return a str/None
                    rel = []
                rel = [str(x).strip() for x in rel if str(x).strip()]
            except Exception:
                rel = []
            if rel:
                context_block = ("Focused relevant evidence (for this question):\n"
                                 + "\n".join(f"- {x}" for x in rel[:12])
                                 + "\n\n" + context_block)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _answer_reasoning(self, notebook_id, question, top_hits, elements, history=""):
        """Synthesise the reasoning-mode answer: reuse _answer_context for KG
        hits (so [k] anchors/citations stay identical to fast mode), then append
        fallback document passages as reference-only context (no [k] id, so they
        never become anchors). Returns (answer, llm_grounded, anchors)."""
        context_block, id_map = self._answer_context(notebook_id, top_hits)
        if elements:
            extra = "\n".join(
                f"(原文 {i+1}) {el.source_title} · {el.location_label}: {el.text[:200]}"
                for i, el in enumerate(elements[:6])
            )
            context_block = f"{context_block}\n\n补充原文段落(供参考,无引用编号):\n{extra}"
        raw = self.reasoning_llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            timeout=self.settings.reasoning_timeout_seconds,
            max_retries=self.settings.reasoning_max_retries,
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def ask_reasoning(self, notebook_id: str, payload: AskRequest, on_trace=None) -> AskResponse:
        """Reasoning-mode ask: agentic plan→retrieve→reflect(自由深挖)→answer。
        检索委托 ReasoningRetriever;答案/证据分档复用 fast 路径口径;响应携带
        reasoning_trace。任何阶段异常不向用户抛出(逐层容错 + 兜底空候选)。"""
        from app.services.reasoning_retrieval import ReasoningRetriever
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)

        if not (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg()):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有可用的底层(tier=base)KG;"
                           "请先点『构建知识图谱』,或把一个已建图的笔记本设为底层"
                           "(POST /notebooks/{id}/tier)。",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic", kg_required=True)
            response.mode = "reasoning"
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id)
            return response

        try:
            result = ReasoningRetriever(self, self.settings).run(
                notebook_id, question, history, on_step=on_trace)
            top_hits, elements, trace = result.top_hits, result.elements, result.trace
        except Exception:
            top_hits, elements, trace = [], [], []

        registry = self.effective_schemas()
        seen_ids: set = set()
        related_knowledge: List[KnowledgeRecord] = []
        for item in top_hits:
            if item.object_id in seen_ids:
                continue
            seen_ids.add(item.object_id)
            related_knowledge.append(self._knowledge_record(
                item.object_type,
                {"id": item.object_id, "payload": item.payload, "status": item.status,
                 "owner": getattr(item, "owner", ""),
                 "last_reviewed": getattr(item, "last_reviewed", ""),
                 "evidence": item.evidence},
                registry.get(item.object_type)))
        related_knowledge = related_knowledge[:12]

        cited_element_ids = {ev.element_id for item in top_hits
                             for ev in item.evidence if ev.element_id}
        citations = self._citations_from(top_hits, cited_element_ids, "KG evidence")

        answer, llm_grounded, anchors = "", False, []
        if self.reasoning_llm_client.configured and (top_hits or elements):
            try:
                answer, llm_grounded, anchors = self._answer_reasoning(
                    notebook_id, question, top_hits, elements, history)
            except Exception:
                answer, llm_grounded, anchors = "", False, []

        evidence_level, top_relevance = classify_evidence(
            top_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        grounded = evidence_level == "grounded"

        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            llm_mode = "deterministic"
            conclusion = (
                f"Found {len(top_hits)} relevant KG object(s) for this question."
                if top_hits else
                "The notebook does not yet contain approved knowledge that matches "
                "this question. Upload and review sources to build coverage.")

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors,
            related_knowledge=related_knowledge, citations=citations,
            llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=question, top_relevance=top_relevance,
            reasoning_trace=trace or None,
        )
        response.mode = "reasoning"
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response

    def ask_graph(self, notebook_id: str, payload: "AskRequest",
                  seed_ids: Optional[List[str]] = None) -> AskResponse:
        """Multi-hop graph reasoning mode.

        1. Retrieve top seeds via _retrieve_scored (same as fast path).
        2. Build the rx graph for this notebook (version-cached _rx_graph).
        3. BFS from seed object_ids along DEFAULT_REASONING_EDGES.
        4. Render subgraph → (context_block, id_map) via render_subgraph_context.
        5. Feed context_block to the existing answer LLM + grounding path.

        The [k] anchor markers, _parse_answer_anchors, classify_evidence, and
        AskResponse assembly are all reused verbatim from the fast path.
        """
        from app.services.kg.graph_reason import (
            DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context,
        )
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)

        if not (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg()):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有可用的底层(tier=base)KG;"
                           "请先点『构建知识图谱』,或把一个已建图的笔记本设为底层"
                           "(POST /notebooks/{id}/tier)。",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic", kg_required=True)
            response.mode = "graph"
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id)
            return response

        # Seed: top-N by relevance (federated across base notebooks).
        top_hits = self.federated_retrieve(notebook_id, question)[:self.settings.retrieval_top_n]
        if not top_hits and not seed_ids:
            response = AskResponse(
                answer_id="",
                conclusion="The notebook does not yet contain approved knowledge "
                           "that matches this question. Upload and review sources "
                           "to build coverage.",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic",
            )
            response.mode = "graph"
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id)
            return response

        use_seeds = seed_ids if seed_ids else [h.object_id for h in top_hits[:5]]

        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
        subgraph = multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=use_seeds,
            edge_types=DEFAULT_REASONING_EDGES,
            max_depth=getattr(self.settings, "graph_max_depth", 3),
            max_fan_out=getattr(self.settings, "graph_max_fan_out", 8),
        )
        # Render subgraph into (context_block, id_map) — same k{i} format as
        # _answer_context so _parse_answer_anchors / _MARKER_RE work unchanged.
        context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

        # Answer-time chain verification: an adversarial LLM check per chain edge.
        # Flagged edges get their confidence demoted to 0.05; the context is then
        # re-rendered so the demotion is visible to the answer LLM. chain_trust is
        # the weakest-link confidence over all edges (1.0 when there are no edges).
        verify_result = {"chain_trust": 1.0, "flagged": [], "edge_results": [],
                         "authority_notes": []}
        if getattr(self.reasoning_llm_client, "configured", False):
            from app.services.kg.graph_reason import verify_chain_edges
            verify_result = verify_chain_edges(
                subgraph, self.reasoning_llm_client,
                votes=1, timeout=self.settings.reasoning_timeout_seconds,
            )
            if verify_result["flagged"]:
                flagged_types = {f["edge_type"] for f in verify_result["flagged"]}
                for _node, edge, _src in subgraph:
                    if edge and edge.get("edge_type") in flagged_types:
                        edge["confidence"] = 0.05
                context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

        # Synthesise the answer through the existing LLM + grounding path.
        answer, llm_grounded, anchors = "", False, []
        if getattr(self.llm_client, "configured", False) and id_map:
            try:
                raw = self.llm_client.chat_json(
                    [{"role": "user",
                      "content": answer_prompt(question, context_block, history)}],
                    ANSWER_SCHEMA_HINT,
                )
                data = json.loads(raw)
                if isinstance(data, dict):
                    answer = str(data.get("answer", "")).strip()
                    llm_grounded = bool(data.get("grounded", False))
                    anchors = self._parse_answer_anchors(answer, id_map)
                    # Scrub citation-shaped tokens that did NOT bind to a real
                    # id_map entry (out-of-map ids like [k99], malformed [ k1]).
                    # Unlike the fast path — whose id_map IS top_hits, so the LLM
                    # rarely invents ids — graph mode shows a wider subgraph and
                    # the answer LLM occasionally emits markers the strict
                    # _MARKER_RE can't bind; left in place they read as fabricated
                    # citations. Strip them so only resolved [k] markers ship.
                    answer = _strip_unbound_markers(answer, {a.key for a in anchors})
            except Exception:
                answer, llm_grounded, anchors = "", False, []

        # classify_evidence keys "grounded" off the relevance of the CITED hit.
        # In the fast path id_map IS built from top_hits, so every anchor is a
        # scored hit. In graph mode the cited node can be a multi-hop NEIGHBOUR
        # that is in id_map but NOT in top_hits → its relevance would read 0 and
        # the answer would be demoted to "overview" even though it cites a real,
        # chain-connected node (the q17/q18 "overview while citing specifics"
        # contradiction). Mirror the fast-path invariant: give each cited
        # neighbour a relevance inherited from the strongest seed, discounted by
        # chain_trust (the verifier's weakest-link confidence), so a trusted
        # chain can reach "grounded" while a flagged/weak one still falls back.
        hits_for_classify = list(top_hits)
        if anchors:
            scored_oids = {h.object_id for h in top_hits}
            seed_rel = max((h.relevance for h in top_hits), default=0.0)
            neighbour_rel = seed_rel * float(verify_result.get("chain_trust", 1.0))
            for a in anchors:
                if a.object_id in scored_oids:
                    continue
                scored_oids.add(a.object_id)
                hits_for_classify.append(RetrievedKnowledge(
                    object_id=a.object_id, object_type=a.object_type,
                    payload={"name": a.name}, relevance=neighbour_rel,
                    tier=getattr(a, "tier", "personal"), notebook_id=notebook_id))

        evidence_level, top_relevance = classify_evidence(
            hits_for_classify, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        # Report the genuine seed relevance, not the synthetic neighbour value.
        top_relevance = max((h.relevance for h in top_hits), default=top_relevance)
        grounded = evidence_level == "grounded"
        if answer:
            conclusion = _MARKER_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        else:
            conclusion = (
                f"Graph traversal found {len(subgraph)} node(s) across "
                f"{len(use_seeds)} seed(s).")
            llm_mode = "deterministic"

        from app.models.schemas import TraceStep
        graph_trace = [TraceStep(
            step_type="graph_verify",
            summary=(f"chain_trust={verify_result['chain_trust']:.2f}; "
                     f"{len(verify_result['flagged'])} edge(s) flagged; "
                     f"{len(subgraph)} node(s) traversed"),
            detail={**verify_result,
                    "authority_notes": verify_result.get("authority_notes", [])},
        )]

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
            citations=[], llm_mode=llm_mode,
            conversation_id=conversation_id, retrieval_query=question,
            top_relevance=top_relevance, reasoning_trace=graph_trace,
        )
        response.mode = "graph"
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response

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
                tier=ctx.get("tier", "personal"),
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
        with self._write() as db:
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
                "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
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
        used_reasoning = bool(turns[-1].response.reasoning_trace) if turns else False
        return ConversationDetail(
            id=conv["id"],
            notebook_id=conv["notebook_id"],
            title=conv["title"] or "",
            updated_at=conv["updated_at"] or "",
            turn_count=len(turns),
            used_reasoning=used_reasoning,
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
                "(SELECT COUNT(*) FROM answers a WHERE a.conversation_id = c.id) AS turn_count, "
                "(SELECT COALESCE(json_array_length(json_extract(a.payload, '$.reasoning_trace')), 0) > 0 "
                "   FROM answers a WHERE a.conversation_id = c.id "
                "  ORDER BY a.rowid DESC LIMIT 1) AS used_reasoning "
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
                used_reasoning=bool(row["used_reasoning"]),
            )
            for row in rows
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        with self._write() as db:
            cur = db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, _now(), conversation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        with self._write() as db:
            cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            if cur.rowcount == 0:
                raise KeyError(conversation_id)
            db.execute("DELETE FROM answers WHERE conversation_id=?", (conversation_id,))

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse:
        if payload.rating not in {"useful", "not_useful"}:
            raise ValueError("rating must be useful or not_useful")
        now = _now()
        feedback_id = f"fb-{uuid4().hex[:10]}"
        with self._write() as db:
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
            knowledge_counts=knowledge_counts,
            source_status_counts=source_status_counts,
        )

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
        with self._write() as db:
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
        with self._write() as db:
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
        with self._write() as db:
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
            tier=row["tier"] if "tier" in keys else "personal",
            kg_ready=self._has_kg(db, row["id"]),
            base_kg_available=self._any_base_notebook_has_kg(db),
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
