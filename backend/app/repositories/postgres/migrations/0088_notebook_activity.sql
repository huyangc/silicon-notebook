-- Collaboration pulse D11(b), D15: non-owner activity hints, not compliance
-- audit. Actor identity is retained without a users FK; notebook deletion
-- cascades. The configured retention sweep defaults to ninety days.
CREATE TABLE IF NOT EXISTS notebook_activity (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  kind text COLLATE "C" NOT NULL,
  capability text COLLATE "C" NOT NULL,
  actor_id text COLLATE "C" NOT NULL,
  actor_name text NOT NULL,
  target text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_notebook_activity PRIMARY KEY (id),
  CONSTRAINT fk_notebook_activity_notebook_id__notebooks
    FOREIGN KEY (notebook_id) REFERENCES notebooks (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notebook_activity_nb_created
  ON notebook_activity (notebook_id, created_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_notebook_activity_created ON notebook_activity (created_at);
