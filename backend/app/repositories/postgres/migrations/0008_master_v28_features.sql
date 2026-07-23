-- Reconcile master SQLite v24-v28 features with the PostgreSQL adapter.

CREATE TABLE kg_canonical_scratch (
  notebook_id text COLLATE "C" NOT NULL,
  run_id text COLLATE "C" NOT NULL,
  seed text COLLATE "C" NOT NULL,
  canonical_id text COLLATE "C" NOT NULL,
  canonical_name text COLLATE "C" NOT NULL DEFAULT '',
  canonical_description text COLLATE "C" NOT NULL DEFAULT '',
  canonical_desc_sig text COLLATE "C" NOT NULL DEFAULT ''
);
CREATE INDEX idx_kg_canonical_scratch_nb_run_seed
  ON kg_canonical_scratch(notebook_id, run_id, seed);

UPDATE user_profiles SET model_settings='{}'::jsonb;
DELETE FROM model_service_status;

CREATE TABLE system_model_service_status (
  service_id text COLLATE "C" NOT NULL,
  config_fingerprint text COLLATE "C" NOT NULL,
  status text COLLATE "C" NOT NULL,
  latency_ms bigint NOT NULL DEFAULT 0,
  code text COLLATE "C" NOT NULL DEFAULT '',
  trigger text COLLATE "C" NOT NULL,
  support_id text COLLATE "C" NOT NULL DEFAULT '',
  checked_at timestamptz NOT NULL,
  CONSTRAINT pk_system_model_service_status PRIMARY KEY (service_id),
  CONSTRAINT ck_system_model_service_status_status
    CHECK (status IN ('ok', 'error')),
  CONSTRAINT ck_system_model_service_status_trigger
    CHECK (trigger IN ('manual_test', 'observed_failure', 'recovery_probe'))
);

CREATE TABLE knowhow_changes (
  id text COLLATE "C" NOT NULL,
  table_id text COLLATE "C" NOT NULL,
  seq bigint NOT NULL,
  kind text COLLATE "C" NOT NULL,
  actor text COLLATE "C" NOT NULL DEFAULT '',
  origin text COLLATE "C" NOT NULL DEFAULT 'user',
  payload_json text COLLATE "C" NOT NULL,
  fingerprint text COLLATE "C" NOT NULL,
  note text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_knowhow_changes PRIMARY KEY (id),
  CONSTRAINT fk_knowhow_changes_table FOREIGN KEY (table_id)
    REFERENCES knowhow_tables(id) ON DELETE CASCADE,
  CONSTRAINT uq_knowhow_changes_table_seq UNIQUE (table_id, seq)
);
CREATE INDEX idx_knowhow_changes_table ON knowhow_changes(table_id, seq DESC);

CREATE TABLE knowhow_milestones (
  id text COLLATE "C" NOT NULL,
  table_id text COLLATE "C" NOT NULL,
  seq bigint NOT NULL,
  name text COLLATE "C" NOT NULL,
  note text COLLATE "C" NOT NULL DEFAULT '',
  created_by text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_knowhow_milestones PRIMARY KEY (id),
  CONSTRAINT fk_knowhow_milestones_table FOREIGN KEY (table_id)
    REFERENCES knowhow_tables(id) ON DELETE CASCADE,
  CONSTRAINT uq_knowhow_milestones_table_name UNIQUE (table_id, name)
);
CREATE INDEX idx_knowhow_milestones_table
  ON knowhow_milestones(table_id, seq DESC);

ALTER TABLE sources ADD COLUMN chunked_at timestamptz;
UPDATE sources SET chunked_at=updated_at
WHERE chunked_at IS NULL
  AND parse_status IN ('parsed', 'extracting', 'extracted');

CREATE TABLE app_settings (
  key text COLLATE "C" NOT NULL,
  value text COLLATE "C" NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_app_settings PRIMARY KEY (key)
);
ALTER TABLE user_profiles ADD COLUMN upload_document_limit bigint;
