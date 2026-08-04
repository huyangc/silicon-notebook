-- Restartable offline backfill state for historical source-local facts.
-- Only stable codes/counts/ids are persisted; evidence text never enters this ledger.

ALTER TABLE knowledge_source_facts
  ADD COLUMN projection_origin text COLLATE "C" NOT NULL DEFAULT 'live';
ALTER TABLE knowledge_source_facts
  ADD CONSTRAINT ck_knowledge_source_facts_projection_origin CHECK (
    projection_origin IN ('live','historical')
  );

CREATE TABLE knowledge_source_fact_backfills (
  source_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_generation text COLLATE "C" NOT NULL,
  projection_version bigint NOT NULL DEFAULT 1,
  status text COLLATE "C" NOT NULL DEFAULT 'running',
  after_object_id text COLLATE "C" NOT NULL DEFAULT '',
  objects_scanned bigint NOT NULL DEFAULT 0,
  facts_written bigint NOT NULL DEFAULT 0,
  incomplete_objects bigint NOT NULL DEFAULT 0,
  incomplete_reason text COLLATE "C" NOT NULL DEFAULT '',
  failure_code text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_knowledge_source_fact_backfills PRIMARY KEY (source_id),
  CONSTRAINT ck_knowledge_source_fact_backfills_status CHECK (
    status IN ('running','complete','incomplete','failed')
  )
);

ALTER TABLE knowledge_source_fact_backfills
  ADD CONSTRAINT fk_ksfb_source FOREIGN KEY (source_id) REFERENCES sources(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
ALTER TABLE knowledge_source_fact_backfills
  ADD CONSTRAINT fk_ksfb_notebook FOREIGN KEY (notebook_id) REFERENCES notebooks(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE INDEX idx_knowledge_source_fact_backfills_notebook
  ON knowledge_source_fact_backfills(notebook_id, status, source_id);

CREATE INDEX idx_kos_source_object
  ON knowledge_object_sources(source_id, object_id);

CREATE INDEX idx_knowledge_source_facts_source_generation_global
  ON knowledge_source_facts(
    source_id, source_generation, projection_origin, global_object_id
  );
