-- Auto user-memory suggestions (design
-- docs/superpowers/specs/2026-10-28-auto-user-memory-design_zh.md section 10,
-- plan G1/G2/G21). Mirrors SQLite v106 / _migration_106.
--
-- memory_items.suggested_signal marks a system suggestion: the signal class
-- that produced it ('clarified' / 'repeated'; 'corrected' later), NULL for
-- every other memory. A suggestion keeps origin = 'ask_answer', so the origin
-- CHECK is untouched and no table is rebuilt (decision D5(b)); the value domain
-- is an application Literal, so the column carries no CHECK either. Existing
-- rows read NULL: zero backfill.
--
-- Two partial indexes, both empty until a user opts in:
--   idx_memory_suggest_unadopted -- "unadopted suggestions" (suggested_signal
--     IS NOT NULL AND status IN ('candidate','rejected')), the subtrahend of
--     every "your memories" count (original count minus this, in one
--     statement) and the backlog gate; its predicate is the memory_sql
--     unadopted_suggestion fragment, spelled out here because a migration
--     must not change meaning when the live fragment does;
--   idx_memory_suggest_expiry -- open suggestions by age for the 30-day
--     expiry sweep.
--
-- memory_suggest_users: one row per user who ever touched the switch. The
-- ask-completed observer reads it by primary key only (a default user has no
-- row: one PK miss, zero writes). quota_day/quota_used are the per-user UTC
-- daily model-call budget, reserved by one conditional UPDATE.
--
-- memory_suggest_jobs: one single-flight row per (notebook, owner), the
-- agent_profile_jobs protocol (status + claim_token generation). status is
-- only idle/running; a run's terminal outcome goes to last_outcome and its
-- reason code or exception class name to failure_reason. The cursor
-- (cursor_created_at, cursor_id) points at ask_jobs (created_at, id) and starts
-- AT the ask that created the row (cursor_inclusive = 1; strictly after once
-- it first moves). window_started_at is the row's creation point: the row's
-- lifetime is the sampling window, so deleting it on every loss of read
-- access means nothing before re-joining is ever sampled.
--
-- memory_suggest_read_fences: last complete read loss, one id/time row per
-- (notebook, owner), retained independently of jobs so delayed old Ask
-- completions cannot reopen a previous membership. No historic backfill.
-- The enabled-user partial index bounds before/after read-loss candidates.
--
-- memory_suggest_fingerprints: hashes only, never text, pointing at no memory
-- row. kind 'proposed' is permanent (expires_at NULL); 'declined' is the
-- negative cache with an expiry. The key is the (owner, notebook, fingerprint)
-- triple; it leads with notebook_id (like memory_suggest_jobs) so the
-- notebook-delete cascade and the per-notebook deletes use the primary key,
-- and every read binds both owner and notebook.
--
-- The four owner_id columns carry NO foreign key to users (account deletion
-- treats them as scaffolding, app/repositories/user_references.py). All four
-- tables are local to one environment (not synced, not copied, not exported).
-- Suggestion indexes use plain CREATE. The sampling hot-path index below
-- uses IF NOT EXISTS only after exact collision validation; the migration-
-- derived shadow catalog reads both forms.
-- Hot-path batch 10 (plan G20). On 100k skewed asks / 50 notebooks, the
-- ascending/descending sample reads fell from p50 5.64/4.55 ms to 0.34/0.35 ms.
-- The index occupied 3.25 MiB. A 2000-row insert grew by about 4.9 ms
-- (2.46 us per row); this is a read/write trade-off, not a zero-cost claim.
-- It contains only finished web answers, never running asks, MCP or reports.
-- On a busy deployment run scripts/build_hotpath_indexes.py --apply FIRST:
-- the online builder creates it CONCURRENTLY before this migration takes its
-- ledger lock. The same-name validation below makes the migration a no-op
-- only for a valid, exact definition. No SQLite index is required for parity.
DO $$
DECLARE
  existing record;
  actual_keys text[];
  actual_opclasses text[];
  actual_collations text[];
  actual_predicate text;
BEGIN
  SELECT i.indexrelid, i.indisvalid, i.indisready, i.indisunique,
         i.indnkeyatts, i.indnatts, i.indclass::oid[] AS opclass_oids,
         i.indcollation::oid[] AS collation_oids, am.amname,
         tbl.relname AS table_name,
         pg_get_expr(i.indpred, i.indrelid, true) AS predicate
  INTO existing
  FROM pg_index i
  JOIN pg_class idx ON idx.oid = i.indexrelid
  JOIN pg_class tbl ON tbl.oid = i.indrelid
  JOIN pg_am am ON am.oid = idx.relam
  JOIN pg_namespace ns ON ns.oid = idx.relnamespace
  WHERE ns.nspname = current_schema()
    AND idx.relname = 'idx_ask_jobs_memory_suggest_sample';
  IF FOUND THEN
    IF NOT existing.indisvalid OR NOT existing.indisready THEN
      RAISE EXCEPTION 'hotpath batch 10: pre-existing index idx_ask_jobs_memory_suggest_sample is INVALID; run DROP INDEX CONCURRENTLY idx_ask_jobs_memory_suggest_sample, rerun scripts/build_hotpath_indexes.py --apply, then migrate again';
    END IF;
    actual_keys := ARRAY(
      SELECT pg_get_indexdef(existing.indexrelid, n, true)
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
    -- Compare PostgreSQL's canonical echo without altering literal case or
    -- whitespace: status='DONE' and status='done ' are both wrong predicates.
    actual_predicate := existing.predicate;
    IF existing.table_name <> 'ask_jobs' OR existing.amname <> 'btree'
       OR existing.indisunique OR existing.indnkeyatts <> 4 OR existing.indnatts <> 4
       OR actual_keys <> ARRAY['created_by', 'notebook_id', 'created_at', 'id']
       OR actual_opclasses <> ARRAY['pg_catalog:text_ops', 'pg_catalog:text_ops', 'pg_catalog:timestamptz_ops', 'pg_catalog:text_ops']
       OR actual_collations <> ARRAY['pg_catalog:C', 'pg_catalog:C', '', 'pg_catalog:C']
       OR actual_predicate IS DISTINCT FROM 'status = ''done''::text AND submitted_via = ''web''::text AND output = ''answer''::text AND report_id = ''''::text' THEN
      RAISE EXCEPTION 'hotpath batch 10: pre-existing index idx_ask_jobs_memory_suggest_sample does not match the expected definition; resolve the name collision manually, then migrate again';
    END IF;
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_ask_jobs_memory_suggest_sample
  ON ask_jobs(created_by, notebook_id, created_at, id)
  WHERE status = 'done' AND submitted_via = 'web' AND output = 'answer' AND report_id = '';

ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS suggested_signal text COLLATE "C";

CREATE INDEX idx_memory_suggest_unadopted
    ON memory_items (created_by, notebook_id, status)
    WHERE suggested_signal IS NOT NULL AND status IN ('candidate', 'rejected');
CREATE INDEX idx_memory_suggest_expiry
    ON memory_items (created_at)
    WHERE suggested_signal IS NOT NULL AND status = 'candidate';

CREATE TABLE memory_suggest_users (
  owner_id text COLLATE "C" NOT NULL,
  enabled integer NOT NULL DEFAULT 0,
  enabled_at timestamp with time zone,
  quota_day text COLLATE "C" NOT NULL DEFAULT '',
  quota_used integer NOT NULL DEFAULT 0,
  updated_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_memory_suggest_users PRIMARY KEY (owner_id)
);

CREATE TABLE memory_suggest_jobs (
  notebook_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL,
  status text COLLATE "C" NOT NULL DEFAULT 'idle',
  pending_signal integer NOT NULL DEFAULT 0,
  cursor_created_at timestamp with time zone NOT NULL,
  cursor_id text COLLATE "C" NOT NULL DEFAULT '',
  cursor_inclusive integer NOT NULL DEFAULT 1,
  window_started_at timestamp with time zone NOT NULL,
  consecutive_failures integer NOT NULL DEFAULT 0,
  claim_token text COLLATE "C" NOT NULL DEFAULT '',
  runs integer NOT NULL DEFAULT 0,
  candidates_written integer NOT NULL DEFAULT 0,
  last_outcome text COLLATE "C" NOT NULL DEFAULT '',
  failure_reason text COLLATE "C" NOT NULL DEFAULT '',
  started_at timestamp with time zone,
  finished_at timestamp with time zone,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_memory_suggest_jobs PRIMARY KEY (notebook_id, owner_id),
  CONSTRAINT ck_memory_suggest_jobs_status CHECK (status IN ('idle', 'running'))
);

ALTER TABLE memory_suggest_jobs
  ADD CONSTRAINT fk_memory_suggest_jobs_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE TABLE memory_suggest_read_fences (
  notebook_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL,
  lost_at timestamptz NOT NULL,
  CONSTRAINT pk_memory_suggest_read_fences PRIMARY KEY (notebook_id, owner_id),
  CONSTRAINT fk_memory_suggest_read_fences_notebook_id__notebooks
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
CREATE INDEX idx_memory_suggest_users_enabled
  ON memory_suggest_users(owner_id) WHERE enabled = 1;

CREATE TABLE memory_suggest_fingerprints (
  notebook_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL,
  fingerprint text COLLATE "C" NOT NULL,
  kind text COLLATE "C" NOT NULL,
  expires_at timestamp with time zone,
  created_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_memory_suggest_fingerprints PRIMARY KEY (notebook_id, owner_id, fingerprint),
  CONSTRAINT ck_memory_suggest_fingerprints_kind CHECK (kind IN ('proposed', 'declined'))
);

ALTER TABLE memory_suggest_fingerprints
  ADD CONSTRAINT fk_memory_suggest_fingerprints_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
