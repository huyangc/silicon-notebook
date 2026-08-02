-- Mirror SQLite v39's command-catalog extraction tables so the packaged
-- PostgreSQL schema stays in one-for-one parity with the reviewed SQLite
-- contract.
--
-- catalog_jobs carries the cross-process single-flight guard for one source's
-- extraction, the same way kg_build_jobs carries it for one notebook's KG
-- build. The partial unique index covers queued AND running: the row is
-- inserted before the worker thread starts, and a duplicate POST landing in
-- that window must be rejected rather than scheduling a second writer for the
-- same candidate set. The guard is per source, not per notebook — one manual
-- is one source, and another manual in the same notebook has no reason to be
-- blocked.
--
-- catalog_candidates holds unconfirmed extraction output, INCLUDING the
-- entries grounding rejected outright (state='rejected'). Keeping the rejects
-- is deliberate: when a run produces nothing, those rows and their reject_info
-- are the only way a person can tell "the model went wrong" from "this source
-- is not a command manual".
--
-- position is a per-job insertion ordinal and exists for exactly one reason:
-- the review list pages by keyset, and neither the random uuid id nor
-- created_at (identical across a section's rows) orders the rows totally. The
-- job is single-flight and never resumed, so the one worker thread owns the
-- counter and no MAX()+1 read-modify-write is needed.
--
-- failure_reason is user-facing copy written to the user_error() standard;
-- diagnostic is internal and never reaches a screen. They are separate columns
-- so "may a user see this" stays a provenance judgement rather than a guess
-- about a field's shape.
--
-- source_generation is the third pre-release review fix (R8) and stores the
-- ELEMENT generation this job was created against: MAX(source_elements.created_at),
-- which replace_elements writes as one stamp across the whole replacement batch,
-- so it moves if and only if the source's elements were swapped. Candidate rows
-- carry command names, excerpts and section paths taken from the generation the
-- run actually read; confirming them after a reparse would write content the
-- document no longer contains. sources.updated_at was rejected as the signal:
-- it is the deliberately COARSE change token (it also moves on every lifecycle
-- transition — extracting/extracted, summary writes, re-extraction — none of
-- which touch source_elements), and a false positive here costs a whole paid
-- re-extraction plus a message that claims a reparse that never happened.
--
-- truncated_sections and applied_table_id are pre-release C1b review fixes,
-- folded directly into this not-yet-shipped table (never a separate later
-- migration — this whole table is new in this same feature). truncated_sections
-- surfaces C1a's own SectionOutcome.truncation.applied count so the review UI
-- can say "N sections were cut for length". applied_table_id records which
-- knowhow table a job's confirms landed in: the first successful apply writes
-- it, and every later apply for the SAME job resolves its target through this
-- column first (falling back to a by-title lookup only when it is empty or the
-- table it names is gone) — so renaming that table between two apply calls for
-- one job cannot split the job's confirms across two tables.

CREATE TABLE catalog_jobs (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  created_by text COLLATE "C" NOT NULL DEFAULT '',
  status text COLLATE "C" NOT NULL DEFAULT 'queued',
  sections_total bigint NOT NULL DEFAULT 0,
  sections_done bigint NOT NULL DEFAULT 0,
  entries bigint NOT NULL DEFAULT 0,
  rejected bigint NOT NULL DEFAULT 0,
  uncovered bigint NOT NULL DEFAULT 0,
  truncated_sections bigint NOT NULL DEFAULT 0,
  failure_reason text COLLATE "C" NOT NULL DEFAULT '',
  diagnostic text COLLATE "C" NOT NULL DEFAULT '',
  applied_table_id text COLLATE "C" NOT NULL DEFAULT '',
  source_generation text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  finished_at timestamptz,
  CONSTRAINT pk_catalog_jobs PRIMARY KEY (id)
);

CREATE TABLE catalog_candidates (
  id text COLLATE "C" NOT NULL,
  job_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  position bigint NOT NULL DEFAULT 0,
  section_path text COLLATE "C" NOT NULL DEFAULT '',
  command_name text COLLATE "C" NOT NULL DEFAULT '',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  state text COLLATE "C" NOT NULL DEFAULT 'candidate',
  reject_info jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_catalog_candidates PRIMARY KEY (id)
);

ALTER TABLE catalog_jobs
  ADD CONSTRAINT fk_catalog_jobs_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE catalog_jobs
  ADD CONSTRAINT fk_catalog_jobs_source_id__sources
  FOREIGN KEY (source_id) REFERENCES sources (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

-- catalog_candidates.job_id deliberately carries NO foreign key, and the rows
-- hang off notebooks/sources directly instead. Two reasons, in order:
--
--  1. Correctness is unaffected. Job rows are history and are never deleted on
--     their own; every real deletion path is "the source went away" or "the
--     notebook went away", and both cascades below cover the candidates
--     directly.
--  2. A job_id foreign key would make catalog_jobs a non-leaf table, and the
--     partial unique index below is a single-column surface on source_id --
--     itself a foreign-key column. The forward-shadow replicator parks a
--     UNIQUE conflict by nulling a nullable column, by writing a sentinel into
--     a non-FK/non-CHECK text/bigint column, or by delete/reinsert on a leaf
--     table with no incoming foreign key. source_id is NOT NULL and is an FK
--     column, so leaf delete/reinsert is the only remaining strategy, exactly
--     as it already is for idx_kg_build_jobs_one_running. An incoming FK would
--     remove it and make the whole PG16 catalog unparkable at
--     ShadowReplicator construction time.
CREATE INDEX idx_catalog_candidates_nb ON catalog_candidates(notebook_id);
CREATE INDEX idx_catalog_candidates_source ON catalog_candidates(source_id);

ALTER TABLE catalog_candidates
  ADD CONSTRAINT fk_catalog_candidates_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE catalog_candidates
  ADD CONSTRAINT fk_catalog_candidates_source_id__sources
  FOREIGN KEY (source_id) REFERENCES sources (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE UNIQUE INDEX idx_catalog_jobs_one_active
  ON catalog_jobs(source_id) WHERE status IN ('queued', 'running');
CREATE INDEX idx_catalog_jobs_source_created
  ON catalog_jobs(source_id, created_at DESC, id DESC);
CREATE INDEX idx_catalog_jobs_nb_created
  ON catalog_jobs(notebook_id, created_at DESC, id DESC);
CREATE INDEX idx_catalog_candidates_job_state
  ON catalog_candidates(job_id, state, position);
