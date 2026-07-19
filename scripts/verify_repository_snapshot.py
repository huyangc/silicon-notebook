#!/usr/bin/env python3
"""Backup-only verification that the current repository opens a pre-refactor
SQLite database without changing it.

~~~text
python scripts/verify_repository_snapshot.py \
  --database PATH \
  --storage-dir PATH
~~~

Safety contract (Task 28):

- The original database is opened only as a SQLite URI ``mode=ro``, only long
  enough to call ``Connection.backup()`` into a temporary file.  The
  repository is NEVER constructed with the original database path.
- The original storage directory is never handed to the repository either; the
  repository receives an empty temporary storage directory.  The original
  storage is only ``stat``-ed (file list / size / mtime) to prove it is
  untouched.
- Settings are built from explicit field values (never ambient ``.env`` /
  environment defaults) and every model/embedding/rerank/MinerU provider must
  be unconfigured/off; a socket guard rejects any network call while the
  repository is alive.
- Schema migration and startup seed manifests enumerate every accepted
  object, primary key and stable value; no object or row is trusted by a
  ``builtin`` label alone.  Recovery and admin normalization remain limited
  to their documented fields, and every other row digest must match.
- Original database/WAL metadata is guarded.  A read-only attachment to a
  live WAL may change rebuildable ``-shm`` mtime; SHM existence/size and
  database/WAL size/mtime remain exact.
- stdout carries object names, counts and digests only — never row content.

Success line::

    repository-snapshot: PASS schema=v<source_user_version> changed_tables=0
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.services.sqlite_repository import (
    SCHEMA_VERSION,
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)

RESTART_ERROR = "中断:服务重启"

# Tables whose rows the startup sequence may legitimately touch; every one is
# compared row-by-row against the documented allowance instead of digest-only.
SPECIAL_TABLES = (
    "users",
    "user_profiles",
    "concept_whitelist",
    "object_schemas",
    "merge_review_jobs",
    "ask_jobs",
)

# The admin in-place upgrade (`_seed`) rewrites exactly these user-local
# columns on every startup.
ADMIN_UPGRADE_COLUMNS = frozenset(
    {
        "username",
        "role",
        "password_hash",
        "password_salt",
        "password_iterations",
        "updated_at",
    }
)

EXPECTED_KG_REBUILD_CHECKPOINT_SQL = """CREATE TABLE kg_rebuild_checkpoint (
                    notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    input_version TEXT NOT NULL,
                    stage         TEXT NOT NULL,
                    item_key      TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, input_version, stage, item_key)
                )"""

EXPECTED_MEMORY_TABLES = {
    "agent_profiles": """CREATE TABLE agent_profiles (
                  id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','revoked')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
    "agent_access_tokens": """CREATE TABLE agent_access_tokens (
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
                )""",
    "agent_token_notebooks": """CREATE TABLE agent_token_notebooks (
                  token_id TEXT NOT NULL
                    REFERENCES agent_access_tokens(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL
                    REFERENCES notebooks(id) ON DELETE CASCADE,
                  PRIMARY KEY (token_id, notebook_id)
                )""",
    "memory_items": """CREATE TABLE memory_items (
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
                )""",
    "memory_revisions": """CREATE TABLE memory_revisions (
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
                )""",
    "memory_provenance": """CREATE TABLE memory_provenance (
                  id TEXT PRIMARY KEY,
                  memory_id TEXT NOT NULL UNIQUE
                    REFERENCES memory_items(id) ON DELETE CASCADE,
                  origin TEXT NOT NULL
                    CHECK(origin IN ('ask_answer','external_agent')),
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                )""",
    "memory_embeddings": """CREATE TABLE memory_embeddings (
                  memory_id TEXT PRIMARY KEY
                    REFERENCES memory_items(id) ON DELETE CASCADE,
                  model TEXT NOT NULL,
                  dimension INTEGER NOT NULL CHECK(dimension > 0),
                  vector BLOB NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
    "memory_items_fts": """CREATE VIRTUAL TABLE memory_items_fts USING fts5(
                  title,
                  content_md,
                  tags_json,
                  content='memory_items',
                  content_rowid='rowid',
                  tokenize='trigram'
                )""",
    "memory_items_fts_data": "CREATE TABLE 'memory_items_fts_data'(id INTEGER PRIMARY KEY, block BLOB)",
    "memory_items_fts_idx": "CREATE TABLE 'memory_items_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID",
    "memory_items_fts_docsize": "CREATE TABLE 'memory_items_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)",
    "memory_items_fts_config": "CREATE TABLE 'memory_items_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID",
}

EXPECTED_MEMORY_INDEXES = {
    "idx_agent_profiles_owner_status": """CREATE INDEX idx_agent_profiles_owner_status
                  ON agent_profiles(owner_id, status, updated_at DESC)""",
    "idx_agent_tokens_profile": """CREATE INDEX idx_agent_tokens_profile
                  ON agent_access_tokens(agent_profile_id, revoked_at, expires_at)""",
    "idx_agent_token_notebooks_notebook": """CREATE INDEX idx_agent_token_notebooks_notebook
                  ON agent_token_notebooks(notebook_id, token_id)""",
    "idx_memory_answer_once": """CREATE UNIQUE INDEX idx_memory_answer_once
                  ON memory_items(created_by, source_answer_id)
                  WHERE source_answer_id IS NOT NULL""",
    "idx_memory_owner_notebook_status": """CREATE INDEX idx_memory_owner_notebook_status
                  ON memory_items(created_by, notebook_id, status, updated_at DESC)""",
    "idx_memory_agent_candidate": """CREATE INDEX idx_memory_agent_candidate
                  ON memory_items(created_by, notebook_id, status, agent_profile_id)""",
    "idx_memory_revisions_memory": """CREATE INDEX idx_memory_revisions_memory
                  ON memory_revisions(memory_id, revision DESC)""",
    "idx_memory_embeddings_model": """CREATE INDEX idx_memory_embeddings_model
                  ON memory_embeddings(model, dimension)""",
}

EXPECTED_MEMORY_TRIGGERS = {
    "memory_items_fts_insert": """CREATE TRIGGER memory_items_fts_insert
                AFTER INSERT ON memory_items BEGIN
                  INSERT INTO memory_items_fts(rowid, title, content_md, tags_json)
                  VALUES (new.rowid, new.title, new.content_md, new.tags_json);
                END""",
    "memory_items_fts_delete": """CREATE TRIGGER memory_items_fts_delete
                AFTER DELETE ON memory_items BEGIN
                  INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content_md, tags_json)
                  VALUES ('delete', old.rowid, old.title, old.content_md, old.tags_json);
                END""",
    "memory_items_fts_update": """CREATE TRIGGER memory_items_fts_update
                AFTER UPDATE OF title, content_md, tags_json ON memory_items BEGIN
                  INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content_md, tags_json)
                  VALUES ('delete', old.rowid, old.title, old.content_md, old.tags_json);
                  INSERT INTO memory_items_fts(rowid, title, content_md, tags_json)
                  VALUES (new.rowid, new.title, new.content_md, new.tags_json);
                END""",
}

# FTS5 initializes these shadow tables with index metadata even when the
# external-content table has no rows. Their exact definitions are still
# guarded by EXPECTED_MEMORY_TABLES; non-empty state here is structural, not user
# data introduced by the migration.
MEMORY_FTS_SHADOW_TABLES = frozenset(
    {
        "memory_items_fts_config",
        "memory_items_fts_data",
        "memory_items_fts_docsize",
        "memory_items_fts_idx",
    }
)

# Every schema object accepted for one version hop is named here with its exact
# sqlite_master SQL.  An absent hop means that this verifier does not know how
# to prove that migration and must fail closed.
MASTER_SCALE_INDEXES = {
    "idx_element_embeddings_source":
        "CREATE INDEX idx_element_embeddings_source ON element_embeddings(source_id)",
    "idx_knowledge_relations_nb_review":
        "CREATE INDEX idx_knowledge_relations_nb_review ON knowledge_relations(notebook_id, review_status)",
    "idx_comentions_nb_b":
        "CREATE INDEX idx_comentions_nb_b ON concept_comentions(notebook_id, canonical_b)",
    "idx_sources_nb_parse_status":
        "CREATE INDEX idx_sources_nb_parse_status ON sources(notebook_id, parse_status)",
}

# v14 (Task 1, memory-kg-extract): sources.memory_id link column + its partial
# unique index (at most one Memory-derived synthetic source per Memory).
SOURCES_MEMORY_ID_COLUMN = {
    "memory_id": ("memory_id", "TEXT", 0, None, 0),
}
SOURCES_MEMORY_ID_INDEX = {
    "idx_sources_memory_id":
        "CREATE UNIQUE INDEX idx_sources_memory_id "
        "ON sources(memory_id) WHERE memory_id IS NOT NULL AND memory_id != ''",
}

# v15 (Task 5, memory-kg-extract): covering index for the memory-filtered
# user-facing source counts (analytics parse_status GROUP BY +
# NotebookSummary.visible_source_count, both `AND source_type != 'memory'`).
# _migration_15 adds only this index — no new table/column/trigger. Any DB at
# a version below 15 gains it on the way to current, so it belongs in every
# hop's index allowlist (harmless where a constructed source already carries
# it: it simply won't appear in the after-before added set).
SOURCES_PARSE_STATUS_TYPE_INDEX = {
    "idx_sources_nb_parse_status_type":
        "CREATE INDEX idx_sources_nb_parse_status_type "
        "ON sources(notebook_id, parse_status, source_type)",
}

# v16 (knowhow-tables Task 1): five new tables for the editable-grid truth
# source (knowhow_tables/knowhow_columns/knowhow_rows/knowhow_cells) plus
# notebook_assets (row-embedded image upload metadata), each with its own
# FK-index. _migration_16 adds only these tables/indexes — no new column,
# trigger, or view, and no existing table is altered. Any DB below 16 gains
# them on the way to current, so they belong in every hop's tables/indexes
# allowlist (harmless where a constructed source already carries them: they
# simply won't appear in the after-before added set).
#
# NOTE (v19, source-asset-linking Task 2): notebook_assets' literal below
# already carries the `, source_id TEXT)` tail that _migration_19 appends via
# ALTER TABLE ADD COLUMN. Every hop that creates notebook_assets fresh (any
# pre-version below 16) migrates straight through to current SCHEMA_VERSION
# in one migrate() pass, so only the POST-_migration_19 shape is ever
# observed for those hops — see NOTEBOOK_ASSETS_SOURCE_ID_COLUMN below for the
# hops (16+) where the table already exists and the column is a genuine
# before/after ALTER instead.
KNOWHOW_TABLES = {
    "knowhow_cells": """CREATE TABLE knowhow_cells (
                  id TEXT PRIMARY KEY,
                  row_id TEXT NOT NULL REFERENCES knowhow_rows(id) ON DELETE CASCADE,
                  column_id TEXT NOT NULL REFERENCES knowhow_columns(id) ON DELETE CASCADE,
                  content_md TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL,
                  UNIQUE(row_id, column_id)
                )""",
    "knowhow_columns": """CREATE TABLE knowhow_columns (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  name TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'plain',
                  position INTEGER NOT NULL
                )""",
    "knowhow_rows": """CREATE TABLE knowhow_rows (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  projection_status TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
    "knowhow_tables": """CREATE TABLE knowhow_tables (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  mutation_seq INTEGER NOT NULL DEFAULT 0,
                  hidden_source_id TEXT,
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
    "notebook_assets": """CREATE TABLE notebook_assets (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  filename TEXT NOT NULL,
                  mime TEXT NOT NULL,
                  size INTEGER NOT NULL,
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                , source_id TEXT)""",
}
KNOWHOW_INDEXES = {
    "idx_knowhow_cells_row":
        "CREATE INDEX idx_knowhow_cells_row ON knowhow_cells(row_id)",
    "idx_knowhow_columns_table":
        "CREATE INDEX idx_knowhow_columns_table ON knowhow_columns(table_id)",
    "idx_knowhow_rows_table":
        "CREATE INDEX idx_knowhow_rows_table ON knowhow_rows(table_id)",
    "idx_knowhow_tables_nb":
        "CREATE INDEX idx_knowhow_tables_nb ON knowhow_tables(notebook_id)",
    "idx_notebook_assets_nb":
        "CREATE INDEX idx_notebook_assets_nb ON notebook_assets(notebook_id)",
}

# v17 (paper-metadata Task 1): two new tables — source_paper_meta (1:1 with
# sources; row existence means "already attempted", is_paper=0 marks a
# judged-not-a-paper source so the same source is never re-sent to the LLM)
# and source_authors (1:N; position = byline order) — each with its own
# FK-index. _migration_17 adds only these tables/indexes — no new column,
# trigger, or view, and no existing table is altered. Any DB below 17 gains
# them on the way to current, so they belong in every hop's tables/indexes
# allowlist (harmless where a constructed source already carries them: they
# simply won't appear in the after-before added set).
PAPER_META_TABLES = {
    "source_authors": """CREATE TABLE source_authors (
                  id TEXT PRIMARY KEY,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  affiliation TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                )""",
    "source_paper_meta": """CREATE TABLE source_paper_meta (
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
                )""",
}
PAPER_META_INDEXES = {
    "idx_source_authors_nb":
        """CREATE INDEX idx_source_authors_nb
                  ON source_authors(notebook_id)""",
    "idx_source_authors_source":
        """CREATE INDEX idx_source_authors_source
                  ON source_authors(source_id)""",
    "idx_source_paper_meta_nb":
        """CREATE INDEX idx_source_paper_meta_nb
                  ON source_paper_meta(notebook_id)""",
}

# v18 (knowhow-tables PR-2+3 Task 1): one new table — per-cell code
# attachments (knowhow_cell_code, UNIQUE(row_id, column_id)) with its FK
# index. _migration_18 additionally rewrites knowhow_columns.role VALUES
# (five legacy roles -> four behavior kinds), which is pure row data — no
# schema object changes beyond this table/index pair, so the manifest (a
# schema-object diff allowlist) carries only these two entries. Any DB below
# 18 gains them on the way to current, so they belong in every hop's
# tables/indexes allowlist, mirroring the v16 constants above.
KNOWHOW_CELL_CODE_TABLE = {
    "knowhow_cell_code": """CREATE TABLE knowhow_cell_code (
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
                )""",
}
KNOWHOW_CELL_CODE_INDEX = {
    "idx_knowhow_cell_code_row":
        "CREATE INDEX idx_knowhow_cell_code_row ON knowhow_cell_code(row_id)",
}

# v19 (source-asset-linking Task 2): notebook_assets gains a nullable
# source_id column — links a MinerU-extracted embedded image back to its
# originating source (source-view rendering + delete/reparse cascade
# cleanup); knowhow paste-in images leave it NULL — plus its lookup index.
# _migration_19 ALTERs the already-existing table (mirrors the v14
# sources.memory_id column pattern) — no new table/trigger/view. Any DB below
# 19 gains the column + index on the way to current, so
# NOTEBOOK_ASSETS_SOURCE_INDEX belongs in every hop's index allowlist; the
# COLUMN dict only applies to hops whose pre-version is >= 16 (notebook_assets
# already exists there as a real before/after ALTER) — see the NOTE beside
# KNOWHOW_TABLES above for why hops below 16 fold source_id straight into that
# table's CREATE literal instead.
NOTEBOOK_ASSETS_SOURCE_ID_COLUMN = {
    "source_id": ("source_id", "TEXT", 0, None, 0),
}
NOTEBOOK_ASSETS_SOURCE_INDEX = {
    "idx_notebook_assets_source":
        "CREATE INDEX idx_notebook_assets_source ON notebook_assets(source_id)",
}

# v20 (multi-domain-base Task 1): a new reference-library mount table —
# notebook_bases(notebook_id, base_notebook_id, created_at, created_by),
# PRIMARY KEY (notebook_id, base_notebook_id) — plus its lookup index, and a
# genuine before/after ALTER on the already-existing promotion_candidates
# table (target_base_id: the promotion destination base). _migration_20 adds
# exactly these three schema objects — no new trigger/view. Any DB below 20
# gains notebook_bases + its index on the way to current, so
# NOTEBOOK_BASES_TABLE/NOTEBOOK_BASES_INDEX belong in every hop's
# tables/indexes allowlist; promotion_candidates has existed since the v1
# baseline, so PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN belongs in every
# hop's columns allowlist too (mirrors the sources.memory_id / v14 pattern
# above, not the notebook_assets.source_id v16-cutoff nuance — there is no
# cutoff here because promotion_candidates predates every hop's pre-version).
NOTEBOOK_BASES_TABLE = {
    "notebook_bases": """CREATE TABLE notebook_bases (
                  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  created_by TEXT REFERENCES users(id),
                  PRIMARY KEY (notebook_id, base_notebook_id),
                  CHECK (notebook_id != base_notebook_id)
                )""",
}
NOTEBOOK_BASES_INDEX = {
    "idx_notebook_bases_base":
        "CREATE INDEX idx_notebook_bases_base ON notebook_bases(base_notebook_id)",
}
PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN = {
    "target_base_id": ("target_base_id", "TEXT", 1, "''", 0),
}

MIGRATION_MANIFEST = {
    # Cumulative delta from the frozen v9 fixture to merged schema v20.
    (9, 20): {
        "tables": {
            "kg_rebuild_checkpoint": EXPECTED_KG_REBUILD_CHECKPOINT_SQL,
            **EXPECTED_MEMORY_TABLES,
            **KNOWHOW_TABLES,
            **PAPER_META_TABLES,
            **KNOWHOW_CELL_CODE_TABLE,
            **NOTEBOOK_BASES_TABLE,
        },
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**MASTER_SCALE_INDEXES, **EXPECTED_MEMORY_INDEXES,
                    **SOURCES_MEMORY_ID_INDEX, **SOURCES_PARSE_STATUS_TYPE_INDEX,
                    **KNOWHOW_INDEXES, **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": EXPECTED_MEMORY_TRIGGERS,
        "views": {},
    },
    (10, 20): {
        "tables": {**EXPECTED_MEMORY_TABLES, **KNOWHOW_TABLES, **PAPER_META_TABLES,
                   **KNOWHOW_CELL_CODE_TABLE, **NOTEBOOK_BASES_TABLE},
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**MASTER_SCALE_INDEXES, **EXPECTED_MEMORY_INDEXES,
                    **SOURCES_MEMORY_ID_INDEX, **SOURCES_PARSE_STATUS_TYPE_INDEX,
                    **KNOWHOW_INDEXES, **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": EXPECTED_MEMORY_TRIGGERS,
        "views": {},
    },
    # Both branches independently used v11 before merge. Select the exact
    # lineage below from the source schema, then admit only the missing objects.
    (11, 20, "memory"): {
        "tables": {**KNOWHOW_TABLES, **PAPER_META_TABLES, **KNOWHOW_CELL_CODE_TABLE,
                   **NOTEBOOK_BASES_TABLE},
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**MASTER_SCALE_INDEXES, **SOURCES_MEMORY_ID_INDEX,
                    **SOURCES_PARSE_STATUS_TYPE_INDEX, **KNOWHOW_INDEXES,
                    **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    (11, 20, "master"): {
        "tables": {**EXPECTED_MEMORY_TABLES, **KNOWHOW_TABLES, **PAPER_META_TABLES,
                   **KNOWHOW_CELL_CODE_TABLE, **NOTEBOOK_BASES_TABLE},
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {
            "idx_sources_nb_parse_status": MASTER_SCALE_INDEXES["idx_sources_nb_parse_status"],
            **EXPECTED_MEMORY_INDEXES,
            **SOURCES_MEMORY_ID_INDEX,
            **SOURCES_PARSE_STATUS_TYPE_INDEX,
            **KNOWHOW_INDEXES,
            **PAPER_META_INDEXES,
            **KNOWHOW_CELL_CODE_INDEX,
            **NOTEBOOK_ASSETS_SOURCE_INDEX,
            **NOTEBOOK_BASES_INDEX,
        },
        "triggers": EXPECTED_MEMORY_TRIGGERS,
        "views": {},
    },
    (12, 20): {
        "tables": {**EXPECTED_MEMORY_TABLES, **KNOWHOW_TABLES, **PAPER_META_TABLES,
                   **KNOWHOW_CELL_CODE_TABLE, **NOTEBOOK_BASES_TABLE},
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**EXPECTED_MEMORY_INDEXES, **SOURCES_MEMORY_ID_INDEX,
                    **SOURCES_PARSE_STATUS_TYPE_INDEX, **KNOWHOW_INDEXES,
                    **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": EXPECTED_MEMORY_TRIGGERS,
        "views": {},
    },
    # v13 -> current: v13 was the shipping schema before Task 1
    # (memory-kg-extract). _migration_14 adds the sources.memory_id column + its
    # partial unique index; _migration_15 adds the parse_status/source_type
    # covering index; _migration_16 adds the five knowhow/notebook_assets
    # tables + their indexes; _migration_17 adds the two paper-metadata
    # tables + their indexes; _migration_18 adds the knowhow_cell_code table +
    # its index; _migration_19 adds notebook_assets.source_id + its index;
    # _migration_20 adds notebook_bases + its index and
    # promotion_candidates.target_base_id — no new triggers on any hop.
    (13, 20): {
        "tables": {**KNOWHOW_TABLES, **PAPER_META_TABLES, **KNOWHOW_CELL_CODE_TABLE,
                   **NOTEBOOK_BASES_TABLE},
        "columns": {"sources": SOURCES_MEMORY_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**SOURCES_MEMORY_ID_INDEX, **SOURCES_PARSE_STATUS_TYPE_INDEX,
                    **KNOWHOW_INDEXES, **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v14 -> current hop (Task 5 + knowhow-tables PR-1 Task 1 +
    # paper-metadata Task 1 + cell-code Task 1 + source-asset-linking Task 2 +
    # multi-domain-base Task 1): a database already carrying sources.memory_id
    # gains the parse_status/source_type covering index, the five knowhow/
    # notebook_assets tables, the two paper-metadata tables, the
    # knowhow_cell_code table, notebook_assets.source_id's index,
    # notebook_bases + its index, and promotion_candidates.target_base_id.
    # No new trigger — mirrors Task 1's single-object (13, 14) entry.
    (14, 20): {
        "tables": {**KNOWHOW_TABLES, **PAPER_META_TABLES, **KNOWHOW_CELL_CODE_TABLE,
                   **NOTEBOOK_BASES_TABLE},
        "columns": {"promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**SOURCES_PARSE_STATUS_TYPE_INDEX, **KNOWHOW_INDEXES,
                    **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v15 -> current hop (knowhow-tables PR-1 Task 1 + paper-metadata
    # Task 1 + cell-code Task 1 + source-asset-linking Task 2 +
    # multi-domain-base Task 1): a database already at v15 gains the five
    # knowhow/notebook_assets tables, the two paper-metadata tables, the
    # knowhow_cell_code table, notebook_assets.source_id's index,
    # notebook_bases + its index, and promotion_candidates.target_base_id.
    # No new trigger/view — mirrors Task 5's single-object (14, 15) entry.
    (15, 20): {
        "tables": {**KNOWHOW_TABLES, **PAPER_META_TABLES, **KNOWHOW_CELL_CODE_TABLE,
                   **NOTEBOOK_BASES_TABLE},
        "columns": {"promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**KNOWHOW_INDEXES, **PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v16 -> current hop (paper-metadata Task 1 + cell-code Task 1 +
    # source-asset-linking Task 2 + multi-domain-base Task 1): a database
    # already at v16 (carrying the five knowhow/notebook_assets tables —
    # notebook_assets WITHOUT source_id) gains the two paper-metadata tables,
    # the knowhow_cell_code table, notebook_bases + its index, and — same
    # before/after-ALTER reasoning as notebook_assets.source_id —
    # promotion_candidates.target_base_id.
    (16, 20): {
        "tables": {**PAPER_META_TABLES, **KNOWHOW_CELL_CODE_TABLE, **NOTEBOOK_BASES_TABLE},
        "columns": {"notebook_assets": NOTEBOOK_ASSETS_SOURCE_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**PAPER_META_INDEXES, **KNOWHOW_CELL_CODE_INDEX,
                    **NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v17 -> current hop (knowhow-tables PR-2+3 Task 1 + source-asset-
    # linking Task 2 + multi-domain-base Task 1): a database already at v17
    # gains the cell-code table + its index — _migration_18's role remap
    # rewrites row VALUES, not schema objects, so it has no entry here — plus
    # notebook_assets.source_id, notebook_bases + its index, and
    # promotion_candidates.target_base_id (both genuine before/after ALTERs,
    # same reasoning as the v16 hop above).
    (17, 20): {
        "tables": {**KNOWHOW_CELL_CODE_TABLE, **NOTEBOOK_BASES_TABLE},
        "columns": {"notebook_assets": NOTEBOOK_ASSETS_SOURCE_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**KNOWHOW_CELL_CODE_INDEX, **NOTEBOOK_ASSETS_SOURCE_INDEX,
                    **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v18 -> current hop (source-asset-linking Task 2 + multi-domain-base
    # Task 1): a database already at v18 gains notebook_assets.source_id
    # (before/after ALTER, same reasoning as the v16/v17 hops above) + its
    # lookup index, plus notebook_bases + its index and
    # promotion_candidates.target_base_id — no new trigger/view besides
    # notebook_bases itself.
    (18, 20): {
        "tables": NOTEBOOK_BASES_TABLE,
        "columns": {"notebook_assets": NOTEBOOK_ASSETS_SOURCE_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v19 -> v20 hop (multi-domain-base Task 1): a database already at
    # v19 gains only notebook_bases + its lookup index, plus
    # promotion_candidates.target_base_id (before/after ALTER — the table has
    # existed since the v1 baseline) — no new trigger/view.
    (19, 20): {
        "tables": NOTEBOOK_BASES_TABLE,
        "columns": {"promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": NOTEBOOK_BASES_INDEX,
        "triggers": {},
        "views": {},
    },
}

_BUILTIN_WHITELIST = (
    "ac", "adc", "bicmos", "bjt", "cmos", "cmrr", "dac", "dc", "esd",
    "fet", "ic", "if", "jfet", "lna", "lo", "mos", "mosfet", "nmos",
    "op amp", "pll", "pmos", "psrr", "pvt", "rf", "vco",
)

_BUILTIN_OBJECT_SCHEMAS = {
    "concept": {
        "object_type": "concept",
        "plural": "concepts",
        "fields": '["name", "section_path"]',
        "primary_field": "name",
        "description": "a named entity (term/method/component/device/material)",
        "label": "概念 Concept",
        "list_fields": "[]",
        "source": "builtin",
        "status": "active",
        "rationale": "",
        "notebook_id": "",
    },
    "claim": {
        "object_type": "claim",
        "plural": "claims",
        "fields": '["name", "section_path"]',
        "primary_field": "name",
        "description": "a truth-evaluable assertion about concepts",
        "label": "论断 Claim",
        "list_fields": "[]",
        "source": "builtin",
        "status": "active",
        "rationale": "",
        "notebook_id": "",
    },
    "formula": {
        "object_type": "formula",
        "plural": "formulas",
        "fields": '["name", "section_path"]',
        "primary_field": "name",
        "description": "an equation / expression",
        "label": "公式 Formula",
        "list_fields": "[]",
        "source": "builtin",
        "status": "active",
        "rationale": "",
        "notebook_id": "",
    },
    "procedure": {
        "object_type": "procedure",
        "plural": "procedures",
        "fields": '["name", "section_path"]',
        "primary_field": "name",
        "description": "an ordered process / derivation / worked example",
        "label": "过程 Procedure",
        "list_fields": "[]",
        "source": "builtin",
        "status": "active",
        "rationale": "",
        "notebook_id": "",
    },
}

# Stable values for every startup-insertable row.  Timestamp, password hash,
# and salt columns are explicitly volatile; no other key or value is accepted.
SEED_MANIFEST = {
    "users": {
        "user-local": {
            "id": "user-local",
            "email": "local-user@silicon-notebook.dev",
            "display_name": "Local Curator",
            "role": "admin",
            "status": "active",
            "username": "admin",
            "password_iterations": 200_000,
        },
    },
    "user_profiles": {
        "profile-local": {
            "id": "profile-local",
            "user_id": "user-local",
            "memory_mode": "manual",
            "domain_focus": '["Analog IC", "Packaging", "Reliability"]',
            "model_settings": "{}",
        },
    },
    "concept_whitelist": {
        term: {"term": term, "note": "builtin"} for term in _BUILTIN_WHITELIST
    },
    "object_schemas": _BUILTIN_OBJECT_SCHEMAS,
}

SEED_VOLATILE_COLUMNS = {
    "users": frozenset(
        {"password_hash", "password_salt", "created_at", "updated_at"}
    ),
    "user_profiles": frozenset({"created_at", "updated_at"}),
    "concept_whitelist": frozenset({"created_at"}),
    "object_schemas": frozenset({"created_at", "updated_at"}),
}


def _fail(message: str) -> "SystemExit":
    return SystemExit(f"repository-snapshot: FAIL {message}")


# ---------------------------------------------------------------------------
# Offline settings (explicit field values only — hostile environments must not
# leak through; init kwargs take precedence over env/.env in pydantic-settings)
# ---------------------------------------------------------------------------

def offline_settings(database: Path, storage: Path) -> Settings:
    settings = Settings(
        database_url=f"sqlite:///{database}",
        storage_dir=str(storage),
        openai_compat_base_url="",
        openai_compat_api_key="",
        openai_compat_model="",
        reasoning_llm_base_url="",
        reasoning_llm_api_key="",
        reasoning_llm_model="",
        rewrite_llm_base_url="",
        rewrite_llm_api_key="",
        rewrite_llm_model="",
        kg_llm_base_url="",
        kg_llm_api_key="",
        kg_llm_model="",
        embed_provider="",
        embed_model="",
        embed_base_url="",
        embed_api_key="",
        rerank_model="",
        rerank_base_url="",
        rerank_api_key="",
        mineru_mode="off",
        mineru_api_url="",
        mineru_vlm_server_url="",
        mineru_api_token="",
        scale_index_auto_enabled=False,
        event_log_enabled=False,
        llm_log_enabled=False,
        debug_logs_enabled=False,
        auth_optional=True,
    )
    _assert_offline(settings)
    return settings


def _assert_offline(settings: Settings) -> None:
    offline_checks = {
        "llm_configured": settings.llm_configured,
        "reasoning_llm_configured": settings.reasoning_llm_configured,
        "rewrite_llm_configured": settings.rewrite_llm_configured,
        "kg_llm_configured": settings.kg_llm_configured,
        "embedder_configured": settings.embedder_configured,
        "rerank_model": bool(settings.rerank_model),
        "rerank_base_url": bool(settings.rerank_base_url),
        "mineru_enabled": settings.mineru_enabled,
        "mineru_cloud_enabled": settings.mineru_cloud_enabled,
        "scale_index_auto_enabled": settings.scale_index_auto_enabled,
        "event_log_enabled": settings.event_log_enabled,
        "llm_log_enabled": settings.llm_log_enabled,
        "debug_logs_enabled": settings.debug_logs_enabled,
    }
    leaked = sorted(name for name, value in offline_checks.items() if value)
    if leaked:
        raise _fail(f"provider configuration leaked into verification: {leaked}")


@contextmanager
def _no_network() -> Iterator[None]:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("repository-snapshot verification attempted network access")

    saved_create = socket.create_connection
    saved_connect = socket.socket.connect
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.socket.connect = refuse  # type: ignore[method-assign, assignment]
    try:
        yield
    finally:
        socket.create_connection = saved_create  # type: ignore[assignment]
        socket.socket.connect = saved_connect  # type: ignore[method-assign, assignment]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@dataclass
class TableSnapshot:
    columns: Tuple[Tuple[str, str, int, Any, int], ...]
    column_names: Tuple[str, ...]
    pk_columns: Tuple[str, ...]
    sql: str
    virtual: bool
    row_count: int
    digest: str


@dataclass
class DatabaseSnapshot:
    user_version: int
    tables: Dict[str, TableSnapshot]
    schema_objects: Dict[str, Dict[str, str]]
    special_rows: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]]


def _digest_update(h: "hashlib._Hash", value: Any) -> None:
    if value is None:
        segment = b"N"
    elif isinstance(value, bool):
        segment = b"I" + str(int(value)).encode()
    elif isinstance(value, int):
        segment = b"I" + str(value).encode()
    elif isinstance(value, float):
        segment = b"F" + repr(value).encode()
    elif isinstance(value, bytes):
        segment = b"B" + value
    else:
        segment = b"T" + str(value).encode("utf-8", "surrogatepass")
    h.update(str(len(segment)).encode())
    h.update(b":")
    h.update(segment)


def _comparable(value: Any) -> Any:
    if isinstance(value, bytes):
        return ("blob-sha256", hashlib.sha256(value).hexdigest())
    return value


def _table_digest(
    digest_conn: sqlite3.Connection,
    table: str,
    column_names: Sequence[str],
    pk_columns: Sequence[str],
) -> str:
    quoted = ", ".join(f'"{name}"' for name in column_names)
    h = hashlib.sha256()
    try:
        # rowid tables: rowid order is a stable physical identity and gives a
        # sequential scan; the rowid itself joins the digest (implicit PK).
        cursor = digest_conn.execute(
            f'SELECT rowid, {quoted} FROM "{table}" ORDER BY rowid'
        )
    except sqlite3.OperationalError:
        order = (
            ", ".join(f'"{name}"' for name in pk_columns) if pk_columns else quoted
        )
        cursor = digest_conn.execute(
            f'SELECT {quoted} FROM "{table}" ORDER BY {order}'
        )
    while True:
        rows = cursor.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            for value in row:
                _digest_update(h, value)
    return h.hexdigest()


def _special_table_rows(
    meta_conn: sqlite3.Connection,
    table: str,
    column_names: Sequence[str],
    pk_columns: Sequence[str],
) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    quoted = ", ".join(f'"{name}"' for name in column_names)
    rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    if pk_columns:
        for row in meta_conn.execute(f'SELECT {quoted} FROM "{table}"'):
            data = {name: _comparable(row[index]) for index, name in enumerate(column_names)}
            rows[tuple(data[name] for name in pk_columns)] = data
    else:
        for row in meta_conn.execute(
            f'SELECT rowid, {quoted} FROM "{table}" ORDER BY rowid'
        ):
            data = {
                name: _comparable(row[index + 1])
                for index, name in enumerate(column_names)
            }
            rows[(row[0],)] = data
    return rows


def _sqlite_read_uri(database: Path, *, immutable: bool = False) -> str:
    encoded_path = quote(Path(database).as_posix(), safe="/")
    suffix = "&immutable=1" if immutable else ""
    return f"file:{encoded_path}?mode=ro{suffix}"


def snapshot_database(
    db_path: Path, column_plan: Optional[Dict[str, Tuple[str, ...]]] = None
) -> DatabaseSnapshot:
    uri = _sqlite_read_uri(db_path)
    meta_conn = sqlite3.connect(uri, uri=True)
    digest_conn = sqlite3.connect(uri, uri=True)
    digest_conn.text_factory = bytes  # byte-exact TEXT digesting, no decode risk
    try:
        user_version = int(meta_conn.execute("PRAGMA user_version").fetchone()[0])
        master = meta_conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        schema_objects: Dict[str, Dict[str, str]] = {
            kind: {
                name: sql or ""
                for object_kind, name, sql in master
                if object_kind == kind
            }
            for kind in ("table", "index", "trigger", "view")
        }
        tables: Dict[str, TableSnapshot] = {}
        special_rows: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]] = {}
        for kind, name, sql in master:
            if kind != "table":
                continue
            info = meta_conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            columns = tuple(
                (row[1], row[2], row[3], row[4], row[5]) for row in info
            )
            all_column_names = tuple(row[1] for row in info)
            pk_columns = tuple(
                row[1]
                for row in sorted(info, key=lambda item: item[5])
                if row[5]
            )
            virtual = "VIRTUAL TABLE" in (sql or "").upper()
            row_count = int(
                meta_conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
            planned = (column_plan or {}).get(name, all_column_names)
            digest_columns = tuple(c for c in planned if c in all_column_names)
            digest = (
                ""
                if virtual
                else _table_digest(digest_conn, name, digest_columns, pk_columns)
            )
            tables[name] = TableSnapshot(
                columns=columns,
                column_names=all_column_names,
                pk_columns=pk_columns,
                sql=sql or "",
                virtual=virtual,
                row_count=row_count,
                digest=digest,
            )
            if name in SPECIAL_TABLES:
                special_rows[name] = _special_table_rows(
                    meta_conn, name, digest_columns, pk_columns
                )
        return DatabaseSnapshot(
            user_version=user_version,
            tables=tables,
            schema_objects=schema_objects,
            special_rows=special_rows,
        )
    finally:
        meta_conn.close()
        digest_conn.close()


# ---------------------------------------------------------------------------
# Comparison (documented normalizations only)
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    ok: bool
    source_user_version: int
    final_user_version: int
    table_counts: Dict[str, int]
    table_digests: Dict[str, str]
    changed_tables: List[str]
    discrepancies: List[str]
    migration_added_tables: List[str]
    normalized: Dict[str, int]
    reads: Dict[str, int]
    storage_unchanged: bool
    source_unchanged: bool
    storage_file_count: int
    database: str = ""
    duration_seconds: float = 0.0


def _empty_normalized() -> Dict[str, int]:
    return {
        "merge_review_jobs": 0,
        "ask_jobs": 0,
        "seeded_users": 0,
        "seeded_user_profiles": 0,
        "seeded_concept_whitelist": 0,
        "seeded_object_schemas": 0,
        "admin_upgraded": 0,
    }


def _compare_special_rows(
    table: str,
    pre_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    post_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    normalized: Dict[str, int],
    problems: List[str],
) -> None:
    def rows_equal(pre: Dict[str, Any], post: Dict[str, Any], skip: frozenset) -> bool:
        keys = set(pre) | set(post)
        return all(pre.get(k) == post.get(k) for k in keys if k not in skip)

    def seed_row_matches(post: Dict[str, Any]) -> bool:
        manifest_rows = SEED_MANIFEST.get(table, {})
        row_key = str(next(iter(post.get(name) for name in (
            "id", "term", "object_type"
        ) if post.get(name) is not None), ""))
        expected = manifest_rows.get(row_key)
        if expected is None:
            return False
        volatile = SEED_VOLATILE_COLUMNS.get(table, frozenset())
        if set(post) - set(expected) - set(volatile):
            return False
        return all(post.get(name) == value for name, value in expected.items())

    for key, pre_row in pre_rows.items():
        post_row = post_rows.get(key)
        if post_row is None:
            problems.append(f"table={table} reason=row-deleted")
            continue
        if table == "users" and pre_row.get("id") == "user-local":
            if not rows_equal(pre_row, post_row, ADMIN_UPGRADE_COLUMNS):
                problems.append(f"table={table} reason=admin-row-changed-beyond-upgrade")
            elif any(
                post_row.get(name) != value
                for name, value in SEED_MANIFEST["users"]["user-local"].items()
                if name in ADMIN_UPGRADE_COLUMNS
            ):
                problems.append(f"table={table} reason=admin-upgrade-value-mismatch")
            elif not rows_equal(pre_row, post_row, frozenset()):
                normalized["admin_upgraded"] = 1
            continue
        if table == "merge_review_jobs" and pre_row.get("status") == "running":
            if (
                post_row.get("status") == "failed"
                and post_row.get("error") == RESTART_ERROR
                and rows_equal(pre_row, post_row, frozenset({"status", "error"}))
            ):
                normalized["merge_review_jobs"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if table == "ask_jobs" and pre_row.get("status") == "running":
            if (
                post_row.get("status") == "interrupted"
                and post_row.get("error") == RESTART_ERROR
                and rows_equal(pre_row, post_row, frozenset({"status", "error"}))
            ):
                normalized["ask_jobs"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if not rows_equal(pre_row, post_row, frozenset()):
            problems.append(f"table={table} reason=row-changed")

    for key, post_row in post_rows.items():
        if key in pre_rows:
            continue
        if not seed_row_matches(post_row):
            problems.append(f"table={table} reason=row-inserted")
        elif table == "users":
            normalized["seeded_users"] += 1
        elif table == "user_profiles":
            normalized["seeded_user_profiles"] += 1
        elif table == "concept_whitelist":
            normalized["seeded_concept_whitelist"] += 1
        elif table == "object_schemas":
            normalized["seeded_object_schemas"] += 1
        else:
            problems.append(f"table={table} reason=row-inserted")


def compare_snapshots(
    pre: DatabaseSnapshot, post: DatabaseSnapshot
) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    """Returns (changed_tables, discrepancies, migration_added_tables, normalized)."""
    discrepancies: List[str] = []
    migration_added: List[str] = []
    normalized = _empty_normalized()
    changed: Dict[str, List[str]] = {}

    def note(table: str, reason: str) -> None:
        changed.setdefault(table, []).append(reason)

    if post.user_version != SCHEMA_VERSION:
        discrepancies.append(
            f"user_version={post.user_version} expected={SCHEMA_VERSION}"
        )

    if pre.user_version == post.user_version:
        migration_manifest: Dict[str, Any] = {
            "tables": {}, "columns": {}, "indexes": {}, "triggers": {}, "views": {}
        }
    else:
        manifest_key: tuple[Any, ...] = (pre.user_version, post.user_version)
        if pre.user_version == 11 and post.user_version == SCHEMA_VERSION:
            lineage = (
                "memory"
                if "memory_items" in pre.schema_objects.get("table", {})
                else "master"
            )
            manifest_key = (*manifest_key, lineage)
        migration_manifest = MIGRATION_MANIFEST.get(manifest_key, {})
        if not migration_manifest:
            discrepancies.append(
                "migration-manifest-missing="
                f"v{pre.user_version}-to-v{post.user_version}"
            )

    manifest_key = {
        "table": "tables",
        "index": "indexes",
        "trigger": "triggers",
        "view": "views",
    }
    for kind, key in manifest_key.items():
        before = pre.schema_objects.get(kind, {})
        after = post.schema_objects.get(kind, {})
        expected_added = migration_manifest.get(key, {})
        for name in sorted(set(before) - set(after)):
            if kind == "table":
                note(name, "table-dropped")
            else:
                discrepancies.append(f"schema={kind} name={name} reason=dropped")
        for name in sorted(set(before) & set(after)):
            if before[name] == after[name]:
                continue
            if kind == "table":
                if name in migration_manifest.get("columns", {}):
                    # A manifested bare-column ALTER (e.g. sources.memory_id)
                    # rewrites sqlite_master's stored CREATE TABLE text, so the
                    # raw before/after SQL always differs here. The per-column
                    # loop below re-derives the exact added-column set against
                    # this same manifest entry and fails closed on anything
                    # beyond it, so defer to that stricter check instead of
                    # flagging the expected text delta as unexplained.
                    continue
                note(name, "schema-sql-changed")
            else:
                discrepancies.append(
                    f"schema={kind} name={name} reason=definition-changed"
                )
        for name in sorted(set(after) - set(before)):
            expected_sql = expected_added.get(name)
            if expected_sql is None:
                if kind == "table":
                    note(name, "unmanifested-table-added")
                else:
                    discrepancies.append(
                        f"schema={kind} name={name} reason=unmanifested-addition"
                    )
                continue
            if after[name] != expected_sql:
                if kind == "table":
                    note(name, "migration-table-definition-mismatch")
                else:
                    discrepancies.append(
                        f"schema={kind} name={name} reason=manifest-definition-mismatch"
                    )
                continue
            if kind == "table":
                if post.tables[name].row_count and name not in MEMORY_FTS_SHADOW_TABLES:
                    note(name, "migration-added-table-not-empty")
                else:
                    migration_added.append(name)
        for name in sorted(set(expected_added) - (set(after) - set(before))):
            discrepancies.append(
                f"schema={kind} name={name} reason=manifest-addition-missing"
            )

    for name, pre_table in pre.tables.items():
        post_table = post.tables.get(name)
        if post_table is None:
            continue
        pre_columns = {column[0]: column for column in pre_table.columns}
        post_columns = {column[0]: column for column in post_table.columns}
        if set(pre_columns) - set(post_columns):
            note(name, "column-removed")
            continue
        expected_columns = migration_manifest.get("columns", {}).get(name, {})
        added_columns = set(post_columns) - set(pre_columns)
        if added_columns != set(expected_columns):
            note(name, f"unmanifested-columns={sorted(added_columns)}")
        else:
            for column, definition in expected_columns.items():
                if post_columns[column] != tuple(definition):
                    note(name, f"column-definition-mismatch={column}")
        if any(
            pre_columns[column] != post_columns[column]
            for column in set(pre_columns) & set(post_columns)
        ):
            note(name, "column-definition-changed")
        if pre_table.pk_columns != post_table.pk_columns:
            note(name, "primary-key-changed")
            continue
        if name in SPECIAL_TABLES:
            problems: List[str] = []
            _compare_special_rows(
                name,
                pre.special_rows.get(name, {}),
                post.special_rows.get(name, {}),
                normalized,
                problems,
            )
            if problems:
                for problem in problems:
                    note(name, problem.split("reason=", 1)[-1])
            continue
        if pre_table.virtual or post_table.virtual:
            # FTS virtual tables are covered byte-exactly by their shadow
            # tables; only the row count is compared here.
            if pre_table.row_count != post_table.row_count:
                note(name, "virtual-count-changed")
            continue
        if pre_table.row_count != post_table.row_count:
            note(name, "row-count-changed")
        elif pre_table.digest != post_table.digest:
            note(name, "row-digest-changed")

    changed_tables = sorted(changed)
    for table in changed_tables:
        for reason in changed[table]:
            discrepancies.append(f"table={table} reason={reason}")
    return changed_tables, discrepancies, sorted(migration_added), normalized


# ---------------------------------------------------------------------------
# Representative reads (repository behaves like a live open of the old data)
# ---------------------------------------------------------------------------

def _read_counts() -> Dict[str, int]:
    return {
        "notebooks": 0,
        "sources": 0,
        "knowledge_types": 0,
        "knowledge_rows": 0,
        "unified_status": 0,
        "conversations": 0,
        "answers": 0,
        "ask_jobs": 0,
        "reports": 0,
        "search_hits": 0,
    }


def exercise_reads(repo: Any, backup_path: Path) -> Dict[str, int]:
    counts = _read_counts()
    admin = repo.maintenance.resolve_owner_profile(None)
    if admin is None:
        raise _fail("seeded admin account resolution returned no profile")

    notebook_rows = repo.maintenance.notebook_rows()
    counts["notebooks"] = len(notebook_rows)
    ko_counts = repo.maintenance.kg_object_counts_by_notebook()
    ranked = sorted(
        notebook_rows,
        key=lambda row: (-(ko_counts.get(row["id"], 0)), row["id"]),
    )

    probe = sqlite3.connect(_sqlite_read_uri(backup_path), uri=True)
    try:
        # Top KG notebooks plus notebooks that actually hold Ask jobs/reports,
        # so those read paths are exercised whenever the database has them.
        wanted = {row["id"] for row in ranked[:3]}
        for table in ("ask_jobs", "reports"):
            for row in probe.execute(
                f"SELECT DISTINCT notebook_id FROM {table} LIMIT 2"
            ):
                wanted.add(row[0])
        sample = [row for row in ranked if row["id"] in wanted][:6]
        usernames = {
            row[0]: row[1]
            for row in probe.execute("SELECT id, username FROM users")
        }
        for notebook in sample:
            notebook_id = notebook["id"]
            username = usernames.get(notebook.get("created_by") or "")
            owner = (
                repo.maintenance.resolve_owner_profile(username)
                if username
                else None
            ) or admin
            token = set_request_user(owner)
            try:
                repo.get_notebook(notebook_id)
                sources = repo.list_sources(notebook_id)
                counts["sources"] += len(sources)
                types = repo.knowledge_types(notebook_id)
                counts["knowledge_types"] += len(types)
                if types:
                    page = repo.list_knowledge(
                        notebook_id, types[0].object_type, limit=5
                    )
                    counts["knowledge_rows"] += len(page.items)
                if isinstance(repo.unified_kg_status(notebook_id), dict):
                    counts["unified_status"] += 1
                conversations = repo.list_conversations(notebook_id)
                counts["conversations"] += len(conversations)
                if conversations:
                    detail = repo.get_conversation(conversations[0].id)
                    counts["answers"] += len(detail.turns)
                job_row = probe.execute(
                    "SELECT id FROM ask_jobs WHERE notebook_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (notebook_id,),
                ).fetchone()
                if job_row and repo.ask_job_detail(job_row[0]):
                    counts["ask_jobs"] += 1
                reports = repo.list_reports(notebook_id)
                counts["reports"] += len(reports)
                if reports:
                    repo.get_report(notebook_id, reports[0]["id"])
                title = sources[0].title if sources else ""
                token_text = next(
                    (part for part in re.split(r"\s+", title) if len(part) >= 2),
                    title[:8],
                )
                needle = token_text[:12]
                if needle:
                    counts["search_hits"] += len(
                        repo.search_notebook(notebook_id, needle).hits
                    )
            finally:
                reset_request_user(token)
    finally:
        probe.close()
    return counts


# ---------------------------------------------------------------------------
# Storage / source metadata guards (stat only — file contents are never read)
# ---------------------------------------------------------------------------

def storage_manifest(storage: Path) -> List[Tuple[str, int, int]]:
    return sorted(
        (
            str(path.relative_to(storage)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in storage.rglob("*")
        if path.is_file()
    )


def _source_read_uri(database: Path) -> str:
    """Read-only URI for the original database.

    A WAL-flagged database opened via plain ``mode=ro`` still makes SQLite
    create 0-byte ``-wal``/``-shm`` sidecars next to the ORIGINAL file.  When
    neither sidecar exists there is no live connection and no WAL content to
    read, so ``immutable=1`` gives a byte-pure read with zero filesystem side
    effects.  When sidecars exist (live database or copied WAL content) the
    plain ``mode=ro`` path is required so committed WAL rows are visible; it
    creates no new files because the sidecars are already there.
    """
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    if wal.exists() or shm.exists():
        return _sqlite_read_uri(database)
    return _sqlite_read_uri(database, immutable=True)


def _source_metadata(database: Path) -> List[Tuple[str, int, int]]:
    """size/mtime of the database and its -wal sidecar.  The -shm file is a
    rebuildable lock/index support file whose mtime legitimately moves when a
    read-only reader attaches to a live WAL database, so its exact size and
    existence are tracked while its mtime is normalized."""
    entries: List[Tuple[str, int, int]] = []
    for candidate in (database, Path(f"{database}-wal")):
        if candidate.exists():
            stat = candidate.stat()
            entries.append((candidate.name, stat.st_size, stat.st_mtime_ns))
    shm = Path(f"{database}-shm")
    shm_size = shm.stat().st_size if shm.exists() else -1
    entries.append((f"{database.name}-shm", shm_size, 0))
    return entries


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_snapshot(database: Path, storage_dir: Path) -> VerificationResult:
    import time as _time

    started = _time.monotonic()
    database = Path(database)
    storage_dir = Path(storage_dir)
    if not database.is_file():
        raise _fail(f"missing database: {database}")
    if not storage_dir.is_dir():
        raise _fail(f"missing storage dir: {storage_dir}")

    storage_before = storage_manifest(storage_dir)
    source_before = _source_metadata(database)

    temp_root = Path(tempfile.mkdtemp(prefix="repository-snapshot-"))
    cleanup_problem = ""
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        backup_path = temp_root / "backup.db"
        temp_storage = temp_root / "storage"
        temp_storage.mkdir()

        source_conn = sqlite3.connect(_source_read_uri(database), uri=True)
        try:
            backup_conn = sqlite3.connect(backup_path)
            try:
                source_conn.backup(backup_conn)
            finally:
                backup_conn.close()
        finally:
            source_conn.close()

        pre = snapshot_database(backup_path)
        if pre.user_version > SCHEMA_VERSION:
            raise _fail(
                f"database user_version={pre.user_version} is newer than "
                f"SCHEMA_VERSION={SCHEMA_VERSION}"
            )

        settings = offline_settings(backup_path, temp_storage)
        repo_db = Path(settings.sqlite_path).resolve()
        repo_storage = Path(settings.storage_dir)
        original_db = database.resolve()
        original_storage = storage_dir.resolve()
        if repo_db == original_db or original_db in repo_db.parents:
            raise _fail("repository would be constructed with the original database")
        if repo_storage.is_symlink():
            raise _fail("repository storage directory must not be a symlink")
        resolved_storage = repo_storage.resolve()
        if (
            resolved_storage == original_storage
            or original_storage in resolved_storage.parents
        ):
            raise _fail("repository would be constructed with the original storage")

        with _no_network():
            repo = SQLiteRepository(settings)
            reads = exercise_reads(repo, backup_path)

        column_plan = {
            name: table.column_names for name, table in pre.tables.items()
        }
        post = snapshot_database(backup_path, column_plan=column_plan)
        changed_tables, discrepancies, migration_added, normalized = (
            compare_snapshots(pre, post)
        )
    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__
    finally:
        try:
            shutil.rmtree(temp_root)
        except OSError:
            # The retained backup may contain private rows.  Report its path so
            # the operator can remove it, but never include exception text.
            cleanup_problem = f"temporary_backup_retained={temp_root}"

    if primary_error is not None:
        if cleanup_problem:
            raise _fail(cleanup_problem) from None
        raise primary_error.with_traceback(primary_traceback)

    storage_after = storage_manifest(storage_dir)
    source_after = _source_metadata(database)
    storage_unchanged = storage_after == storage_before
    source_unchanged = source_after == source_before
    if not storage_unchanged:
        discrepancies.append("original-storage-changed")
    if not source_unchanged:
        discrepancies.append("original-database-metadata-changed")
    if cleanup_problem:
        discrepancies.append(cleanup_problem)

    return VerificationResult(
        ok=not changed_tables and not discrepancies,
        source_user_version=pre.user_version,
        final_user_version=post.user_version,
        table_counts={name: t.row_count for name, t in pre.tables.items()},
        table_digests={name: t.digest for name, t in pre.tables.items()},
        changed_tables=changed_tables,
        discrepancies=discrepancies,
        migration_added_tables=migration_added,
        normalized=normalized,
        reads=reads,
        storage_unchanged=storage_unchanged,
        source_unchanged=source_unchanged,
        storage_file_count=len(storage_before),
        database=str(database),
        duration_seconds=_time.monotonic() - started,
    )


def _print_report(result: VerificationResult) -> None:
    echo = print
    echo(
        "repository-snapshot: database "
        f"user_version=v{result.source_user_version} "
        f"final_user_version=v{result.final_user_version} "
        f"tables={len(result.table_counts)} "
        f"duration_s={result.duration_seconds:.1f}"
    )
    for name in sorted(result.table_counts):
        digest = result.table_digests.get(name, "")
        echo(
            f"repository-snapshot: table name={name} "
            f"rows={result.table_counts[name]} "
            f"digest={digest[:16] if digest else 'virtual'}"
        )
    if result.migration_added_tables:
        echo(
            "repository-snapshot: migration_added="
            + ",".join(result.migration_added_tables)
        )
    echo(
        "repository-snapshot: normalized "
        + " ".join(f"{key}={value}" for key, value in sorted(result.normalized.items()))
    )
    echo(
        "repository-snapshot: reads "
        + " ".join(f"{key}={value}" for key, value in sorted(result.reads.items()))
    )
    echo(
        "repository-snapshot: storage "
        f"files={result.storage_file_count} "
        f"unchanged={str(result.storage_unchanged).lower()} "
        f"source_unchanged={str(result.source_unchanged).lower()}"
    )
    for line in result.discrepancies:
        echo(f"repository-snapshot: changed {line}")
    status = "PASS" if result.ok else "FAIL"
    echo(
        f"repository-snapshot: {status} "
        f"schema=v{result.source_user_version} "
        f"changed_tables={len(result.changed_tables)}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify (backup-only) that the current repository opens a "
            "pre-refactor SQLite database without changing it."
        )
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--storage-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result = verify_snapshot(args.database, args.storage_dir)
    _print_report(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
