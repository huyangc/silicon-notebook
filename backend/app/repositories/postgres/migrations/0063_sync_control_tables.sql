-- Cross-environment notebook sync control state. Mirrors SQLite v83 /
-- _migration_83. Three adapter-internal tables, not replicated business
-- data (see backend/app/migration/shadow/manifest.py's LOCAL_EPHEMERAL
-- registration): each backend's own export/import bookkeeping is local to
-- that environment and must never travel through the shadow-migration
-- copy path or a notebook deep-copy.
--
-- sync_export_state tracks, per target environment, how far this
-- environment's change log has already been exported (exported_through_seq)
-- and which package that export was written into.
CREATE TABLE sync_export_state (
  target_env text COLLATE "C" NOT NULL,
  exported_through_seq bigint NOT NULL DEFAULT 0,
  exported_at timestamp with time zone NOT NULL,
  package_id text COLLATE "C" NOT NULL DEFAULT '',
  CONSTRAINT pk_sync_export_state PRIMARY KEY (target_env)
);

-- sync_imports is one row per import package applied to this environment,
-- carrying the source environment's sequence range and the run's outcome.
CREATE TABLE sync_imports (
  package_id text COLLATE "C" NOT NULL,
  source_env text COLLATE "C" NOT NULL,
  from_seq bigint NOT NULL DEFAULT 0,
  to_seq bigint NOT NULL DEFAULT 0,
  status text COLLATE "C" NOT NULL DEFAULT 'running',
  started_at timestamp with time zone NOT NULL,
  finished_at timestamp with time zone,
  report_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT pk_sync_imports PRIMARY KEY (package_id)
);

-- sync_import_progress is per-table progress within one import run, so a
-- crashed or resumed import can tell which tables it already finished
-- applying without re-deriving that from the package contents.
CREATE TABLE sync_import_progress (
  package_id text COLLATE "C" NOT NULL,
  table_name text COLLATE "C" NOT NULL,
  rows_applied bigint NOT NULL DEFAULT 0,
  completed_at timestamp with time zone,
  CONSTRAINT pk_sync_import_progress PRIMARY KEY (package_id, table_name),
  CONSTRAINT fk_sync_import_progress_package FOREIGN KEY (package_id)
    REFERENCES sync_imports (package_id) ON DELETE CASCADE
);
