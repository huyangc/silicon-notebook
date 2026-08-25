-- Unpublished notebook indexing generations.  Payloads contain already
-- computed core-schema rows; the durable job store publishes them only after
-- a generation/job/source-snapshot CAS succeeds.
CREATE TABLE indexing_pipeline_stages (
  job_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  pipeline_id text COLLATE "C" NOT NULL DEFAULT '',
  pipeline_version text COLLATE "C" NOT NULL,
  pipeline_generation text COLLATE "C" NOT NULL,
  source_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_indexing_pipeline_stages PRIMARY KEY (job_id),
  CONSTRAINT fk_indexing_pipeline_stages_job FOREIGN KEY (job_id)
    REFERENCES kg_build_jobs(id) ON DELETE CASCADE,
  CONSTRAINT fk_indexing_pipeline_stages_notebook FOREIGN KEY (notebook_id)
    REFERENCES notebooks(id) ON DELETE CASCADE
);
CREATE INDEX idx_indexing_pipeline_stages_notebook
  ON indexing_pipeline_stages(notebook_id);

CREATE TABLE indexing_pipeline_stage_sources (
  job_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  status text COLLATE "C" NOT NULL DEFAULT 'pending',
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamp with time zone NOT NULL,
  updated_at timestamp with time zone NOT NULL,
  CONSTRAINT pk_indexing_pipeline_stage_sources PRIMARY KEY (job_id, source_id),
  CONSTRAINT ck_indexing_pipeline_stage_sources_status
    CHECK (status IN ('pending','completed','failed')),
  CONSTRAINT fk_indexing_pipeline_stage_sources_stage FOREIGN KEY (job_id)
    REFERENCES indexing_pipeline_stages(job_id) ON DELETE CASCADE,
  CONSTRAINT fk_indexing_pipeline_stage_sources_source FOREIGN KEY (source_id)
    REFERENCES sources(id) ON DELETE CASCADE
);
CREATE INDEX idx_indexing_pipeline_stage_sources_source
  ON indexing_pipeline_stage_sources(source_id);
