from __future__ import annotations

import json
import logging
import sqlite3
from typing import Callable

from app.core.config import Settings
from app.repositories.sqlite.anchor_normalization import sqlite_js_trim_expression
from app.repositories.sqlite.database import SqliteDatabase

from app.domain.extraction_profiles import LIST_FIELDS, OBJECT_SCHEMAS, OBJECT_TYPE_LABELS

logger = logging.getLogger("silicon_notebook.sqlite.maintenance")

# Both sides originally allocated migration 24 independently.  Version 29 is
# the merge migration that makes either already-deployed lineage converge.
# v30 adds idx_sources_notebook_file_hash for content-hash upload dedup /
# batch_ingest resume (previously a full-table scan). v31 adds the two
# shadow-internal tables used by transactional forward capture. Capture
# triggers remain run-scoped and are installed by migration.shadow.capture.
# v32 persists the Deep Report question-understanding review contract.
# v33 adds stable relation endpoint keyset indexes for bounded lexical recall.
# v34 adds bounded relation-completion paging state and its source keyset index.
# v35 persists the browser submission instant on in-flight Ask jobs.
# v36 adds the three KG-quality-analysis precompute products (cross-board
# edges, per-source board profiles, statistic snapshots) — all derived, all
# rewritten wholesale by rebuild_communities under the community_seq gate.
# v37 adds the indexed (source_id, element_type, created_at, id) keyset order
# on source_elements for bounded, per-type collection enumeration.
# v39 adds the command-catalog extraction job row (with its per-source
# single-flight partial unique index) and its reviewable candidate rows.
# v42 adds notebook-scoped durable progress for the source reverse-index
# rebuild so a stopped deployment can resume after process loss without
# clearing and rescanning already committed pages.
# v45 adds user_profiles.ui_mode (nullable; application layer defaults NULL
# to "auto") for the per-user auto/advanced interface mode preference.
# v46 adds the element→chunk reverse index (chunk_elements), its restartable
# offline backfill ledger (chunk_element_backfills) and the per-notebook
# chunk_elements_indexed marker that forks the read path.
# v47 adds notebook_object_schemas, the notebook-owned copy-on-write overrides
# and notebook-only definitions layered over the global object_schemas baseline.
# v48 adds sources.agent_profile_id (nullable; NULL means "a person added
# this source"), the provenance an Agent-facing delete has to prove.
# v49 adds the group-sharing tables (groups, group_members, notebook_grants)
# for P1 of group knowledge sharing. Additive only: no consumer reads them
# yet, so this migration is zero-behavior-change.
# v50 adds notebook_share_requests for P2 of group knowledge sharing: a
# member's request to share their own notebook with a group they belong to,
# pending a group admin's approval. Additive only: no consumer reads or
# writes it yet, so this migration is zero-behavior-change.
# v51 adds the two Agentic-Memory P1 tables (agent_notebook_profile, the
# per-notebook understanding blocks with their shared base / per-member
# overlay layering, and agent_profile_jobs, the per-chain consolidation
# state). Additive only: no reader or writer exists yet.
# v52 adds conversation public-share tokens + a read watermark for T1 of
# question-answer session sharing. Additive only: the repository methods
# that use these columns have no caller yet (that lands in a later task),
# so this migration is zero-behavior-change.
# v53 adds agent_profile_jobs.claim_token — the consolidation chain's claim
# GENERATION. One nullable-free TEXT column with an empty default; existing
# rows keep '' and the startup sweep force-settles every leftover
# queued/running row anyway, so no backfill is needed.
# v54 adds retrieval_experiences — Agentic Memory P2's DEPLOYMENT-GLOBAL
# retrieval-strategy experience library. One synthesised content-addressed
# TEXT primary key, no notebook_id, no owner, no foreign key.
# v55 adds agent_observations (Agentic Memory P3's per-(notebook, owner,
# agent) append-only observation log, MCP-writable via add_observation) and
# user_profiles.search_profile_json (nullable; NULL means "not set yet",
# same as v45's ui_mode). Zero behavior change: no reader or writer exists
# yet.
# v56 adds groups.owner_id. created_by remains immutable creation audit;
# owner_id is the live, transferable authority and is backfilled from it.
# v57 adds one revocable group invitation capability to each group. The token
# is nullable (no active link), unique while present, and deleted with the
# group because it lives on the aggregate root.
# v60 adds agent_observations.kind — 'note' (a line the Agent itself wrote via
# add_observation) vs 'call' (one recorded tool call against this notebook).
# Existing rows are 'note' by construction: every row that can exist before
# this hop was written by add_observation.
# v61 adds five of hot-path fix batch 1's six groups (seven of the eight
# indexes; parity with PostgreSQL 0039_hotpath_batch1_indexes.sql):
# concept_clusters(notebook_id,
# canonical_id), concept_clusters(notebook_id, lower(canonical_name)),
# three reverse-FK covers (extraction_runs.notebook_id,
# knowledge_source_fact_elements.notebook_id, memory_items.notebook_id),
# knowledge_relations(notebook_id, source_object_id, target_object_id,
# edge_type), and a partial sources(notebook_id, source_type) WHERE
# source_type IN ('memory','knowhow'). The sixth group
# (chunks(source_id, ordinal) on PostgreSQL) has no SQLite twin: SQLite's
# planner does not eliminate the ORDER BY sort through a rowid-suffixed
# secondary index (verified empirically; see _migration_61's docstring), so
# a chunks(source_id, "rowid") index would cost a write-side B-tree for zero
# read benefit. Pure index additions: no table, column, foreign key, or
# unique surface changes.
# v62 adds the creator/time/id expression index used by the cross-notebook
# question overview. Its expression is byte-for-byte aligned with the activity
# query's normalized absolute-time ordering, so keyset LIMIT avoids a global
# ask_jobs scan plus temp sort.
# v63 adds extension_runtime_toggles — the deployment-plugin runtime
# enable/disable switch + audit (who, when). No row means enabled, so a
# fresh/unmodified deployment behaves exactly as before this table existed.
# v64 adds idx_clusters_nb_canonical_member on concept_clusters(notebook_id,
# canonical_id, member_object_id) — hot-path fix batch 3, parity with
# PostgreSQL migration 0043. Pure index addition backing the concept-detail
# hub-cluster keyset page's ORDER BY member_object_id.
# v65 adds retained_user_activity, a content-minimal, notebook-FK-free snapshot
# written atomically immediately before notebook deletion. Its configured
# expires_at drives both read visibility and bounded physical cleanup.
SCHEMA_VERSION = 65

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

                CREATE TABLE IF NOT EXISTS notebook_bases (
                  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  created_by TEXT REFERENCES users(id),
                  PRIMARY KEY (notebook_id, base_notebook_id),
                  CHECK (notebook_id != base_notebook_id)
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
                -- a fresh-vs-upgraded column-order divergence. Leaving
                -- sources' literal untouched and adding memory_id solely via
                -- _migration_14 (which every fresh DB also runs, in the same
                -- migrate() sweep) makes fresh and upgraded DBs land on the
                -- identical [..., doc_type, memory_id] order.

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
                  updated_at TEXT NOT NULL,
                  target_base_id TEXT NOT NULL DEFAULT ''
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

                CREATE INDEX IF NOT EXISTS idx_notebook_bases_base ON notebook_bases(base_notebook_id);

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
            # source_index_backfilled: a completeness certificate, not an
            # emptiness bit.  Pre-existing rows start false/unknown and use the
            # authoritative evidence compatibility path until explicit offline
            # backfill certifies them.  New notebooks are inserted as certified
            # empty by NotebookStore and online KG writers maintain the reverse
            # rows transactionally thereafter.
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
        """多领域基准库：参考库挂载边 notebook_bases + 晋升目标 target_base_id。

        基准库不再全局唯一——每个 notebook 显式声明挂载哪些库(0..N)，检索参与集
        由本表解析而非 `WHERE tier='base'`。已部署库(user_version>=1 时
        _migration_1 短路)靠本迁移补建，与 _migration_2/_migration_4/_migration_19
        同款。CREATE TABLE/INDEX IF NOT EXISTS + PRAGMA 列存在性守卫保证可重入。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS notebook_bases (
                  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  created_by TEXT REFERENCES users(id),
                  PRIMARY KEY (notebook_id, base_notebook_id),
                  CHECK (notebook_id != base_notebook_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebook_bases_base "
                "ON notebook_bases(base_notebook_id)"
            )
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(promotion_candidates)").fetchall()
            }
            if "target_base_id" not in cols:
                db.execute(
                    "ALTER TABLE promotion_candidates "
                    "ADD COLUMN target_base_id TEXT NOT NULL DEFAULT ''"
                )

    def _migration_21(self) -> None:
        """Index guarded anchor membership by ECMAScript-trimmed content.

        Interactive reformat saves compare every complete anchor-group member
        while holding ``BEGIN IMMEDIATE``. The leading-wildcard prefilter could
        still scan the table per save unit; this expression index makes the
        exact ``column_id + JS-trim(content_md)`` lookup indexable without an
        application-defined SQLite function or materialized column.
        """
        with self._connect() as db:
            normalized_anchor = sqlite_js_trim_expression("content_md")
            db.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_knowhow_cells_column_normalized_anchor_row "
                f"ON knowhow_cells(column_id, {normalized_anchor}, row_id)"
            )

    def _migration_22(self) -> None:
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

    def _migration_23(self) -> None:
        """Persist each user's latest observed model-service status."""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_service_status (
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  service TEXT NOT NULL,
                  config_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
                  latency_ms INTEGER NOT NULL DEFAULT 0,
                  code TEXT NOT NULL DEFAULT '',
                  trigger TEXT NOT NULL CHECK (trigger IN ('manual_test', 'observed_failure')),
                  checked_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, service)
                );
                CREATE INDEX IF NOT EXISTS idx_model_service_status_user_checked
                  ON model_service_status(user_id, checked_at DESC);
                """
            )

    def _migration_24(self) -> None:
        """写锁瘦身改造点 2:concept_clusters 整表替换拆成【预备段(scratch)+
        切换段(纯 SQL)】(design doc §5.5)。预备段把每个 object_type 算出的
        seed→canonical 映射(canonical 名/描述及其签名)落进这张新 scratch 表;
        切换段再用一条纯 SQL DELETE+INSERT...SELECT,把它和 kg_cluster_scratch
        (object_id→seed)连接,一次性写 concept_clusters —— 写锁只覆盖切换段的
        纯 SQL,不再覆盖预备段的 Python 计算与逐行 executemany 构造。

        与 kg_cluster_scratch 同款:无主键、纯 transient、run_id 隔离并发
        rebuild。不需要 object_type 列——_write_cluster_map_streamed 在写入
        每个 type 前先清空本 run 的行(镜像 _stream_seed_reps 对
        kg_cluster_scratch 的 clear-at-start),所以切换时表里只有当前 type
        的行。索引支持 (notebook_id, run_id, seed) 上的等值连接(切换段的
        JOIN 谓词)。"""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_canonical_scratch (
                  notebook_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  seed TEXT NOT NULL,
                  canonical_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  canonical_description TEXT NOT NULL DEFAULT '',
                  canonical_desc_sig TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_kg_canonical_scratch_nb_run_seed
                  ON kg_canonical_scratch(notebook_id, run_id, seed);
                """
            )

    def _migration_25(self) -> None:
        """Irreversibly retire per-user model credentials and health rows."""
        with self.database.write() as db:
            self.database.begin_immediate(db)
            db.execute("UPDATE user_profiles SET model_settings = '{}'")
            db.execute("DELETE FROM model_service_status")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS system_model_service_status (
                  service_id TEXT PRIMARY KEY,
                  config_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
                  latency_ms INTEGER NOT NULL DEFAULT 0,
                  code TEXT NOT NULL DEFAULT '',
                  trigger TEXT NOT NULL CHECK (
                    trigger IN ('manual_test', 'observed_failure', 'recovery_probe')
                  ),
                  support_id TEXT NOT NULL DEFAULT '',
                  checked_at TEXT NOT NULL
                )
                """
            )
            # The irreversible scrub and its version stamp are one commit. A
            # failed stamp must leave the database wholly at v24 so startup can
            # safely retry instead of reporting v25 over partially scrubbed
            # state (or committing the scrub while still reporting v24).
            db.execute("PRAGMA user_version = 25")

    def _migration_26(self) -> None:
        """knowhow 表版本管理：变更流水 + 命名里程碑。

        设计见 docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md。
        与 _migration_16/_migration_17 同款两层写法(仅新建表，不改 _migration_1
        baseline；全新表无历史行，无列序顾虑)。

        knowhow_milestones.seq 刻意**不设** FK 到 knowhow_changes：流水被
        "清理历史"删除后里程碑要保留为"已失效标记"(灰显、不可回退)，
        级联删掉用户亲手命名过的东西是不可接受的。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowhow_changes (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  actor TEXT NOT NULL DEFAULT '',
                  origin TEXT NOT NULL DEFAULT 'user',
                  payload_json TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_changes_table
                  ON knowhow_changes(table_id, seq DESC);

                CREATE TABLE IF NOT EXISTS knowhow_milestones (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_milestones_table
                  ON knowhow_milestones(table_id, seq DESC);
                """
            )

    def _migration_27(self) -> None:
        """给 sources 补 chunked_at（本代 elements 已成功走完分块的时刻；NULL=未成功
        分块）。存量回填见 spec E 节：凡老代码里跑到过分块步的源一律置 updated_at,
        否则合法的纯标题/短文 md 会被 H3 集体误报缺分块。

        撞号顺延:P1.5 原为 _migration_26,但 #327(knowhow 表版本管理)与 #328 先合入、
        已占 _migration_26/SCHEMA 26,本迁移接在其后为 _migration_27、SCHEMA 27。"""
        with self._connect() as db:
            self.add_column_if_missing(db, "sources", "chunked_at", "TEXT")
            db.execute(
                "UPDATE sources SET chunked_at = updated_at "
                "WHERE chunked_at IS NULL AND parse_status IN "
                "('parsed','extracting','extracted')"
            )

    def _migration_28(self) -> None:
        """每笔记本文档数量上限(per-user 可配)：app_settings KV 表 + user_profiles
        新增可空列 upload_document_limit。

        与近期迁移(v22/23/26/27)同款单层写法:新表/新列**只**在本步定义,不再回写
        _migration_1 baseline。migrate() 对全新库全量 sweep 1..28,本步随之执行建表/
        补列;已部署库(user_version>=1)也靠本步补齐。CREATE TABLE IF NOT EXISTS +
        add_column_if_missing 均幂等。upload_document_limit 经 add_column_if_missing
        追加,落在 user_profiles 末尾(model_settings 之后);全新库与升级库列序一致
        (同 memory_id / doc_type 的列序约束)。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            self.add_column_if_missing(
                db, "user_profiles", "upload_document_limit", "INTEGER"
            )

    def _migration_29(self) -> None:
        """Reconcile the two migration-24 lineages after the PostgreSQL merge.

        PostgreSQL-adapter builds used v24 for the cluster-membership unique
        index, while master used it for ``kg_canonical_scratch``.  Repeating
        both idempotent changes here makes upgrades from either lineage safe.
        """
        with self._connect() as db:
            db.executescript(
                """
                DELETE FROM concept_clusters
                WHERE id IN (
                  SELECT id FROM (
                    SELECT
                      id,
                      ROW_NUMBER() OVER (
                        PARTITION BY notebook_id, object_type, member_object_id
                        ORDER BY created_at DESC, id COLLATE BINARY DESC
                      ) AS duplicate_rank
                    FROM concept_clusters
                  ) AS ranked
                  WHERE duplicate_rank > 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_clusters_notebook_type_member
                  ON concept_clusters(notebook_id, object_type, member_object_id);

                CREATE TABLE IF NOT EXISTS kg_canonical_scratch (
                  notebook_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  seed TEXT NOT NULL,
                  canonical_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  canonical_description TEXT NOT NULL DEFAULT '',
                  canonical_desc_sig TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_kg_canonical_scratch_nb_run_seed
                  ON kg_canonical_scratch(notebook_id, run_id, seed);
                """
            )

    def _migration_30(self) -> None:
        """按内容哈希查源（上传去重 / batch_ingest 续跑）此前是全表扫。"""
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_notebook_file_hash "
                "ON sources(notebook_id, file_hash)"
            )

    def _migration_31(self) -> None:
        """Add inert, payload-free metadata for transactional shadow capture.

        The migration deliberately creates no control row and no business-table
        trigger.  A run identity is required for both, so the shadow capture
        installer creates them later in one ``BEGIN IMMEDIATE`` transaction
        after its logical-key scans have passed.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_change_log (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  table_name TEXT NOT NULL,
                  key_json TEXT NOT NULL,
                  operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
                  schema_epoch INTEGER NOT NULL,
                  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS shadow_capture_control (
                  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                  write_frozen INTEGER NOT NULL CHECK (write_frozen IN (0, 1)),
                  apply_active INTEGER NOT NULL DEFAULT 0 CHECK (apply_active IN (0, 1)),
                  run_id TEXT NOT NULL,
                  schema_epoch INTEGER NOT NULL
                );
                """
            )

    def _migration_32(self) -> None:
        """Persist report question-understanding and clarification review state."""
        with self._connect() as db:
            self.add_column_if_missing(
                db,
                "reports",
                "understanding_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )

    def _migration_33(self) -> None:
        """Stable keyset order for bounded source/target relation probes."""
        with self._connect() as db:
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source_id
                  ON knowledge_relations(notebook_id, source_object_id, id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_target_id
                  ON knowledge_relations(notebook_id, target_object_id, id);
                """
            )

    def _migration_34(self) -> None:
        """Persistent, generation-scoped relation-completion keyset state."""
        with self._connect() as db:
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_objects_source_id
                  ON knowledge_objects(source_id, id);
                CREATE TABLE IF NOT EXISTS kg_relation_completion_state (
                  notebook_id TEXT NOT NULL,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  next_object_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  schema_version INTEGER NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(source_id, source_generation, mode)
                );
                CREATE INDEX IF NOT EXISTS idx_kg_relation_completion_state_nb_status
                  ON kg_relation_completion_state(notebook_id, status, updated_at);
                """
            )

    def _migration_35(self) -> None:
        """Keep the browser-captured Ask instant available while a durable job
        is still running. Completed turns continue to carry the same value in
        the existing answer payload JSON."""
        with self._connect() as db:
            self.add_column_if_missing(
                db, "ask_jobs", "asked_at", "TEXT NOT NULL DEFAULT ''"
            )

    def _migration_36(self) -> None:
        """KG 质量分析的三张预计算产物表(设计 §3.4)。

        全部是**派生数据**:由 `rebuild_communities` 在社区构建那一批里**一次事务**
        整体重写。没有任何写路径会增量改它们,所以不需要 `updated_at` / 冲突消解语义
        —— 一次 DELETE + 一批 INSERT 就是它们的全部生命周期。

        `kg_analysis_artifacts` 是**产物账本**(设计 §3.3「逐指标标注新鲜度」):每一份
        产物在这里恰好一行,带上它建于哪个 `kg_mutation_seq`。为什么必须有它,而不是只
        靠外部反查 `unified_kg_state`:
          · 生产 base 库上出现过「communities 有 88580 个板块、但库尚未 rebuild」导致
            重大误读的真实事故 —— 产物严重落后于当前 KG 在生产里是**常态**,不是边缘
            情况。T3 要按**每一份产物**标注落后多少,而不是在整份报告上挂一条笼统横幅。
          · **空产物与缺失产物必须可区分**:单一板块的图 legitimately 产出 0 条跨板块边,
            没有账本行就无法把它和「从来没算过」分开。账本行的**存在与否**才是「这份
            产物在不在」的唯一判据,行数不是。
        三条统计快照(簇大小直方图 / 最大簇榜单 / 边出处构成)的 payload 就是产物本身;
        两张明细表的账本行 payload 只放汇总与口径参数(阈值、头部板块数、level)。

        ⚠ **三张表都没有 `level` 列**,这是刻意的。社区层的新鲜度闸
        `unified_kg_state.community_seq` 本身就不分 level,所以一次预计算产出的永远是
        **一套**自洽的产物,它描述的 level 记在账本 payload 的 `level` 字段里。给
        `kg_community_edges` 单独带一个 level 列而另两张表没有,只会让删除口径三处不
        一致:一次 level=1 的重建会抹掉 level-0 的账本、却留下 level-0 的明细行 ——
        按「账本存在与否是唯一判据」它们「不存在」,却仍会被 `WHERE level=0` 读出来。

        `notebook_id` 的外键与 `communities` / `concept_clusters` 同款
        (ON DELETE CASCADE):删库时派生产物必须一起走,不能留成孤儿报告。
        `source_id` **刻意不加外键**:`knowledge_objects.source_id` 本来就没有外键
        (见 kg_quality_audit 的孤儿来源统计——历史清理会留下指向已删 source 的引用),
        加了外键会让预计算在这种存量库上直接写不进去。

        主键按查询形态定:
          · kg_community_edges 按 notebook_id 整体读 → 主键前缀即索引,
            同时把「一对板块只能有一行」这条折叠不变式钉死在库里。
          · kg_source_profiles 按 notebook_id 整体读、按 mainstream_share 升序取
            「关联稀疏」的头部来源 → 主键 (notebook_id, source_id) 保证每源一行,
            另加 (notebook_id, mainstream_share) 索引给升序扫描用。
          · kg_analysis_artifacts 按 (notebook_id, kind) 点读 → 复合主键即索引。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_community_edges (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  src_community_id TEXT NOT NULL,
                  dst_community_id TEXT NOT NULL,
                  weight INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (notebook_id, src_community_id, dst_community_id)
                );

                CREATE TABLE IF NOT EXISTS kg_source_profiles (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL,
                  n_objects INTEGER NOT NULL DEFAULT 0,
                  n_graph_objects INTEGER NOT NULL DEFAULT 0,
                  top_community_id TEXT NOT NULL DEFAULT '',
                  top_share REAL NOT NULL DEFAULT 0,
                  community_spread INTEGER NOT NULL DEFAULT 0,
                  mainstream_share REAL NOT NULL DEFAULT 0,
                  PRIMARY KEY (notebook_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_kg_source_profiles_nb_mainstream
                  ON kg_source_profiles(notebook_id, mainstream_share);

                CREATE TABLE IF NOT EXISTS kg_analysis_artifacts (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,
                  kg_mutation_seq INTEGER NOT NULL DEFAULT -1,
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (notebook_id, kind)
                );
                """
            )

    def _migration_37(self) -> None:
        """Indexed per-source, per-element-type keyset order for bounded
        collection enumeration (formula/table/image/code_block listings)."""
        with self._connect() as db:
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_source_elements_source_type
                  ON source_elements(source_id, element_type, created_at, id);
                """
            )

    def _migration_38(self) -> None:
        """Bounded user-visible source identity roster for reasoning scope."""
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_visible_identity "
                "ON sources(notebook_id, created_at, id) "
                "WHERE source_type NOT IN ('memory','knowhow')"
            )

    def _migration_39(self) -> None:
        """命令目录抽取(方案 C)的任务行与候选行。

        `catalog_jobs` 是**跨进程单飞守卫的载体**,与 `kg_build_jobs` 同一条先例:
        条件唯一索引把「一个来源同时只能有一个活跃抽取」钉在库里,而不是靠进程内
        的一个集合——后者在多 worker / 进程重启后一文不值。守卫的粒度是 **source**
        而非 notebook:一本手册一个来源,同库的另一本手册没有理由被挡住。
        `queued` 与 `running` 都进守卫谓词:行先建、线程后起,那个窗口里重复 POST
        必须被挡住,否则会排出两个写同一份候选的 job。

        `catalog_candidates` 存**未确认**的抽取结果,包括被接地校验整条拦下的那些
        (`state='rejected'`)。被拦条目入表是刻意的:一次「零产出」的抽取,用户唯一
        能自己判断「是模型错了还是文档不是手册」的依据就是这些被拦记录与它们的
        `reject_info`;把它们丢掉只会留下一个无从解释的空目录。

        `position` 是**每个 job 内自增的插入序号**,存在的理由只有一个:候选列表要按
        keyset 翻页,而 id 是随机 uuid、`created_at` 在同一节内逐字相同,两者都排不出
        稳定全序。job 是单飞且不续跑的,所以序号由跑批的那一个线程递增即可,不需要
        库内 `MAX()+1` 的读-改-写。索引 `(job_id, state, position)` 正是分页谓词。

        `payload` / `reject_info` 是 JSON 文本(PostgreSQL 侧为 jsonb),`diagnostic`
        是**内部诊断**、绝不上屏——只有 `failure_reason` 是按 `user_error()` 口径写的
        中文用户文案。两列分开而不是合并,是为了让「能给用户看」这件事按出处判定,
        而不是靠事后猜测某个字段的形态。

        两张表的 `ON DELETE CASCADE` 都指向真正的主人:删来源/删库时,抽取任务与候选
        必须一起走,不能留成指向已删来源的孤儿审阅队列。`catalog_candidates.job_id`
        **刻意没有外键**(与 PostgreSQL 侧逐字对齐,理由见 0016 迁移的注释):任务行是
        历史、从不单独删,真正的删除路径只有「删来源」「删库」,而候选自己挂在这两张
        表上;反过来,一条指向 catalog_jobs 的入向外键会让它不再是叶表,而
        `idx_catalog_jobs_one_active` 是 source_id 单列面(source_id 本身又是外键列、
        NOT NULL),正向 shadow 的 UNIQUE 停车策略就只剩「叶表同事务 delete/reinsert」
        这一条——加了那条外键会让整个 PG16 catalog 变成不可停车。

        `truncated_sections` 与 `applied_table_id` 是这份迁移**发布前**的两处 C1b
        评审修复,直接改在这张刚建、还没有任何部署过的表上(而不是追加
        `_migration_40`)——`catalog_jobs` 本身就是本特性这一批引入的新表,没有
        "已部署库版本闸短路" 的顾虑。`truncated_sections` 把 C1a 早就统计好的
        `SectionOutcome.truncation.applied` 计数透传出来,好让审阅界面能说
        「N 节因过长被截断」而不是让这条信息停在服务层。`applied_table_id` 记录
        confirm 落进的知识库表:第一次 apply 成功后写入,后续同一个 job 的 apply
        优先按它解析目标——按标题回退只在这一列为空、或它指向的表已经不在时才
        发生,这样表被人工改名之后,同一个 job 的第二页确认依然写回同一张表,而
        不是因为标题对不上就另起一张「命令目录：<来源>」。

        `source_generation` 是同一批**发布前**评审修复的第三处(R8):这一列存的是
        任务创建那一刻**来源元素的代次**——`MAX(source_elements.created_at)`,而
        `replace_elements` 把重解析后的整批新元素写成同一个 `created_at`,所以它
        当且仅当元素被换过时才变。候选行里的命令名、摘录、出处全部指向抽取时读到
        的那一代元素;重解析之后原地把它们确认进知识表,写进去的就是文档里已经不
        存在的内容。刻意**不**用 `sources.updated_at`(那也是这一列曾经的候选):它
        是有意做粗的变更信号,每次生命周期状态迁移(`extracting`/`extracted`、摘要、
        重新抽取)都会推进,而这些都不碰 `source_elements`——按它判会在一次普通的
        重新抽取之后谎报「来源已重新解析」,并逼用户重跑一整轮付费识别。

        `idx_knowhow_tables_nb_title` 是本迁移里**唯一建在既有表上**的索引,由 R14 P2
        评审追加。命令目录按标题解析目标表(`knowhow_table_id_by_title`)是一次
        `WHERE notebook_id=? AND title=? ORDER BY created_at,id LIMIT 1`;库里原本只有
        `idx_knowhow_tables_nb` 单列索引,那条查询会把该 notebook 下**每一张**表都取出来
        再按标题过滤、再排序,代价随知识库里的表数线性增长——而它落在 apply/dismiss 的
        持锁窗口内,正是最不该做线性扫描的地方。四列复合索引把它变成一次索引定位:前两
        列做等值 seek,后两列直接给出 `list_knowhow_tables` 那份 `ORDER BY created_at,id`
        的 tie-break 顺序,`LIMIT 1` 不再需要额外排序步骤。给既有表加索引写在这份**尚未
        发布**的迁移里是合法追加(与 `truncated_sections`/`applied_table_id`/
        `source_generation` 三处发布前修复同一条理由):v39 还没有任何已部署库,版本闸
        不会对谁短路,已部署的 v38 库升级时会照常执行到这一句。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalog_jobs (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  created_by TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'queued',
                  sections_total INTEGER NOT NULL DEFAULT 0,
                  sections_done INTEGER NOT NULL DEFAULT 0,
                  entries INTEGER NOT NULL DEFAULT 0,
                  rejected INTEGER NOT NULL DEFAULT 0,
                  uncovered INTEGER NOT NULL DEFAULT 0,
                  truncated_sections INTEGER NOT NULL DEFAULT 0,
                  failure_reason TEXT NOT NULL DEFAULT '',
                  diagnostic TEXT NOT NULL DEFAULT '',
                  applied_table_id TEXT NOT NULL DEFAULT '',
                  source_generation TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_jobs_one_active
                  ON catalog_jobs(source_id) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS idx_catalog_jobs_source_created
                  ON catalog_jobs(source_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_catalog_jobs_nb_created
                  ON catalog_jobs(notebook_id, created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS catalog_candidates (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL DEFAULT 0,
                  section_path TEXT NOT NULL DEFAULT '',
                  command_name TEXT NOT NULL DEFAULT '',
                  payload TEXT NOT NULL DEFAULT '{}',
                  state TEXT NOT NULL DEFAULT 'candidate',
                  reject_info TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_candidates_job_state
                  ON catalog_candidates(job_id, state, position);
                CREATE INDEX IF NOT EXISTS idx_catalog_candidates_nb
                  ON catalog_candidates(notebook_id);
                CREATE INDEX IF NOT EXISTS idx_catalog_candidates_source
                  ON catalog_candidates(source_id);

                CREATE INDEX IF NOT EXISTS idx_knowhow_tables_nb_title
                  ON knowhow_tables(notebook_id, title, created_at, id);
                """
            )

    def _migration_40(self) -> None:
        """Persist immutable source-local KG facts before global fusion.

        Facts deliberately do not foreign-key ``global_object_id``: fusion and
        governance may deprecate or remove a global projection, while the
        source-generation fact remains the stable input for scoped retrieval.
        Source/notebook deletion still cascades through the real owners.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_source_facts (
                  id TEXT NOT NULL PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  local_object_id TEXT NOT NULL,
                  global_object_id TEXT NOT NULL DEFAULT '',
                  object_type TEXT NOT NULL,
                  payload TEXT NOT NULL DEFAULT '{}',
                  evidence TEXT NOT NULL DEFAULT '[]',
                  projection_version INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_source_facts_source_generation
                  ON knowledge_source_facts(source_id, source_generation, id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_source_facts_generation_local
                  ON knowledge_source_facts(
                    source_id, source_generation, local_object_id
                  );
                CREATE INDEX IF NOT EXISTS idx_knowledge_source_facts_notebook_object
                  ON knowledge_source_facts(notebook_id, global_object_id);

                CREATE TABLE IF NOT EXISTS knowledge_source_fact_elements (
                  fact_id TEXT NOT NULL REFERENCES knowledge_source_facts(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  element_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (fact_id, element_id)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_source_fact_elements_source
                  ON knowledge_source_fact_elements(
                    source_id, source_generation, element_id, fact_id
                  );
                """
            )

    def _migration_41(self) -> None:
        """Persist restartable source-fact backfill progress and audit state."""
        with self._connect() as db:
            self.add_column_if_missing(
                db,
                "knowledge_source_facts",
                "projection_origin",
                "TEXT NOT NULL DEFAULT 'live' "
                "CHECK(projection_origin IN ('live','historical'))",
            )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_source_fact_backfills (
                  source_id TEXT NOT NULL PRIMARY KEY
                    REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  projection_version INTEGER NOT NULL DEFAULT 1,
                  status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running','complete','incomplete','failed')),
                  after_object_id TEXT NOT NULL DEFAULT '',
                  objects_scanned INTEGER NOT NULL DEFAULT 0,
                  facts_written INTEGER NOT NULL DEFAULT 0,
                  incomplete_objects INTEGER NOT NULL DEFAULT 0,
                  incomplete_reason TEXT NOT NULL DEFAULT '',
                  failure_code TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_source_fact_backfills_notebook
                  ON knowledge_source_fact_backfills(notebook_id, status, source_id);
                CREATE INDEX IF NOT EXISTS idx_kos_source_object
                  ON knowledge_object_sources(source_id, object_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_source_facts_source_generation_global
                  ON knowledge_source_facts(
                    source_id, source_generation, projection_origin,
                    global_object_id
                  );
                """
            )

    def _migration_42(self) -> None:
        """Persist restartable source reverse-index rebuild progress."""
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_index_backfills (
                  notebook_id TEXT NOT NULL PRIMARY KEY
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  kg_mutation_seq INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running','complete','failed')),
                  after_object_id TEXT NOT NULL DEFAULT '',
                  total_objects INTEGER NOT NULL DEFAULT 0,
                  objects_scanned INTEGER NOT NULL DEFAULT 0,
                  rows_written INTEGER NOT NULL DEFAULT 0,
                  failure_code TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_source_index_backfills_status
                  ON source_index_backfills(status, notebook_id);
                """
            )

    def _migration_43(self) -> None:
        """Per-report public share tokens.

        The token lives on the report rather than in a side table: it is one
        nullable value with the same lifetime as its row, so a deleted report
        takes its public link with it without a second cascade to maintain.
        The partial unique index only covers issued tokens, so the unshared
        majority costs nothing and NULLs never collide.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "reports", "share_token", "TEXT")
            # Nullable, not `NOT NULL DEFAULT ''`: PostgreSQL stores this as a
            # nullable timestamptz, and the forward-shadow timestamp converter
            # only maps *registered* empty sentinels to NULL.  An unregistered
            # `''` would call `datetime.fromisoformat("")` on every unshared
            # report — i.e. almost every row — and abort the migration.
            self.add_column_if_missing(db, "reports", "shared_at", "TEXT")
            db.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_share_token
                  ON reports(share_token) WHERE share_token IS NOT NULL;
                """
            )

    def _migration_44(self) -> None:
        """Optional generated-question vectors bound to original chunks."""
        with self._connect() as db:
            self.add_column_if_missing(db, "chunks", "question_indexed_at", "TEXT")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunk_questions (
                  id TEXT PRIMARY KEY,
                  chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  question TEXT NOT NULL,
                  vector BLOB NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(chunk_id, question)
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_questions_nb
                  ON chunk_questions(notebook_id, id);
                CREATE INDEX IF NOT EXISTS idx_chunk_questions_source
                  ON chunk_questions(source_id, id);
                """
            )

    def _migration_45(self) -> None:
        """User-level interface mode preference ("auto" | "advanced").

        Nullable column on user_profiles (same pattern as v28's
        upload_document_limit): NULL means "no explicit choice yet", and the
        application layer (identity store read path) falls back to "auto"
        rather than this migration writing a default into every existing
        row.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "user_profiles", "ui_mode", "TEXT")

    def _migration_46(self) -> None:
        """element→chunk reverse index (+ its restartable backfill ledger).

        ``chunks.element_ids`` is the forward direction; answering "which
        chunks contain this element" previously meant scanning every chunk row
        of the notebook and ``json.loads``-ing each one.  The reverse rows make
        that an indexed point lookup for the per-query consumer.

        The composite primary key IS the covering index for the
        ``(notebook_id, element_id)`` seek; the extra ``chunk_id`` index only
        serves the ``ON DELETE CASCADE`` from ``chunks`` (source delete /
        reparse / knowhow cell rewrite all delete chunk rows and must not turn
        into a table scan of this side table).

        Migration only creates the empty tables.  Existing rows are projected
        by the explicit offline ``backfill-chunk-elements`` CLI — never inside
        a request — exactly like ``source_index_backfills``.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunk_elements (
                  notebook_id TEXT NOT NULL,
                  element_id TEXT NOT NULL,
                  chunk_id TEXT NOT NULL
                    REFERENCES chunks(id) ON DELETE CASCADE,
                  PRIMARY KEY (notebook_id, element_id, chunk_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_elements_chunk
                  ON chunk_elements(chunk_id);
                CREATE TABLE IF NOT EXISTS chunk_element_backfills (
                  notebook_id TEXT NOT NULL PRIMARY KEY
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  kg_mutation_seq INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running','complete','failed')),
                  after_chunk_id TEXT NOT NULL DEFAULT '',
                  total_chunks INTEGER NOT NULL DEFAULT 0,
                  chunks_scanned INTEGER NOT NULL DEFAULT 0,
                  rows_written INTEGER NOT NULL DEFAULT 0,
                  failure_code TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_chunk_element_backfills_status
                  ON chunk_element_backfills(status, notebook_id);
                """
            )
            # chunk_elements_indexed: 0/1 marker — once set, the element→chunk
            # reverse lookup trusts chunk_elements for this notebook and skips
            # the legacy whole-notebook chunk scan.  Additive; pre-existing DBs
            # start at 0 (never backfilled) and keep the legacy behaviour
            # byte-for-byte until the offline CLI flips it.
            self.add_column_if_missing(
                db, "unified_kg_state", "chunk_elements_indexed",
                "INTEGER NOT NULL DEFAULT 0",
            )

    def _migration_47(self) -> None:
        """Notebook-owned object-schema overrides and custom definitions.

        ``object_schemas`` remains the administrator-owned global baseline.
        Older schema-induction rows carried a non-empty ``notebook_id`` in that
        global table; move those rows into the scoped table so two notebooks may
        propose the same type independently and no proposal can leak cross-library.
        """
        with self._connect() as db:
            orphan = db.execute(
                "SELECT os.object_type, os.notebook_id FROM object_schemas os "
                "LEFT JOIN notebooks n ON n.id=os.notebook_id "
                "WHERE os.notebook_id<>'' AND n.id IS NULL LIMIT 1"
            ).fetchone()
            if orphan is not None:
                raise RuntimeError(
                    "cannot migrate notebook-scoped object schema with missing notebook"
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS notebook_object_schemas (
                  notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  object_type TEXT NOT NULL,
                  plural TEXT NOT NULL DEFAULT '',
                  fields TEXT NOT NULL DEFAULT '[]',
                  primary_field TEXT NOT NULL DEFAULT '',
                  description TEXT NOT NULL DEFAULT '',
                  label TEXT NOT NULL DEFAULT '',
                  list_fields TEXT NOT NULL DEFAULT '[]',
                  source TEXT NOT NULL DEFAULT 'custom',
                  status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','proposed','disabled')),
                  rationale TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (notebook_id, object_type)
                );
                CREATE INDEX IF NOT EXISTS idx_notebook_object_schemas_status
                  ON notebook_object_schemas(notebook_id, status, object_type);

                INSERT INTO notebook_object_schemas
                (notebook_id, object_type, plural, fields, primary_field,
                 description, label, list_fields, source, status, rationale,
                 created_by, created_at, updated_at)
                SELECT os.notebook_id, os.object_type, os.plural, os.fields,
                       os.primary_field, os.description, os.label, os.list_fields,
                       os.source, os.status, os.rationale,
                       COALESCE(n.created_by, ''), os.created_at, os.updated_at
                FROM object_schemas os
                JOIN notebooks n ON n.id = os.notebook_id
                WHERE os.notebook_id <> '';

                DELETE FROM object_schemas
                WHERE notebook_id <> ''
                  AND EXISTS (
                    SELECT 1 FROM notebooks n WHERE n.id=object_schemas.notebook_id
                  );
                """
            )

    def _migration_48(self) -> None:
        """Agent provenance on ``sources`` (nullable ``agent_profile_id``).

        NULL is the load-bearing value: it means "a person added this source",
        and the Agent-facing delete tool is only ever allowed to remove rows
        whose provenance proves the Agent itself added them.  Nothing is
        backfilled — every already-deployed row is user-added by definition,
        which is exactly what NULL states.

        Deliberately no index and no unique constraint, unlike ``memory_id``
        (v14): the delete permission check is a single-row read on the primary
        key, and nothing enumerates "sources of this agent".  Deliberately no
        foreign key to ``agent_profiles`` either — mirroring ``memory_id``,
        which likewise stores a bare id.  Provenance must survive the profile
        being deleted (the row was still not user-added), and an incoming FK
        would also add an edge to the forward-shadow parent closure.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "sources", "agent_profile_id", "TEXT")

    def _migration_49(self) -> None:
        """Group knowledge sharing P1: groups, group membership, notebook grants.

        Zero behavior change — three additive tables with no reader or writer
        anywhere in the codebase yet. ``access_sql.py``'s read-access predicate
        extension, the group/grant API surface and the frontend all land in
        later tasks of the same feature; this migration only lays the schema
        down so later tasks touch data, not DDL.

        ``groups`` is the group row itself. ``kind`` (project/department/domain)
        and every other enumerated column in this migration is validated in the
        application layer, not by a ``CHECK`` constraint — the design doc's
        already-decided list requires this, because the forward-shadow UNIQUE
        park strategy on ``notebook_grants`` (see below) depends on at least one
        of ``principal_type``/``principal_id`` staying free of both CHECK and FK
        so it remains a legal SENTINEL_TEXT/NULL park column; a CHECK on
        ``principal_type`` would remove that candidate.

        ``group_members`` is deliberately shaped like ``notebook_members``
        (v16): composite ``(group_id, user_id)`` primary key, both columns
        explicitly ``NOT NULL`` so this table needs no entry in the shadow
        migration's ``_SQLITE_NULL_GUARD_KEYS`` (that set exists only for
        rowid tables whose declared PK is nullable — this one's isn't).
        ``ON DELETE CASCADE`` on ``group_id`` means deleting a group silently
        drops its membership rows; ``user_id`` has no ON DELETE clause,
        mirroring every other ``REFERENCES users(id)`` column in this schema.

        ``notebook_grants`` is the polymorphic authorization edge: one row
        grants ``role`` (viewer|admin) to ``principal_type`` (user|group|
        group_admins|everyone) identified by ``principal_id``. ``principal_id``
        is deliberately a bare, unconstrained TEXT column with NO foreign key —
        it references either ``users.id`` or ``groups.id`` depending on
        ``principal_type``, and no single-table FK can express that (the
        ``catalog_candidates.job_id`` precedent, v39). It is ``NOT NULL
        DEFAULT ''`` — the ``everyone`` principal stores ``''``, NOT NULL:
        both backends treat NULLs as distinct in UNIQUE comparisons, so a
        nullable ``principal_id`` would silently exempt every ``everyone``
        row from ``UNIQUE (notebook_id, principal_type, principal_id)`` —
        duplicate grants would accumulate and revoking one would not revoke
        the permission. Keeping the column NOT NULL also hands the
        forward-shadow UNIQUE park strategy to ``principal_type``
        (SENTINEL_TEXT) — which is why neither of those two columns may ever
        gain a CHECK or FK (see the park-candidate note above). Consumers must
        match ``principal_type`` against the four known values exactly and
        never infer ``everyone`` from ``principal_id`` — a parked shadow row
        temporarily carries a sentinel ``principal_type``, and exact matching
        makes such rows fail safe (matching nobody). ``id`` is the primary key
        and every PK/composite-PK column in this migration is explicitly
        ``NOT NULL`` for the same null-guard-avoidance reason as
        ``group_members`` above.

        The UNIQUE constraint's implicit index already serves lookups by
        ``notebook_id`` (leftmost prefix), so there is deliberately no
        separate ``idx_notebook_grants_nb``; ``idx_notebook_grants_principal``
        is NOT redundant (its leading column differs) and stays.

        All three tables are additive with no backfill: no existing row in any
        other table implies a group or a grant, so there is nothing to
        populate. ``notebook_grants`` and ``group_members`` both CASCADE off
        their owning row (``notebooks``/``groups``) so deleting the parent
        cannot leave an orphaned authorization or membership row behind.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                  id          TEXT NOT NULL PRIMARY KEY,
                  name        TEXT NOT NULL,
                  kind        TEXT NOT NULL DEFAULT 'project',
                  description TEXT NOT NULL DEFAULT '',
                  created_by  TEXT REFERENCES users(id),
                  created_at  TEXT NOT NULL,
                  updated_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_members (
                  group_id  TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                  user_id   TEXT NOT NULL REFERENCES users(id),
                  role      TEXT NOT NULL DEFAULT 'member',
                  added_at  TEXT NOT NULL,
                  added_by  TEXT REFERENCES users(id),
                  PRIMARY KEY (group_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
                CREATE TABLE IF NOT EXISTS notebook_grants (
                  id             TEXT NOT NULL PRIMARY KEY,
                  notebook_id    TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  principal_type TEXT NOT NULL,
                  principal_id   TEXT NOT NULL DEFAULT '',
                  role           TEXT NOT NULL,
                  created_by     TEXT REFERENCES users(id),
                  created_at     TEXT NOT NULL,
                  UNIQUE (notebook_id, principal_type, principal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_notebook_grants_principal ON notebook_grants(principal_type, principal_id);
                """
            )

    def _migration_50(self) -> None:
        """Group knowledge sharing P2: notebook_share_requests (member
        contribution approval workflow).

        Zero behavior change — one additive table with no reader or writer
        anywhere in the codebase yet. The axis here is easy to get backwards,
        so it is spelled out explicitly: ``requested_by`` is someone who
        already has admin/owner (manage) rights on ``notebook_id`` — they can
        share it unilaterally to any group whose *admin* they also are — but
        is merely an ordinary member (not necessarily an admin) of
        ``group_id``, the group they are asking to contribute the notebook
        to. A group admin sharing into their own group never goes through
        this table at all; they write a ``notebook_grants`` row directly.
        This table exists only for the other half: a manage-rights holder on
        a notebook who is *not* an admin of the group they want to share
        into has to ask, and a group admin later approves or rejects the
        request. The API surface and frontend land in a later task of the
        same feature; this migration only lays the schema down.

        Unlike ``notebook_grants.principal_id`` (v49), this table has no
        polymorphic column — ``group_id`` always references ``groups.id`` —
        so it carries a real foreign key with no park-strategy tradeoff.
        ``status`` (pending|approved|rejected) is validated in the
        application layer, not by a ``CHECK`` constraint, matching every
        other enumerated column in the group-sharing schema (v49's ``kind``,
        ``role``, ``principal_type``). ``id`` is the primary key and is
        explicitly ``NOT NULL`` so this table needs no entry in the shadow
        migration's ``_SQLITE_NULL_GUARD_KEYS`` — the same reasoning as
        ``notebook_grants.id`` in v49.

        Both ``notebook_id`` and ``group_id`` are ``ON DELETE CASCADE``:
        deleting the notebook or the group being requested-into drops the
        request, so no request can ever point at a notebook or group that no
        longer exists. ``requested_by`` and ``decided_by`` reference
        ``users(id)`` with no ON DELETE clause, mirroring every other
        ``REFERENCES users(id)`` column in this schema (e.g.
        ``notebook_grants.created_by``). ``decided_at`` is genuinely
        nullable TEXT with no default — it is unset (SQL NULL, not an empty
        string) until a group admin approves or rejects the request, so it
        needs no entry in ``POSTGRES_EMPTY_TIME_SENTINELS`` either: there is
        no legacy '' convention here to preserve, only true NULL on both
        backends.

        ``idx_share_requests_group`` is a plain (non-unique) index on
        ``(group_id, status)`` — the pending-requests-for-this-group lookup
        a group admin's review queue will use. It is deliberately NOT
        unique: a notebook can accumulate more than one request to the same
        group over time (e.g. rejected, then requested again), and nothing
        in the design requires collapsing them.

        ``uq_share_requests_one_pending`` is a **partial** unique index on
        ``(notebook_id, group_id, status)`` scoped to ``WHERE status =
        'pending'`` — it collapses to a plain two-column uniqueness
        constraint on ``(notebook_id, group_id)`` for pending rows only, so
        the same notebook can never accumulate two simultaneously-pending
        requests into the same group. The third column (``status``) is not
        needed for that semantics alone; it is added deliberately so the
        forward-shadow UNIQUE park strategy has a non-FK TEXT column to park
        a poisoned row into (SENTINEL_TEXT) without having to fall back to
        requiring this table forever have no incoming foreign key
        (LEAF_DELETE) — the same tradeoff already made for
        ``uq_notebook_grants_principal`` in v49. **Consumers must match
        ``status`` against its known values with exact equality and never
        write something like ``status != 'pending'`` to mean "already
        decided"** — mirroring the ``principal_type`` red line in v49: a
        row temporarily parked by the shadow replicator carries a sentinel
        string in ``status``, and exact matching against ``'pending'``/
        ``'approved'``/``'rejected'`` makes such a row fail safe (it matches
        none of them) instead of being silently miscounted as decided.

        This table is additive with no backfill: no existing row in any
        other table implies a share request, so there is nothing to
        populate.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS notebook_share_requests (
                  id           TEXT NOT NULL PRIMARY KEY,
                  notebook_id  TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  group_id     TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                  requested_by TEXT NOT NULL REFERENCES users(id),
                  status       TEXT NOT NULL DEFAULT 'pending',
                  decided_by   TEXT REFERENCES users(id),
                  decided_at   TEXT,
                  created_at   TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_share_requests_group ON notebook_share_requests(group_id, status);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_share_requests_one_pending
                  ON notebook_share_requests(notebook_id, group_id, status) WHERE status = 'pending';
                """
            )

    def _migration_51(self) -> None:
        """Agentic Memory P1: per-notebook understanding blocks + chain state.

        零行为变化——两张纯追加表,全仓没有任何读写方。store / ports / facade、
        注入面、巡固 job 与 API 都落在本特性的后续任务里;本迁移只铺 DDL,让后面
        的任务只碰数据、不碰 schema。

        ``agent_notebook_profile`` 是理解块本身:``owner_id=''`` 是这本库的共享
        底座,非空 ``owner_id`` 是该成员自己的覆盖层。``agent_profile_jobs`` 是
        每条链(库/成员)一行的巡固状态,兼当阈值计数器。

        六个已拍板的取舍(逐条登记,免得下一个读者把它们当疏漏"修"回去):

        1. ``owner_id`` 用 ``NOT NULL DEFAULT ''`` 哨兵,不用 NULL。理由与 v49
           ``notebook_grants.principal_id`` 逐字同源(见 ``_migration_49`` 与
           ``0027_group_sharing.sql`` 的模块注释):两个后端的 UNIQUE 都把 NULL
           视为互不相等,可空的 ``owner_id`` 会让**每一行底座**逃出唯一性——重复
           底座行可累积、清空清不干净、巡固的乐观并发失效。设计文档里"nullable
           列正是 shadow 停车最省事的形状"那句解决的是停车方案,而停车方案在这里
           由第 2 条更彻底地解决了。

        2. 唯一面用复合主键,不用独立 UNIQUE(仿 v47 ``notebook_object_schemas``
           的 ``PRIMARY KEY (notebook_id, object_type)``,shadow manifest 里是
           ``DECLARED_PK``)。这是**停车方案本身**:
           ``replicator.py::_build_unique_surfaces`` 的第一条分支是
           ``if set(columns) == set(spec.replication_key)`` → ``REPLICATION_KEY``,
           只要 shadow manifest 里这张表的 ``replication_key`` 逐字等于这几列,
           停车策略自动落到 ``REPLICATION_KEY``,不需要哨兵列、不需要 nullable
           列、不需要叶表 delete/reinsert。两张新表都按这条走。

        3. ``owner_id`` 刻意无外键到 ``users``:``''`` 哨兵与 FK 天然冲突;且与
           ``notebook_grants.principal_id``、``catalog_candidates.job_id``、
           ``sources.agent_profile_id`` 同一先例——出处/归属列存裸 id。这也让两张
           表不新增任何入向 FK(保持叶表)。**任何时候都不得给 ``owner_id`` /
           ``label`` 加 CHECK 或 FK**:那会同时破坏第 2 条的停车前提与枚举值走
           应用层校验的惯例(``label`` 取
           corpus_shape|key_entities|corpus_gaps|retrieval_notes|usage_gaps,
           ``updated_origin`` 取 job|user,``status`` 取
           idle|queued|running|done|failed|cancelled,都在应用层校验)。

        4. PK 三列全部显式 ``NOT NULL`` ⇒ 两张表都**不进** shadow
           ``manifest.py::_SQLITE_NULL_GUARD_KEYS``(那个集合只服务"rowid 表的
           声明 PK 可空"这一种形态)。

        5. 不建条件唯一索引,因此不需要 ``replicator.py::_UNIQUE_PREDICATES`` 的
           pin。单飞由 ``agent_profile_jobs`` 的 PK 行 CAS
           (``UPDATE ... WHERE (notebook_id, owner_id)=? AND status NOT IN
           ('queued','running')`` + rowcount 判据)承担,跨进程语义与
           ``idx_catalog_jobs_one_active`` / ``idx_kg_build_jobs_one_running``
           等价而更强(一把 PK 行锁,没有第二个唯一面)。**为什么不照抄
           ``catalog_jobs`` 的「每 run 一行 + 条件唯一索引」**:那个形状的存在理由
           是候选审阅队列与逐窗进度需要 per-run 历史行;这里 UI 只轮询「当前这条链
           在不在忙」,而阈值计数器又必须挂在链上——一行 per-chain 状态同时满足
           两者,并少一个必须在 ``_UNIQUE_PREDICATES`` 里手工 pin 的部分唯一面
           (漏 pin 会让 ``ShadowReplicator`` 构造期对整个 schema 版本 fail
           closed)。

        6. 变更历史用块表内的 ``history_json`` 环形列,不新建
           ``agent_profile_changes`` 表——设计文档把这一项明文留给定稿,此处拍板
           选 JSON 列。理由:(a) P1 的界面只有「看/改/清空/手动重建」,不含历史
           回看与回滚,一张可查询的流水表买不到任何 P1 能力;(b) 每张新表都要付
           manifest 条目 + copy_rank + 停车面 + 深拷贝登记;(c) knowhow
           ``record_change`` 的形态是为「整表逆序 delta 重放 + 前后置指纹」服务
           的,而这里的回滚只是单行值交换;(d) 块值本身有硬字符上限,20 条的环形
           缓冲每行 ≤ 数 KB,有界。写入契约仍抄 knowhow 的那一半:历史条目必须在
           同一个写事务内、在块 UPDATE 之后追加(before/after + origin + actor +
           at + revision),超出上限丢最旧。

        **刻意不建任何二级索引**:PK 覆盖全部读路径——块按
        ``(notebook_id, owner_id IN ('', ?))`` 前缀查、按 PK 点写;启动兜底扫
        ``agent_profile_jobs`` 的 ``status`` 是每进程一次,表规模是「曾触发过的
        (库, 成员)」量级。写在这里免得后来者补一个无人使用的索引。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_notebook_profile (
                  notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  owner_id      TEXT NOT NULL DEFAULT '',
                  label         TEXT NOT NULL,
                  value         TEXT NOT NULL DEFAULT '',
                  evidence_json TEXT NOT NULL DEFAULT '[]',
                  history_json  TEXT NOT NULL DEFAULT '[]',
                  revision      INTEGER NOT NULL DEFAULT 1,
                  updated_by    TEXT NOT NULL DEFAULT '',
                  updated_origin TEXT NOT NULL DEFAULT 'job',
                  created_at    TEXT NOT NULL,
                  updated_at    TEXT NOT NULL,
                  PRIMARY KEY (notebook_id, owner_id, label)
                );
                CREATE TABLE IF NOT EXISTS agent_profile_jobs (
                  notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  owner_id      TEXT NOT NULL DEFAULT '',
                  status        TEXT NOT NULL DEFAULT 'idle',
                  pending_signal INTEGER NOT NULL DEFAULT 0,
                  runs          INTEGER NOT NULL DEFAULT 0,
                  blocks_written INTEGER NOT NULL DEFAULT 0,
                  failure_reason TEXT NOT NULL DEFAULT '',
                  diagnostic    TEXT NOT NULL DEFAULT '',
                  started_at    TEXT NOT NULL DEFAULT '',
                  finished_at   TEXT NOT NULL DEFAULT '',
                  created_at    TEXT NOT NULL,
                  updated_at    TEXT NOT NULL,
                  PRIMARY KEY (notebook_id, owner_id)
                );
                """
            )

    def _migration_52(self) -> None:
        """Per-conversation public share tokens + a read watermark (T1 of
        question-answer session sharing).

        Same shape as v43's per-report share token (``reports.share_token``)
        and the pre-v9 ``notebooks.share_token``: the token lives on the
        conversation row rather than in a side table, so a deleted
        conversation takes its public link with it without a second cascade
        to maintain. The partial unique index only covers issued tokens, so
        the unshared majority costs nothing and NULLs never collide — the
        same park strategy PostgreSQL's forward-shadow replicator already
        has pinned for ``idx_reports_share_token``/``idx_notebooks_share_
        token`` (see ``_UNIQUE_PREDICATES`` in
        ``app/migration/shadow/replicator.py``).

        Unlike a report, a conversation keeps growing after it is shared —
        the design doc's decision is "freeze + explicit update" (sharing
        again pushes the boundary; it never happens implicitly), so this
        migration adds TWO watermark columns beyond the token:

        * ``shared_through_at`` is a literal ISO-8601 TIMESTAMP VALUE, not a
          foreign key to an answer row. Storing an answer id instead would
          make the watermark meaningless the moment that answer is deleted
          (no created_at to compare against, no way to tell what came before
          it); a literal value keeps the boundary predicate
          (``julianday(created_at) <= julianday(shared_through_at)``)
          self-contained regardless of whether the referenced row still
          exists. This is also why the closed-interval comparison is ``<=``:
          the watermark is captured from the last answer that existed at
          share time.
        * ``shared_through_id`` is a denormalized copy of that answer's
          ``id`` — recorded for display/audit ("shared through this turn"),
          NOT consumed by the boundary predicate above (which is
          self-contained on purpose, see above). It is a plain snapshotted
          string, not a live reference, so it carries the same
          delete-safety as ``shared_through_at``.

        The read order for a conversation's answers (oldest -> newest) is a
        three-key tie-break — ``julianday(created_at) ASC, created_at ASC,
        rowid ASC`` — that already exists verbatim in
        ``AskStateStore.get_conversation``. The design doc's C-3 decision
        requires the public snapshot query to read turns in the SAME order
        an author sees them, so that ordering is promoted to a named module
        constant (``CONVERSATION_ANSWERS_ORDER_ASC`` /
        ``_DESC`` in ``app/repositories/sqlite/ask_state_store.py``) shared
        by both queries, rather than being retyped a second time where it
        could silently drift.

        Zero behavior change: the store methods that read/write these three
        columns (``share_conversation`` / ``unshare_conversation`` /
        ``conversation_share_state`` / ``public_conversation_by_token``) have
        no caller anywhere in the codebase yet — the API surface, the
        row-level ``created_by`` gate and "must have at least one written
        answer" policy all land in a later task of the same feature. This
        migration only lays the schema down.

        Deep notebook copy does NOT need to scrub these columns before or
        after copying: ``_COPY_VALIDATED_TABLES`` in
        ``app/repositories/sqlite/sharing_store.py`` does not include
        ``conversations`` at all (conversations do not travel with a deep
        copy — same as ``reports``, ``answers`` and every other per-user
        working-state table). Unlike ``notebooks.share_token`` /
        ``reports.share_token`` (which DO need an explicit clear-on-copy
        step because the deep-copy path deliberately keeps a copy's rows in
        the destination tables those columns live on), there is no copy path
        that ever touches a conversation row, so there is nothing to scrub.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "conversations", "share_token", "TEXT DEFAULT NULL")
            self.add_column_if_missing(db, "conversations", "shared_through_at", "TEXT DEFAULT NULL")
            self.add_column_if_missing(db, "conversations", "shared_through_id", "TEXT DEFAULT NULL")
            db.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_share_token
                  ON conversations(share_token) WHERE share_token IS NOT NULL;
                """
            )

    def _migration_53(self) -> None:
        """Agentic Memory P2: 给巡固链的单飞加一个**代际**。

        P1 的单飞只有 ``status`` CAS。行被删除后重建(成员被移出 →
        ``clear_job_row`` 删行;重新加入 → 下一次 ``bump_signal``/``claim`` 的
        ``INSERT OR IGNORE`` 造一行新的)之后,旧 worker 的 ``settle`` 仍会命中
        新行——这就是 P1 登记的 R4 ABA:**结算错代**(消费掉新 run 的 claim 快照)
        与**写错代**(``expected_revision=0`` 把移出前的笔记写回去)。

        ``claim_token`` 每次 ``claim()`` 换一个新值,删除+重建必然换值,ABA 因此
        在结构上关闭:``settle`` 与 ``write_block`` 都把它当 CAS 条件的一部分。

        三条已拍板取舍(登记,免得后来者当疏漏"修"回去):

        1. **``runs`` 计数当不了代际**:它在 settle 时 +1、claim 时不动,行被删重建
           后新行 ``runs=0``,而首轮 run 的旧 worker 也持有 0——恰好在最常见的形态
           上碰撞。

        2. **``started_at`` 当不了代际**:SQLite 的 ``now()`` 截到秒
           (``memory_revisions`` 已登记过这个教训),同秒内的删除+重建会给出同一个
           值。乐观并发的代际只能是显式分配的不透明值,不能是时间戳。

        3. **``TEXT NOT NULL DEFAULT ''``,不是 nullable**:与
           ``_migration_51`` 第 1 条同源。这一列不进任何唯一面,所以 NULL 不会像
           ``owner_id`` 那样破坏 PK;但空串哨兵让「未认领」有一个可写进 SQL 的确定
           取值(``claim_token=''``),而 ``IS NULL`` 与 ``=?`` 在同一条 CAS 谓词里
           混用正是这类判据出错的经典形态。既有行升级后落在 ''——它们要么是
           ``idle``/终态(CAS 的 status 条件已经把它们排除),要么是崩溃遗留的
           ``running``(启动兜底 ``sweep_stale_on_start`` 无条件扫掉),所以不需要
           回填。

        **不建索引**:这一列只作为 PK 点查之后的**残余条件**出现
        (``WHERE notebook_id=? AND owner_id=? AND claim_token=?``),PK 已经把行定位
        到一行,加索引买不到任何东西。

        ⚠ 必须走 ``add_column_if_missing``(与 ``_migration_48`` 的
        ``sources.agent_profile_id`` 同一先例):SQLite 没有
        ``ADD COLUMN IF NOT EXISTS``,而「已部署库补建」那一族测试的做法是**只**回滚
        ``user_version`` 再重开——整条梯子会在一张已经带着这一列的表上重跑本迁移,
        裸 ALTER 直接 ``duplicate column name`` 炸在启动路径上。
        """
        with self._connect() as db:
            self.add_column_if_missing(
                db, "agent_profile_jobs", "claim_token",
                "TEXT NOT NULL DEFAULT ''",
            )

    def _migration_54(self) -> None:
        """Agentic Memory P2 (A/T5): 部署级**全局**的检索策略经验库。

        一条经验 = 「在**这类问题形态**下,**这个检索动作**值得/不值得用」加一句
        模型撰写的理由。它跨用户、跨笔记本共享,因此本表刻意**没有**
        ``notebook_id``、**没有** owner 列——不是漏了,是这张表的定位:它存的是
        「怎么查」的通用打法,不是任何人的、任何库的内容。

        七条已拍板取舍(逐条登记,免得后来者当疏漏"修"回去):

        1. **``id`` 是内容寻址的合成主键**,取「情境指纹 + 动作」的确定性哈希
           (``retrieval_experience_projection.experience_id``)。这一条同时买到
           三样东西:
           (a) 「同一情境同一动作只有一行」由主键自己保证,不需要第二个唯一面;
           (b) ``scripts/merge_dbs.py`` 的 ``GLOBAL_UNION_TABLES`` 用
               ``INSERT OR IGNORE`` 做跨部署并集,要求 id 跨独立部署稳定且不碰撞
               ——内容哈希正好满足,而**递增整数 id 会静默丢行**(两个部署各自的
               1 号经验撞主键,后者被无声丢弃);
           (c) 停车方案:``replicator.py::_build_unique_surfaces`` 的第一条分支是
               ``set(索引列) == set(replication_key)`` → ``REPLICATION_KEY``,
               单列 PK 逐字等于 shadow manifest 里声明的 replication key,于是
               零哨兵列、零 nullable 列、零叶表 delete/reinsert。

        2. **绝不给「情境指纹」单独加 UNIQUE 索引**。去重已由第 1 条的主键完成;
           多一个唯一面就多一个必须手工推导停车策略的面(而它还是个 TEXT 列,
           会掉进哨兵/候选搜索那条更贵的路径)。

        3. **无外键、无 CHECK**。``action``/``polarity`` 取自应用层封闭词表
           (``retrieval_experience_projection`` 的 ``RETRIEVAL_ACTIONS`` /
           ``EXPERIENCE_POLARITIES``),与 v51 两张表对 ``label``/``status`` 的
           同款惯例;而 CHECK 会破坏第 1(c) 条的停车前提。无入向外键 ⇒ 叶表。

        4. **两个时间列都 ``NOT NULL``**,所以本表**不进**
           ``schema_manifest.POSTGRES_EMPTY_TIME_SENTINELS``——那个集合只服务
           「SQLite ``TEXT NOT NULL DEFAULT ''`` 的可选时间列 ↔ PG nullable
           ``timestamptz``」这一种形态。这里两列在每条写路径上都必写。

        5. **``provenance_json`` 只放不透明 run id**,绝不放 ``notebook_id`` /
           ``user_id``:后两者能把「某人在某个库里问过某类问题」从一张全局表里
           拼回来,而这张表的每一行都对全体部署可见。它的作用是**幂等**:同一批
           run 被重复蒸馏时,靠 run id 集合去重,``support`` 不会重复累加。它有界
           (``RETRIEVAL_EXPERIENCE_PROVENANCE_MAX``),而「每批读取的 run 数
           ≤ 该上限」是一条被测试钉住的不变式——批次大于它,溢出丢弃的那些 run id
           就会在下一批里被重新计一次数。

        6. **``support`` 与 ``adopted`` 是两个不同的数**,不能合并:``support``
           是「多少次 run 支持这条结论」(蒸馏侧累加),``adopted`` 是「注入之后模型
           真的选了这个动作多少次」(T6 的注入侧累加)。淘汰按
           ``(adopted, support, updated_at)`` 升序——先淘汰没人用的,再淘汰证据薄
           的,最后才看新旧。两个数合成一个就没法表达「证据很多但从没人采纳」。

        7. **刻意不建任何二级索引**。全表行数有硬上限
           (``RETRIEVAL_EXPERIENCE_MAX_ENTRIES``),读路径只有两条:按 id 点查
           (主键覆盖)与「读全表在内存里按当前情境打分取 top-k」(有界全扫,300 行
           量级)。淘汰用的 ``(adopted, support, updated_at)`` 排序同样是那次有界
           全扫的一部分。给一张恒定几百行的表加索引,买到的是零。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS retrieval_experiences (
                  id              TEXT PRIMARY KEY,
                  situation_json  TEXT NOT NULL DEFAULT '{}',
                  action          TEXT NOT NULL DEFAULT '',
                  polarity        TEXT NOT NULL DEFAULT '',
                  rationale       TEXT NOT NULL DEFAULT '',
                  support         INTEGER NOT NULL DEFAULT 0,
                  adopted         INTEGER NOT NULL DEFAULT 0,
                  provenance_json TEXT NOT NULL DEFAULT '[]',
                  created_at      TEXT NOT NULL,
                  updated_at      TEXT NOT NULL
                );
                """
            )

    def _migration_55(self) -> None:
        """Agentic Memory P3 (T1): ``agent_observations`` + ``user_profiles.
        search_profile_json``. Zero behavior change — the store, port and
        MCP tool that read/write these land in later tasks of the same
        feature; this migration only lays the schema down.

        ``agent_observations`` is an append-only log of short lines an
        external Agent writes via the (later-landing) ``add_observation``
        MCP tool — one line "I noticed X while working in this notebook".
        They feed the per-member overlay consolidation job (T4) as
        UNTRUSTED input, never the answer/report path itself.

        Eight already-decided trade-offs (recorded so the next reader can
        tell a decision from an oversight):

        1. ``owner_id`` is the identity chain — whose overlay this
           observation eventually feeds. ``NOT NULL DEFAULT ''`` sentinel,
           not nullable, same reasoning as ``agent_notebook_profile.
           owner_id``/``notebook_grants.principal_id`` (see
           ``_migration_51``/``_migration_49``): a nullable owner_id would
           make every row incomparable under a UNIQUE index that includes
           it. It is not part of this table's PRIMARY KEY (unlike
           ``agent_notebook_profile``, whose PK IS
           ``(notebook_id, owner_id, label)``), but it IS the second column
           of the idempotency unique index below — making it nullable would
           let NULL-owner rows escape that index entirely (duplicate
           ``client_request_id`` writes would land) and would drift the
           shadow park column off ``client_request_id``.

        2. ``agent_profile_id`` is a bare provenance id with NO foreign key
           — same precedent as ``sources.agent_profile_id`` (v48) and
           ``catalog_candidates.job_id`` (v39): an out-of-band identifier
           that is never joined against, only carried for attribution and
           per-Agent clearing.

        3. No incoming foreign key on this table at all (it stays a leaf
           table); the one OUTGOING foreign key to ``notebooks`` is kept —
           it is the only mechanism that cascades this table's rows away
           when the notebook itself is deleted, mirroring
           ``agent_notebook_profile``'s own FK to ``notebooks``.

        4. ``client_request_id`` stays NULLABLE, paired with a PARTIAL
           unique index over
           ``(notebook_id, owner_id, agent_profile_id, client_request_id)
           WHERE client_request_id IS NOT NULL``. This nullable column +
           partial index IS the shadow park strategy itself (see the park
           argument recorded alongside T1 in the P3 implementation plan):
           a write that carries no client_request_id parks for free by
           simply not participating in the unique surface. The application
           layer must NEVER write NULL here — the MCP write path
           normalizes an empty/missing client_request_id into a rejected
           request the same way ``memory_inputs.normalize_client_request_id``
           already does for Memory proposals — so in practice every row this
           service ever writes carries a real value; NULL is a shape this
           column supports for the shadow migration's benefit only. Do NOT
           change this column to ``NOT NULL DEFAULT ''``: that would push
           every "no request id supplied" row onto the SENTINEL_TEXT park
           path (there is no such row today, but the column's nullability
           is the contract, not an accident of the current caller). Do NOT
           turn the index non-partial either — a non-partial unique index
           would make the NULL park strategy inapplicable to this surface.

        5. ``idx_agent_observations_scope`` covers ``(notebook_id, owner_id,
           created_at, id)`` — a NON-unique index, so it adds nothing to the
           forward-shadow unique surface. T2's quality review measured the
           two hot read paths on a 100k-row table WITHOUT this index —
           ``append_observation``'s eviction DELETE at ~9.5ms and
           ``recent_observations``/``list_observations`` at ~3.2ms, both
           table-scanning past every OTHER ``(notebook_id, owner_id)``
           group's rows to find the one being read — and WITH it, ~1.1ms and
           ~0.07ms respectively. Superseded T1 registered "no index" as a
           deferred cost call awaiting measurement rather than a proof; this
           is that measurement. ``id`` closes the same ordering the eviction
           DELETE and both reads already use (see the port's ``ORDER BY``
           contract) — a covering index without it would still leave every
           same-``created_at`` tie unindexed for the final tie-break.

        5b. ``id`` is ``NOT NULL`` (SQLite's own PK-implies-NOT-NULL rule
            only fires for ``INTEGER PRIMARY KEY``; a ``TEXT PRIMARY KEY``
            column is nullable unless told otherwise). Without this, a
            single NULL-id row would poison the ring eviction: SQLite's
            ``NOT IN (SELECT id FROM ... LIMIT N)`` evaluates to NULL — never
            TRUE — for every row being compared once the subquery result
            contains even one NULL, so the DELETE silently deletes nothing
            and the ring grows unbounded for that whole
            ``(notebook_id, owner_id)`` group. The application layer never
            constructs a NULL id (``new_id("obs")`` always returns a real
            string), so this is a defence against a corrupt/foreign write,
            not a documented legitimate shape — unlike ``client_request_id``
            above, which the schema deliberately leaves nullable for the
            shadow park strategy.

        6. Deep notebook copy does NOT carry this table — same as
           ``agent_notebook_profile``/``agent_profile_jobs`` (see
           ``_migration_51``): observations are process state about how an
           Agent has been using THIS notebook, not knowledge a copy should
           inherit. A fresh copy starts with an empty observation log.

        7. ``scripts/merge_dbs.py`` unions this table by NOTEBOOK ownership
           (``NOTEBOOK_SCOPED_TABLES``), not as a deployment-global table —
           unlike ``retrieval_experiences`` (v54), which is intentionally
           notebook-less. Every row here belongs to one notebook via its
           REQUIRED ``notebook_id`` foreign key, so merging two deployments
           re-parents each notebook's observation rows the same way every
           other per-notebook table already does.

        8. ``created_at`` accepts ONLY ISO timestamps — never the empty
           string. It is deliberately NOT in
           ``POSTGRES_EMPTY_TIME_SENTINELS``: the SQLite side would accept
           ``''`` without any symptom, and forward shadow would then hand
           ``''`` to a PG ``timestamptz`` and poison the whole direction
           (the ``notebook_share_requests.decided_at`` lesson). Same
           handling as ``retrieval_experiences``.

        9. ``user_profiles.search_profile_json`` is nullable; NULL means
           "the user has never set a preference and no consolidation job
           has ever written one" — same contract as v45's ``ui_mode``. It
           is NOT backfilled for existing rows, and it does NOT go into
           ``schema_manifest.POSTGRES_JSON_COLUMNS`` on the PostgreSQL
           side even though it holds a JSON document: like ``ui_mode``, it
           is read and written whole (one document per user, no per-key
           query ever touches it), and the existing ``ui_mode`` precedent
           already stores a comparably-shaped small preference value as
           plain text rather than ``jsonb``. Keeping it plain text avoids a
           JSON-parse-on-every-read cost this column's access pattern never
           needs.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_observations (
                  id                TEXT PRIMARY KEY NOT NULL,
                  notebook_id       TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  owner_id          TEXT NOT NULL DEFAULT '',
                  agent_profile_id  TEXT NOT NULL DEFAULT '',
                  text              TEXT NOT NULL DEFAULT '',
                  client_request_id TEXT,
                  created_at        TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_observations_request
                  ON agent_observations(notebook_id, owner_id, agent_profile_id, client_request_id)
                  WHERE client_request_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_agent_observations_scope
                  ON agent_observations(notebook_id, owner_id, created_at, id);
                """
            )
            self.add_column_if_missing(
                db, "user_profiles", "search_profile_json", "TEXT",
            )

    def _migration_56(self) -> None:
        """Give every group one transferable owner without rewriting audit.

        ``groups.created_by`` remains the immutable historical creator.
        ``owner_id`` is the live authority used by transfer/delete/leave
        protection. Existing groups choose a current admin deterministically,
        preferring the creator only when the creator is still an admin. This
        preserves the pre-owner membership state instead of reviving someone
        who already left or was demoted.

        The required plain TEXT value deliberately has no foreign key. Group
        ownership must imply a matching ``group_members`` row; a users FK does
        not express that invariant, while a composite FK would add a duplicate
        unique surface. Stores enforce membership and ownership together under
        the group aggregate lock. This additive migration adds no table, index,
        foreign key, or unique surface.
        """
        with self._connect() as db:
            self.add_column_if_missing(
                db, "groups", "owner_id", "TEXT NOT NULL DEFAULT ''",
            )
            db.execute(
                "UPDATE groups SET owner_id=COALESCE(("
                "SELECT gm.user_id FROM group_members gm "
                "JOIN groups candidate_group ON candidate_group.id=gm.group_id "
                "WHERE gm.group_id=groups.id AND gm.role='admin' "
                "ORDER BY CASE WHEN gm.user_id=candidate_group.created_by THEN 0 ELSE 1 END, "
                "gm.added_at ASC, gm.user_id ASC LIMIT 1), '') "
                "WHERE owner_id=''"
            )
            # Store APIs have always protected the last admin, so this fallback
            # is only for manually corrupted historical rows. It repairs the
            # aggregate rather than leaving a non-empty owner invariant broken.
            db.execute(
                "UPDATE group_members SET role='admin' "
                "WHERE (group_id,user_id) IN ("
                "SELECT id,created_by FROM groups WHERE owner_id='')"
            )
            db.execute(
                "INSERT OR IGNORE INTO group_members "
                "(group_id,user_id,role,added_at,added_by) "
                "SELECT id,created_by,'admin',created_at,created_by "
                "FROM groups WHERE owner_id=''"
            )
            db.execute(
                "UPDATE groups SET owner_id=created_by WHERE owner_id=''"
            )

    def _migration_57(self) -> None:
        """Add one reusable, revocable invitation capability per group.

        The raw token is intentionally stored on ``groups`` just like the
        existing notebook/report/conversation share capabilities: authorized
        admins must be able to reopen the workspace and copy the same live
        link. ``NULL`` means no active link. The partial unique index keeps a
        capability from ever resolving to two groups without making inactive
        rows collide with each other.

        ``invite_created_at`` is either SQL NULL or an ISO timestamp; never an
        empty string, so PostgreSQL's paired ``timestamptz`` remains lossless.
        ``invite_created_by`` is audit metadata, not an authorization pointer,
        and therefore deliberately has no foreign key.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "groups", "invite_token", "TEXT")
            self.add_column_if_missing(db, "groups", "invite_created_at", "TEXT")
            self.add_column_if_missing(db, "groups", "invite_created_by", "TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_groups_invite_token "
                "ON groups(invite_token) WHERE invite_token IS NOT NULL"
            )

    def _migration_58(self) -> None:
        """Persist desired and published notebook indexing-pipeline identity.

        The desired selection is nullable on ``notebooks`` (NULL = builtin).
        Its version and opaque generation make the later whole-notebook
        publication a CAS: a late worker can never publish after a newer
        selection (including an A→B→A sequence) has taken authority.
        Published identity stays on the existing product-state row so a
        deployment plugin can be removed without making old core-schema
        artifacts unreadable.  The whole-notebook chunk publisher changes the
        selection and these two identity columns in its atomic swap.
        """
        with self._connect() as db:
            self.add_column_if_missing(db, "notebooks", "indexing_pipeline", "TEXT")
            self.add_column_if_missing(
                db,
                "notebooks",
                "indexing_pipeline_version",
                "TEXT NOT NULL DEFAULT 'builtin.chunk.v1'",
            )
            self.add_column_if_missing(
                db,
                "notebooks",
                "indexing_pipeline_generation",
                "TEXT NOT NULL DEFAULT ''",
            )
            self.add_column_if_missing(
                db,
                "notebooks",
                "indexing_pipeline_job_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self.add_column_if_missing(
                db,
                "unified_kg_state",
                "indexing_pipeline_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self.add_column_if_missing(
                db,
                "unified_kg_state",
                "indexing_pipeline_version",
                "TEXT NOT NULL DEFAULT 'builtin.chunk.v1'",
            )
            self.add_column_if_missing(
                db,
                "extraction_runs",
                "indexing_pipeline_id",
                "TEXT NOT NULL DEFAULT ''",
            )
            self.add_column_if_missing(
                db,
                "extraction_runs",
                "indexing_pipeline_version",
                "TEXT NOT NULL DEFAULT 'builtin.chunk.v1'",
            )

    def _migration_59(self) -> None:
        """Durable, unpublished notebook indexing generations.

        A stage is owned by the durable rebuild job.  Per-source JSON payloads
        contain only already-computed core-schema rows; the final publisher
        validates the frozen visible-source snapshot and moves every product
        into the live tables in one transaction.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS indexing_pipeline_stages (
                  job_id TEXT NOT NULL PRIMARY KEY
                    REFERENCES kg_build_jobs(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  pipeline_id TEXT NOT NULL DEFAULT '',
                  pipeline_version TEXT NOT NULL,
                  pipeline_generation TEXT NOT NULL,
                  source_snapshot TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_indexing_pipeline_stages_notebook
                  ON indexing_pipeline_stages(notebook_id);

                CREATE TABLE IF NOT EXISTS indexing_pipeline_stage_sources (
                  job_id TEXT NOT NULL
                    REFERENCES indexing_pipeline_stages(job_id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','completed','failed')),
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (job_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_indexing_pipeline_stage_sources_source
                  ON indexing_pipeline_stage_sources(source_id);
                """
            )

    def _migration_60(self) -> None:
        """``agent_observations.kind`` —— 同一张表承载两种「Agent 记录」。

        ``'note'``  = Agent 自己经 ``add_observation`` 写下的短句(v55 起就有的
                      那一种,也是本迁移之前**唯一**可能存在的行,所以默认值就是
                      历史真相,不需要回填)。
        ``'call'``  = 一次经 MCP 落到这个笔记本上的工具调用记账(谁、什么时候、
                      按哪一档能力),由 ``_selected_notebook`` 这个唯一收口写。

        **为什么加列而不是新建一张表。** 两种行的归属键逐字相同——
        ``(notebook_id, owner_id, agent_profile_id)``——生命周期也相同(成员被移出
        共享笔记本时一起清、笔记本删除时一起级联、深拷贝一起不带)。新建一张表要
        为这份完全一样的归属再走一遍固定开销(shadow 业务表清单、replicator 停车
        策略、双后端 sharing_store 的 NOTEBOOK_SCOPED_TABLES、快照校验器、
        merge_dbs 分类、fixtures),换来的只是一个额外的表名。

        **两种行必须互不侵占,这一条由读写两侧同时保证,不靠约定:**

        · 环形淘汰**按 kind 分组**(见 ``agent_observation_store`` 的
          ``append_observation``/``append_call``):调用记账每次调用写一行、频率
          远高于 Agent 手写的短句,共用一个环意味着一次密集检索就能把用户攒了
          很久的短句全部挤掉。
        · 巡固读取(``recent_observations``)在 SQL 里钉死 ``kind='note'``:调用
          记账是系统自己记的账,不是外部 Agent 写给这位成员的话,**绝不**进模型
          输入。它进不了 prompt 这件事因此是查询形态的性质,不是调用方的自觉。

        **刻意不加索引。** 既有的 ``idx_agent_observations_scope``
        ``(notebook_id, owner_id, created_at, id)`` 已经把读写收窄到一个
        ``(笔记本, 成员)`` 分组;而每个分组的行数由两条环形上限封死(见
        ``AGENT_OBSERVATION_RING_MAX`` / ``AGENT_CALL_RING_MAX``),``kind`` 只是
        分组内几百行上的剩余谓词。给一个恒定几百行的分组再加一条以 kind 打头的
        索引,买到的是零,付出的是每次写多维护一棵 B 树——而写正是这次要省的那一侧。
        """
        with self._connect() as db:
            self.add_column_if_missing(
                db, "agent_observations", "kind", "TEXT NOT NULL DEFAULT 'note'",
            )

    def _migration_61(self) -> None:
        """热路径修复批 1:此前缺失的索引,parity 于 PostgreSQL
        ``0039_hotpath_batch1_indexes.sql``(该文件的头注释有每组的完整
        「服务哪条查询」证据与形态取舍,这里不重复)。纯增量索引——不改任何查询
        语句、不改任何服务代码。PostgreSQL 侧六组共八条;SQLite 侧落地五组共七条,
        第 5 组(``chunks(source_id, ordinal)``)在 SQLite 上不适用,理由见下方。

        1. ``idx_clusters_nb_canonical`` ON
           ``concept_clusters(notebook_id, canonical_id)`` —— 概念详情/共提对端
           名/关系端点名三族调用当前按 notebook_id 之后再筛 canonical_id,无法
           用索引收窄 canonical_id 这一段。
        2. ``idx_clusters_nb_canonical_name_lower`` ON
           ``concept_clusters(notebook_id, lower(canonical_name))`` ——
           ``resolve_focal`` 的 ``lower(canonical_name)=?``,SQLite 表达式索引
           原生支持,写法与 Postgres 侧逐字同义。
        3. 三条反向 FK 覆盖:``idx_extraction_runs_notebook``
           ``(extraction_runs.notebook_id)``、
           ``idx_knowledge_source_fact_elements_notebook``
           ``(knowledge_source_fact_elements.notebook_id)``、
           ``idx_memory_items_notebook`` ``(memory_items.notebook_id)`` ——
           三张表都对 ``notebooks(id) ON DELETE CASCADE``,但都没有一条以
           notebook_id 打头的索引(memory_items 现有两条复合索引都以
           created_by 打头),笔记本删除级联对这三张表退化为整表扫描。
        4. ``idx_knowledge_relations_nb_source_target_edge`` ON
           ``knowledge_relations(notebook_id, source_object_id,
           target_object_id, edge_type)`` —— ``in_network_relation_rows``
           (答案装配 ``in_network_relations`` 的落地查询)按两端 id 集合筛选,
           既有的 ``idx_knowledge_relations_nb_source``/``_nb_target`` 只覆盖
           单一端点,hub 端点命中时会被迫逐行过滤另一端。
        5. **不适用**。PostgreSQL 侧 ``chunks(source_id, ordinal)`` 服务
           ``chunk_section_rows``(``chunks_by_section`` 的落地 SQL)的
           ``WHERE source_id=? ... ORDER BY ordinal LIMIT ?``——``ordinal`` 是
           一个真实存储列,普通复合索引能让 planner 用索引序满足这条 ORDER BY
           而不必排序。SQLite 侧同一查询排的是隐式 ``rowid``(SQLite 侧
           ``chunks_by_section`` 的 docstring 已经说明理由:id 是随机 128-bit
           surrogate,排它会打乱一个 section 内的顺序)。SQLite **允许**用带
           双引号的 ``"rowid"`` 把它编进一条二级索引(``PRAGMA index_info``
           会把这一列报成保留位 ``-2``),但经过实测验证(四配置矩阵:
           有/无这条新索引 × 是否强制 planner 选中它),SQLite 的查询规划器
           **不会**用这样的索引消去 ``ORDER BY rowid`` 的排序步骤——建一条
           ``chunks(source_id, "rowid")`` 索引后,若用 ``INDEXED BY`` 强制
           planner 真的选中它,``EXPLAIN QUERY PLAN`` 对
           ``WHERE source_id=? ORDER BY rowid LIMIT ?`` 报
           ``USE TEMP B-TREE FOR ORDER BY``——这一步只在这条 rowid 尾列索引
           被选中时才出现,rowid 伪列尾巴不像真实存储列那样能让优化器确认
           索引序已经满足这条 ORDER BY。而不加 ``INDEXED BY`` 时,规划器压根
           不会挑这条新索引,自然落回既有的 ``idx_chunks_source``——后者本来
           就**免费**给出这份 rowid 序(同一键值下,二级索引条目本就按 rowid
           排列,``EXPLAIN QUERY PLAN`` 对它不报任何排序步骤)。也就是说这条
           新索引不仅换不来任何读侧收益,一旦真被 planner 选中反而比什么都
           不建更差(多一步排序)。对照组(把 ``rowid`` 换成一个同构的真实
           整数列)同一 planner 版本下,建了同构复合索引后确实消去了排序步骤,
           证明差异来自 rowid 伪列本身,不是这条索引的写法问题。这样一条索引
           除了拿现有 ``idx_chunks_source`` 已经提供的 ``source_id`` 等值
           收窄之外不会再带来别的收益,只会让每次写 chunk 多付一棵 B 树的
           维护成本,因此本迁移不建它——这是两个后端在这一点上真实的规划器
           能力差异,不是遗漏。
        6. ``idx_sources_nb_hidden_type`` ON
           ``sources(notebook_id, source_type) WHERE source_type IN
           ('memory','knowhow')``(partial)—— 服务 ``hidden_source_ids`` 自己
           的谓词。互补的多数谓词(``NOT IN``)已经在既有扫描的投影里求值
           (``source_change_signal_rows`` 的注释:「求值放在投影里而不是另开
           一条 id 查询」),给多数谓词建一条全量索引买不到什么,只会让每次
           来源写入多付一棵 B 树的维护成本——partial 谓词精确覆盖收益、不
           覆盖成本。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical
                  ON concept_clusters(notebook_id, canonical_id);

                CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical_name_lower
                  ON concept_clusters(notebook_id, lower(canonical_name));

                CREATE INDEX IF NOT EXISTS idx_extraction_runs_notebook
                  ON extraction_runs(notebook_id);

                CREATE INDEX IF NOT EXISTS idx_knowledge_source_fact_elements_notebook
                  ON knowledge_source_fact_elements(notebook_id);

                CREATE INDEX IF NOT EXISTS idx_memory_items_notebook
                  ON memory_items(notebook_id);

                CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source_target_edge
                  ON knowledge_relations(notebook_id, source_object_id, target_object_id, edge_type);

                CREATE INDEX IF NOT EXISTS idx_sources_nb_hidden_type
                  ON sources(notebook_id, source_type)
                  WHERE source_type IN ('memory', 'knowhow');
                """
            )

    def _migration_62(self) -> None:
        """Index the creator-wide question-overview keyset order.

        ``ask_jobs.created_at`` contains both naive and offset timestamps, plus
        historical empty strings. The expression therefore deliberately matches
        ``sqlite.query_store._absolute_instant`` instead of indexing the raw text;
        otherwise the planner could use an index whose order disagrees with the
        public activity cursor.
        """
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ask_jobs_creator_activity "
                "ON ask_jobs("
                "created_by, "
                "COALESCE(julianday(created_at), "
                "julianday('0001-01-01T00:00:00+00:00')) DESC, "
                "id DESC)"
            )

    def _migration_63(self) -> None:
        """``extension_runtime_toggles`` —— 部署插件运行时开关 + 审计。

        「关」是一道全局闸,不是「卸载」:registry 冻结时仍照 TOML 装载这个
        插件,只是它在贡献点求值(``contribution_availability`` /
        ``capability_availability``)处提前返回 ``DISABLED, "admin_disabled"``。
        **这条闸的每次求值只读进程内快照,绝不现查这张表**——本表是写路径与
        刷新路径唯一的持久层:写路径改完本表立即让快照失效并重建,其余进程
        靠低频轮询把本表读进各自的快照。求值路径因此仍是零 I/O,不会退化成
        每次贡献点判定都打一次数据库(那既是 N+1,也破坏 probe 零 I/O 的既有
        教义)。秒级生效、不需要重启。TOML 的 ``enabled=false`` / 未点名的
        插件仍然是「装不装载」这条更早的闸,本表管不到——两层语义故意分开。

        **无行 = 启用**,这是这张表唯一的存在理由能不动老部署的关键:全新库、
        或从未有管理员碰过这张表的老库,查询结果恒为空集,行为与这张表出现
        之前逐字节相同。``updated_by``/``updated_at`` 是审计字段——谁在什么
        时候关/开过这个插件;写路径在同一写事务内按 actor 现时角色复检 admin,
        逐条镜像 ``identity_store.set_user_role`` 的复检与拒绝方式(非 admin
        → ``PermissionError``,且不写入)。

        ``enabled`` 刻意不加 ``CHECK(enabled IN (0,1))``——仓库里其它 INTEGER
        标志位列同样不加,这里维持现状而非另立新规。代价是 0/1 之外的值能写
        进这一列,后果在 ``sqlite_to_postgres.py`` 的 ``_transform_sqlite_value``
        ``bool`` 分支里现身:那种值会 ``raise SqliteToPostgresMigrationError``
        硬失败,而不是被悄悄折成 ``true``/``false``——这是接受的取舍,不是遗漏。

        行**不**随插件从 TOML 移除而删除:管理员的决定要在配置调整、甚至该
        插件被临时摘掉又装回来之间幸存——同一个 ``plugin_id`` 再次出现时,
        沿用它上一次被设置的开关,而不是悄悄回到默认启用。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS extension_runtime_toggles (
                  plugin_id TEXT NOT NULL PRIMARY KEY,
                  enabled INTEGER NOT NULL,
                  updated_by TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )

    def _migration_64(self) -> None:
        """热路径修复批 3:概念簇 keyset 覆盖索引,parity 于 PostgreSQL
        ``0043_concept_cluster_keyset_index.sql``(该文件的头注释有完整的
        「服务哪条查询」证据,这里不重复)。纯增量索引——不改任何查询语句、
        不改任何服务代码。

        ``idx_clusters_nb_canonical_member`` ON
        ``concept_clusters(notebook_id, canonical_id, member_object_id)``——
        服务 ``concept_cluster_detail_rows``/``concept_cluster_member_total``
        (概念详情 hub 簇 keyset 分页与其配套计数)的
        ``WHERE notebook_id=? AND canonical_id=? ... ORDER BY member_object_id``。
        既有的 ``idx_clusters_nb_canonical``(_migration_61)只覆盖等值前缀,
        不带 member_object_id 顺序信息——hub 概念的后续分页(第二页起)因此要
        对整簇成员排序一次才能应用 keyset 谓词与 LIMIT。这条三列复合索引把
        排序键并进同一条索引,SQLite 的 planner 能直接按索引序取出结果,无需
        额外排序步骤(与 PostgreSQL 侧同一物理设计;两条迁移是独立手写的
        同形拷贝——一份 SQL 文件无法在 apply 时 import Python)。

        既有的 ``idx_clusters_nb_canonical`` 现在是这条新索引的严格前缀,
        登记为写放大冗余债、本迁移不下线——沿用 ``idx_chunks_source`` 在
        _migration_61 里的先例(该索引后来被 ``idx_chunks_source_ordinal``
        完全覆盖,同样只登记不下线)。
        """
        with self._connect() as db:
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_clusters_nb_canonical_member "
                "ON concept_clusters(notebook_id, canonical_id, member_object_id)"
            )

    def _migration_65(self) -> None:
        """Notebook deletion must not erase the bounded user-analysis record.

        This is deliberately a projection, not a second notebook aggregate:
        it has no foreign key to notebooks and stores only fields needed by the
        user-usage totals and Activity UI. Answers, citations, source content,
        report sections and reasoning traces remain owned by the notebook and
        are still deleted immediately. ``expires_at`` is stamped by the delete
        path from ``Settings.user_activity_retention_days``; no retention
        literal lives in this schema.

        The two activity indexes mirror ``query_store._absolute_instant`` so
        creator-wide and owner-wide keyset reads stay bounded despite SQLite's
        historical mix of naive and offset timestamps. The expiry index backs
        startup/delete-path physical GC; the notebook index bounds replacement
        of a merge-era archive while the notebook delete transaction holds its
        write lock.
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS retained_user_activity (
                  activity_type TEXT NOT NULL,
                  record_id TEXT NOT NULL,
                  actor_id TEXT NOT NULL DEFAULT '',
                  notebook_id TEXT NOT NULL DEFAULT '',
                  notebook_owner_id TEXT NOT NULL DEFAULT '',
                  notebook_name TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT '',
                  asked_at TEXT NOT NULL DEFAULT '',
                  conversation_id TEXT NOT NULL DEFAULT '',
                  question TEXT NOT NULL DEFAULT '',
                  mode TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT '',
                  display_title TEXT NOT NULL DEFAULT '',
                  file_name TEXT NOT NULL DEFAULT '',
                  source_type TEXT NOT NULL DEFAULT '',
                  parse_status TEXT NOT NULL DEFAULT '',
                  parse_failed INTEGER NOT NULL DEFAULT 0,
                  depth INTEGER NOT NULL DEFAULT 0,
                  generation_started_at TEXT NOT NULL DEFAULT '',
                  deleted_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  PRIMARY KEY (activity_type, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_retained_activity_actor_type_created
                  ON retained_user_activity(
                    actor_id, activity_type,
                    COALESCE(julianday(created_at),
                      julianday('0001-01-01T00:00:00+00:00')) DESC,
                    record_id DESC
                  );
                CREATE INDEX IF NOT EXISTS idx_retained_activity_owner_created
                  ON retained_user_activity(
                    notebook_owner_id,
                    COALESCE(julianday(created_at),
                      julianday('0001-01-01T00:00:00+00:00')) DESC,
                    record_id DESC
                  );
                CREATE INDEX IF NOT EXISTS idx_retained_activity_expires
                  ON retained_user_activity(julianday(expires_at));
                CREATE INDEX IF NOT EXISTS idx_retained_activity_notebook
                  ON retained_user_activity(notebook_id);
                """
            )

    def _recover_interrupted_jobs(self) -> None:
        """服务端启动的崩溃兜底（与版本化 schema 迁移解耦，无条件运行）：后端单进程，
        merge-review / ask 等 daemon 线程任务无法跨进程重启存活，故启动时仍是 'running'
        的行定义上就是上次崩溃/重启遗留的陈旧行，否则会永久卡死该 notebook 的单飞守卫。
        knowhow 行投影同理：background_jobs 不跨进程存活，重启后仍处 'syncing'/'pending'
        的行定义上是被遗弃的（没有任何代码路径会再回访它们），置 'failed' 以暴露前端的
        「重投影」重试入口，而非让它们看起来永久「同步中/待处理」。
        解析中的源同理且更严重：'queued'/'parsing' 的源没有任何进程会再回访，留着就是
        永久「排队中/解析中」的搁浅行——用户既看不到失败也无从重试。置 'failed' 让它落到
        一个可重试的终态。注意**不能**像 'extracting' 那样回退成 'parsed'：搁浅源可能连
        source_elements 都没有，标「已解析」是谎报，还会让它从「待处理」视图里消失。
        落到 'failed' 还让「重新上传同一个文件」能按失败源走重试
        （见 upload_sources 的 reuse_uploaded_source）。
        只由服务端 lifespan 启动路径调用一次（见 initialize 的说明）。
        幂等——safe every boot。

        逐语句独立事务，双写后端同一条依据（见 PostgreSQL 孪生
        ``PostgresMaintenanceAdapter.recover_interrupted_jobs`` 的完整说明）：
        任一条结算失败只记日志并跳过，不再让它的异常回滚其余语句已经做完的
        结算。方言差异：PostgreSQL 侧把两张 KG 聚类 scratch 表从无谓词 `DELETE`
        换成了 `TRUNCATE`（PostgreSQL 的 `DELETE` 是逐元组 MVCC 标记，9M 级
        残留行足以让单条语句超过启动时间预算）。SQLite 没有 `TRUNCATE` 语句，
        也不需要等价物：无 `WHERE` 子句的 `DELETE FROM` 本就会走 SQLite 自带的
        "truncate optimization"（按页释放，不逐行访问/不触发行级判定），因此
        这里的 `DELETE FROM` 保持不变。"""
        now = _now()
        failed_steps: list[str] = []

        def _settle(label: str, action: Callable[[], None]) -> None:
            try:
                action()
            except Exception:  # noqa: BLE001 — 逐语句尽力,任一条失败绝不阻断其余语句
                logger.exception(
                    "_recover_interrupted_jobs: 结算 %s 失败,已跳过继续其余语句", label
                )
                failed_steps.append(label)

        def _write(sql: str, params: tuple = ()) -> None:
            with self._connect() as db:
                db.execute(sql, params)

        _settle("retained_user_activity", lambda: _write(
            "DELETE FROM retained_user_activity "
            "WHERE julianday(expires_at) <= julianday(?)",
            (now,),
        ))
        _settle("merge_review_jobs", lambda: _write(
            "UPDATE merge_review_jobs SET status='failed', "
            "error='中断:服务重启' WHERE status='running'"))
        _settle("ask_jobs", lambda: _write(
            "UPDATE ask_jobs SET status='interrupted', "
            "error='中断:服务重启' WHERE status='running'"))
        _settle("knowhow_rows", lambda: _write(
            "UPDATE knowhow_rows SET projection_status='failed' "
            "WHERE projection_status IN ('syncing','pending')"))
        _settle("sources(extracting)", lambda: _write(
            "UPDATE sources SET status='parsed', parse_status='parsed', "
            "error_message='', updated_at=? "
            "WHERE parse_status='extracting'",
            (now,),
        ))
        _settle("sources(queued/parsing)", lambda: _write(
            "UPDATE sources SET status='failed', parse_status='failed', "
            "error_message='服务重启导致文档解析中断；文件已保留，可重新解析或重新上传。', "
            "updated_at=? WHERE parse_status IN ('queued','parsing')",
            (now,),
        ))
        _settle("extraction_runs", lambda: _write(
            "UPDATE extraction_runs SET status='failed', "
            "error_message='worker_interrupted: 服务重启导致知识图谱分析中断', "
            "updated_at=? WHERE run_type='kg' AND status='running'",
            (now,),
        ))
        _settle("kg_build_jobs", lambda: _write(
            "UPDATE kg_build_jobs SET status='failed', stage='finished', "
            "error_code='worker_interrupted', "
            "error_message='服务重启导致本次分析中断；"
            "已完成内容已保留，可继续分析未完成内容。', "
            "updated_at=?, finished_at=? WHERE status='running'",
            (now, now),
        ))
        # Staged products are unpublished scratch owned by those running
        # jobs.  No worker survives process restart, so retaining them can
        # only waste space or tempt a later generation to consume stale
        # payload.  The live chunk/KG tables were never touched.
        _settle("indexing_pipeline_stages", lambda: _write(
            "DELETE FROM indexing_pipeline_stages"
        ))
        # 命令目录抽取同理,且**必须连 'queued' 一起收**:单飞守卫的谓词是
        # `status IN ('queued','running')`,只扫 running 会让一行崩在「已建行、
        # 线程未起」窗口里的 queued 永久占住该来源的守卫,用户既看不到进度也
        # 无法重新发起,而离线 CLI 无权自清。
        # 措辞与 app/services/catalog_job.py 的 INTERRUPTED_MESSAGE 同步维护:
        # 用户可见的动作叫「识别」,不是内部叫法「抽取」——这条 SQL 字面量绕开了
        # user_error()/前端词汇门,原样上屏,必须自己保证界面词正确。R6 P1:
        # 去掉「可重新发起识别」的无条件承诺——保留不等于够得着,`.../job` 只
        # 返回最近一次任务,新任务一发起旧候选就永久孤儿化(见 catalog_job.py
        # 的 `_reject_if_pending_candidates`,它现在也拦截这个终态)。
        _settle("catalog_jobs", lambda: _write(
            "UPDATE catalog_jobs SET status='failed', "
            "failure_reason='服务重启导致命令目录识别中断；已生成的候选已保留，"
            "请先在审阅面板确认或跳过，再重新发起识别。', "
            "diagnostic='worker_interrupted', "
            "updated_at=?, finished_at=? WHERE status IN ('queued','running')",
            (now, now),
        ))
        # 重聚类的两张 scratch 表是**纯瞬态**的:只有一次进行中的 rebuild 会读
        # 自己那个 run_id 的行,进程重启后没有任何代码路径会再回访它们。
        # rebuild 的 finally 只能兜住异常/取消,兜不住 SIGKILL / 掉电——那种情况
        # 下 run_id 随进程一起消失,行就永久不可达(每张表都没有时间戳列,事后
        # 也无从按年龄清理),几次被打断的大库 rebuild 就能把库撑起来。
        # 与本函数其余各行同一条依据:后端单进程,启动这一刻不可能有 rebuild 在飞,
        # 所以此时表里的任何行按定义都是上次崩溃的遗留。
        _settle("kg_cluster_scratch", lambda: _write("DELETE FROM kg_cluster_scratch"))
        _settle(
            "kg_canonical_scratch", lambda: _write("DELETE FROM kg_canonical_scratch")
        )

        if failed_steps:
            logger.error(
                "_recover_interrupted_jobs: %d 条结算语句失败并被跳过: %s",
                len(failed_steps), ", ".join(failed_steps),
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
            from app.domain.auth_utils import hash_password
            pw_hash, pw_salt, pw_iters = hash_password(self.settings.admin_password)
            db.execute(
                "UPDATE users SET role='admin', username='admin', "
                "password_hash=?, password_salt=?, password_iterations=?, updated_at=? "
                "WHERE id='user-local'",
                (pw_hash, pw_salt, pw_iters, now),
            )
            from app.domain.kg.names import normalize_name as _wl_norm
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
            # v25 stamps itself inside the same BEGIN IMMEDIATE transaction as
            # its irreversible credential/status scrub. Preserve the existing
            # migration/stamp behavior byte-for-byte for v1-v24.
            if version != 25:
                with self._connect() as db:
                    db.execute(f"PRAGMA user_version = {version}")
            applied.append(version)
        return applied

    def recover_interrupted_jobs(self) -> None:
        self._recover_interrupted_jobs()

    def seed(self) -> None:
        self._seed()

    def initialize(self) -> list[int]:
        """构造仓储时跑的那部分：migrate + seed，**不含**崩溃兜底。

        恢复（``recover_interrupted_jobs``）已移交服务端 lifespan 启动路径显式调用
        （``app/services/startup_warmup.py::run_startup``，在 ``mark_ready()`` 之前）。
        理由：``SQLiteRepository.__init__`` 会无条件跑到这里，而离线 CLI／脚本每次
        运行都会直接构造一个仓储——它们不是这个库的主人，没有资格把「进行中」的行
        判成上次崩溃的残骸。旧行为下，跑一次离线脚本就会把服务端正在处理的 pending
        行刷成 failed。"""
        applied = self.migrate()
        self.seed()
        return applied
