-- Batch 3 · W1 · PR-3 (Phase A): tombstone + phases 0/1/2/5 of the six-phase
-- delete-jobization job, plus the three FK/keyset indexes design doc Sec 1.4
-- registers for this work (originally slated for "0047" in the design doc's
-- own numbering, but 0047 was taken by PR-2's kg_reset_epoch column -- see
-- that migration's own header comment, which explicitly defers these three
-- indexes to this migration). Design doc:
-- docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-design_zh.md,
-- Sec 1.4 (indexes), Sec T-2 (tombstone), Sec T-3 (six-phase job), Sec T-4
-- (job carrier + delete pool).
--
-- ============================================================================
-- Part 1: three FK/keyset-covering indexes (Sec 1.4). Each one is a
-- PREREQUISITE for a specific step of this design turning from a sequential
-- scan into an index scan -- not a generic "optimization":
--
--   idx_agent_tokens_default_notebook ON agent_access_tokens(default_notebook_id)
--     Phase 5's `DELETE FROM notebooks` FK-cascade probe. Sec 1.1: this is
--     the ONLY one of the 47 L1 FK constraints with no leading index on its
--     referencing column today (agent_access_tokens' only index is
--     idx_agent_tokens_profile ON (agent_profile_id, revoked_at, expires_at)
--     -- 0003_core_indexes.sql), so every notebook delete pays a full
--     sequential scan of this table to find rows to cascade.
--
--   idx_knowhow_cell_code_column ON knowhow_cell_code(column_id)
--     Sec 1.3's B-class knowhow chain's column_id leg (Phase B's batched
--     table cleanup walks this table by column_id when clearing a page of
--     knowhow_columns). knowhow_cell_code today only has
--     idx_knowhow_cell_code_row ON (row_id) -- 0005_memory_knowhow_
--     governance_indexes.sql:2; its sibling knowhow_cells already has both
--     legs indexed (0005:3,4). Without this index the column_id leg of the
--     B-class chain degrades to a full table scan per page.
--
--   idx_conversations_notebook ON conversations(notebook_id, id)
--     The closure-external cleanup table `conversations` (Sec 1.3's
--     "closure-external" list) has no notebook_id-leading index today (only
--     created_by -- 0003:13 -- and share_token -- 0030:49). Sec 1.5's
--     form-two (ctid) batch-delete loop requires a notebook_id-leading index
--     on every table it targets or its inner `LIMIT n` rescans from block 0
--     every batch (O(N^2) -- Sec 1.5's "前置条件" paragraph). Phase A does
--     not yet exercise this loop (Phase B does), but the index is added now
--     alongside its two siblings so Phase B needs no further migration.
--
-- All three are plain (non-partial) single-purpose btree indexes over
-- already-COLLATE-"C" text columns (0001_initial.sql), so -- exactly like
-- migration 0043's idx_clusters_nb_canonical_member -- a bare (unqualified)
-- btree index inherits that collation on every key with no explicit
-- COLLATE/opclass qualifier needed in the DDL. None carries any of migration
-- 0042's GIN-specific concerns (fastupdate, multi-minute builds, double-
-- digit-GB footprint): three narrow btree indexes over already-indexed-style
-- FK/lookup columns build in seconds even at this schema's largest table
-- sizes.
--
-- Write-amplification: registered, not mitigated -- each is one more btree
-- entry maintained per INSERT/UPDATE-of-key on its table; all three tables
-- already carry comparable indexes, so this is in-line with their existing
-- write cost, not a new order of magnitude.
--
-- Rollback (any/all): `DROP INDEX CONCURRENTLY <name>;` -- purely additive,
-- every consuming query still runs (just slower -- a sequential scan on
-- agent_access_tokens/knowhow_cell_code, an O(N^2) batch loop on
-- conversations once Phase B lands) with the index absent.
--
-- Relationship to the offline CONCURRENTLY builder
-- (scripts/build_hotpath_indexes.py): same shared advisory-locked
-- builder/inspector as migrations 0039/0042/0043
-- (app/repositories/postgres/hotpath_indexes.py's HOTPATH_INDEX_SPECS, which
-- now carries all fourteen indexes across four batches). On a database with
-- pre-existing production traffic, an operator runs that script's --apply
-- mode FIRST, online, with CREATE INDEX CONCURRENTLY (this migration runner
-- executes every migration inside a transaction, and CONCURRENTLY cannot run
-- inside one). On a fresh deploy this migration's IF NOT EXISTS clauses are
-- sufficient by themselves.
--
-- Pre-existing same-named index validation (codex #636 R1 P2, same pattern
-- as migrations 0042/0043's own DO blocks -- see 0043's header comment for
-- the full rationale). backend/tests/test_hotpath_indexes_batch4.py
-- re-derives every expected value below from HOTPATH_INDEX_SPECS and asserts
-- it appears verbatim in this file.
DO $$
DECLARE
  rec record;
  existing record;
  actual_keys text[];
  actual_opclasses text[];
  actual_collations text[];
BEGIN
  FOR rec IN
    SELECT * FROM (VALUES
      ('idx_agent_tokens_default_notebook',
       'agent_access_tokens',
       'btree',
       ARRAY['default_notebook_id'],
       ARRAY['pg_catalog:text_ops'],
       ARRAY['pg_catalog:C']),
      ('idx_knowhow_cell_code_column',
       'knowhow_cell_code',
       'btree',
       ARRAY['column_id'],
       ARRAY['pg_catalog:text_ops'],
       ARRAY['pg_catalog:C']),
      ('idx_conversations_notebook',
       'conversations',
       'btree',
       ARRAY['notebook_id', 'id'],
       ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops'],
       ARRAY['pg_catalog:C', 'pg_catalog:C'])
    ) AS v(index_name, expected_table, expected_am, expected_keys,
           expected_opclasses, expected_collations)
  LOOP
    SELECT i.indexrelid, i.indisvalid, i.indisready, i.indisunique,
           i.indnkeyatts, i.indnatts,
           i.indclass::oid[] AS opclass_oids,
           i.indcollation::oid[] AS collation_oids,
           am.amname,
           tbl.relname AS table_name
    INTO existing
    FROM pg_index i
    JOIN pg_class idx ON idx.oid = i.indexrelid
    JOIN pg_class tbl ON tbl.oid = i.indrelid
    JOIN pg_am am ON am.oid = idx.relam
    JOIN pg_namespace ns ON ns.oid = idx.relnamespace
    WHERE ns.nspname = current_schema() AND idx.relname = rec.index_name;
    IF NOT FOUND THEN
      CONTINUE;
    END IF;
    IF NOT existing.indisvalid OR NOT existing.indisready THEN
      RAISE EXCEPTION 'hotpath batch 4: pre-existing index % is INVALID (an interrupted CONCURRENTLY build left it unusable); run DROP INDEX CONCURRENTLY %, rerun scripts/build_hotpath_indexes.py --apply, then migrate again',
        rec.index_name, rec.index_name;
    END IF;
    actual_keys := ARRAY(
      SELECT btrim(regexp_replace(replace(
               lower(pg_get_indexdef(existing.indexrelid, n, true)),
               '::text', ''), '\s+', ' ', 'g'))
      FROM generate_series(1, existing.indnkeyatts) AS n ORDER BY n);
    actual_opclasses := ARRAY(
      SELECT opc_ns.nspname || ':' || opc.opcname
      FROM unnest(existing.opclass_oids) WITH ORDINALITY op(oid, ord)
      JOIN pg_opclass opc ON opc.oid = op.oid
      JOIN pg_namespace opc_ns ON opc_ns.oid = opc.opcnamespace
      ORDER BY op.ord);
    actual_collations := ARRAY(
      SELECT COALESCE(coll_ns.nspname || ':' || coll.collname, '')
      FROM unnest(existing.collation_oids) WITH ORDINALITY co(oid, ord)
      LEFT JOIN pg_collation coll ON coll.oid = co.oid
      LEFT JOIN pg_namespace coll_ns ON coll_ns.oid = coll.collnamespace
      ORDER BY co.ord);
    IF existing.table_name <> rec.expected_table
       OR existing.amname <> rec.expected_am
       OR existing.indisunique
       OR existing.indnkeyatts <> array_length(rec.expected_keys, 1)
       OR existing.indnatts <> array_length(rec.expected_keys, 1)
       OR actual_keys <> rec.expected_keys
       OR actual_opclasses <> rec.expected_opclasses
       OR actual_collations <> rec.expected_collations THEN
      RAISE EXCEPTION 'hotpath batch 4: pre-existing index % does not match the expected definition; resolve the name collision manually, then migrate again',
        rec.index_name;
    END IF;
  END LOOP;
END
$$;

CREATE INDEX IF NOT EXISTS idx_agent_tokens_default_notebook
  ON agent_access_tokens(default_notebook_id);

CREATE INDEX IF NOT EXISTS idx_knowhow_cell_code_column
  ON knowhow_cell_code(column_id);

CREATE INDEX IF NOT EXISTS idx_conversations_notebook
  ON conversations(notebook_id, id);

-- ============================================================================
-- Part 2: the delete-job carrier tables (Sec T-3/T-4).
--
-- notebook_delete_jobs shape mirrors kg_build_jobs (0001_initial.sql:234-252)
-- deliberately -- same durable-job idiom (status/phase/cursor/error columns,
-- a partial unique index for single-flight) the rest of this schema already
-- uses for background work. Columns:
--   status        'queued'|'running'|'waiting'|'failed' -- no 'succeeded':
--                 phase 5 (finalize) deletes this row itself in the SAME
--                 transaction that deletes the notebooks row, so a
--                 successful job leaves no row behind to read (Sec T-4:
--                 "不需要去重表" / no dedup table needed).
--   phase         'mark'|'paths'|'quiesce'|'rows'|'files'|'finalize' -- the
--                 six phases of Sec T-3's table. Phase A only performs real
--                 work for mark/paths/quiesce/finalize; rows/files are
--                 no-op placeholders in Phase A (see notebook_delete.py's
--                 own module docstring) that Phase B replaces with the
--                 batched table cleanup and disk-artifact sweep.
--   cursor_table / cursor_key   resumption position within the current
--                 phase (e.g. phase 'paths' uses cursor_key for the
--                 sources.id keyset cursor). Phase A only exercises this for
--                 phase 'paths'; cursor_table is reserved for Phase B's
--                 per-table batch cursor.
--   deleted_rows  running counter, Phase B's batched cleanup increments it
--                 per page (every `advance_phase` call from phase 3/4 passes
--                 a nonzero `deleted_delta`; phase 2's quiesce heartbeat and
--                 phase 1's path materialization pass 0, leaving it
--                 unchanged) -- an operator-visible progress signal, not a
--                 correctness input to any phase.
--   lease_token   Phase B follow-up (P2-a code review round): a fencing
--                 token minted fresh by every successful `mark_running`
--                 (both the initial CAS from 'queued'/'waiting' AND a
--                 sweep-driver-A steal of a 'running'-but-stale row -- see
--                 that method's docstring). Every write this job's OWN
--                 in-flight run() issues after that point carries the SAME
--                 token in its WHERE clause, so a worker that has been
--                 superseded (its row stolen out from under it by a second
--                 resubmission) writes nothing further even if it is only
--                 slow, not actually dead, and keeps executing after losing
--                 ownership.
--   attempts      Phase B follow-up (P1-E code review round): incremented
--                 by `finish(..., 'failed', ...)` (never by a successful
--                 path -- a successful finalize deletes this row, so
--                 'attempts' never needs to reach a reader for it). Sweep
--                 driver B's `recreate_for_deleting_notebook` and driver A's
--                 stale-job resume both consult it to apply exponential
--                 backoff and an attempt ceiling -- see
--                 `services/notebook_delete.py`'s own constants for the
--                 exact policy (mirrors the design doc's registered "give a
--                 chronically-failing job a bounded retry policy, not
--                 unbounded ticks" follow-up).
--
-- Deliberately NO foreign key to notebooks (Sec T-3's own explicit
-- rationale, reproduced here because it is easy to "fix" by accident): the
-- sweep's driver-A special case ("job row present, notebooks row absent" --
-- reachable only via an out-of-band notebooks-row deletion: a legacy
-- unbounded delete_notebook() call, a sweep_stale_copies() misfire, or a
-- manual DBA delete) needs that STATE to be representable in the data at
-- all. An FK CASCADE would make it unrepresentable -- an out-of-band
-- notebooks-row delete would silently erase the job row (and with it, the
-- only durable record that cleanup was incomplete) along with it, leaving
-- orphan child-table rows and leaked disk artifacts with no trace that
-- anything is still owed.
CREATE TABLE notebook_delete_jobs (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  status text COLLATE "C" NOT NULL DEFAULT 'queued',
  phase text COLLATE "C" NOT NULL DEFAULT 'mark',
  cursor_table text COLLATE "C" NOT NULL DEFAULT '',
  cursor_key text COLLATE "C" NOT NULL DEFAULT '',
  deleted_rows bigint NOT NULL DEFAULT 0,
  lease_token text COLLATE "C" NOT NULL DEFAULT '',
  attempts bigint NOT NULL DEFAULT 0,
  error_code text COLLATE "C" NOT NULL DEFAULT '',
  error_message text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL,
  finished_at timestamp with time zone,
  CONSTRAINT pk_notebook_delete_jobs PRIMARY KEY (id)
);

-- Single-flight defense-in-depth (Sec 4.1's mutex matrix: "另一次删除同库 |
-- 无 | 单飞 | notebooks CAS + 作业表部分唯一索引"). The PRIMARY mechanism is
-- the tombstone CAS on notebooks.status (a second concurrent DELETE request
-- loses the CAS and never reaches the INSERT below); this index guards the
-- data shape itself against any future direct-insert bypass of that CAS,
-- exactly like idx_kg_build_jobs_one_running guards kg_build_jobs
-- (0002_integrity_indexes.sql:22) against a caller that skips
-- KgBuildJobStore.create_job's own UniqueViolation handling.
CREATE UNIQUE INDEX idx_notebook_delete_jobs_one_active
  ON notebook_delete_jobs(notebook_id)
  WHERE status IN ('queued', 'running', 'waiting');

-- Sweep driver A ("stale active job rows") scans by status+updated_at.
CREATE INDEX idx_notebook_delete_jobs_status_updated
  ON notebook_delete_jobs(status, updated_at);

-- Phase 1 ("paths") materializes the full `sources.file_path` set for one
-- job before any sources row can be deleted (Sec T-3.1's "崩溃不安全"
-- rejection of the alternative "delete each page's files as that page's
-- sources rows are deleted" design -- a mid-page crash there loses that
-- page's paths permanently once the DELETE already committed). ordinal is a
-- per-job monotonic sequence (not tied to source id) so a resumed
-- materialization pass can resume by `MAX(ordinal)+1` regardless of the
-- underlying sources.id keyset cursor shape.
CREATE TABLE notebook_delete_files (
  job_id text COLLATE "C" NOT NULL,
  ordinal bigint NOT NULL,
  file_path text COLLATE "C" NOT NULL,
  CONSTRAINT pk_notebook_delete_files PRIMARY KEY (job_id, ordinal)
);
