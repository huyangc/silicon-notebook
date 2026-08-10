-- element -> chunk reverse index plus its restartable offline backfill ledger.
-- `chunks.element_ids` is the forward direction; answering "which chunks
-- contain this element" previously scanned every chunk row of the notebook.
-- The composite primary key is the covering index for the
-- (notebook_id, element_id) seek; the chunk_id index only serves the cascade
-- from chunks, so a source delete / reparse / knowhow cell rewrite never turns
-- into a sequential scan of this side table.
-- Only stable ids, counters, state codes, and timestamps are persisted.

CREATE TABLE chunk_elements (
  notebook_id text COLLATE "C" NOT NULL,
  element_id text COLLATE "C" NOT NULL,
  chunk_id text COLLATE "C" NOT NULL,
  CONSTRAINT pk_chunk_elements PRIMARY KEY (notebook_id, element_id, chunk_id),
  CONSTRAINT fk_chunk_elements_chunk FOREIGN KEY (chunk_id)
    REFERENCES chunks(id) ON UPDATE NO ACTION ON DELETE CASCADE
);

CREATE INDEX idx_chunk_elements_chunk ON chunk_elements(chunk_id);

CREATE TABLE chunk_element_backfills (
  notebook_id text COLLATE "C" NOT NULL,
  kg_mutation_seq bigint NOT NULL DEFAULT 0,
  status text COLLATE "C" NOT NULL DEFAULT 'running',
  after_chunk_id text COLLATE "C" NOT NULL DEFAULT '',
  total_chunks bigint NOT NULL DEFAULT 0,
  chunks_scanned bigint NOT NULL DEFAULT 0,
  rows_written bigint NOT NULL DEFAULT 0,
  failure_code text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  completed_at timestamptz,
  CONSTRAINT pk_chunk_element_backfills PRIMARY KEY (notebook_id),
  CONSTRAINT ck_chunk_element_backfills_status CHECK (
    status IN ('running','complete','failed')
  ),
  CONSTRAINT fk_chunk_element_backfills_notebook FOREIGN KEY (notebook_id)
    REFERENCES notebooks(id) ON UPDATE NO ACTION ON DELETE CASCADE
);

CREATE INDEX idx_chunk_element_backfills_status
  ON chunk_element_backfills(status, notebook_id);

ALTER TABLE unified_kg_state
  ADD COLUMN chunk_elements_indexed bigint NOT NULL DEFAULT 0;
