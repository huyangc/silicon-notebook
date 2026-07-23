-- Integrity indexes that must remain active during shadow bulk COPY.
-- pg_trgm is a database-wide prerequisite. Keep it in the stable shared
-- namespace so dropping a disposable/application schema cannot remove it.
-- A restricted production migration role may require the DBA to preinstall
-- the extension here; an incompatible existing namespace fails closed below.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

DO $migration$
DECLARE
  extension_schema text COLLATE "C";
BEGIN
  SELECT n.nspname INTO extension_schema
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_trgm';
  IF extension_schema IS DISTINCT FROM 'public' THEN
    RAISE EXCEPTION 'pg_trgm must be installed in the public schema';
  END IF;
END
$migration$;

CREATE UNIQUE INDEX idx_kg_build_jobs_one_running ON kg_build_jobs(notebook_id) WHERE status = 'running';
CREATE UNIQUE INDEX idx_memory_answer_once ON memory_items(created_by, source_answer_id) WHERE source_answer_id IS NOT NULL;
CREATE UNIQUE INDEX idx_notebooks_share_token ON notebooks(share_token) WHERE share_token IS NOT NULL;
CREATE UNIQUE INDEX idx_promotion_object ON promotion_candidates(object_id) WHERE status NOT IN ('approved', 'rejected');
CREATE UNIQUE INDEX idx_sources_memory_id ON sources(memory_id) WHERE memory_id IS NOT NULL AND memory_id != '';
CREATE UNIQUE INDEX idx_users_username ON users(username) WHERE username != '';
