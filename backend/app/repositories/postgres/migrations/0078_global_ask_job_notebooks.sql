-- Physical attribution of finished global Ask jobs. Mirrors SQLite v98 /
-- _migration_98.
--
-- global_ask_job_notebooks holds one row per library a done global Ask job is
-- attributed to: app/domain/global_ask_attribution.py::job_attribution (the
-- libraries the run searched or cited; when it did neither, its participants
-- minus the skipped ones), the very rule attribution_sql spells over
-- payload_json. GlobalAskStore.save writes the rows in the statement batch
-- that moves the job to 'done'; a failed, cancelled or interrupted job gets
-- none. A child table rather than a text[] column on global_ask_jobs: SQLite
-- has no array type to index, and with the same model on both backends the
-- shared GlobalAskStore writes it once; (notebook_id, job_id) is an exact
-- btree lookup (no recheck) for the notebook analytics count. Deleting a job
-- (retention purge, conversation delete) cascades to its rows; notebook_id
-- has no foreign key because a retained turn outlives its deleted library.
--
-- global_ask_jobs.attributed is 1 once that table holds a done job's
-- attribution (possibly no row: a run attributed to no library) and 0 for
-- every row present now. No backfill here: existing done jobs are attributed
-- by the offline, resumable scripts/backfill_global_ask_attribution.py; until
-- then the readers count a done job with attributed = 0 through the payload
-- predicate, and the partial idx_global_ask_jobs_unattributed holds exactly
-- those rows (empty once the backfill has run). An integer flag (1/0) for
-- SQLite parity, like agent_access_tokens.allow_answer_links. The indexes are
-- spelled without IF NOT EXISTS: app/migration/shadow/postgres_catalog.py
-- reads them out of this file and does not parse that clause.
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS attributed integer NOT NULL DEFAULT 0;
CREATE TABLE global_ask_job_notebooks (
  job_id text NOT NULL REFERENCES global_ask_jobs(id) ON DELETE CASCADE,
  notebook_id text NOT NULL,
  CONSTRAINT pk_global_ask_job_notebooks PRIMARY KEY (job_id, notebook_id)
);
CREATE INDEX idx_global_ask_job_notebooks_notebook
    ON global_ask_job_notebooks (notebook_id, job_id);
CREATE INDEX idx_global_ask_jobs_unattributed
    ON global_ask_jobs (id) WHERE status = 'done' AND attributed = 0;
