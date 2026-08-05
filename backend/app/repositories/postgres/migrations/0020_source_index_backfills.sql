-- Restartable notebook-scoped progress for the source reverse-index rebuild.
-- Only stable ids, counters, state codes, and timestamps are persisted.

CREATE TABLE source_index_backfills (
  notebook_id text COLLATE "C" NOT NULL,
  kg_mutation_seq bigint NOT NULL DEFAULT 0,
  status text COLLATE "C" NOT NULL DEFAULT 'running',
  after_object_id text COLLATE "C" NOT NULL DEFAULT '',
  total_objects bigint NOT NULL DEFAULT 0,
  objects_scanned bigint NOT NULL DEFAULT 0,
  rows_written bigint NOT NULL DEFAULT 0,
  failure_code text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  completed_at timestamptz,
  CONSTRAINT pk_source_index_backfills PRIMARY KEY (notebook_id),
  CONSTRAINT ck_source_index_backfills_status CHECK (
    status IN ('running','complete','failed')
  ),
  CONSTRAINT fk_source_index_backfills_notebook FOREIGN KEY (notebook_id)
    REFERENCES notebooks(id) ON UPDATE NO ACTION ON DELETE CASCADE
);

CREATE INDEX idx_source_index_backfills_status
  ON source_index_backfills(status, notebook_id);
