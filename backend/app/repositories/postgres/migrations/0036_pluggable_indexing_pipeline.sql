-- Desired notebook selection plus the identity of the atomically published
-- core-schema products. NULL desired selection means the built-in pipeline.
ALTER TABLE notebooks
  ADD COLUMN indexing_pipeline text COLLATE "C",
  ADD COLUMN indexing_pipeline_version text COLLATE "C" NOT NULL DEFAULT 'builtin.chunk.v1',
  ADD COLUMN indexing_pipeline_generation text COLLATE "C" NOT NULL DEFAULT '',
  ADD COLUMN indexing_pipeline_job_id text COLLATE "C" NOT NULL DEFAULT '';

ALTER TABLE unified_kg_state
  ADD COLUMN indexing_pipeline_id text COLLATE "C" NOT NULL DEFAULT '',
  ADD COLUMN indexing_pipeline_version text COLLATE "C" NOT NULL DEFAULT 'builtin.chunk.v1';

ALTER TABLE extraction_runs
  ADD COLUMN indexing_pipeline_id text COLLATE "C" NOT NULL DEFAULT '',
  ADD COLUMN indexing_pipeline_version text COLLATE "C" NOT NULL DEFAULT 'builtin.chunk.v1';
