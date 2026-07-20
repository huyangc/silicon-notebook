from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase

from app.services.extraction_profiles import LIST_FIELDS, OBJECT_SCHEMAS, OBJECT_TYPE_LABELS
from app.services.auth_utils import hash_password
from app.services.kg.filters import _norm as _wl_norm

SCHEMA_VERSION = 20

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

class SqliteMigrator:
    """Owns schema migration, startup recovery, and deterministic seed data."""
    def __init__(self, database: SqliteDatabase, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def _connect(self):
        return self.database.connect()

    @staticmethod
    def add_column_if_missing(db: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
        cols = {
            (r["name"] if isinstance(r, sqlite3.Row) else r[1])
            for r in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if cols and column not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")

    @staticmethod
    def _add_column_if_missing(db: sqlite3.Connection, table: str, column: str, coldef: str) -> None:
        SqliteMigrator.add_column_if_missing(db, table, column, coldef)

    def _migration_1(self) -> None:
        with self._connect() as db:
            kcs_info = db.execute("PRAGMA table_info(kg_cluster_scratch)").fetchall()
            if kcs_info and "run_id" not in {r["name"] for r in kcs_info}:
                db.executescript(
                    "DROP TABLE IF EXISTS kg_cluster_scratch; "
                    "DROP INDEX IF EXISTS idx_kg_cluster_scratch_nb_run;"
                )
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

                CREATE TABLE IF NOT EXISTS auth_sessions (
                  token TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

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
                  source_url TEXT NOT NULL DEFAULT '',
                  file_size INTEGER NOT NULL DEFAULT 0,
                  file_hash TEXT NOT NULL DEFAULT '',
                  summary TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                -- memory_id is intentionally NOT listed here (Task 1):
                -- _migration_1 still runs the doc_type add_column_if_missing
                -- call below (line ~699) for every truly-fresh DB, appending
                -- doc_type AFTER whatever this literal ends with. If memory_id
                -- were listed here it would land BEFORE doc_type on a fresh
                -- DB, but _migration_14's ALTER lands it AFTER doc_type on any
                -- already-deployed DB (doc_type already exists there) --
                -- a column-order mismatch the schema_contract.txt golden
                -- would catch. Leaving sources' literal untouched and adding
                -- memory_id solely via _migration_14 (which every fresh DB
                -- also runs, in the same migrate() sweep) makes fresh and
                -- upgraded DBs land on the identical [..., doc_type,
                -- memory_id] order.

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
                -- _scale_index_version's "SELECT COUNT(*), MAX(created_at) FROM
                -- chunks WHERE notebook_id=?" — same cold-miss cost as the other
                -- three version-probe aggregates (idx_chunks_nb above is
                -- single-column, not covering for MAX(created_at)). Row-order
                -- audit (PR#136 lesson, re-verified rather than assumed): every
                -- `FROM chunks` site without ORDER BY is either an
                -- aggregate/EXISTS/DELETE, an id-keyed `id IN (...)` hydration, a
                -- JOIN feeding score_chunks (unordered candidate pool, ranked by
                -- score not position), or copy_notebook's full per-row remap-copy
                -- (every row is included, not positionally truncated). The one
                -- borderline site, _elem_chunk_map's per-element chunk list (built
                -- from an unordered `SELECT id, element_ids FROM chunks WHERE
                -- notebook_id=?`), was explicitly decoupled from chunks physical
                -- scan order by the immediately-preceding commit (_kg_source_chunks
                -- determinism now comes from object_ids/evidence array order, not
                -- table scan order — see that method's and test_query_hotpath_
                -- cache.py's "NOT ... a contract" comments), so it does not need an
                -- ORDER BY pin here either.
                CREATE INDEX IF NOT EXISTS idx_chunks_nb_created ON chunks(notebook_id, created_at);

                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                  chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_nb ON chunk_embeddings(notebook_id);

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
                -- _extraction_warning's "SELECT error_message FROM extraction_runs
                -- WHERE source_id=? ORDER BY created_at DESC LIMIT 1" — paid on
                -- every source-list page render (_source_from_row, per row). No
                -- prior index at all on this table; without one, SQLite must scan
                -- every extraction_runs row for the source and sort in memory to
                -- satisfy ORDER BY + LIMIT 1 — a full table scan per row of a
                -- sources page. This composite makes it an index-order scan
                -- (LIMIT 1 short-circuits after the first matching row). Row-order
                -- audit: the only other site reading extraction_runs is an
                -- id-scoped DELETE (order-irrelevant — see
                -- _delete_relations_for_source's sibling "DELETE FROM
                -- extraction_runs WHERE source_id=?"); the two writers (INSERT on
                -- run start, UPDATE on completion/failure) are id/source_id-scoped
                -- point writes, unaffected by a new index. _extraction_warning
                -- itself already carries an explicit ORDER BY, so this index only
                -- makes that ordering cheap to satisfy — it does not change what
                -- row wins.
                CREATE INDEX IF NOT EXISTS idx_extraction_runs_source_created ON extraction_runs(source_id, created_at);


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

                CREATE TABLE IF NOT EXISTS relation_embeddings (
                  relation_id TEXT PRIMARY KEY REFERENCES knowledge_relations(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relation_embeddings_nb ON relation_embeddings(notebook_id);

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
                -- notebook_analytics' "SELECT COUNT(*) FROM answers WHERE
                -- notebook_id=?" — a §16 analytics-page COUNT that previously had
                -- zero index on this table and did a full table scan. Row-order
                -- audit: every notebook_id-scoped SELECT against answers already
                -- carries an explicit ORDER BY (_conversation_history:
                -- "ORDER BY created_at ASC"; get_conversation: "ORDER BY created_at
                -- ASC, rowid ASC") or is a single-row id-keyed lookup
                -- (answer_owner / user_can_read_answer) or a bulk id-scoped DELETE
                -- (delete_conversation / bulk conversation cleanup) — none rely on
                -- physical scan order, so this index changes no result ordering,
                -- only how fast the COUNT is computed.
                CREATE INDEX IF NOT EXISTS idx_answers_nb ON answers(notebook_id);

                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL,
                  title TEXT DEFAULT '',
                  created_by TEXT DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                -- 深度报告(report_engine):后台生成的多节技术报告落库。
                -- outline/sections/gaps 为 JSON 列;content_md 为汇总后的完整 markdown。
                CREATE TABLE IF NOT EXISTS reports (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  question TEXT NOT NULL,
                  outline_json TEXT NOT NULL DEFAULT '[]',
                  sections_json TEXT NOT NULL DEFAULT '[]',
                  gaps_json TEXT NOT NULL DEFAULT '[]',
                  references_json TEXT NOT NULL DEFAULT '[]',
                  depth INTEGER NOT NULL DEFAULT 2,
                  section_status_json TEXT NOT NULL DEFAULT '[]',
                  content_md TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  progress TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_nb_created ON reports(notebook_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS feedback (
                  id TEXT PRIMARY KEY,
                  answer_id TEXT NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  rating TEXT NOT NULL,
                  comment TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                -- notebook_analytics' two §16 COUNTs — "... WHERE notebook_id=? AND
                -- rating='useful'" / "...rating='not_useful'" — previously zero
                -- index on this table, full scan + filter each. This composite
                -- covers both (notebook_id, rating) predicates as an index-range
                -- scan. Separately, feedback submission (submit_feedback) does a
                -- single answer_id-keyed point lookup on `answers` (unaffected)
                -- then an INSERT here — unaffected by a new secondary index.
                CREATE INDEX IF NOT EXISTS idx_feedback_nb_rating ON feedback(notebook_id, rating);
                -- feedback.answer_id is a FOREIGN KEY ... ON DELETE CASCADE, but
                -- SQLite does not auto-index FK columns. Deleting an answer
                -- (delete_conversation / bulk conversation cleanup: "DELETE FROM
                -- answers WHERE conversation_id=?", fired once per answer row)
                -- fires the CASCADE, which — without this index — must full-scan
                -- feedback per deleted answer to find child rows. This index turns
                -- that into a point lookup (verified via EXPLAIN QUERY PLAN on the
                -- DELETE itself, which shows the FK-cascade sub-scan). Row-order
                -- audit: the notebook_analytics low_rated JOIN (feedback→answers on
                -- answer_id) carries its own explicit "ORDER BY f.created_at DESC
                -- LIMIT 10", unaffected by which index the planner picks for the
                -- join key; the planner may prefer idx_feedback_nb_rating over this
                -- one for that particular query shape (both are valid access paths
                -- — this index's role is the FK-cascade scan, not that join).
                CREATE INDEX IF NOT EXISTS idx_feedback_answer ON feedback(answer_id);

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
                  canonical_desc_sig TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clusters_nb ON concept_clusters(notebook_id);
                CREATE INDEX IF NOT EXISTS idx_clusters_member ON concept_clusters(member_object_id);
                -- _scale_index_version's per-call "SELECT COUNT(*), MAX(created_at)
                -- FROM concept_clusters WHERE notebook_id=?" (called every retrieval
                -- / PPR / status request) otherwise walks idx_clusters_nb then
                -- back to the table row for created_at. This composite index makes
                -- it a covering index scan (no row lookups) for both the COUNT and
                -- the MAX.
                CREATE INDEX IF NOT EXISTS idx_clusters_nb_created ON concept_clusters(notebook_id, created_at);

                CREATE TABLE IF NOT EXISTS concept_merge_candidates (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  canonical_a TEXT NOT NULL, canonical_b TEXT NOT NULL,
                  score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_nb_status ON concept_merge_candidates(notebook_id, status);

                CREATE TABLE IF NOT EXISTS merge_review_jobs (
                  notebook_id TEXT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                  status TEXT NOT NULL DEFAULT 'idle',
                  total INTEGER NOT NULL DEFAULT 0,
                  done INTEGER NOT NULL DEFAULT 0,
                  started_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS kg_conflict_candidates (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,
                  left_ref TEXT NOT NULL,
                  right_ref TEXT NOT NULL,
                  conflict_type TEXT,
                  resolution TEXT,
                  winner_ref TEXT,
                  resolved_payload TEXT,
                  confidence REAL,
                  rationale TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conflict_candidates_nb_status ON kg_conflict_candidates(notebook_id, status);

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
                  -- Monotonic per-notebook mutation counter, bumped by
                  -- _mark_unified_kg_dirty on EVERY KG write (the single choke point).
                  -- It is the primary change signal for _cluster_input_version:
                  -- deterministic + O(1), with NO clock-granularity hole (a timestamp
                  -- MAX at 1s resolution misses same-second in-place edits).
                  kg_mutation_seq INTEGER NOT NULL DEFAULT 0,
                  -- P0-A: monotonic counter bumped ONLY by concept_clusters writes
                  -- (_bump_cluster_mutation_seq, called from the 4 cluster-write
                  -- sites). Independent from kg_mutation_seq because rebuild
                  -- DELIBERATELY keeps kg_mutation_seq stable across a rebuild
                  -- (idempotency, see _cluster_input_version) while still rewriting
                  -- concept_clusters — this column is _scale_index_version's O(1)
                  -- change signal for that rewrite, replacing a per-call
                  -- COUNT/MAX(created_at) scan of concept_clusters.
                  cluster_mutation_seq INTEGER NOT NULL DEFAULT 0,
                  -- O(1) content-hash of the clustering INPUTS at the last rebuild
                  -- (see _cluster_input_version). rebuild_unified_kg skips the whole
                  -- recompute when this still matches; empty '' means never rebuilt.
                  cluster_input_version TEXT NOT NULL DEFAULT '',
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

                -- Transient scratch for memory-bounded rebuild_unified_kg: maps
                -- object_id -> seed for the type currently being clustered. NOT a
                -- TEMP TABLE on purpose (the rebuild spans multiple connections +
                -- mid-rebuild LLM calls; a connection-scoped temp would force one
                -- long-held write lock). Cleared per-type and at rebuild start/end.
                -- run_id isolates concurrent same-notebook rebuilds so two overlapping
                -- calls never wipe/interleave each other's scratch rows.
                CREATE TABLE IF NOT EXISTS kg_cluster_scratch (
                  notebook_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  object_id TEXT NOT NULL,
                  seed TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_cluster_scratch_nb_run ON kg_cluster_scratch(notebook_id, run_id);

                CREATE INDEX IF NOT EXISTS idx_sources_notebook_status ON sources(notebook_id, status);
                CREATE INDEX IF NOT EXISTS idx_sources_notebook_created ON sources(notebook_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_source_elements_source ON source_elements(source_id);
                CREATE INDEX IF NOT EXISTS idx_source_elements_source_created ON source_elements(source_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_type_status ON knowledge_objects(notebook_id, object_type, status);
                -- Covers list_knowledge's page query ORDER BY created_at, id: without it
                -- SQLite does USE TEMP B-TREE FOR ORDER BY, reading+sorting ALL matching
                -- rows (their full payload/evidence via SELECT *) just to return 50 — O(N)
                -- per page load, catastrophic at 10^5+ objects. With it the ORDER BY is
                -- index-satisfied and only LIMIT rows are read.
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_type_created ON knowledge_objects(notebook_id, object_type, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_status ON knowledge_objects(notebook_id, status);
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_source ON knowledge_objects(source_id);
                -- _scale_index_version's "SELECT COUNT(*), MAX(updated_at) FROM
                -- knowledge_objects WHERE notebook_id=?" (cold-cache path, fired by
                -- every concurrent KG-page/status/rebuild caller before the P1-8 memo
                -- is warm) otherwise walks idx_knowledge_objects_nb_status then hits
                -- the base table per row for updated_at — a full per-row fetch over a
                -- GB-scale table (measured 96-147s overlapping on a 490k-object
                -- deployment). This composite makes it a covering index scan for
                -- both the COUNT and the MAX. Row-order audit (PR#136 lesson): every
                -- other `FROM knowledge_objects` site without ORDER BY is either an
                -- aggregate/EXISTS/DELETE (order-irrelevant) or an id-keyed/`WHERE
                -- id IN (...)` hydration (order-irrelevant); the two order-sensitive
                -- streaming sites (_stream_seed_reps's alias-map + canonical-name
                -- passes) already pin `ORDER BY rowid` explicitly, so they are
                -- immune to this index's row order regardless of which index the
                -- planner picks for the WHERE clause.
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_updated ON knowledge_objects(notebook_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source ON knowledge_relations(notebook_id, source_object_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_target ON knowledge_relations(notebook_id, target_object_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_source ON knowledge_relations(source_id);
                -- _scale_index_version's "SELECT COUNT(*), MAX(created_at) FROM
                -- knowledge_relations WHERE notebook_id=?" — same cold-miss cost as
                -- the knowledge_objects aggregate above; this is knowledge_relations'
                -- FIRST composite index (previously only single-column nb_source /
                -- nb_target existed, neither covering for a bare notebook_id COUNT).
                -- Row-order audit: every `FROM knowledge_relations` site without
                -- ORDER BY is an aggregate/COUNT/EXISTS/DELETE, an id-keyed hydration
                -- (`source_object_id IN (...)` / `target_object_id IN (...)` — the
                -- caller folds results into a dict/set keyed by object id, order
                -- irrelevant), or a full per-notebook scan whose consumer builds an
                -- rx_graph / PPR node-edge list from unordered dict/list accumulation
                -- (edge identity, not position, carries meaning) — none are
                -- order-sensitive, so no ORDER BY needs to be pinned.
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_created ON knowledge_relations(notebook_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_nb ON knowledge_embeddings(notebook_id);
                -- _scale_index_version's "SELECT COUNT(*), MAX(created_at) FROM
                -- knowledge_embeddings WHERE notebook_id=?" — same cold-miss cost.
                -- knowledge_embeddings' FIRST composite index (idx_knowledge_embeddings_nb
                -- above is single-column, not covering for MAX(created_at)). Row-order
                -- audit: build_scale_index's `_kg_matrix()` ("SELECT object_id AS vid,
                -- vector FROM knowledge_embeddings WHERE notebook_id=?", no ORDER BY)
                -- feeds build_matrix() → its row enumeration order becomes ann_labels'
                -- persisted order and the hnsw index's internal insertion-id order
                -- (0..n-1). This does NOT break internal alignment (ids/vectors stay
                -- zipped by build_matrix regardless of order) and does NOT cause
                -- manifest staleness (manifest["version"] is _scale_index_version's
                -- COUNT/MAX tuple, which does not encode row order — a pre-existing
                -- on-disk index built before this migration still matches). It CAN
                -- change hnsw's approximate KNN tie-breaking among equally-similar
                -- neighbors between a from-scratch rebuild before vs. after this
                -- index exists — but hnsw builds were never guaranteed byte-identical
                -- across rebuilds anyway (approximate by construction), so this is
                -- not a new risk class; no test/manifest asserts row-order equality
                -- here. _vector_matrix (the other knowledge_embeddings reader, used
                -- by retrieval fallback paths) is likewise order-agnostic: it
                -- version-caches on (COUNT, MAX(created_at)) and returns an
                -- (ids, matrix) pair consumed by id-keyed lookups, not position.
                CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_nb_created ON knowledge_embeddings(notebook_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_element_embeddings_nb ON element_embeddings(notebook_id);

                -- P0-4 反查表: knowledge_objects.evidence 是 JSON (每条 evidence item
                -- 携带自己的 source_id — 一个合并后的 object 可引用多个来源), 所以
                -- "找出引用某 source 的所有 object" 原来要整本 notebook 逐行
                -- json.loads (490k 行量级)。这张表把 evidence[].source_id 打平成行,
                -- 让该查询变成一次索引命中的 SQL。Additive, 前向维护见
                -- store_kg / merge_knowledge / confirm_promotion; 首次使用时对旧库
                -- 惰性回填(见 _clear_source_extraction_state)。
                CREATE TABLE IF NOT EXISTS knowledge_object_sources (
                  object_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kos_source ON knowledge_object_sources(source_id);
                CREATE INDEX IF NOT EXISTS idx_kos_object ON knowledge_object_sources(object_id);
                CREATE INDEX IF NOT EXISTS idx_kos_notebook ON knowledge_object_sources(notebook_id);

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

                -- 社区反向索引:canonical_id → 所在社区(O(1) 定位焦点社区,避免扫
                -- communities.member_ids JSON)。存 canonical_name/centrality 供
                -- community_peers 直接重排,不再回查 concept_clusters。
                CREATE TABLE IF NOT EXISTS community_members (
                  canonical_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL,
                  level INTEGER NOT NULL DEFAULT 0,
                  community_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  centrality REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_can ON community_members(notebook_id, canonical_id);
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_comm ON community_members(notebook_id, community_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS kg_objects_fts
                  USING fts5(object_id UNINDEXED, notebook_id UNINDEXED, name,
                             tokenize='trigram');

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
                  USING fts5(chunk_id UNINDEXED, notebook_id UNINDEXED, text,
                             tokenize='trigram');
                """
            )
            # canonical 关系层(P1):关系端点折叠到 canonical 空间后的聚合,随
            # rebuild_unified_kg 全量重写(seq 闸)。派生数据,可随时重建。
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_relations (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    canonical_src TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    canonical_tgt TEXT NOT NULL,
                    support_count INTEGER NOT NULL DEFAULT 1,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    sample_relation_ids TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, canonical_src, edge_type, canonical_tgt)
                )
                """
            )
            # 共提桥接层(P2):claim 文本确定性提取的「claim→跨源概念」mention 边,
            # 以及同一 claim 命中的 canonical 组合两两计入的共提对。随
            # rebuild_unified_kg 全量重写(mention_seq 闸)。派生数据,可随时重建。
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS mention_edges (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    claim_object_id TEXT NOT NULL,
                    concept_canonical_id TEXT NOT NULL,
                    matched_alias TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (notebook_id, claim_object_id, concept_canonical_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS concept_comentions (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    canonical_a TEXT NOT NULL,
                    canonical_b TEXT NOT NULL,
                    bridge_claims INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (notebook_id, canonical_a, canonical_b)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_rebuild_checkpoint (
                    notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    input_version TEXT NOT NULL,
                    stage         TEXT NOT NULL,
                    item_key      TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, input_version, stage, item_key)
                )
                """
            )
            # Lightweight column migrations for pre-existing databases.
            # SQLite has no `ADD COLUMN IF NOT EXISTS`; guard via PRAGMA so this
            # runs idempotently on every init.
            self.add_column_if_missing(db, "answers", "conversation_id", "TEXT")
            # conversation_id is added via ALTER TABLE above (SQLite has no
            # "ADD COLUMN ... indexed" shorthand), so its index must live here
            # rather than in the CREATE TABLE block. _conversation_history /
            # get_conversation / list_conversations' turn_count and
            # used_reasoning subqueries all filter on conversation_id — every one
            # of them already carries its own explicit ORDER BY (see the answers
            # composite index comment above), so this index changes no result
            # order, only how those per-conversation scans are located.
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_answers_conversation "
                "ON answers(conversation_id)"
            )
            self.add_column_if_missing(db, "conversations", "created_by", "TEXT DEFAULT ''")
            db.execute(
                "UPDATE conversations SET created_by='user-local' "
                "WHERE created_by IS NULL OR created_by=''"
            )
            self.add_column_if_missing(db, "knowledge_objects", "last_reviewed", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "knowledge_objects", "source_id", "TEXT NOT NULL DEFAULT ''")
            for col in ("target_users", "expected_questions", "source_types", "taxonomy", "access_scope", "template"):
                self.add_column_if_missing(db, "notebooks", col, "TEXT NOT NULL DEFAULT ''")
            # purpose_auto=1 means the description is auto-derived from sources and
            # may be regenerated; set to 0 once the user edits it manually.
            self.add_column_if_missing(db, "notebooks", "purpose_auto", "INTEGER NOT NULL DEFAULT 0")
            # tier column: 'personal' (default) or 'base' (analog-textbook KG).
            # Existing notebooks default to 'personal'; the base KG is marked via
            # mark_notebook_base(). PRAGMA guard keeps this idempotent.
            self.add_column_if_missing(db, "notebooks", "tier", "TEXT NOT NULL DEFAULT 'personal'")
            # Notebook sharing (Phase 1): is_shared toggles an opaque share_token
            # that lets others deep-copy the small library into their own space.
            # Unique index (partial, NULL 不参与) 保证 token 全局唯一。
            self.add_column_if_missing(db, "notebooks", "is_shared", "INTEGER NOT NULL DEFAULT 0")
            self.add_column_if_missing(db, "notebooks", "share_token", "TEXT DEFAULT NULL")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_notebooks_share_token "
                "ON notebooks(share_token) WHERE share_token IS NOT NULL"
            )
            # Notebook sharing (Phase 2): 大库无法深拷贝 → 改为「只读共享」，他人凭
            # share_token 加入为只读成员(role='reader')。读权 = owner ∪ 成员;写权仍
            # owner-only(user_can_access_notebook 不动)。CASCADE 保证删库自动清成员。
            db.execute(
                "CREATE TABLE IF NOT EXISTS notebook_members ("
                "  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,"
                "  user_id TEXT NOT NULL REFERENCES users(id),"
                "  role TEXT NOT NULL DEFAULT 'reader',"
                "  added_at TEXT NOT NULL,"
                "  PRIMARY KEY (notebook_id, user_id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_notebook_members_user ON notebook_members(user_id)")
            # Per-source document type drives schema/profile selection at extraction.
            self.add_column_if_missing(db, "sources", "doc_type", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "sources", "source_url", "TEXT NOT NULL DEFAULT ''")
            # LLM review metadata columns for concept_merge_candidates.
            self.add_column_if_missing(db, "concept_merge_candidates", "confidence", "REAL NOT NULL DEFAULT 0")
            self.add_column_if_missing(db, "concept_merge_candidates", "rationale", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "concept_merge_candidates", "reviewed_by", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "concept_merge_candidates", "seed_a", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "concept_merge_candidates", "seed_b", "TEXT NOT NULL DEFAULT ''")
            # object_type column for concept_clusters (per-type isolation).
            self.add_column_if_missing(db, "concept_clusters", "object_type", "TEXT NOT NULL DEFAULT 'concept'")
            self.add_column_if_missing(db, "concept_clusters", "canonical_description", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "concept_clusters", "canonical_desc_sig", "TEXT NOT NULL DEFAULT ''")
            # cluster_input_version for unified_kg_state: content-hash of the
            # clustering inputs at the last rebuild, so rebuild_unified_kg can skip
            # the recompute when inputs are unchanged. Additive; pre-existing DBs
            # get '' (never-rebuilt) and thus never wrongly skip.
            self.add_column_if_missing(db, "unified_kg_state", "cluster_input_version", "TEXT NOT NULL DEFAULT ''")
            # kg_mutation_seq: monotonic counter bumped by _mark_unified_kg_dirty.
            # Additive; pre-existing DBs start at 0. Any prior stored
            # cluster_input_version was 'v1' (timestamp-based) and won't match the
            # new 'v2' scheme, so the first post-migration rebuild recomputes — safe.
            self.add_column_if_missing(db, "unified_kg_state", "kg_mutation_seq", "INTEGER NOT NULL DEFAULT 0")
            # community_seq: kg_mutation_seq at which communities were last (re)built.
            # 版本闸:社区随 KG 变动增量重建;-1 默认 → 首次必建(never matches seq>=0)。
            self.add_column_if_missing(db, "unified_kg_state", "community_seq", "INTEGER NOT NULL DEFAULT -1")
            # canonical_rel_seq: canonical_relations 上次重建时的 kg_mutation_seq。
            # -1 默认 → 首次必建(同 community_seq 语义)。
            self.add_column_if_missing(db, "unified_kg_state", "canonical_rel_seq", "INTEGER NOT NULL DEFAULT -1")
            # mention_seq: mention_edges/concept_comentions 上次重建时的 kg_mutation_seq。
            # -1 默认 → 首次必建(同 canonical_rel_seq 语义)。
            self.add_column_if_missing(db, "unified_kg_state", "mention_seq", "INTEGER NOT NULL DEFAULT -1")
            # source_index_backfilled: 0/1 marker — once set, _clear_source_extraction_state
            # trusts knowledge_object_sources for this notebook and skips the legacy
            # full-evidence-scan fallback. Set by the backfill-on-first-use scan itself
            # (the scan callers were already paying becomes the LAST one) or by the
            # standalone `backfill-source-index` CLI. Additive; pre-existing DBs start
            # at 0 (never backfilled), so they correctly take the legacy-scan path once.
            self.add_column_if_missing(
                db, "unified_kg_state", "source_index_backfilled", "INTEGER NOT NULL DEFAULT 0"
            )
            # Community report columns (title/summary/findings).
            for col, default in (("title", "''"), ("summary", "''"), ("findings", "'[]'")):
                self.add_column_if_missing(db, "communities", col, f"TEXT NOT NULL DEFAULT {default}")
            # Track E: edge review_status column (curation feedback loop).
            self.add_column_if_missing(
                db, "knowledge_relations", "review_status", "TEXT NOT NULL DEFAULT 'pending'"
            )
            # 用户系统：username + 密码列（守卫式 ALTER 幂等）。
            self.add_column_if_missing(db, "users", "username", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "users", "password_hash", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "users", "password_salt", "TEXT NOT NULL DEFAULT ''")
            self.add_column_if_missing(db, "users", "password_iterations", "INTEGER NOT NULL DEFAULT 0")
            # 小写 username 唯一（空串不算冲突：用部分索引排除空串）。
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
                "ON users(username) WHERE username != ''"
            )
            # 每用户模型服务配置(JSON;明文存,API 层只写不回显)。
            self.add_column_if_missing(db, "user_profiles", "model_settings", "TEXT NOT NULL DEFAULT '{}'")
            # kg_cluster_scratch: run_id column added in SP3 to isolate concurrent
            # rebuilds. Pre-existing DBs have the table without this column.
            # The scratch table is purely transient (always cleared before use), so
            # drop-and-recreate is safe; no data is lost.
            kcs_cols = {r["name"] for r in db.execute("PRAGMA table_info(kg_cluster_scratch)").fetchall()}
            if kcs_cols and "run_id" not in kcs_cols:
                db.executescript(
                    """
                    DROP TABLE IF EXISTS kg_cluster_scratch;
                    CREATE TABLE IF NOT EXISTS kg_cluster_scratch (
                      notebook_id TEXT NOT NULL,
                      run_id TEXT NOT NULL,
                      object_id TEXT NOT NULL,
                      seed TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_kg_cluster_scratch_nb_run
                      ON kg_cluster_scratch(notebook_id, run_id);
                    """
                )
            # 深度报告:depth(智能滑块封顶 reflect 轮数)+ section_status_json
            # (节内实时进度)。Additive;pre-existing reports 表 CREATE TABLE
            # IF NOT EXISTS 不会补列,须守卫式 ALTER。默认 depth=2、进度空 '[]'。
            self.add_column_if_missing(db, "reports", "depth", "INTEGER NOT NULL DEFAULT 2")
            self.add_column_if_missing(
                db, "reports", "section_status_json", "TEXT NOT NULL DEFAULT '[]'"
            )

    def _migration_2(self) -> None:
        """admin 用户总览:按 created_by 分组统计的覆盖索引(幂等)。
        独立迁移步——_migration_1 已在既有库封版(user_version=1),新增索引必须
        走新的版本步才能到达那些已迁移的库(否则 _migrate 快路径直接跳过)。"""
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebooks_created_by "
                "ON notebooks(created_by)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_created_by "
                "ON conversations(created_by)")

    def _migration_3(self) -> None:
        """已部署库补建 community_members 反向索引表(canonical→社区)。

        该表原本在 _migration_1 的 baseline executescript 里(全新库随之建),但 community
        层把它作为后加表塞进 _migration_1 却未 bump SCHEMA_VERSION —— 已达 user_version<=2
        的旧库因 `_migrate` 的 `if current >= SCHEMA_VERSION: return []` 版本闸短路、不重跑
        _migration_1,导致缺表:community_peers()/expand_community 与深度报告的社区/横向对比
        节查该表 `no such table: community_members` 崩(communities 表因更早就在 baseline
        里而存在,故只缺反向索引这一张)。幂等 CREATE IF NOT EXISTS,已有该表的库 no-op。
        DDL 与 _migration_1 中的定义逐字一致。"""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS community_members (
                  canonical_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL,
                  level INTEGER NOT NULL DEFAULT 0,
                  community_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  centrality REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_can ON community_members(notebook_id, canonical_id);
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_comm ON community_members(notebook_id, community_id);
                """
            )

    def _migration_4(self) -> None:
        """已部署库补建 unified_kg_state.community_seq 版本闸列(社区上次重建时的
        kg_mutation_seq;DEFAULT -1=从未建过)。

        与 community_members 表(_migration_3)同源:community 层把 community_seq 列塞进
        _migration_1 的守卫 ALTER 却未 bump SCHEMA_VERSION —— 已部署库版本闸短路不重跑
        _migration_1 → 缺列 → rebuild_communities 查它 `no such column: community_seq`
        → 社区建不了。_migration_3(PR#210)只补了表、漏了这列,故独立补一步。守卫 ALTER
        幂等,已有该列的库 no-op。"""
        with self._connect() as db:
            self.add_column_if_missing(
                db, "unified_kg_state", "community_seq", "INTEGER NOT NULL DEFAULT -1")

    def _migration_5(self) -> None:
        """cluster_mutation_seq: concept_clusters 写路径的单调计数器(P0-A 探针
        O(1) 化)。DEFAULT 0 = "从未 bump":首次探针 memo 必 miss→冷路径重算,
        旧库升级后行为无缝。"""
        with self._connect() as db:
            self.add_column_if_missing(
                db, "unified_kg_state", "cluster_mutation_seq",
                "INTEGER NOT NULL DEFAULT 0")

    def _migration_6(self) -> None:
        """WS2a: ask 脱离连接执行的 ask_jobs 表(每个在途 ask 一行,支撑显式取消/
        重启兜底/WS2b 轨迹持久化)。全新库经 _migrate 跑到此步得表,已部署库单独补建。"""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ask_jobs (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  conversation_id TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  mode TEXT NOT NULL DEFAULT '',
                  question TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'running',
                  -- 已由 _migration_7 的 ask_trace_steps 子表取代(perf fast-follow：
                  -- 消 O(N^2) 全量重写 + 全局写锁长持有)。保留此列仅为兼容旧行/
                  -- 避免重建表；append_ask_trace 起停止写入，读侧也不再读它。
                  trace_json TEXT NOT NULL DEFAULT '',
                  answer_id TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ask_jobs_conv ON ask_jobs(conversation_id);
                CREATE INDEX IF NOT EXISTS idx_ask_jobs_nb_status ON ask_jobs(notebook_id, status);
                """
            )

    def _migration_7(self) -> None:
        """perf fast-follow(折入 PR#220「进行中动作刷新韧性」):ask 轨迹持久化从
        ask_jobs.trace_json 单列「读整个 JSON 数组→append→写回」改成 append-only
        子表 ask_trace_steps(每 trace step 一行)。旧写法是 O(N^2) 累积序列化 +
        每次都占用全站唯一的 _write() 全局写锁；子表把它变成 O(1) 单行 INSERT，
        锁持有时间恒定、不再随轨迹变长而增长。

        PK (job_id, seq) 本身即索引，覆盖 `WHERE job_id=? ORDER BY seq` 读取路径与
        FK 级联删除的 `WHERE job_id=?` 匹配，故不必再补一张同列索引。
        ON DELETE CASCADE 依赖 `_connect()` 常开的 `PRAGMA foreign_keys = ON`
        （与仓库其它子表一致），job 所在 ask_jobs 行被删时子表行随之清空。"""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ask_trace_steps (
                  job_id TEXT NOT NULL REFERENCES ask_jobs(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  step_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY (job_id, seq)
                );
                """
            )

    def _migration_8(self) -> None:
        """canonical 关系层(P1):canonical_relations 表 + unified_kg_state.canonical_rel_seq。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_3/_migration_4 同款两层写法(baseline 双写 + 独立迁移)。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_relations (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    canonical_src TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    canonical_tgt TEXT NOT NULL,
                    support_count INTEGER NOT NULL DEFAULT 1,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    sample_relation_ids TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, canonical_src, edge_type, canonical_tgt)
                )
                """
            )
            self.add_column_if_missing(db, "unified_kg_state", "canonical_rel_seq", "INTEGER NOT NULL DEFAULT -1")

    def _migration_9(self) -> None:
        """共提桥接层(P2):mention_edges/concept_comentions 表 + unified_kg_state.mention_seq。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_3/_migration_4/_migration_8 同款两层写法(baseline 双写 + 独立迁移)。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS mention_edges (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    claim_object_id TEXT NOT NULL,
                    concept_canonical_id TEXT NOT NULL,
                    matched_alias TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (notebook_id, claim_object_id, concept_canonical_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS concept_comentions (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    canonical_a TEXT NOT NULL,
                    canonical_b TEXT NOT NULL,
                    bridge_claims INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (notebook_id, canonical_a, canonical_b)
                )
                """
            )
            self.add_column_if_missing(db, "unified_kg_state", "mention_seq", "INTEGER NOT NULL DEFAULT -1")

    def _migration_10(self) -> None:
        """rebuild 断点续跑:kg_rebuild_checkpoint 版本键崩溃恢复日志,让 merge 审查 /
        概念描述两个 LLM 阶段可中断续跑。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_8/_migration_9 同款两层写法(baseline 双写 + 独立迁移)。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_rebuild_checkpoint (
                    notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    input_version TEXT NOT NULL,
                    stage         TEXT NOT NULL,
                    item_key      TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, input_version, stage, item_key)
                )
                """
            )

    @staticmethod
    def _rebuild_memory_fts(db: sqlite3.Connection) -> None:
        """Rebuild the external-content Memory index without scanning in queries."""
        db.execute("INSERT INTO memory_items_fts(memory_items_fts) VALUES ('rebuild')")

    def _migration_13(self) -> None:
        """Creator-private notebook Memory, revision history, and Agent access.

        The Memory branch originally used version 11 while master independently
        used v11/v12 for scale indexes.  Keep Memory as the new v13 hop and
        idempotently ensure the master indexes so a database produced by either
        pre-merge v11 lineage converges to the same schema.
        """
        with self._connect() as db:
            self.add_column_if_missing(
                db, "knowledge_relations", "review_status", "TEXT NOT NULL DEFAULT 'pending'"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_element_embeddings_source ON element_embeddings(source_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_review ON knowledge_relations(notebook_id, review_status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_comentions_nb_b ON concept_comentions(notebook_id, canonical_b)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sources_nb_parse_status ON sources(notebook_id, parse_status)")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_profiles (
                  id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','revoked')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_profiles_owner_status
                  ON agent_profiles(owner_id, status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS agent_access_tokens (
                  id TEXT PRIMARY KEY,
                  agent_profile_id TEXT NOT NULL
                    REFERENCES agent_profiles(id) ON DELETE CASCADE,
                  token_hash TEXT NOT NULL UNIQUE,
                  scopes_json TEXT NOT NULL DEFAULT '[]',
                  default_notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  expires_at TEXT,
                  revoked_at TEXT,
                  last_used_at TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tokens_profile
                  ON agent_access_tokens(agent_profile_id, revoked_at, expires_at);

                CREATE TABLE IF NOT EXISTS agent_token_notebooks (
                  token_id TEXT NOT NULL
                    REFERENCES agent_access_tokens(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  PRIMARY KEY (token_id, notebook_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_token_notebooks_notebook
                  ON agent_token_notebooks(notebook_id, token_id);

                CREATE TABLE IF NOT EXISTS memory_items (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_by TEXT NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,
                  agent_profile_id TEXT
                    REFERENCES agent_profiles(id) ON DELETE SET NULL,
                  source_answer_id TEXT,
                  origin TEXT NOT NULL
                    CHECK(origin IN ('ask_answer','external_agent')),
                  status TEXT NOT NULL
                    CHECK(status IN ('candidate','confirmed','rejected','deprecated')),
                  promotion_state TEXT NOT NULL DEFAULT 'none'
                    CHECK(promotion_state IN ('none','proposed','approved','rejected')),
                  title TEXT NOT NULL,
                  content_md TEXT NOT NULL,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  confirmed_by TEXT REFERENCES users(id),
                  confirmed_at TEXT,
                  embedding_status TEXT NOT NULL DEFAULT 'pending',
                  embedding_error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_answer_once
                  ON memory_items(created_by, source_answer_id)
                  WHERE source_answer_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_memory_owner_notebook_status
                  ON memory_items(created_by, notebook_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_agent_candidate
                  ON memory_items(created_by, notebook_id, status, agent_profile_id);

                CREATE TABLE IF NOT EXISTS memory_revisions (
                  id TEXT PRIMARY KEY,
                  memory_id TEXT NOT NULL
                    REFERENCES memory_items(id) ON DELETE CASCADE,
                  revision INTEGER NOT NULL CHECK(revision > 0),
                  title TEXT NOT NULL,
                  content_md TEXT NOT NULL,
                  tags_json TEXT NOT NULL DEFAULT '[]',
                  status TEXT NOT NULL
                    CHECK(status IN ('candidate','confirmed','rejected','deprecated')),
                  promotion_state TEXT NOT NULL
                    CHECK(promotion_state IN ('none','proposed','approved','rejected')),
                  changed_by TEXT NOT NULL REFERENCES users(id),
                  change_reason TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(memory_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_revisions_memory
                  ON memory_revisions(memory_id, revision DESC);

                CREATE TABLE IF NOT EXISTS memory_provenance (
                  id TEXT PRIMARY KEY,
                  memory_id TEXT NOT NULL UNIQUE
                    REFERENCES memory_items(id) ON DELETE CASCADE,
                  origin TEXT NOT NULL
                    CHECK(origin IN ('ask_answer','external_agent')),
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_embeddings (
                  memory_id TEXT PRIMARY KEY
                    REFERENCES memory_items(id) ON DELETE CASCADE,
                  model TEXT NOT NULL,
                  dimension INTEGER NOT NULL CHECK(dimension > 0),
                  vector BLOB NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
                  ON memory_embeddings(model, dimension);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
                  title,
                  content_md,
                  tags_json,
                  content='memory_items',
                  content_rowid='rowid',
                  tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS memory_items_fts_insert
                AFTER INSERT ON memory_items BEGIN
                  INSERT INTO memory_items_fts(rowid, title, content_md, tags_json)
                  VALUES (new.rowid, new.title, new.content_md, new.tags_json);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_items_fts_delete
                AFTER DELETE ON memory_items BEGIN
                  INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content_md, tags_json)
                  VALUES ('delete', old.rowid, old.title, old.content_md, old.tags_json);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_items_fts_update
                AFTER UPDATE OF title, content_md, tags_json ON memory_items BEGIN
                  INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content_md, tags_json)
                  VALUES ('delete', old.rowid, old.title, old.content_md, old.tags_json);
                  INSERT INTO memory_items_fts(rowid, title, content_md, tags_json)
                  VALUES (new.rowid, new.title, new.content_md, new.tags_json);
                END;
                """
            )
            self._rebuild_memory_fts(db)
    def _migration_11(self) -> None:
        """Scale hot-path index coverage (per-request full scans → index seeks).
        Separate migration (not a _migration_1 baseline edit) so already-deployed
        libraries at user_version>=1 actually get them; a fresh DB runs the whole
        1..N loop so it picks these up too. All three target derived/secondary
        tables (NOT knowledge_objects), so they can't perturb rebuild's
        ORDER BY rowid canonical insertion order. Single-line CREATE INDEX so the
        stored sqlite_master.sql is stable (snapshot verifier matches it exactly).

        - element_embeddings(source_id): `DELETE FROM element_embeddings WHERE
          source_id=?` (source re-extract / clear) was a full-table scan over
          EVERY notebook's element vectors — only idx_element_embeddings_nb
          (notebook_id) existed and this DELETE has no notebook_id predicate.
        - knowledge_relations(notebook_id, review_status): the pending-edge bell
          count (`... WHERE notebook_id IN (...) AND review_status='pending'`)
          seeked by notebook then row-fetched every relation to test review_status.
        - concept_comentions(notebook_id, canonical_b): comention_peers filters
          `(canonical_a=? OR canonical_b=?)`; PK (notebook_id,canonical_a,
          canonical_b) served only the canonical_a arm.

        review_status is added by _migration_1's add_column_if_missing (line ~741),
        which does NOT re-run on a deployed DB at user_version=10 (the loop runs
        only _migration_11). So guard the column here before indexing it, or a
        deployed DB that never got the column would fail with `no such column`."""
        with self._connect() as db:
            self.add_column_if_missing(
                db, "knowledge_relations", "review_status", "TEXT NOT NULL DEFAULT 'pending'")
            db.execute("CREATE INDEX IF NOT EXISTS idx_element_embeddings_source ON element_embeddings(source_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_review ON knowledge_relations(notebook_id, review_status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_comentions_nb_b ON concept_comentions(notebook_id, canonical_b)")

    def _migration_12(self) -> None:
        """/analytics parse_status GROUP BY covering index. Deployed DBs at
        user_version>=1 short-circuit _migration_1, so this needs its own step;
        sources is a secondary table so it cannot perturb rebuild's ORDER BY
        rowid canonical order."""
        with self._connect() as db:
            db.execute("CREATE INDEX IF NOT EXISTS idx_sources_nb_parse_status ON sources(notebook_id, parse_status)")

    def _migration_14(self) -> None:
        """Memory 派生源：sources.memory_id 关联列 + 每 Memory 至多一条派生源。

        Deployed DBs at user_version>=1 short-circuit _migration_1, so the
        column/index need their own step here. A truly fresh DB runs this
        same step too (migrate() sweeps 1..SCHEMA_VERSION), so memory_id ends
        up appended after sources' existing last column (doc_type) on both
        paths — deliberately NOT duplicated into _migration_1's baseline
        CREATE TABLE, which would instead land memory_id BEFORE doc_type on a
        fresh DB only (_migration_1's own doc_type add_column_if_missing runs
        after its CREATE TABLE), breaking fresh/upgraded column-order parity."""
        with self._connect() as db:
            self.add_column_if_missing(db, "sources", "memory_id", "TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_memory_id "
                "ON sources(memory_id) WHERE memory_id IS NOT NULL AND memory_id != ''"
            )

    def _migration_15(self) -> None:
        """Covering index for the memory-filtered user-facing source counts.

        memory-kg-extract Task 5 added `AND source_type != 'memory'` to the
        /analytics parse_status GROUP BY (QueryStore.notebook_analytics) and to
        NotebookSummary's per-open source COUNT (QueryStore.visible_source_count).
        source_type is NOT in #249's idx_sources_nb_parse_status(notebook_id,
        parse_status), so both queries lost their covering-only property and now
        do a per-row rowid lookup on sources (48k rows at scale, on every board
        open / list_notebooks). This 3-col index restores covering-only scans
        for BOTH: (notebook_id, parse_status, source_type) keeps parse_status
        2nd so the GROUP BY is still index-ordered (no transient sort) while
        source_type rides along in the index for the != filter. Column order
        confirmed by EXPLAIN QUERY PLAN (both queries USING COVERING INDEX, no
        TEMP B-TREE); (notebook_id, source_type, parse_status) was rejected —
        it forces USE TEMP B-TREE FOR GROUP BY.

        Deployed DBs at user_version>=1 short-circuit _migration_1, so this
        needs its own step; a fresh DB runs it too in the same migrate() sweep,
        so fresh == upgraded (the idx is created here only, mirroring Task 1's
        _migration_14). sources is a secondary table, so a new index on it
        cannot perturb rebuild's ORDER BY rowid canonical ordering. The older
        2-col idx_sources_nb_parse_status is left in place (a strict prefix,
        frozen into several schema-contract goldens); the planner deterministically
        prefers this covering superset for the filtered queries."""
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_nb_parse_status_type "
                "ON sources(notebook_id, parse_status, source_type)"
            )

    def _migration_16(self) -> None:
        """Knowhow 表骨架(knowhow-tables PR-1 Task 1):五张新表。

        knowhow_tables/knowhow_columns/knowhow_rows/knowhow_cells 是整表导入
        后的编辑真相源(表→列→行→格);notebook_assets 是行内嵌图片的上传元数据
        (Task 4 落地读写路由)。role 默认 'plain'、projection_status 默认
        'pending'(Task 5 的确定性投影状态机从这里起步)、mutation_seq 默认 0。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_8/_migration_9/_migration_10 同款两层写法(仅新建表,不改
        _migration_1 baseline;全新表没有历史行,不存在 _migration_14 那种列序
        /存量数据顾虑,故无需像 memory_id 列那样纠结 baseline 是否重复声明)。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowhow_tables (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  mutation_seq INTEGER NOT NULL DEFAULT 0,
                  hidden_source_id TEXT,
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_tables_nb ON knowhow_tables(notebook_id);

                CREATE TABLE IF NOT EXISTS knowhow_columns (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'plain',
                  position INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_columns_table ON knowhow_columns(table_id);

                CREATE TABLE IF NOT EXISTS knowhow_rows (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  projection_status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_rows_table ON knowhow_rows(table_id);

                CREATE TABLE IF NOT EXISTS knowhow_cells (
                  id TEXT PRIMARY KEY,
                  row_id TEXT NOT NULL REFERENCES knowhow_rows(id) ON DELETE CASCADE,
                  column_id TEXT NOT NULL REFERENCES knowhow_columns(id) ON DELETE CASCADE,
                  content_md TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  UNIQUE(row_id, column_id)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_cells_row ON knowhow_cells(row_id);

                CREATE TABLE IF NOT EXISTS notebook_assets (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  filename TEXT NOT NULL,
                  mime TEXT NOT NULL,
                  size INTEGER NOT NULL,
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notebook_assets_nb ON notebook_assets(notebook_id);
                """
            )

    def _migration_17(self) -> None:
        """论文元数据两表(paper-metadata Task 1):source_paper_meta(1:1;行存在=
        已尝试,is_paper=0 为「已判定非论文」标记行,防对同一源反复调 LLM)+
        source_authors(1:N;position=署名序,affiliation 多机构以 '; ' 连接)。
        接地校验后的数据才落库(app/services/paper_meta.py)。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_16 同款两层写法(仅新建表,不改 _migration_1 baseline;
        全新表无历史行,无列序顾虑)。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_paper_meta (
                  source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  is_paper INTEGER NOT NULL DEFAULT 0,
                  paper_title TEXT,
                  venue TEXT,
                  pub_year INTEGER,
                  doi TEXT,
                  keywords TEXT NOT NULL DEFAULT '[]',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  model TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_source_paper_meta_nb
                  ON source_paper_meta(notebook_id);

                CREATE TABLE IF NOT EXISTS source_authors (
                  id TEXT PRIMARY KEY,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  affiliation TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_source_authors_source
                  ON source_authors(source_id);
                CREATE INDEX IF NOT EXISTS idx_source_authors_nb
                  ON source_authors(notebook_id);
                """
            )

    def _migration_18(self) -> None:
        """Knowhow 表 PR-2+3 Task 1:cell_code 新表 + role 词表重映射。

        `knowhow_cell_code`——格子级代码附件（design doc §⑥-4/§①「格子级节点模
        型」的代码存储部分):Agent 把某格判别/修复方法生成并验证过的代码本体
        存回来，与该格 `content_md` 分离、每格至多一份(UNIQUE(row_id,
        column_id))。`cell_content_hash` 是写入时刻该格净文本(剥图后)的
        sha256——读取时与格子当前 hash 比对推导新鲜度(implemented/stale)，本
        迁移不建任何"新鲜度"列，推导逻辑完全在读侧(Task 10)。语言/更新者留
        白默认(未知语言/系统写入)。

        role 词表重映射(design doc §①「角色词表(2026-07-15 修订)」)：PR-1 的
        五个时序修复域实例角色(concept/identify/root_cause/fix/tool)重铸为四
        个域中立行为类型(anchor/procedure/entity/attribute)——领域语义此后住
        在列名(自由文本)而非角色词表。identify/root_cause/fix 三个"步骤类"
        角色全部收敛到 procedure(它们的原始子类型信息在这次重映射后不可逆
        地丢失——这是设计决定的代价，见 task brief 的"至多一"放宽同一处决
        策)。ELSE 分支兜底 plain(唯一未被前四个 WHEN 覆盖的存量值)以及任何
        未来才可能出现的杂散值，一律落 attribute。CASE 表达式对全表一次
        UPDATE，不知道列语义、不读表结构，纯值到值映射。注意本 UPDATE **不
        幂等**——重跑会把已重映射的 anchor/procedure/entity 经 ELSE 分支打成
        attribute；恰好执行一次由迁移框架的版本闸保证(user_version 17→18 跑
        一遍后短路)，与历次含存量数据回填的迁移同款约束。

        已部署库(user_version<18 时 _migration_1..17 陆续短路)靠本迁移一次
        补齐 cell_code 表 + 完成存量 role 重映射——与 _migration_16 同款：
        CREATE TABLE IF NOT EXISTS 对全新库和已部署库都跑一次，全新库这里没
        有任何 knowhow_columns 行可重映射，UPDATE 是空操作，无害。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowhow_cell_code (
                  id TEXT PRIMARY KEY,
                  row_id TEXT NOT NULL REFERENCES knowhow_rows(id) ON DELETE CASCADE,
                  column_id TEXT NOT NULL REFERENCES knowhow_columns(id) ON DELETE CASCADE,
                  code_text TEXT NOT NULL,
                  language TEXT NOT NULL DEFAULT '',
                  updated_by TEXT NOT NULL DEFAULT '',
                  cell_content_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(row_id, column_id)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_cell_code_row ON knowhow_cell_code(row_id);
                """
            )
            db.execute(
                """
                UPDATE knowhow_columns SET role = CASE role
                    WHEN 'concept' THEN 'anchor'
                    WHEN 'identify' THEN 'procedure'
                    WHEN 'root_cause' THEN 'procedure'
                    WHEN 'fix' THEN 'procedure'
                    WHEN 'tool' THEN 'entity'
                    ELSE 'attribute'
                END
                """
            )

    def _migration_19(self) -> None:
        """来源内嵌图片资产：notebook_assets 加可空 source_id 列 + 索引。

        MinerU 从 pdf/docx/pptx 抽出的内嵌图片以 source_id 关联到来源，供来源
        视图渲染 + 源删除/重解析级联清理；knowhow 粘贴图片 source_id 为 NULL。
        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移 ALTER 补列——
        与 _migration_2/_migration_4 同款独立迁移；ALTER ADD COLUMN 若列已存在
        会报错，故带 PRAGMA table_info 列存在性守卫，保证可重入。"""
        with self._connect() as db:
            cols = {row[1] for row in db.execute("PRAGMA table_info(notebook_assets)").fetchall()}
            if "source_id" not in cols:
                db.execute("ALTER TABLE notebook_assets ADD COLUMN source_id TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebook_assets_source "
                "ON notebook_assets(source_id)"
            )

    def _migration_20(self) -> None:
        """持久化 KG 构建任务状态与笔记本级单飞约束。"""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_build_jobs (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_by TEXT NOT NULL DEFAULT '',
                  mode TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'running',
                  stage TEXT NOT NULL DEFAULT 'probing',
                  total_sources INTEGER NOT NULL DEFAULT 0,
                  completed_sources INTEGER NOT NULL DEFAULT 0,
                  failed_sources INTEGER NOT NULL DEFAULT 0,
                  error_code TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_build_jobs_one_running
                  ON kg_build_jobs(notebook_id) WHERE status = 'running';
                CREATE INDEX IF NOT EXISTS idx_kg_build_jobs_nb_created
                  ON kg_build_jobs(notebook_id, created_at DESC, id DESC);
                """
            )

    def _recover_interrupted_jobs(self) -> None:
        """每次启动的崩溃兜底（与版本化 schema 迁移解耦，无条件运行）：后端单进程，
        merge-review / ask 等 daemon 线程任务无法跨进程重启存活，故启动时仍是 'running'
        的行定义上就是上次崩溃/重启遗留的陈旧行，否则会永久卡死该 notebook 的单飞守卫。
        knowhow 行投影同理：background_jobs 不跨进程存活，重启后仍处 'syncing'/'pending'
        的行定义上是被遗弃的（没有任何代码路径会再回访它们），置 'failed' 以暴露前端的
        「重投影」重试入口，而非让它们看起来永久「同步中/待处理」。
        幂等——safe every boot。"""
        with self._connect() as db:
            db.execute(
                "UPDATE merge_review_jobs SET status='failed', "
                "error='中断:服务重启' WHERE status='running'")
            db.execute(
                "UPDATE ask_jobs SET status='interrupted', "
                "error='中断:服务重启' WHERE status='running'")
            db.execute(
                "UPDATE knowhow_rows SET projection_status='failed' "
                "WHERE projection_status IN ('syncing','pending')")
            now = _now()
            db.execute(
                "UPDATE kg_build_jobs SET status='failed', stage='finished', "
                "error_code='worker_interrupted', "
                "error_message='服务重启导致本次分析中断；"
                "已完成内容已保留，可继续分析未完成内容。', "
                "updated_at=?, finished_at=? WHERE status='running'",
                (now, now),
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
            # 把内置 user-local 升级为 admin（id 不变=现有 notebook 零迁移）：
            # 每次启动据 settings.admin_password 重置 admin 密码（改密=改环境变量后重启）。
            from app.services.auth_utils import hash_password
            pw_hash, pw_salt, pw_iters = hash_password(self.settings.admin_password)
            db.execute(
                "UPDATE users SET role='admin', username='admin', "
                "password_hash=?, password_salt=?, password_iterations=?, updated_at=? "
                "WHERE id='user-local'",
                (pw_hash, pw_salt, pw_iters, now),
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
            # 内建 object-schema 注册表种子（从 code 默认；INSERT OR IGNORE 保留策展人的
            # 编辑/归纳类型）。与版本化 schema 迁移解耦，随 _seed 每启动补齐——code 新增
            # 内建类型即落库，不必等 SCHEMA_VERSION 递增。
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

    def migrate(self) -> list[int]:
        with self._connect() as db:
            current = int(db.execute("PRAGMA user_version").fetchone()[0])
        if current >= SCHEMA_VERSION:
            return []
        applied: list[int] = []
        for version in range(current + 1, SCHEMA_VERSION + 1):
            getattr(self, f"_migration_{version}")()
            with self._connect() as db:
                db.execute(f"PRAGMA user_version = {version}")
            applied.append(version)
        return applied

    def recover_interrupted_jobs(self) -> None:
        self._recover_interrupted_jobs()

    def seed(self) -> None:
        self._seed()

    def initialize(self) -> list[int]:
        applied = self.migrate()
        self.recover_interrupted_jobs()
        self.seed()
        return applied
