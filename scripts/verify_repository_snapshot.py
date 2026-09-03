#!/usr/bin/env python3
"""Backup-only verification that the current repository opens a pre-refactor
SQLite database without changing it.

"Opens" here means a full SERVER start: construct the repository (migrate +
seed) and then run the startup crash-recovery sweep, which the server's
lifespan drives explicitly (``app/services/startup_warmup.py::run_startup``)
rather than the repository constructor. Modelling only the constructor would
understate what a real boot does to the database.

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
from unittest import mock
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.repositories.sqlite.anchor_normalization import sqlite_js_trim_expression
from app.services.model_registry import WORKLOADS, SystemModelServiceRegistry
from app.services.sqlite_repository import (
    SCHEMA_VERSION,
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)

RESTART_ERROR = "中断:服务重启"
KG_RUN_RESTART_ERROR = "worker_interrupted: 服务重启导致知识图谱分析中断"
KG_JOB_RESTART_MESSAGE = (
    "服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。"
)

# Tables whose rows the startup sequence may legitimately touch; every one is
# compared row-by-row against the documented allowance instead of digest-only.
SPECIAL_TABLES = (
    "users",
    "user_profiles",
    "concept_whitelist",
    "object_schemas",
    "notebooks",
    "notebook_object_schemas",
    "merge_review_jobs",
    "ask_jobs",
    "sources",
    "extraction_runs",
    "kg_build_jobs",
    "model_service_status",
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

# v21: guarded interactive reformat membership seeks exact JS-trimmed anchor
# groups. This expression index is deliberately shared with the migration/store
# expression builder so verifier acceptance cannot drift from the real index.
KNOWHOW_NORMALIZED_ANCHOR_INDEX = {
    "idx_knowhow_cells_column_normalized_anchor_row": (
        "CREATE INDEX idx_knowhow_cells_column_normalized_anchor_row "
        "ON knowhow_cells(column_id, "
        f"{sqlite_js_trim_expression('content_md')}, row_id)"
    ),
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
    # Cumulative delta from the frozen v9 fixture to merged schema v21.
    (9, 21): {
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
    (10, 21): {
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
    (11, 21, "memory"): {
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
    (11, 21, "master"): {
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
    (12, 21): {
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
    (13, 21): {
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
    (14, 21): {
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
    (15, 21): {
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
    (16, 21): {
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
    (17, 21): {
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
    (18, 21): {
        "tables": NOTEBOOK_BASES_TABLE,
        "columns": {"notebook_assets": NOTEBOOK_ASSETS_SOURCE_ID_COLUMN,
                    "promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": {**NOTEBOOK_ASSETS_SOURCE_INDEX, **NOTEBOOK_BASES_INDEX},
        "triggers": {},
        "views": {},
    },
    # The v19 -> v21 hop adds v20's notebook_bases + lookup index and
    # promotion_candidates.target_base_id (before/after ALTER — the table has
    # existed since the v1 baseline) — no new trigger/view.
    (19, 21): {
        "tables": NOTEBOOK_BASES_TABLE,
        "columns": {"promotion_candidates": PROMOTION_CANDIDATES_TARGET_BASE_ID_COLUMN},
        "indexes": NOTEBOOK_BASES_INDEX,
        "triggers": {},
        "views": {},
    },
    # A deployed v20 database already carries all prior migrations. v21 adds
    # exactly the normalized-anchor expression index, nothing else.
    (20, 21): {
        "tables": {},
        "columns": {},
        "indexes": KNOWHOW_NORMALIZED_ANCHOR_INDEX,
        "triggers": {},
        "views": {},
    },
}

# v21's expression index is the only new object in its direct hop and is also
# added when any older supported database migrates straight to current.
for _manifest in MIGRATION_MANIFEST.values():
    _manifest["indexes"] = {
        **_manifest["indexes"],
        **KNOWHOW_NORMALIZED_ANCHOR_INDEX,
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
        model_services_config="",
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
    registry = SystemModelServiceRegistry.load(settings, environ={})
    offline_checks = {
        "model_services": any(
            registry.service_for(workload_id) is not None
            for workload_id in WORKLOADS
        ),
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


@dataclass(frozen=True)
class ClusterDedupeProjection:
    row_count: int
    digest: str
    removed_count: int


@dataclass
class DatabaseSnapshot:
    user_version: int
    tables: Dict[str, TableSnapshot]
    schema_objects: Dict[str, Dict[str, str]]
    special_rows: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]]
    cluster_v24_projection: "ClusterDedupeProjection | None"


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


def _cluster_v24_projection(
    digest_conn: sqlite3.Connection,
    column_names: Sequence[str],
    total_count: int,
) -> ClusterDedupeProjection:
    """Stream the exact row image migration 24 is allowed to publish.

    The SQL window duplicates migration 24's partition and BINARY winner rule.
    Only survivor rows are streamed through the same rowid + column digest used
    by the ordinary post-migration table snapshot, so validation stays bounded
    and never materializes the cluster table in Python.
    """
    quoted = ", ".join(f'"{name}"' for name in column_names)
    cursor = digest_conn.execute(
        f"SELECT __rowid__, {quoted} FROM ("
        f"SELECT rowid AS __rowid__, {quoted}, "
        "ROW_NUMBER() OVER ("
        "PARTITION BY notebook_id, object_type, member_object_id "
        "ORDER BY created_at DESC, id COLLATE BINARY DESC"
        ") AS __duplicate_rank__ "
        "FROM concept_clusters"
        ") WHERE __duplicate_rank__=1 ORDER BY __rowid__"
    )
    digest = hashlib.sha256()
    survivor_count = 0
    while True:
        rows = cursor.fetchmany(2000)
        if not rows:
            break
        survivor_count += len(rows)
        for row in rows:
            for value in row:
                _digest_update(digest, value)
    return ClusterDedupeProjection(
        row_count=survivor_count,
        digest=digest.hexdigest(),
        removed_count=total_count - survivor_count,
    )


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
        cluster_v24_projection: "ClusterDedupeProjection | None" = None
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
            if name == "concept_clusters" and user_version < 29:
                cluster_v24_projection = _cluster_v24_projection(
                    digest_conn,
                    digest_columns,
                    row_count,
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
            cluster_v24_projection=cluster_v24_projection,
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
        "sources": 0,
        "extraction_runs": 0,
        "kg_build_jobs": 0,
        "seeded_users": 0,
        "seeded_user_profiles": 0,
        "seeded_concept_whitelist": 0,
        "seeded_object_schemas": 0,
        "relocated_object_schemas": 0,
        "admin_upgraded": 0,
        "concept_clusters": 0,
        "scrubbed_model_profiles": 0,
        "scrubbed_model_statuses": 0,
    }


def _compare_special_rows(
    table: str,
    pre_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    post_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    normalized: Dict[str, int],
    problems: List[str],
    *,
    allow_model_scrub: bool = False,
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
            if allow_model_scrub and table == "model_service_status":
                normalized["scrubbed_model_statuses"] += 1
                continue
            problems.append(f"table={table} reason=row-deleted")
            continue
        if allow_model_scrub and table == "user_profiles":
            if (
                post_row.get("model_settings") == "{}"
                and rows_equal(
                    pre_row, post_row, frozenset({"model_settings"})
                )
            ):
                if pre_row.get("model_settings") != "{}":
                    normalized["scrubbed_model_profiles"] += 1
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
        if table == "sources" and pre_row.get("parse_status") == "extracting":
            allowed = frozenset(
                {"status", "parse_status", "error_message", "updated_at"}
            )
            if (
                post_row.get("status") == "parsed"
                and post_row.get("parse_status") == "parsed"
                and post_row.get("error_message") == ""
                and post_row.get("updated_at") != pre_row.get("updated_at")
                and rows_equal(pre_row, post_row, allowed)
            ):
                normalized["sources"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if (
            table == "extraction_runs"
            and pre_row.get("run_type") == "kg"
            and pre_row.get("status") == "running"
        ):
            allowed = frozenset({"status", "error_message", "updated_at"})
            if (
                post_row.get("status") == "failed"
                and post_row.get("error_message") == KG_RUN_RESTART_ERROR
                and post_row.get("updated_at") != pre_row.get("updated_at")
                and rows_equal(pre_row, post_row, allowed)
            ):
                normalized["extraction_runs"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if table == "kg_build_jobs" and pre_row.get("status") == "running":
            allowed = frozenset(
                {
                    "status",
                    "stage",
                    "error_code",
                    "error_message",
                    "updated_at",
                    "finished_at",
                }
            )
            if (
                post_row.get("status") == "failed"
                and post_row.get("stage") == "finished"
                and post_row.get("error_code") == "worker_interrupted"
                and post_row.get("error_message") == KG_JOB_RESTART_MESSAGE
                and post_row.get("updated_at") != pre_row.get("updated_at")
                and post_row.get("finished_at") == post_row.get("updated_at")
                and rows_equal(pre_row, post_row, allowed)
            ):
                normalized["kg_build_jobs"] += 1
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

    relocation_active = pre.user_version < 47 <= post.user_version
    relocated_keys: set[Tuple[Any, ...]] = set()
    expected_relocated: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    if relocation_active:
        notebook_owners = {
            row.get("id"): row.get("created_by") or ""
            for row in pre.special_rows.get("notebooks", {}).values()
        }
        for key, row in pre.special_rows.get("object_schemas", {}).items():
            notebook_id = row.get("notebook_id") or ""
            if not notebook_id:
                continue
            relocated_keys.add(key)
            expected_relocated[(notebook_id, row.get("object_type"))] = {
                "notebook_id": notebook_id,
                "object_type": row.get("object_type"),
                "plural": row.get("plural"),
                "fields": row.get("fields"),
                "primary_field": row.get("primary_field"),
                "description": row.get("description"),
                "label": row.get("label"),
                "list_fields": row.get("list_fields"),
                "source": row.get("source"),
                "status": row.get("status"),
                "rationale": row.get("rationale"),
                "created_by": notebook_owners.get(notebook_id, ""),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }

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
            elif kind == "index" and name in migration_manifest.get(
                "dropped_indexes", frozenset()
            ):
                # v71 首次引入「被取代索引的显式退役」:代次化把三条簇索引换成
                # 带 generation 的接替者并 DROP 旧名(0051/_migration_71)。退役
                # 与新增同样走 manifest 白名单,名单之外的 drop 照旧硬失败。
                continue
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
                relocation_table = relocation_active and name == "notebook_object_schemas"
                if (
                    post.tables[name].row_count
                    and name not in MEMORY_FTS_SHADOW_TABLES
                    and not relocation_table
                ):
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
        if (
            name == "concept_clusters"
            and pre.user_version < 29 <= post.user_version
        ):
            projection = pre.cluster_v24_projection
            if projection is None:
                note(name, "migration-v24-projection-missing")
            elif post_table.row_count != projection.row_count:
                note(name, "migration-v24-row-count-mismatch")
            elif post_table.digest != projection.digest:
                note(name, "migration-v24-survivor-digest-mismatch")
            else:
                normalized["concept_clusters"] = projection.removed_count
            continue
        if name in SPECIAL_TABLES:
            problems: List[str] = []
            pre_special_rows = pre.special_rows.get(name, {})
            if relocation_active and name == "object_schemas":
                pre_special_rows = {
                    key: row for key, row in pre_special_rows.items()
                    if key not in relocated_keys
                }
            _compare_special_rows(
                name,
                pre_special_rows,
                post.special_rows.get(name, {}),
                normalized,
                problems,
                allow_model_scrub=(
                    pre.user_version < 25 <= post.user_version
                ),
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

    if relocation_active:
        actual_relocated = post.special_rows.get("notebook_object_schemas", {})
        if actual_relocated != expected_relocated:
            note("notebook_object_schemas", "migration-v47-relocation-mismatch")
        else:
            normalized["relocated_object_schemas"] = len(expected_relocated)

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
                # created_by=None: the snapshot verifier counts every report in
                # the notebook, not one creator's — the per-creator narrowing is
                # an API-surface rule (P1-T3b), not a storage-integrity one.
                reports = repo.list_reports(notebook_id, created_by=None)
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

        # Pin registry resolution to an explicit empty registry for the whole
        # repository lifetime.  This preserves the repository's public
        # constructor seam used by the verifier's mutation probes and prevents
        # hostile process/.env model variables from activating any provider.
        with _no_network(), mock.patch.object(
            SystemModelServiceRegistry,
            "load",
            return_value=SystemModelServiceRegistry({}, {}),
        ):
            repo = SQLiteRepository(settings)
            # Startup crash recovery is no longer a construction side effect
            # (it moved to app/services/startup_warmup.py::run_startup so
            # offline CLIs stop settling the server's in-progress rows). This
            # tool must keep modelling a real SERVER start — otherwise it would
            # report "nothing changes" while the actual boot still normalizes
            # these rows, i.e. a rosier picture than production. Driven
            # explicitly here, against the disposable BACKUP copy.
            repo._recover_interrupted_jobs()
            # codex #659 R24 P2: production startup ALSO runs the delete-job
            # sweep before mark_ready (startup_warmup.py::
            # _sweep_notebook_delete_jobs_on_start) — a backup captured with
            # a stale active delete job, or a status='deleting' notebook
            # whose job row is gone, WILL have that deletion resumed and
            # completed at next boot. Without modelling it here the verifier
            # reports a rosier picture than production (a "nothing changes"
            # PASS for a snapshot boot would mutate heavily). ``_submit`` is
            # patched to run the job INLINE so the sweep is deterministic —
            # no thread pool to drain, and the post-startup snapshot below
            # observes the fully settled state. A resulting non-ok report
            # for a mid-delete backup is deliberate truth-telling, not a
            # regression: normalizing a whole notebook's disappearance would
            # be indistinguishable from silent data loss.
            delete_runner = getattr(repo._runtime, "notebook_delete", None)
            if delete_runner is not None:
                def _inline_submit(job_id: str, notebook_id: str) -> bool:
                    delete_runner.run(job_id)
                    return True

                with mock.patch.object(delete_runner, "_submit", _inline_submit):
                    delete_runner.sweep_once()
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


# v22: durable notebook-scoped KG build status plus a partial unique index
# enforcing one running task per notebook. Appended here to keep the verifier's
# source-audit coordinates stable; compare_snapshots resolves the manifest only
# when invoked, after module initialization has completed.
KG_BUILD_JOB_TABLE = {
    "kg_build_jobs": """CREATE TABLE kg_build_jobs (
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
                )""",
}
KG_BUILD_JOB_INDEXES = {
    "idx_kg_build_jobs_one_running":
        """CREATE UNIQUE INDEX idx_kg_build_jobs_one_running
                  ON kg_build_jobs(notebook_id) WHERE status = 'running'""",
    "idx_kg_build_jobs_nb_created":
        """CREATE INDEX idx_kg_build_jobs_nb_created
                  ON kg_build_jobs(notebook_id, created_at DESC, id DESC)""",
}
MIGRATION_MANIFEST = {
    (key[0], 22, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **KG_BUILD_JOB_TABLE},
        "indexes": {**manifest["indexes"], **KG_BUILD_JOB_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(21, 22)] = {
    "tables": KG_BUILD_JOB_TABLE,
    "columns": {},
    "indexes": KG_BUILD_JOB_INDEXES,
    "triggers": {},
    "views": {},
}

# v23: the latest sanitized health result for each user/model-service role.
# This is account-scoped application state: one row per (user_id, service),
# removed automatically when its user is deleted.
MODEL_SERVICE_STATUS_TABLE = {
    "model_service_status": """CREATE TABLE model_service_status (
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  service TEXT NOT NULL,
                  config_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
                  latency_ms INTEGER NOT NULL DEFAULT 0,
                  code TEXT NOT NULL DEFAULT '',
                  trigger TEXT NOT NULL CHECK (trigger IN ('manual_test', 'observed_failure')),
                  checked_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, service)
                )""",
}
MODEL_SERVICE_STATUS_INDEXES = {
    "idx_model_service_status_user_checked":
        """CREATE INDEX idx_model_service_status_user_checked
                  ON model_service_status(user_id, checked_at DESC)""",
}
MIGRATION_MANIFEST = {
    (key[0], 23, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **MODEL_SERVICE_STATUS_TABLE},
        "indexes": {**manifest["indexes"], **MODEL_SERVICE_STATUS_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(22, 23)] = {
    "tables": MODEL_SERVICE_STATUS_TABLE,
    "columns": {},
    "indexes": MODEL_SERVICE_STATUS_INDEXES,
    "triggers": {},
    "views": {},
}

# v24: write-lock slimming improvement point 2 (design doc §5.5) — the
# kg_canonical_scratch preparation-segment scratch table (seed -> canonical
# mapping) that swap_cluster_map_from_scratch's pure-SQL swap joins against
# kg_cluster_scratch. Same appended-here convention as v22/v23 above.
KG_CANONICAL_SCRATCH_TABLE = {
    "kg_canonical_scratch": """CREATE TABLE kg_canonical_scratch (
                  notebook_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  seed TEXT NOT NULL,
                  canonical_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  canonical_description TEXT NOT NULL DEFAULT '',
                  canonical_desc_sig TEXT NOT NULL DEFAULT ''
                )""",
}
KG_CANONICAL_SCRATCH_INDEX = {
    "idx_kg_canonical_scratch_nb_run_seed":
        """CREATE INDEX idx_kg_canonical_scratch_nb_run_seed
                  ON kg_canonical_scratch(notebook_id, run_id, seed)""",
}

# v25: knowhow table version control — an append-only per-table change log
# (knowhow_changes) plus user-named checkpoints (knowhow_milestones). The
# milestone table deliberately has no FK to knowhow_changes: rows survive
# change-history cleanup as inert "stale" markers instead of cascading away.
KNOWHOW_HISTORY_TABLES = {
    "knowhow_changes": """CREATE TABLE knowhow_changes (
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
                )""",
    "knowhow_milestones": """CREATE TABLE knowhow_milestones (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, name)
                )""",
}
KNOWHOW_HISTORY_INDEXES = {
    "idx_knowhow_changes_table":
        """CREATE INDEX idx_knowhow_changes_table
                  ON knowhow_changes(table_id, seq DESC)""",
    "idx_knowhow_milestones_table":
        """CREATE INDEX idx_knowhow_milestones_table
                  ON knowhow_milestones(table_id, seq DESC)""",
}
MIGRATION_MANIFEST = {
    (key[0], 24, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **KG_CANONICAL_SCRATCH_TABLE},
        "indexes": {**manifest["indexes"], **KG_CANONICAL_SCRATCH_INDEX},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(23, 24)] = {
    "tables": KG_CANONICAL_SCRATCH_TABLE,
    "columns": {},
    "indexes": KG_CANONICAL_SCRATCH_INDEX,
    "triggers": {},
    "views": {},
}
# v25: deployment-wide model-service health plus an irreversible data-only
# scrub of per-user settings and legacy status rows. The retained v23 table and
# user_profiles column are unchanged structurally; compare_snapshots validates
# the only permitted row changes separately.
SYSTEM_MODEL_SERVICE_STATUS_TABLE = {
    "system_model_service_status": """CREATE TABLE system_model_service_status (
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
                )""",
}
MIGRATION_MANIFEST = {
    (key[0], 25, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **SYSTEM_MODEL_SERVICE_STATUS_TABLE},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(24, 25)] = {
    "tables": SYSTEM_MODEL_SERVICE_STATUS_TABLE,
    "columns": {},
    "indexes": {},
    "triggers": {},
    "views": {},
}

# v26 层级联在 v25 之上：字典推导把上面每条 (X, 25) 条目再重基到 (X, 26) 并叠加
# knowhow 历史两表，随后显式登记 (25, 26) hop —— 与 v25 对 v24 所做的一模一样。
# ⚠️ 必须排在 v25 model-service 层之后（knowhow 迁移是 _migration_26，在 master
# 的 _migration_25 之上），否则两个 (24,25) hop 会互相覆盖、丢掉到 v26 的登记。
MIGRATION_MANIFEST = {
    (key[0], 26, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **KNOWHOW_HISTORY_TABLES},
        "indexes": {**manifest["indexes"], **KNOWHOW_HISTORY_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(25, 26)] = {
    "tables": KNOWHOW_HISTORY_TABLES,
    "columns": {},
    "indexes": KNOWHOW_HISTORY_INDEXES,
    "triggers": {},
    "views": {},
}

# v27 (P1.5, source completion marker): sources.chunked_at — the persistent
# "this generation of elements finished chunking" marker that makes an
# extracted+0-chunk source's history decidable (legit 0-chunk vs interrupted
# chunk build). Renumbered from v26 after a number collision: #328 took
# _migration_25 (credential scrub) and #327 took _migration_26 (knowhow history),
# so this segment MUST sit after BOTH — its broadcast lifts every (X, 26) key
# (knowhow's v26 layer included) to (X, 27). Unlike the new-table bumps above,
# this adds a COLUMN to an existing table — so the broadcast segment below merges
# into each manifest's ``columns["sources"]`` nested dict (preserving the
# memory_id column the pre-v14 hops already carry) rather than only unioning
# ``tables``/``indexes``. Nested-merge, never overwrite.
SOURCES_CHUNKED_AT_COLUMN = {
    "chunked_at": ("chunked_at", "TEXT", 0, None, 0),
}
# v28 层级联在 v27 之上：每笔记本文档数量上限 —— app_settings 全局设置 KV 表 +
# user_profiles.upload_document_limit 可空列。字典推导把上面每条 (X, 27) 条目重基到
# (X, 28) 并叠加 app_settings 表与该列，随后显式登记 (27, 28) hop —— 与前面各层同款。
# app_settings 是全新表（每条 hop 的 tables allowlist 都含它）；user_profiles 自 v1
# baseline 就存在（其 model_settings 列在 v9 fixture 里已具备，故不是迁移新增），所以
# upload_document_limit 是每条 hop 唯一新增的 user_profiles 列，属于 columns allowlist
# （镜像 v20 对 promotion_candidates.target_base_id 的处理）。SQL 文本与已部署库
# sqlite_master 存储逐字一致（"IF NOT EXISTS" 被 SQLite 剥除）。
APP_SETTINGS_TABLE = {
    "app_settings": (
        "CREATE TABLE app_settings (\n"
        "                  key TEXT PRIMARY KEY,\n"
        "                  value TEXT NOT NULL,\n"
        "                  updated_at TEXT NOT NULL\n"
        "                )"
    ),
}
USER_PROFILES_UPLOAD_LIMIT_COLUMN = {
    "upload_document_limit": ("upload_document_limit", "INTEGER", 0, None, 0),
}
MIGRATION_MANIFEST = {
    (key[0], 27, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "sources": {
                **manifest["columns"].get("sources", {}),
                **SOURCES_CHUNKED_AT_COLUMN,
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST = {
    (key[0], 28, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **APP_SETTINGS_TABLE},
        "columns": {
            **manifest["columns"],
            "user_profiles": {
                **manifest["columns"].get("user_profiles", {}),
                **USER_PROFILES_UPLOAD_LIMIT_COLUMN,
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
# The single-hop (26, 27) entry. No fixture exercises a pure v26→v27 replay
# today, so the broadcast segment above — which stamps chunked_at onto every
# (X, 27) key — is what the live tests actually go through. This entry is kept
# for structural completeness (a future v26 rollback fixture would key straight
# into it) and to mirror the append convention; verified load-bearing by
# mutating the broadcast segment (not this line) — see the P1.5 commit.
MIGRATION_MANIFEST[(26, 27)] = {
    "tables": {},
    "columns": {"sources": SOURCES_CHUNKED_AT_COLUMN},
    "indexes": {},
    "triggers": {},
    "views": {},
}
# The single-hop (27, 28) entry — 每笔记本文档数量上限层。同上，无 fixture 走纯
# v27→v28 replay，靠上面的 (X,28) 广播段实际生效；保留此条以镜像 append 约定、
# 并供未来 v27 回滚 fixture 直接命中。
MIGRATION_MANIFEST[(27, 28)] = {
    "tables": APP_SETTINGS_TABLE,
    "columns": {"user_profiles": USER_PROFILES_UPLOAD_LIMIT_COLUMN},
    "indexes": {},
    "triggers": {},
    "views": {},
}

# v29: serialize cluster membership at the repository boundary and retain a
# database-level uniqueness guard.  This number deliberately follows master's
# v24-v28 migrations; migration 29 also performs deterministic legacy duplicate
# cleanup before creating the index.
CLUSTER_MEMBERSHIP_UNIQUE_INDEX = {
    "uq_clusters_notebook_type_member":
        """CREATE UNIQUE INDEX uq_clusters_notebook_type_member
                  ON concept_clusters(notebook_id, object_type, member_object_id)""",
}
MIGRATION_MANIFEST = {
    (key[0], 29, *key[2:]): {
        **manifest,
        "indexes": {
            **manifest["indexes"],
            **CLUSTER_MEMBERSHIP_UNIQUE_INDEX,
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(28, 29)] = {
    "tables": {},
    "columns": {},
    "indexes": CLUSTER_MEMBERSHIP_UNIQUE_INDEX,
    "triggers": {},
    "views": {},
}

# v30 (Task 7, LLM/embedding content-cache follow-up): sources(notebook_id,
# file_hash) lookup index backing UI-upload / batch_ingest same-notebook content
# dedup — previously an unindexed full table scan. No new table/column/trigger/
# view. Follows the established cascade exactly: broadcast the index onto every
# prior hop key rebased to v30, then register the explicit (29, 30) single hop.
# SQL text matches sqlite_master storage verbatim (SQLite strips "IF NOT EXISTS").
SOURCE_HASH_DEDUP_INDEX = {
    "idx_sources_notebook_file_hash":
        "CREATE INDEX idx_sources_notebook_file_hash "
        "ON sources(notebook_id, file_hash)",
}
MIGRATION_MANIFEST = {
    (key[0], 30, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **SOURCE_HASH_DEDUP_INDEX},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(29, 30)] = {
    "tables": {},
    "columns": {},
    "indexes": SOURCE_HASH_DEDUP_INDEX,
    "triggers": {},
    "views": {},
}

# v31: inert SQLite-side metadata for run-scoped forward-shadow capture.
# The versioned migration creates only these two payload-free internal tables;
# logical-key guards and capture/freeze triggers are installed later by the
# shadow run installer in one BEGIN IMMEDIATE transaction.
SHADOW_CAPTURE_TABLES = {
    "shadow_change_log": """CREATE TABLE shadow_change_log (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  table_name TEXT NOT NULL,
                  key_json TEXT NOT NULL,
                  operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
                  schema_epoch INTEGER NOT NULL,
                  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )""",
    "shadow_capture_control": """CREATE TABLE shadow_capture_control (
                  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                  enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                  write_frozen INTEGER NOT NULL CHECK (write_frozen IN (0, 1)),
                  apply_active INTEGER NOT NULL DEFAULT 0 CHECK (apply_active IN (0, 1)),
                  run_id TEXT NOT NULL,
                  schema_epoch INTEGER NOT NULL
                )""",
}
MIGRATION_MANIFEST = {
    (key[0], 31, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **SHADOW_CAPTURE_TABLES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(30, 31)] = {
    "tables": SHADOW_CAPTURE_TABLES,
    "columns": {},
    "indexes": {},
    "triggers": {},
    "views": {},
}

# v32: persist the corpus-blind Deep Report question-understanding contract and
# its owner-reviewed clarification state. Broadcast the new reports column over
# every cumulative hop, then retain the explicit v31→v32 single-hop contract.
REPORTS_UNDERSTANDING_COLUMN = {
    "understanding_json": ("understanding_json", "TEXT", 1, "'{}'", 0),
}
MIGRATION_MANIFEST = {
    (key[0], 32, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "reports": {
                **manifest["columns"].get("reports", {}),
                **REPORTS_UNDERSTANDING_COLUMN,
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(31, 32)] = {
    "tables": {},
    "columns": {"reports": REPORTS_UNDERSTANDING_COLUMN},
    "indexes": {},
    "triggers": {},
    "views": {},
}

# v33: relation endpoint probes page by stable relation id within each
# notebook/source or notebook/target stream. These covering indexes keep that
# keyset walk bounded without changing tables, columns, triggers, or views.
RELATION_ENDPOINT_KEYSET_INDEXES = {
    "idx_knowledge_relations_nb_source_id":
        """CREATE INDEX idx_knowledge_relations_nb_source_id
                  ON knowledge_relations(notebook_id, source_object_id, id)""",
    "idx_knowledge_relations_nb_target_id":
        """CREATE INDEX idx_knowledge_relations_nb_target_id
                  ON knowledge_relations(notebook_id, target_object_id, id)""",
}
MIGRATION_MANIFEST = {
    (key[0], 33, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **RELATION_ENDPOINT_KEYSET_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(32, 33)] = {
    "tables": {},
    "columns": {},
    "indexes": RELATION_ENDPOINT_KEYSET_INDEXES,
    "triggers": {},
    "views": {},
}

# v34: resumable relation completion keeps a generation-bound keyset cursor
# and pages source objects through the matching covering index.
RELATION_COMPLETION_TABLES = {
    "kg_relation_completion_state": """CREATE TABLE kg_relation_completion_state (
                  notebook_id TEXT NOT NULL,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  next_object_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'pending',
                  schema_version INTEGER NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(source_id, source_generation, mode)
                )""",
}
RELATION_COMPLETION_INDEXES = {
    "idx_knowledge_objects_source_id":
        """CREATE INDEX idx_knowledge_objects_source_id
                  ON knowledge_objects(source_id, id)""",
    "idx_kg_relation_completion_state_nb_status":
        """CREATE INDEX idx_kg_relation_completion_state_nb_status
                  ON kg_relation_completion_state(notebook_id, status, updated_at)""",
}
MIGRATION_MANIFEST = {
    (key[0], 34, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **RELATION_COMPLETION_TABLES},
        "indexes": {**manifest["indexes"], **RELATION_COMPLETION_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(33, 34)] = {
    "tables": RELATION_COMPLETION_TABLES,
    "columns": {},
    "indexes": RELATION_COMPLETION_INDEXES,
    "triggers": {},
    "views": {},
}

# v35: persist the browser-captured submit instant on durable Ask jobs so an
# in-flight turn keeps the user's local question time after reconnecting.
ASK_JOBS_ASKED_AT_COLUMN = {
    "asked_at": ("asked_at", "TEXT", 1, "''", 0),
}
MIGRATION_MANIFEST = {
    (key[0], 35, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "ask_jobs": {
                **manifest["columns"].get("ask_jobs", {}),
                **ASK_JOBS_ASKED_AT_COLUMN,
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(34, 35)] = {
    "tables": {},
    "columns": {"ask_jobs": ASK_JOBS_ASKED_AT_COLUMN},
    "indexes": {},
    "triggers": {},
    "views": {},
}

# v36: the three KG-quality-analysis precompute product tables plus the one
# explicit index the source-profile read path needs (the other two tables are
# read by their primary-key prefix).  Same cascade as every other v30/v31 hop:
# broadcast onto every prior hop key rebased to v36, then register the explicit
# (35, 36) single hop.  SQL text matches sqlite_master storage verbatim (SQLite
# strips "IF NOT EXISTS" and keeps the source indentation).
KG_ANALYSIS_TABLES = {
    "kg_community_edges": """CREATE TABLE kg_community_edges (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  src_community_id TEXT NOT NULL,
                  dst_community_id TEXT NOT NULL,
                  weight INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (notebook_id, src_community_id, dst_community_id)
                )""",
    "kg_source_profiles": """CREATE TABLE kg_source_profiles (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL,
                  n_objects INTEGER NOT NULL DEFAULT 0,
                  n_graph_objects INTEGER NOT NULL DEFAULT 0,
                  top_community_id TEXT NOT NULL DEFAULT '',
                  top_share REAL NOT NULL DEFAULT 0,
                  community_spread INTEGER NOT NULL DEFAULT 0,
                  mainstream_share REAL NOT NULL DEFAULT 0,
                  PRIMARY KEY (notebook_id, source_id)
                )""",
    "kg_analysis_artifacts": """CREATE TABLE kg_analysis_artifacts (
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,
                  kg_mutation_seq INTEGER NOT NULL DEFAULT -1,
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (notebook_id, kind)
                )""",
}
KG_ANALYSIS_INDEXES = {
    "idx_kg_source_profiles_nb_mainstream":
        "CREATE INDEX idx_kg_source_profiles_nb_mainstream\n"
        "                  ON kg_source_profiles(notebook_id, mainstream_share)",
}
MIGRATION_MANIFEST = {
    (key[0], 36, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **KG_ANALYSIS_TABLES},
        "indexes": {**manifest["indexes"], **KG_ANALYSIS_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(35, 36)] = {
    "tables": KG_ANALYSIS_TABLES,
    "columns": {},
    "indexes": KG_ANALYSIS_INDEXES,
    "triggers": {},
    "views": {},
}

# v37: indexed per-source, per-element-type keyset order for bounded
# collection enumeration (formula/table/image/code_block listings).
SOURCE_ELEMENT_TYPE_INDEX = {
    "idx_source_elements_source_type":
        """CREATE INDEX idx_source_elements_source_type
                  ON source_elements(source_id, element_type, created_at, id)""",
}
MIGRATION_MANIFEST = {
    (key[0], 37, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **SOURCE_ELEMENT_TYPE_INDEX},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(36, 37)] = {
    "tables": {},
    "columns": {},
    "indexes": SOURCE_ELEMENT_TYPE_INDEX,
    "triggers": {},
    "views": {},
}

# v38: bounded identity-only roster for user-visible source scope resolution.
VISIBLE_SOURCE_IDENTITY_INDEX = {
    "idx_sources_visible_identity":
        "CREATE INDEX idx_sources_visible_identity "
        "ON sources(notebook_id, created_at, id) "
        "WHERE source_type NOT IN ('memory','knowhow')",
}
MIGRATION_MANIFEST = {
    (key[0], 38, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **VISIBLE_SOURCE_IDENTITY_INDEX},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(37, 38)] = {
    "tables": {},
    "columns": {},
    "indexes": VISIBLE_SOURCE_IDENTITY_INDEX,
    "triggers": {},
    "views": {},
}

# v39: the command-catalog extraction job row (with its per-source
# queued/running single-flight partial unique index) and its reviewable
# candidate rows.  Same cascade as every other hop: broadcast onto every prior
# hop key rebased to v39, then register the explicit (38, 39) single hop.  SQL
# text matches sqlite_master storage verbatim (SQLite strips "IF NOT EXISTS"
# and keeps the source indentation).
COMMAND_CATALOG_TABLES = {
    "catalog_jobs": """CREATE TABLE catalog_jobs (
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
                )""",
    "catalog_candidates": """CREATE TABLE catalog_candidates (
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
                )""",
}
COMMAND_CATALOG_INDEXES = {
    "idx_catalog_jobs_one_active":
        "CREATE UNIQUE INDEX idx_catalog_jobs_one_active\n"
        "                  ON catalog_jobs(source_id) "
        "WHERE status IN ('queued', 'running')",
    "idx_catalog_jobs_source_created":
        "CREATE INDEX idx_catalog_jobs_source_created\n"
        "                  ON catalog_jobs(source_id, created_at DESC, id DESC)",
    "idx_catalog_jobs_nb_created":
        "CREATE INDEX idx_catalog_jobs_nb_created\n"
        "                  ON catalog_jobs(notebook_id, created_at DESC, id DESC)",
    "idx_catalog_candidates_job_state":
        "CREATE INDEX idx_catalog_candidates_job_state\n"
        "                  ON catalog_candidates(job_id, state, position)",
    "idx_catalog_candidates_nb":
        "CREATE INDEX idx_catalog_candidates_nb\n"
        "                  ON catalog_candidates(notebook_id)",
    "idx_catalog_candidates_source":
        "CREATE INDEX idx_catalog_candidates_source\n"
        "                  ON catalog_candidates(source_id)",
    # R14 P2: the only v39 index on a PRE-EXISTING table, so a deployed v38
    # database gains it without gaining a table.  That is exactly the shape the
    # replay would otherwise miss -- DROP TABLE takes an index with it, a
    # standalone index has to be dropped by name (see the rollback helpers in
    # tests/test_repository_snapshot_verifier.py).
    "idx_knowhow_tables_nb_title":
        "CREATE INDEX idx_knowhow_tables_nb_title\n"
        "                  ON knowhow_tables(notebook_id, title, created_at, id)",
}
MIGRATION_MANIFEST = {
    (key[0], 39, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **COMMAND_CATALOG_TABLES},
        "indexes": {**manifest["indexes"], **COMMAND_CATALOG_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(38, 39)] = {
    "tables": COMMAND_CATALOG_TABLES,
    "columns": {},
    "indexes": COMMAND_CATALOG_INDEXES,
    "triggers": {},
    "views": {},
}

# v40: immutable source-generation facts plus normalized evidence-element
# bindings. Both tables are additive and source-owned; the global object id is
# deliberately data, not a foreign key, so fusion cannot erase source truth.
SOURCE_LOCAL_FACT_TABLES = {
    "knowledge_source_facts": """CREATE TABLE knowledge_source_facts (
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
                )""",
    "knowledge_source_fact_elements": """CREATE TABLE knowledge_source_fact_elements (
                  fact_id TEXT NOT NULL REFERENCES knowledge_source_facts(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  source_generation TEXT NOT NULL,
                  element_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (fact_id, element_id)
                )""",
}
SOURCE_LOCAL_FACT_INDEXES = {
    "idx_knowledge_source_facts_source_generation":
        "CREATE INDEX idx_knowledge_source_facts_source_generation\n"
        "                  ON knowledge_source_facts(source_id, source_generation, id)",
    "idx_knowledge_source_facts_notebook_object":
        "CREATE INDEX idx_knowledge_source_facts_notebook_object\n"
        "                  ON knowledge_source_facts(notebook_id, global_object_id)",
    "uq_knowledge_source_facts_generation_local":
        "CREATE UNIQUE INDEX uq_knowledge_source_facts_generation_local\n"
        "                  ON knowledge_source_facts(\n"
        "                    source_id, source_generation, local_object_id\n"
        "                  )",
    "idx_knowledge_source_fact_elements_source":
        "CREATE INDEX idx_knowledge_source_fact_elements_source\n"
        "                  ON knowledge_source_fact_elements(\n"
        "                    source_id, source_generation, element_id, fact_id\n"
        "                  )",
}
MIGRATION_MANIFEST = {
    (key[0], 40, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **SOURCE_LOCAL_FACT_TABLES},
        "indexes": {**manifest["indexes"], **SOURCE_LOCAL_FACT_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(39, 40)] = {
    "tables": SOURCE_LOCAL_FACT_TABLES,
    "columns": {},
    "indexes": SOURCE_LOCAL_FACT_INDEXES,
    "triggers": {},
    "views": {},
}

# v41: one source-owned operational ledger makes historical source-fact
# projection restartable and auditable without storing evidence text.
SOURCE_FACT_BACKFILL_TABLES = {
    "knowledge_source_fact_backfills": """CREATE TABLE knowledge_source_fact_backfills (
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
                )""",
}
SOURCE_LOCAL_FACT_TABLE_V41 = """CREATE TABLE knowledge_source_facts (
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
                , projection_origin TEXT NOT NULL DEFAULT 'live' CHECK(projection_origin IN ('live','historical')))"""
SOURCE_FACT_BACKFILL_COLUMNS = {
    "knowledge_source_facts": {
        "projection_origin": (
            "projection_origin", "TEXT", 1, "'live'", 0
        ),
    },
}
SOURCE_FACT_BACKFILL_INDEXES = {
    "idx_knowledge_source_fact_backfills_notebook":
        "CREATE INDEX idx_knowledge_source_fact_backfills_notebook\n"
        "                  ON knowledge_source_fact_backfills(notebook_id, status, source_id)",
    "idx_kos_source_object":
        "CREATE INDEX idx_kos_source_object\n"
        "                  ON knowledge_object_sources(source_id, object_id)",
    "idx_knowledge_source_facts_source_generation_global":
        "CREATE INDEX idx_knowledge_source_facts_source_generation_global\n"
        "                  ON knowledge_source_facts(\n"
        "                    source_id, source_generation, projection_origin,\n"
        "                    global_object_id\n"
        "                  )",
}
MIGRATION_MANIFEST = {
    (key[0], 41, *key[2:]): {
        **manifest,
        "tables": {
            **manifest["tables"],
            **(
                {"knowledge_source_facts": SOURCE_LOCAL_FACT_TABLE_V41}
                if "knowledge_source_facts" in manifest["tables"] else {}
            ),
            **SOURCE_FACT_BACKFILL_TABLES,
        },
        "columns": {
            **manifest["columns"],
            **SOURCE_FACT_BACKFILL_COLUMNS,
        },
        "indexes": {**manifest["indexes"], **SOURCE_FACT_BACKFILL_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(40, 41)] = {
    "tables": SOURCE_FACT_BACKFILL_TABLES,
    "columns": SOURCE_FACT_BACKFILL_COLUMNS,
    "indexes": SOURCE_FACT_BACKFILL_INDEXES,
    "triggers": {},
    "views": {},
}

SOURCE_INDEX_BACKFILL_TABLES = {
    "source_index_backfills":
        "CREATE TABLE source_index_backfills (\n"
        "                  notebook_id TEXT NOT NULL PRIMARY KEY\n"
        "                    REFERENCES notebooks(id) ON DELETE CASCADE,\n"
        "                  kg_mutation_seq INTEGER NOT NULL DEFAULT 0,\n"
        "                  status TEXT NOT NULL DEFAULT 'running'\n"
        "                    CHECK(status IN ('running','complete','failed')),\n"
        "                  after_object_id TEXT NOT NULL DEFAULT '',\n"
        "                  total_objects INTEGER NOT NULL DEFAULT 0,\n"
        "                  objects_scanned INTEGER NOT NULL DEFAULT 0,\n"
        "                  rows_written INTEGER NOT NULL DEFAULT 0,\n"
        "                  failure_code TEXT NOT NULL DEFAULT '',\n"
        "                  created_at TEXT NOT NULL,\n"
        "                  updated_at TEXT NOT NULL,\n"
        "                  completed_at TEXT NOT NULL DEFAULT ''\n"
        "                )",
}
SOURCE_INDEX_BACKFILL_INDEXES = {
    "idx_source_index_backfills_status":
        "CREATE INDEX idx_source_index_backfills_status\n"
        "                  ON source_index_backfills(status, notebook_id)",
}
MIGRATION_MANIFEST = {
    (key[0], 42, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **SOURCE_INDEX_BACKFILL_TABLES},
        "indexes": {**manifest["indexes"], **SOURCE_INDEX_BACKFILL_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(41, 42)] = {
    "tables": SOURCE_INDEX_BACKFILL_TABLES,
    "columns": {},
    "indexes": SOURCE_INDEX_BACKFILL_INDEXES,
    "triggers": {},
    "views": {},
}


# v43: per-report public share tokens.  Two nullable/defaulted columns on an
# existing table plus one partial unique index over issued tokens only.
REPORT_SHARE_COLUMNS = {
    "reports": {
        "share_token": ("share_token", "TEXT", 0, None, 0),
        "shared_at": ("shared_at", "TEXT", 0, None, 0),
    },
}
REPORT_SHARE_INDEXES = {
    "idx_reports_share_token":
        "CREATE UNIQUE INDEX idx_reports_share_token\n"
        "                  ON reports(share_token) WHERE share_token IS NOT NULL",
}
MIGRATION_MANIFEST = {
    (key[0], 43, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "reports": {
                **manifest["columns"].get("reports", {}),
                **REPORT_SHARE_COLUMNS["reports"],
            },
        },
        "indexes": {**manifest["indexes"], **REPORT_SHARE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(42, 43)] = {
    "tables": {},
    "columns": REPORT_SHARE_COLUMNS,
    "indexes": REPORT_SHARE_INDEXES,
    "triggers": {},
    "views": {},
}


# v44: optional generated-question vectors. The generated question is an
# address into its original chunk, never evidence; a nullable timestamp on the
# chunk records successful processing even when the model returned no question.
CHUNK_QUESTION_TABLES = {
    "chunk_questions": """CREATE TABLE chunk_questions (
                  id TEXT PRIMARY KEY,
                  chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  question TEXT NOT NULL,
                  vector BLOB NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(chunk_id, question)
                )""",
}
CHUNK_QUESTION_COLUMNS = {
    "chunks": {
        "question_indexed_at": ("question_indexed_at", "TEXT", 0, None, 0),
    },
}
CHUNK_QUESTION_INDEXES = {
    "idx_chunk_questions_nb":
        "CREATE INDEX idx_chunk_questions_nb\n"
        "                  ON chunk_questions(notebook_id, id)",
    "idx_chunk_questions_source":
        "CREATE INDEX idx_chunk_questions_source\n"
        "                  ON chunk_questions(source_id, id)",
}
MIGRATION_MANIFEST = {
    (key[0], 44, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **CHUNK_QUESTION_TABLES},
        "columns": {
            **manifest["columns"],
            "chunks": {
                **manifest["columns"].get("chunks", {}),
                **CHUNK_QUESTION_COLUMNS["chunks"],
            },
        },
        "indexes": {**manifest["indexes"], **CHUNK_QUESTION_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(43, 44)] = {
    "tables": CHUNK_QUESTION_TABLES,
    "columns": CHUNK_QUESTION_COLUMNS,
    "indexes": CHUNK_QUESTION_INDEXES,
    "triggers": {},
    "views": {},
}


# v45: per-user interface mode preference ("auto" | "advanced"). One nullable
# column on an existing table, no new indexes/tables.
UI_MODE_COLUMNS = {
    "user_profiles": {
        "ui_mode": ("ui_mode", "TEXT", 0, None, 0),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 45, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "user_profiles": {
                **manifest["columns"].get("user_profiles", {}),
                **UI_MODE_COLUMNS["user_profiles"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(44, 45)] = {
    "tables": {},
    "columns": UI_MODE_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v46: element -> chunk reverse index, its restartable offline backfill ledger,
# and the per-notebook marker that forks the read path.  Migration only creates
# the empty tables; historical rows are projected by the explicit offline
# `backfill-chunk-elements` CLI.
CHUNK_ELEMENT_TABLES = {
    "chunk_elements":
        "CREATE TABLE chunk_elements (\n"
        "                  notebook_id TEXT NOT NULL,\n"
        "                  element_id TEXT NOT NULL,\n"
        "                  chunk_id TEXT NOT NULL\n"
        "                    REFERENCES chunks(id) ON DELETE CASCADE,\n"
        "                  PRIMARY KEY (notebook_id, element_id, chunk_id)\n"
        "                )",
    "chunk_element_backfills":
        "CREATE TABLE chunk_element_backfills (\n"
        "                  notebook_id TEXT NOT NULL PRIMARY KEY\n"
        "                    REFERENCES notebooks(id) ON DELETE CASCADE,\n"
        "                  kg_mutation_seq INTEGER NOT NULL DEFAULT 0,\n"
        "                  status TEXT NOT NULL DEFAULT 'running'\n"
        "                    CHECK(status IN ('running','complete','failed')),\n"
        "                  after_chunk_id TEXT NOT NULL DEFAULT '',\n"
        "                  total_chunks INTEGER NOT NULL DEFAULT 0,\n"
        "                  chunks_scanned INTEGER NOT NULL DEFAULT 0,\n"
        "                  rows_written INTEGER NOT NULL DEFAULT 0,\n"
        "                  failure_code TEXT NOT NULL DEFAULT '',\n"
        "                  created_at TEXT NOT NULL,\n"
        "                  updated_at TEXT NOT NULL,\n"
        "                  completed_at TEXT NOT NULL DEFAULT ''\n"
        "                )",
}
CHUNK_ELEMENT_INDEXES = {
    "idx_chunk_elements_chunk":
        "CREATE INDEX idx_chunk_elements_chunk\n"
        "                  ON chunk_elements(chunk_id)",
    "idx_chunk_element_backfills_status":
        "CREATE INDEX idx_chunk_element_backfills_status\n"
        "                  ON chunk_element_backfills(status, notebook_id)",
}
CHUNK_ELEMENT_COLUMNS = {
    "unified_kg_state": {
        "chunk_elements_indexed": (
            "chunk_elements_indexed", "INTEGER", 1, "0", 0
        ),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 46, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **CHUNK_ELEMENT_TABLES},
        "columns": {
            **manifest["columns"],
            "unified_kg_state": {
                **manifest["columns"].get("unified_kg_state", {}),
                **CHUNK_ELEMENT_COLUMNS["unified_kg_state"],
            },
        },
        "indexes": {**manifest["indexes"], **CHUNK_ELEMENT_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(45, 46)] = {
    "tables": CHUNK_ELEMENT_TABLES,
    "columns": CHUNK_ELEMENT_COLUMNS,
    "indexes": CHUNK_ELEMENT_INDEXES,
    "triggers": {},
    "views": {},
}


# v47: notebook-owned schema overrides/custom definitions. Historical induced
# rows with a notebook_id are relocated from the global baseline table by the
# migration; the schema manifest records the new durable surface.
NOTEBOOK_OBJECT_SCHEMA_TABLES = {
    "notebook_object_schemas":
        "CREATE TABLE notebook_object_schemas (\n"
        "                  notebook_id TEXT NOT NULL\n"
        "                    REFERENCES notebooks(id) ON DELETE CASCADE,\n"
        "                  object_type TEXT NOT NULL,\n"
        "                  plural TEXT NOT NULL DEFAULT '',\n"
        "                  fields TEXT NOT NULL DEFAULT '[]',\n"
        "                  primary_field TEXT NOT NULL DEFAULT '',\n"
        "                  description TEXT NOT NULL DEFAULT '',\n"
        "                  label TEXT NOT NULL DEFAULT '',\n"
        "                  list_fields TEXT NOT NULL DEFAULT '[]',\n"
        "                  source TEXT NOT NULL DEFAULT 'custom',\n"
        "                  status TEXT NOT NULL DEFAULT 'active'\n"
        "                    CHECK(status IN ('active','proposed','disabled')),\n"
        "                  rationale TEXT NOT NULL DEFAULT '',\n"
        "                  created_by TEXT NOT NULL DEFAULT '',\n"
        "                  created_at TEXT NOT NULL,\n"
        "                  updated_at TEXT NOT NULL,\n"
        "                  PRIMARY KEY (notebook_id, object_type)\n"
        "                )",
}
NOTEBOOK_OBJECT_SCHEMA_INDEXES = {
    "idx_notebook_object_schemas_status":
        "CREATE INDEX idx_notebook_object_schemas_status\n"
        "                  ON notebook_object_schemas(notebook_id, status, object_type)",
}
MIGRATION_MANIFEST = {
    (key[0], 47, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **NOTEBOOK_OBJECT_SCHEMA_TABLES},
        "indexes": {**manifest["indexes"], **NOTEBOOK_OBJECT_SCHEMA_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(46, 47)] = {
    "tables": NOTEBOOK_OBJECT_SCHEMA_TABLES,
    "columns": {},
    "indexes": NOTEBOOK_OBJECT_SCHEMA_INDEXES,
    "triggers": {},
    "views": {},
}


# v48: sources.agent_profile_id — "an Agent, not a person, added this source".
# One nullable column on an existing table; no new tables, indexes or triggers,
# and no backfill (NULL is exactly what every deployed row already means). Same
# nested-merge shape as the v27 sources.chunked_at layer: merge into each
# manifest's ``columns["sources"]`` dict rather than overwriting it, so the
# memory_id / chunked_at columns the earlier hops carry survive.
SOURCE_AGENT_PROVENANCE_COLUMNS = {
    "sources": {
        "agent_profile_id": ("agent_profile_id", "TEXT", 0, None, 0),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 48, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "sources": {
                **manifest["columns"].get("sources", {}),
                **SOURCE_AGENT_PROVENANCE_COLUMNS["sources"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(47, 48)] = {
    "tables": {},
    "columns": SOURCE_AGENT_PROVENANCE_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v49: group knowledge sharing P1 (zero behavior change — no reader or writer
# anywhere in the codebase consumes these tables yet). Same cascade as every
# other hop: broadcast onto every prior hop key rebased to v49, then register
# the explicit (48, 49) single hop. SQL text matches sqlite_master storage
# verbatim (SQLite strips "IF NOT EXISTS" and keeps the source indentation).
GROUP_SHARING_TABLES = {
    "groups": """CREATE TABLE groups (
                  id          TEXT NOT NULL PRIMARY KEY,
                  name        TEXT NOT NULL,
                  kind        TEXT NOT NULL DEFAULT 'project',
                  description TEXT NOT NULL DEFAULT '',
                  created_by  TEXT REFERENCES users(id),
                  created_at  TEXT NOT NULL,
                  updated_at  TEXT NOT NULL
                , owner_id TEXT NOT NULL DEFAULT '', invite_token TEXT, invite_created_at TEXT, invite_created_by TEXT)""",
    "group_members": """CREATE TABLE group_members (
                  group_id  TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                  user_id   TEXT NOT NULL REFERENCES users(id),
                  role      TEXT NOT NULL DEFAULT 'member',
                  added_at  TEXT NOT NULL,
                  added_by  TEXT REFERENCES users(id),
                  PRIMARY KEY (group_id, user_id)
                )""",
    "notebook_grants": """CREATE TABLE notebook_grants (
                  id             TEXT NOT NULL PRIMARY KEY,
                  notebook_id    TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  principal_type TEXT NOT NULL,
                  principal_id   TEXT NOT NULL DEFAULT '',
                  role           TEXT NOT NULL,
                  created_by     TEXT REFERENCES users(id),
                  created_at     TEXT NOT NULL,
                  UNIQUE (notebook_id, principal_type, principal_id)
                )""",
}
GROUP_SHARING_INDEXES = {
    "idx_group_members_user":
        "CREATE INDEX idx_group_members_user ON group_members(user_id)",
    "idx_notebook_grants_principal":
        "CREATE INDEX idx_notebook_grants_principal "
        "ON notebook_grants(principal_type, principal_id)",
}
MIGRATION_MANIFEST = {
    (key[0], 49, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **GROUP_SHARING_TABLES},
        "indexes": {**manifest["indexes"], **GROUP_SHARING_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(48, 49)] = {
    "tables": GROUP_SHARING_TABLES,
    "columns": {},
    "indexes": GROUP_SHARING_INDEXES,
    "triggers": {},
    "views": {},
}


# v50: group knowledge sharing P2 (zero behavior change — no reader or writer
# anywhere in the codebase consumes this table yet). Same cascade as every
# other hop: broadcast onto every prior hop key rebased to v50, then register
# the explicit (49, 50) single hop. SQL text matches sqlite_master storage
# verbatim (SQLite strips "IF NOT EXISTS" and keeps the source indentation).
SHARE_REQUEST_TABLES = {
    "notebook_share_requests": """CREATE TABLE notebook_share_requests (
                  id           TEXT NOT NULL PRIMARY KEY,
                  notebook_id  TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  group_id     TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                  requested_by TEXT NOT NULL REFERENCES users(id),
                  status       TEXT NOT NULL DEFAULT 'pending',
                  decided_by   TEXT REFERENCES users(id),
                  decided_at   TEXT,
                  created_at   TEXT NOT NULL
                )""",
}
SHARE_REQUEST_INDEXES = {
    "idx_share_requests_group":
        "CREATE INDEX idx_share_requests_group "
        "ON notebook_share_requests(group_id, status)",
    "uq_share_requests_one_pending":
        "CREATE UNIQUE INDEX uq_share_requests_one_pending\n"
        "                  ON notebook_share_requests(notebook_id, group_id, status) "
        "WHERE status = 'pending'",
}
MIGRATION_MANIFEST = {
    (key[0], 50, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **SHARE_REQUEST_TABLES},
        "indexes": {**manifest["indexes"], **SHARE_REQUEST_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(49, 50)] = {
    "tables": SHARE_REQUEST_TABLES,
    "columns": {},
    "indexes": SHARE_REQUEST_INDEXES,
    "triggers": {},
    "views": {},
}


# v51: Agentic Memory P1 (zero behavior change — no reader or writer anywhere in
# the codebase consumes these two tables yet). Same cascade as every other hop:
# broadcast onto every prior hop key rebased to v51, then register the explicit
# (50, 51) single hop. SQL text matches sqlite_master storage verbatim (SQLite
# strips "IF NOT EXISTS" and keeps the source indentation). The migration
# deliberately creates no index, so there is no AGENT_PROFILE_INDEXES content to
# broadcast — the empty dict is spelled out rather than omitted so a later
# reader can see the absence was decided (the primary keys cover every read
# path).
AGENT_PROFILE_TABLES = {
    "agent_notebook_profile": """CREATE TABLE agent_notebook_profile (
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
                )""",
    "agent_profile_jobs": """CREATE TABLE agent_profile_jobs (
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
                )""",
}
AGENT_PROFILE_INDEXES: dict[str, str] = {}
MIGRATION_MANIFEST = {
    (key[0], 51, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **AGENT_PROFILE_TABLES},
        "indexes": {**manifest["indexes"], **AGENT_PROFILE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(50, 51)] = {
    "tables": AGENT_PROFILE_TABLES,
    "columns": {},
    "indexes": AGENT_PROFILE_INDEXES,
    "triggers": {},
    "views": {},
}


# v52: per-conversation public share tokens + a read watermark (T1 of
# question-answer session sharing). Same shape as v43's per-report share
# token: three nullable/defaulted columns on an existing table plus one
# partial unique index over issued tokens only. Same cascade as every other
# hop: broadcast onto every prior hop key rebased to v52, then register the
# explicit (51, 52) single hop.
CONVERSATION_SHARE_COLUMNS = {
    "conversations": {
        "share_token": ("share_token", "TEXT", 0, "NULL", 0),
        "shared_through_at": ("shared_through_at", "TEXT", 0, "NULL", 0),
        "shared_through_id": ("shared_through_id", "TEXT", 0, "NULL", 0),
    },
}
CONVERSATION_SHARE_INDEXES = {
    "idx_conversations_share_token":
        "CREATE UNIQUE INDEX idx_conversations_share_token\n"
        "                  ON conversations(share_token) WHERE share_token IS NOT NULL",
}
MIGRATION_MANIFEST = {
    (key[0], 52, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "conversations": {
                **manifest["columns"].get("conversations", {}),
                **CONVERSATION_SHARE_COLUMNS["conversations"],
            },
        },
        "indexes": {**manifest["indexes"], **CONVERSATION_SHARE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(51, 52)] = {
    "tables": {},
    "columns": CONVERSATION_SHARE_COLUMNS,
    "indexes": CONVERSATION_SHARE_INDEXES,
    "triggers": {},
    "views": {},
}


# v53: agent_profile_jobs.claim_token — the consolidation chain's claim
# generation (Agentic Memory P2, closing the registered R4 ABA). One bare
# column ALTER on a table THIS FILE ALREADY MANIFESTS, which is what makes this
# hop different from every other single-column hop (v27 sources.chunked_at, v48
# sources.agent_profile_id): SQLite's ALTER TABLE ADD COLUMN rewrites the stored
# CREATE TABLE text in sqlite_master, splicing the new column in after the LAST
# column definition and before the table constraints. Every rebased (x, 53) key
# with x < 51 sees agent_profile_jobs as a newly ADDED table and compares its
# SQL text byte for byte, so the text registered here has to be the POST-ALTER
# one — hence the table override below alongside the column entry. The (52, 53)
# hop takes the other branch (the table exists on both sides), where the
# verifier defers the changed-text check to the per-column comparison precisely
# because a manifested column ALTER always changes that text.
AGENT_PROFILE_CLAIM_TOKEN_COLUMNS = {
    "agent_profile_jobs": {
        "claim_token": ("claim_token", "TEXT", 1, "''", 0),
    },
}
AGENT_PROFILE_CLAIM_TOKEN_TABLES = {
    "agent_profile_jobs": """CREATE TABLE agent_profile_jobs (
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
                  updated_at    TEXT NOT NULL, claim_token TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY (notebook_id, owner_id)
                )""",
}
MIGRATION_MANIFEST = {
    (key[0], 53, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **AGENT_PROFILE_CLAIM_TOKEN_TABLES},
        "columns": {
            **manifest["columns"],
            "agent_profile_jobs": {
                **manifest["columns"].get("agent_profile_jobs", {}),
                **AGENT_PROFILE_CLAIM_TOKEN_COLUMNS["agent_profile_jobs"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(52, 53)] = {
    "tables": {},
    "columns": AGENT_PROFILE_CLAIM_TOKEN_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v54: retrieval_experiences — Agentic Memory P2's deployment-global
# retrieval-strategy experience library (still zero behavior change at the
# schema hop itself: the table is created here and consumed by the P2 service
# layer). Same cascade as every other new-table hop: broadcast onto every prior
# hop key rebased to v54, then register the explicit (53, 54) single hop. SQL
# text matches sqlite_master storage verbatim (SQLite strips "IF NOT EXISTS"
# and keeps the source indentation). The migration deliberately creates no
# index — the row count is hard-capped and the primary key covers the only
# point lookup — so the empty dict is spelled out rather than omitted, so a
# later reader can see the absence was decided.
RETRIEVAL_EXPERIENCE_TABLES = {
    "retrieval_experiences": """CREATE TABLE retrieval_experiences (
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
                )""",
}
RETRIEVAL_EXPERIENCE_INDEXES: dict[str, str] = {}
MIGRATION_MANIFEST = {
    (key[0], 54, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **RETRIEVAL_EXPERIENCE_TABLES},
        "indexes": {**manifest["indexes"], **RETRIEVAL_EXPERIENCE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(53, 54)] = {
    "tables": RETRIEVAL_EXPERIENCE_TABLES,
    "columns": {},
    "indexes": RETRIEVAL_EXPERIENCE_INDEXES,
    "triggers": {},
    "views": {},
}


# v55: agent_observations (Agentic Memory P3's per-(notebook, owner, agent)
# append-only observation log) + user_profiles.search_profile_json. Both are
# zero behavior change at this schema hop — no reader/writer exists yet.
# Same cascade as every other hop: broadcast onto every prior hop key rebased
# to v55, then register the explicit (54, 55) single hop. SQL text matches
# sqlite_master storage verbatim (SQLite strips "IF NOT EXISTS" and keeps the
# source indentation). Updated by the T2/T6 fix round (still v55 — the
# migration was NOT yet merged/shipped when the round landed, so it was
# amended in place rather than appended as a new hop): `id` gained an
# explicit NOT NULL, and the non-unique `idx_agent_observations_scope`
# covering index was added.
AGENT_OBSERVATION_TABLES = {
    "agent_observations": """CREATE TABLE agent_observations (
                  id                TEXT PRIMARY KEY NOT NULL,
                  notebook_id       TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  owner_id          TEXT NOT NULL DEFAULT '',
                  agent_profile_id  TEXT NOT NULL DEFAULT '',
                  text              TEXT NOT NULL DEFAULT '',
                  client_request_id TEXT,
                  created_at        TEXT NOT NULL
                )""",
}
AGENT_OBSERVATION_INDEXES = {
    "idx_agent_observations_request":
        "CREATE UNIQUE INDEX idx_agent_observations_request\n"
        "                  ON agent_observations(notebook_id, owner_id, agent_profile_id, client_request_id)\n"
        "                  WHERE client_request_id IS NOT NULL",
    # T2/T6 修复轮:非唯一 scope 索引,依据实测数字(见迁移注释)追加,不进
    # shadow unique surface。
    "idx_agent_observations_scope":
        "CREATE INDEX idx_agent_observations_scope\n"
        "                  ON agent_observations(notebook_id, owner_id, created_at, id)",
}
SEARCH_PROFILE_COLUMNS = {
    "user_profiles": {
        "search_profile_json": ("search_profile_json", "TEXT", 0, None, 0),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 55, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **AGENT_OBSERVATION_TABLES},
        "columns": {
            **manifest["columns"],
            "user_profiles": {
                **manifest["columns"].get("user_profiles", {}),
                **SEARCH_PROFILE_COLUMNS["user_profiles"],
            },
        },
        "indexes": {**manifest["indexes"], **AGENT_OBSERVATION_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(54, 55)] = {
    "tables": AGENT_OBSERVATION_TABLES,
    "columns": SEARCH_PROFILE_COLUMNS,
    "indexes": AGENT_OBSERVATION_INDEXES,
    "triggers": {},
    "views": {},
}


# v56: groups.owner_id is the live, transferable ownership pointer. Existing
# groups deterministically inherit created_by; the latter remains immutable
# audit. This is one column only: no new table/index/trigger/view.
GROUP_OWNER_COLUMNS = {
    "groups": {
        "owner_id": ("owner_id", "TEXT", 1, "''", 0),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 56, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "groups": {
                **manifest["columns"].get("groups", {}),
                **GROUP_OWNER_COLUMNS["groups"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(55, 56)] = {
    "tables": {},
    "columns": GROUP_OWNER_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v57: one reusable group invitation capability plus its issue audit. Inactive
# groups keep all three values NULL; the partial unique index only covers live
# tokens so one capability can never resolve to two groups.
GROUP_INVITE_COLUMNS = {
    "groups": {
        "invite_token": ("invite_token", "TEXT", 0, None, 0),
        "invite_created_at": ("invite_created_at", "TEXT", 0, None, 0),
        "invite_created_by": ("invite_created_by", "TEXT", 0, None, 0),
    },
}
GROUP_INVITE_INDEXES = {
    "idx_groups_invite_token":
        "CREATE UNIQUE INDEX idx_groups_invite_token "
        "ON groups(invite_token) WHERE invite_token IS NOT NULL",
}
MIGRATION_MANIFEST = {
    (key[0], 57, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "groups": {
                **manifest["columns"].get("groups", {}),
                **GROUP_INVITE_COLUMNS["groups"],
            },
        },
        "indexes": {**manifest["indexes"], **GROUP_INVITE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(56, 57)] = {
    "tables": {},
    "columns": GROUP_INVITE_COLUMNS,
    "indexes": GROUP_INVITE_INDEXES,
    "triggers": {},
    "views": {},
}


# v58: desired/published notebook indexing-pipeline identity.  These are bare
# columns on existing tables; the migration performs no row rewrite beyond
# SQLite materializing each declared default for historical rows.
INDEXING_PIPELINE_IDENTITY_COLUMNS = {
    "notebooks": {
        "indexing_pipeline": ("indexing_pipeline", "TEXT", 0, None, 0),
        "indexing_pipeline_version": (
            "indexing_pipeline_version", "TEXT", 1, "'builtin.chunk.v1'", 0
        ),
        "indexing_pipeline_generation": (
            "indexing_pipeline_generation", "TEXT", 1, "''", 0
        ),
        "indexing_pipeline_job_id": (
            "indexing_pipeline_job_id", "TEXT", 1, "''", 0
        ),
    },
    "unified_kg_state": {
        "indexing_pipeline_id": ("indexing_pipeline_id", "TEXT", 1, "''", 0),
        "indexing_pipeline_version": (
            "indexing_pipeline_version", "TEXT", 1, "'builtin.chunk.v1'", 0
        ),
    },
    "extraction_runs": {
        "indexing_pipeline_id": ("indexing_pipeline_id", "TEXT", 1, "''", 0),
        "indexing_pipeline_version": (
            "indexing_pipeline_version", "TEXT", 1, "'builtin.chunk.v1'", 0
        ),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 58, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            **{
                table: {
                    **manifest["columns"].get(table, {}),
                    **columns,
                }
                for table, columns in INDEXING_PIPELINE_IDENTITY_COLUMNS.items()
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(57, 58)] = {
    "tables": {},
    "columns": INDEXING_PIPELINE_IDENTITY_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v59: durable unpublished rebuild payloads.  They are empty on migration and
# startup recovery may only delete abandoned rows, so no data normalization is
# admitted here.
INDEXING_PIPELINE_STAGE_TABLES = {
    "indexing_pipeline_stages": """CREATE TABLE indexing_pipeline_stages (
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
                )""",
    "indexing_pipeline_stage_sources": """CREATE TABLE indexing_pipeline_stage_sources (
                  job_id TEXT NOT NULL
                    REFERENCES indexing_pipeline_stages(job_id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','completed','failed')),
                  payload TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (job_id, source_id)
                )""",
}
INDEXING_PIPELINE_STAGE_INDEXES = {
    "idx_indexing_pipeline_stages_notebook":
        "CREATE INDEX idx_indexing_pipeline_stages_notebook\n"
        "                  ON indexing_pipeline_stages(notebook_id)",
    "idx_indexing_pipeline_stage_sources_source":
        "CREATE INDEX idx_indexing_pipeline_stage_sources_source\n"
        "                  ON indexing_pipeline_stage_sources(source_id)",
}
MIGRATION_MANIFEST = {
    (key[0], 59, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **INDEXING_PIPELINE_STAGE_TABLES},
        "indexes": {**manifest["indexes"], **INDEXING_PIPELINE_STAGE_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(58, 59)] = {
    "tables": INDEXING_PIPELINE_STAGE_TABLES,
    "columns": {},
    "indexes": INDEXING_PIPELINE_STAGE_INDEXES,
    "triggers": {},
    "views": {},
}


# v60: agent_observations.kind separates the Agent's own written notes
# ('note', every row that could exist before this hop) from recorded tool
# calls ('call'). One column only: no new table/index/trigger/view — the
# existing (notebook_id, owner_id, created_at, id) index still points both
# reads and the per-kind eviction at one bounded group.
AGENT_OBSERVATION_KIND_COLUMNS = {
    "agent_observations": {
        "kind": ("kind", "TEXT", 1, "'note'", 0),
    },
}
# A baseline older than v55 CREATEs agent_observations at v55 and then ALTERs
# it here, so by the end of that replay the table is an ADDED table whose
# stored SQL no longer matches the v55 literal — the added-table check compares
# final text verbatim and has no per-column fallback (that fallback exists only
# for tables which already existed in the baseline, e.g. user_profiles gaining
# search_profile_json at the same hop). The expected text for every hop that
# ENDS at v60 therefore has to be the post-ALTER rendering: SQLite appends the
# new column immediately before the closing paren, on the line the previous
# column ended, exactly as reproduced below.
AGENT_OBSERVATION_KIND_TABLES = {
    "agent_observations": AGENT_OBSERVATION_TABLES["agent_observations"][:-1]
    + ", kind TEXT NOT NULL DEFAULT 'note')",
}
MIGRATION_MANIFEST = {
    (key[0], 60, *key[2:]): {
        **manifest,
        # Only rewrite the entry when this hop actually carries the table (a
        # replay whose baseline already has agent_observations never adds it,
        # and must not start claiming it does).
        "tables": (
            {**manifest["tables"], **AGENT_OBSERVATION_KIND_TABLES}
            if "agent_observations" in manifest["tables"]
            else manifest["tables"]
        ),
        "columns": {
            **manifest["columns"],
            "agent_observations": {
                **manifest["columns"].get("agent_observations", {}),
                **AGENT_OBSERVATION_KIND_COLUMNS["agent_observations"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(59, 60)] = {
    "tables": {},
    "columns": AGENT_OBSERVATION_KIND_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v61: hot-path fix batch 1 — five previously-missing indexes on pre-existing
# tables (parity with PostgreSQL migrations/0039_hotpath_batch1_indexes.sql;
# the sixth PostgreSQL group, chunks(source_id, ordinal), has no SQLite twin —
# see app/repositories/sqlite/migrations.py's _migration_61 docstring for why).
# No new table, column, trigger, or view. SQL text matches sqlite_master
# storage verbatim (SQLite strips "IF NOT EXISTS" and keeps the source
# indentation, same as every other index-only hop above).
HOTPATH_BATCH1_INDEXES = {
    "idx_clusters_nb_canonical":
        """CREATE INDEX idx_clusters_nb_canonical
                  ON concept_clusters(notebook_id, canonical_id)""",
    "idx_clusters_nb_canonical_name_lower":
        """CREATE INDEX idx_clusters_nb_canonical_name_lower
                  ON concept_clusters(notebook_id, lower(canonical_name))""",
    "idx_extraction_runs_notebook":
        """CREATE INDEX idx_extraction_runs_notebook
                  ON extraction_runs(notebook_id)""",
    "idx_knowledge_source_fact_elements_notebook":
        """CREATE INDEX idx_knowledge_source_fact_elements_notebook
                  ON knowledge_source_fact_elements(notebook_id)""",
    "idx_memory_items_notebook":
        """CREATE INDEX idx_memory_items_notebook
                  ON memory_items(notebook_id)""",
    "idx_knowledge_relations_nb_source_target_edge":
        """CREATE INDEX idx_knowledge_relations_nb_source_target_edge
                  ON knowledge_relations(notebook_id, source_object_id, target_object_id, edge_type)""",
    "idx_sources_nb_hidden_type":
        """CREATE INDEX idx_sources_nb_hidden_type
                  ON sources(notebook_id, source_type)
                  WHERE source_type IN ('memory', 'knowhow')""",
}


# v62: creator-wide question activity can follow its exact normalized
# (created_at DESC, id DESC) keyset order from an index instead of scanning and
# sorting the global ask_jobs table on every page.
ASK_CREATOR_ACTIVITY_INDEXES = {
    "idx_ask_jobs_creator_activity":
        "CREATE INDEX idx_ask_jobs_creator_activity ON ask_jobs(created_by, "
        "COALESCE(julianday(created_at), "
        "julianday('0001-01-01T00:00:00+00:00')) DESC, id DESC)",
}


# v63: extension_runtime_toggles, the deployment-plugin runtime
# enable/disable switch + audit (who, when). No row means enabled, so a
# fresh/untouched deployment behaves exactly as before this table existed.
# No index, trigger, or view — the primary key alone serves both the admin
# page's full listing and the admission-refresh scan.
EXTENSION_RUNTIME_TOGGLES_TABLES = {
    "extension_runtime_toggles": """CREATE TABLE extension_runtime_toggles (
                  plugin_id TEXT NOT NULL PRIMARY KEY,
                  enabled INTEGER NOT NULL,
                  updated_by TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
}
MIGRATION_MANIFEST = {
    (key[0], 61, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **HOTPATH_BATCH1_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(60, 61)] = {
    "tables": {},
    "columns": {},
    "indexes": HOTPATH_BATCH1_INDEXES,
    "triggers": {},
    "views": {},
}
MIGRATION_MANIFEST = {
    (key[0], 62, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **ASK_CREATOR_ACTIVITY_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(61, 62)] = {
    "tables": {},
    "columns": {},
    "indexes": ASK_CREATOR_ACTIVITY_INDEXES,
    "triggers": {},
    "views": {},
}
MIGRATION_MANIFEST = {
    (key[0], 63, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **EXTENSION_RUNTIME_TOGGLES_TABLES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(62, 63)] = {
    "tables": EXTENSION_RUNTIME_TOGGLES_TABLES,
    "columns": {},
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v64: hot-path fix batch 3 -- the concept-detail hub-cluster keyset page's
# ORDER BY member_object_id can follow index order instead of scanning and
# sorting the whole (notebook_id, canonical_id) slice on every later page.
CONCEPT_CLUSTER_KEYSET_INDEXES = {
    "idx_clusters_nb_canonical_member":
        "CREATE INDEX idx_clusters_nb_canonical_member "
        "ON concept_clusters(notebook_id, canonical_id, member_object_id)",
}
MIGRATION_MANIFEST = {
    (key[0], 64, *key[2:]): {
        **manifest,
        "indexes": {**manifest["indexes"], **CONCEPT_CLUSTER_KEYSET_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(63, 64)] = {
    "tables": {},
    "columns": {},
    "indexes": CONCEPT_CLUSTER_KEYSET_INDEXES,
    "triggers": {},
    "views": {},
}


# v65: deletion-independent, content-minimal activity projection. The table
# intentionally has no notebook foreign key; its expires_at is enforced by
# every activity read and pruned by startup/new-archive maintenance.
RETAINED_USER_ACTIVITY_TABLES = {
    "retained_user_activity": """CREATE TABLE retained_user_activity (
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
                )""",
}
RETAINED_USER_ACTIVITY_INDEXES = {
    "idx_retained_activity_actor_type_created":
        """CREATE INDEX idx_retained_activity_actor_type_created
                  ON retained_user_activity(
                    actor_id, activity_type,
                    COALESCE(julianday(created_at),
                      julianday('0001-01-01T00:00:00+00:00')) DESC,
                    record_id DESC
                  )""",
    "idx_retained_activity_owner_created":
        """CREATE INDEX idx_retained_activity_owner_created
                  ON retained_user_activity(
                    notebook_owner_id,
                    COALESCE(julianday(created_at),
                      julianday('0001-01-01T00:00:00+00:00')) DESC,
                    record_id DESC
                  )""",
    "idx_retained_activity_expires":
        """CREATE INDEX idx_retained_activity_expires
                  ON retained_user_activity(julianday(expires_at))""",
    "idx_retained_activity_notebook":
        """CREATE INDEX idx_retained_activity_notebook
                  ON retained_user_activity(notebook_id)""",
}
MIGRATION_MANIFEST = {
    (key[0], 65, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **RETAINED_USER_ACTIVITY_TABLES},
        "indexes": {**manifest["indexes"], **RETAINED_USER_ACTIVITY_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(64, 65)] = {
    "tables": RETAINED_USER_ACTIVITY_TABLES,
    "columns": {},
    "indexes": RETAINED_USER_ACTIVITY_INDEXES,
    "triggers": {},
    "views": {},
}


# v66: visible-source upload actor. Existing visible rows are attributed to
# the notebook owner because no finer historical provenance exists; hidden
# synthetic rows remain NULL and are outside the activity index.
SOURCE_UPLOAD_ACTOR_COLUMNS = {
    "sources": {
        "uploaded_by": ("uploaded_by", "TEXT", 0, None, 0),
    },
}
SOURCE_UPLOAD_ACTOR_INDEXES = {
    "idx_sources_uploaded_by_created":
        "CREATE INDEX idx_sources_uploaded_by_created "
        "ON sources(uploaded_by, created_at, id) "
        "WHERE uploaded_by IS NOT NULL "
        "AND source_type NOT IN ('memory','knowhow')",
}
MIGRATION_MANIFEST = {
    (key[0], 66, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "sources": {
                **manifest["columns"].get("sources", {}),
                **SOURCE_UPLOAD_ACTOR_COLUMNS["sources"],
            },
        },
        "indexes": {**manifest["indexes"], **SOURCE_UPLOAD_ACTOR_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(65, 66)] = {
    "tables": {},
    "columns": SOURCE_UPLOAD_ACTOR_COLUMNS,
    "indexes": SOURCE_UPLOAD_ACTOR_INDEXES,
    "triggers": {},
    "views": {},
}


# v67: global wish wall and its one-vote-per-user relation. Both tables are
# intentionally empty after upgrading an older deployment; only their schema
# and indexes are admitted by this migration manifest.
WISH_WALL_TABLES = {
    "wishes": """CREATE TABLE wishes (
                  id TEXT NOT NULL PRIMARY KEY,
                  kind TEXT NOT NULL,
                  title TEXT NOT NULL,
                  content TEXT NOT NULL,
                  author_id TEXT NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )""",
    "wish_votes": """CREATE TABLE wish_votes (
                  wish_id TEXT NOT NULL REFERENCES wishes(id) ON DELETE CASCADE,
                  user_id TEXT NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  PRIMARY KEY (wish_id, user_id)
                )""",
}
WISH_WALL_INDEXES = {
    "idx_wishes_kind_created":
        "CREATE INDEX idx_wishes_kind_created\n"
        "                  ON wishes(kind, julianday(created_at) DESC, id DESC)",
    "idx_wish_votes_user":
        "CREATE INDEX idx_wish_votes_user\n"
        "                  ON wish_votes(user_id, wish_id)",
}
MIGRATION_MANIFEST = {
    (key[0], 67, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **WISH_WALL_TABLES},
        "indexes": {**manifest["indexes"], **WISH_WALL_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(66, 67)] = {
    "tables": WISH_WALL_TABLES,
    "columns": {},
    "indexes": WISH_WALL_INDEXES,
    "triggers": {},
    "views": {},
}


# v68 (batch-3-W1 PR-2): unified_kg_state.kg_reset_epoch -- a persistent,
# monotonically-increasing per-notebook "how many times has this notebook's
# KG been reset to empty" counter, DEFAULT 0. Written by exactly one caller
# (delete_notebook_graph_rows), in the SAME transaction that resets
# kg_mutation_seq back to 0 instead of dropping the row. Pure column
# addition -- no new tables/indexes/triggers/views.
KG_RESET_EPOCH_COLUMNS = {
    "unified_kg_state": {
        "kg_reset_epoch": ("kg_reset_epoch", "INTEGER", 1, "0", 0),
    },
}
MIGRATION_MANIFEST = {
    (key[0], 68, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "unified_kg_state": {
                **manifest["columns"].get("unified_kg_state", {}),
                **KG_RESET_EPOCH_COLUMNS["unified_kg_state"],
            },
        },
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(67, 68)] = {
    "tables": {},
    "columns": KG_RESET_EPOCH_COLUMNS,
    "indexes": {},
    "triggers": {},
    "views": {},
}


# v69 (batch-3-W1 PR-3): three FK/keyset-covering indexes design doc
# Sec 1.4 registers as prerequisites for the delete-jobization work
# (idx_agent_tokens_default_notebook, idx_knowhow_cell_code_column,
# idx_conversations_notebook -- pure additive index creation, no query/
# service change here), plus the two new delete-job carrier tables
# notebook_delete_jobs / notebook_delete_files (Sec T-3/T-4) and their two
# supporting indexes. ``notebook_delete_jobs`` also carries ``lease_token``/
# ``attempts`` (Phase B code review round: owner/lease CAS fencing and a
# bounded failed-job retry policy, added directly to this not-yet-merged
# migration's own CREATE TABLE rather than a follow-up ALTER -- see
# ``sqlite/migrations.py``'s ``_migration_69`` and ``postgres/migrations/
# 0049_notebook_delete_jobs.sql`` for the matching column comments).
NOTEBOOK_DELETE_JOBS_TABLES = {
    "notebook_delete_jobs": (
        "CREATE TABLE notebook_delete_jobs (\n"
        "                  id TEXT NOT NULL PRIMARY KEY,\n"
        "                  notebook_id TEXT NOT NULL,\n"
        "                  status TEXT NOT NULL DEFAULT 'queued',\n"
        "                  phase TEXT NOT NULL DEFAULT 'mark',\n"
        "                  cursor_table TEXT NOT NULL DEFAULT '',\n"
        "                  cursor_key TEXT NOT NULL DEFAULT '',\n"
        "                  deleted_rows INTEGER NOT NULL DEFAULT 0,\n"
        "                  lease_token TEXT NOT NULL DEFAULT '',\n"
        "                  attempts INTEGER NOT NULL DEFAULT 0,\n"
        "                  error_code TEXT NOT NULL DEFAULT '',\n"
        "                  error_message TEXT NOT NULL DEFAULT '',\n"
        "                  created_at TEXT NOT NULL,\n"
        "                  updated_at TEXT NOT NULL,\n"
        "                  finished_at TEXT\n"
        "                )"
    ),
    "notebook_delete_files": (
        "CREATE TABLE notebook_delete_files (\n"
        "                  job_id TEXT NOT NULL,\n"
        "                  ordinal INTEGER NOT NULL,\n"
        "                  file_path TEXT NOT NULL,\n"
        "                  PRIMARY KEY (job_id, ordinal)\n"
        "                )"
    ),
}
NOTEBOOK_DELETE_JOBS_INDEXES = {
    "idx_agent_tokens_default_notebook": (
        "CREATE INDEX idx_agent_tokens_default_notebook\n"
        "                  ON agent_access_tokens(default_notebook_id)"
    ),
    "idx_knowhow_cell_code_column": (
        "CREATE INDEX idx_knowhow_cell_code_column\n"
        "                  ON knowhow_cell_code(column_id)"
    ),
    "idx_conversations_notebook": (
        "CREATE INDEX idx_conversations_notebook\n"
        "                  ON conversations(notebook_id, id)"
    ),
    "idx_notebook_delete_jobs_one_active": (
        "CREATE UNIQUE INDEX idx_notebook_delete_jobs_one_active\n"
        "                  ON notebook_delete_jobs(notebook_id)\n"
        "                  WHERE status IN ('queued', 'running', 'waiting')"
    ),
    "idx_notebook_delete_jobs_status_updated": (
        "CREATE INDEX idx_notebook_delete_jobs_status_updated\n"
        "                  ON notebook_delete_jobs(status, updated_at)"
    ),
}
MIGRATION_MANIFEST = {
    (key[0], 69, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **NOTEBOOK_DELETE_JOBS_TABLES},
        "indexes": {**manifest["indexes"], **NOTEBOOK_DELETE_JOBS_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(68, 69)] = {
    "tables": NOTEBOOK_DELETE_JOBS_TABLES,
    "columns": {},
    "indexes": NOTEBOOK_DELETE_JOBS_INDEXES,
    "triggers": {},
    "views": {},
}


# v70 (Ask submission idempotency key, parity with PostgreSQL 0050): the
# nullable ``ask_jobs.client_request_id`` column plus the partial unique index
# ``idx_ask_jobs_client_request`` over (created_by, client_request_id) WHERE
# client_request_id IS NOT NULL. Same nullable-column + partial-index shape as
# v55's ``agent_observations.client_request_id``; no new table, trigger or
# view, and no backfill (existing rows stay NULL).
ASK_CLIENT_REQUEST_COLUMNS = {
    "ask_jobs": {
        "client_request_id": ("client_request_id", "TEXT", 0, None, 0),
    },
}
ASK_CLIENT_REQUEST_INDEXES = {
    "idx_ask_jobs_client_request": (
        "CREATE UNIQUE INDEX idx_ask_jobs_client_request "
        "ON ask_jobs(created_by, client_request_id) "
        "WHERE client_request_id IS NOT NULL"
    ),
}
MIGRATION_MANIFEST = {
    (key[0], 70, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            "ask_jobs": {
                **manifest["columns"].get("ask_jobs", {}),
                **ASK_CLIENT_REQUEST_COLUMNS["ask_jobs"],
            },
        },
        "indexes": {**manifest["indexes"], **ASK_CLIENT_REQUEST_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(69, 70)] = {
    "tables": {},
    "columns": ASK_CLIENT_REQUEST_COLUMNS,
    "indexes": ASK_CLIENT_REQUEST_INDEXES,
    "triggers": {},
    "views": {},
}


# v71 (batch-3-W2 PR-1): generational cluster/community swap schema half,
# parity with PostgreSQL 0051_derived_generation.sql. Row-level `generation`
# on the three derived-graph tables, the generational control block on
# unified_kg_state, and the three-index rework: the four-column unique
# REPLACES v29's uq_clusters_notebook_type_member (the three-column unique
# physically forbids two generations coexisting), and the two covering
# replacements carry generation as a trailing key (SQLite has no INCLUDE).
# Index SQL text matches sqlite_master storage verbatim (SQLite strips
# "IF NOT EXISTS" and keeps the source indentation). The three superseded
# indexes are the FIRST manifested index retirements -- carried in the new
# "dropped_indexes" allowlist the schema-diff loop honours; any drop outside
# the allowlist still fails closed.
DERIVED_GENERATION_COLUMNS = {
    "concept_clusters": {
        "generation": ("generation", "INTEGER", 1, "0", 0),
    },
    "communities": {
        "generation": ("generation", "INTEGER", 1, "0", 0),
    },
    "community_members": {
        "generation": ("generation", "INTEGER", 1, "0", 0),
    },
    "unified_kg_state": {
        "cluster_generation": ("cluster_generation", "INTEGER", 1, "0", 0),
        "community_generation": ("community_generation", "INTEGER", 1, "0", 0),
        "derived_generation_counter": (
            "derived_generation_counter", "INTEGER", 1, "0", 0),
        "derived_building_generation": (
            "derived_building_generation", "INTEGER", 1, "0", 0),
        "derived_building_claimed_at": (
            "derived_building_claimed_at", "TEXT", 0, None, 0),
        "derived_catchup_from": ("derived_catchup_from", "TEXT", 0, None, 0),
    },
}
DERIVED_GENERATION_INDEXES = {
    "uq_clusters_nb_type_member_generation": (
        "CREATE UNIQUE INDEX uq_clusters_nb_type_member_generation\n"
        "                  ON concept_clusters(notebook_id, object_type, member_object_id, generation)"
    ),
    "idx_clusters_nb_canonical_member_gen": (
        "CREATE INDEX idx_clusters_nb_canonical_member_gen\n"
        "                  ON concept_clusters(notebook_id, canonical_id, member_object_id, generation)"
    ),
    "idx_clusters_nb_created_gen": (
        "CREATE INDEX idx_clusters_nb_created_gen\n"
        "                  ON concept_clusters(notebook_id, created_at, generation)"
    ),
}
DERIVED_GENERATION_DROPPED_INDEXES = frozenset(
    {
        "uq_clusters_notebook_type_member",
        "idx_clusters_nb_canonical_member",
        "idx_clusters_nb_created",
    }
)
MIGRATION_MANIFEST = {
    (key[0], 71, *key[2:]): {
        **manifest,
        "columns": {
            **manifest["columns"],
            **{
                table: {
                    **manifest["columns"].get(table, {}),
                    **columns,
                }
                for table, columns in DERIVED_GENERATION_COLUMNS.items()
            },
        },
        # 旧跳里被 v71 退役的索引(v29 的唯一索引、v64 的覆盖索引)在链上是
        # 「建了又删」——终态不存在,必须同时从「期望新增」里剔除,否则
        # manifest-addition-missing 反向失败。
        "indexes": {
            name: sql
            for name, sql in {
                **manifest["indexes"], **DERIVED_GENERATION_INDEXES
            }.items()
            if name not in DERIVED_GENERATION_DROPPED_INDEXES
        },
        "dropped_indexes": (
            frozenset(manifest.get("dropped_indexes", frozenset()))
            | DERIVED_GENERATION_DROPPED_INDEXES
        ),
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(70, 71)] = {
    "tables": {},
    "columns": DERIVED_GENERATION_COLUMNS,
    "indexes": DERIVED_GENERATION_INDEXES,
    "dropped_indexes": DERIVED_GENERATION_DROPPED_INDEXES,
    "triggers": {},
    "views": {},
}


if __name__ == "__main__":
    raise SystemExit(main())
