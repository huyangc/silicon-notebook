-- Local, restart-safe relink work; never copied or replicated. No data backfill.
-- Source keys deliberately have no FK: orphan/empty source partitions are valid.
CREATE TABLE kg_relink_state (
  notebook_id TEXT NOT NULL,
  algorithm_version INTEGER NOT NULL,
  full_pending INTEGER NOT NULL DEFAULT 0 CHECK (full_pending IN (0, 1)),
  started_at_seq BIGINT NOT NULL DEFAULT 0,
  CONSTRAINT pk_kg_relink_state PRIMARY KEY (notebook_id),
  CONSTRAINT fk_kg_relink_state_notebook FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
CREATE TABLE kg_relink_work (
  notebook_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  revision BIGINT NOT NULL DEFAULT 1,
  algorithm_version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  isolated_before BIGINT NOT NULL DEFAULT 0,
  isolated_after BIGINT NOT NULL DEFAULT 0,
  edges_added BIGINT NOT NULL DEFAULT 0,
  CONSTRAINT pk_kg_relink_work PRIMARY KEY (notebook_id, source_id),
  CONSTRAINT fk_kg_relink_work_notebook FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
CREATE INDEX idx_kg_relink_work_pending
  ON kg_relink_work (notebook_id, source_id COLLATE "C") WHERE status = 'pending';
