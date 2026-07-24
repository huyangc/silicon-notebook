CREATE SCHEMA silicon_shadow;

CREATE TABLE silicon_shadow.runs (
    run_id text COLLATE "C" NOT NULL,
    source_identity text COLLATE "C" NOT NULL,
    source_identity_hash char(64) COLLATE "C" NOT NULL,
    target_identity text COLLATE "C" NOT NULL,
    target_identity_hash char(64) COLLATE "C" NOT NULL,
    target_business_schema text COLLATE "C" NOT NULL,
    sqlite_version integer NOT NULL,
    postgres_version integer NOT NULL,
    schema_epoch integer NOT NULL,
    phase text COLLATE "C" NOT NULL,
    active_backend text COLLATE "C" NOT NULL,
    revision bigint NOT NULL DEFAULT 0,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb,
    sqlite_guard_report_hash char(64) COLLATE "C",
    postgres_guard_report_hash char(64) COLLATE "C",
    capture_enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    terminal_reason text COLLATE "C",
    CONSTRAINT pk_shadow_runs PRIMARY KEY (run_id),
    CONSTRAINT ck_shadow_runs_phase CHECK (phase IN ('off', 'sqlite_to_postgres', 'cutover_readonly', 'postgres_to_sqlite', 'retired')),
    CONSTRAINT ck_shadow_runs_active_backend CHECK (active_backend IN ('sqlite', 'postgresql')),
    CONSTRAINT ck_shadow_runs_revision CHECK (revision >= 0)
);

CREATE TABLE silicon_shadow.checkpoints (
    run_id text COLLATE "C" NOT NULL,
    direction text COLLATE "C" NOT NULL,
    last_seq bigint NOT NULL DEFAULT 0,
    revision bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_shadow_checkpoints PRIMARY KEY (run_id, direction),
    CONSTRAINT fk_shadow_checkpoints_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE,
    CONSTRAINT ck_shadow_checkpoints_direction CHECK (direction IN ('sqlite_to_postgres', 'postgres_to_sqlite')),
    CONSTRAINT ck_shadow_checkpoints_last_seq CHECK (last_seq >= 0),
    CONSTRAINT ck_shadow_checkpoints_revision CHECK (revision >= 0)
);

CREATE TABLE silicon_shadow.workers (
    run_id text COLLATE "C" NOT NULL,
    direction text COLLATE "C" NOT NULL,
    worker_id text COLLATE "C" NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_shadow_workers PRIMARY KEY (run_id, direction),
    CONSTRAINT fk_shadow_workers_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE
);

CREATE TABLE silicon_shadow.poison_events (
    run_id text COLLATE "C" NOT NULL,
    direction text COLLATE "C" NOT NULL,
    source_seq bigint NOT NULL,
    category text COLLATE "C" NOT NULL,
    safe_summary text COLLATE "C" NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_shadow_poison_events PRIMARY KEY (run_id, direction, source_seq),
    CONSTRAINT fk_shadow_poison_events_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE
);

CREATE TABLE silicon_shadow.copy_progress (
    run_id text COLLATE "C" NOT NULL,
    table_name text COLLATE "C" NOT NULL,
    last_key_json jsonb,
    rows_copied bigint NOT NULL DEFAULT 0,
    bytes_copied bigint NOT NULL DEFAULT 0,
    rolling_hash char(64) COLLATE "C",
    complete boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_shadow_copy_progress PRIMARY KEY (run_id, table_name),
    CONSTRAINT fk_shadow_copy_progress_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE,
    CONSTRAINT ck_shadow_copy_progress_rows CHECK (rows_copied >= 0),
    CONSTRAINT ck_shadow_copy_progress_bytes CHECK (bytes_copied >= 0)
);

CREATE TABLE silicon_shadow.verification_runs (
    verification_id text COLLATE "C" NOT NULL,
    run_id text COLLATE "C" NOT NULL,
    level text COLLATE "C" NOT NULL,
    status text COLLATE "C" NOT NULL,
    source_barrier bigint,
    target_checkpoint bigint,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CONSTRAINT pk_shadow_verification_runs PRIMARY KEY (verification_id),
    CONSTRAINT fk_shadow_verification_runs_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE
);

CREATE TABLE silicon_shadow.verification_differences (
    verification_id text COLLATE "C" NOT NULL,
    difference_no bigint NOT NULL,
    table_name text COLLATE "C" NOT NULL,
    key_hash char(64) COLLATE "C",
    category text COLLATE "C" NOT NULL,
    safe_summary text COLLATE "C" NOT NULL,
    CONSTRAINT pk_shadow_verification_differences PRIMARY KEY (verification_id, difference_no),
    CONSTRAINT fk_shadow_verification_differences_run FOREIGN KEY (verification_id) REFERENCES silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE
);

CREATE TABLE silicon_shadow.verifier_barriers (
    verification_id text COLLATE "C" NOT NULL,
    run_id text COLLATE "C" NOT NULL,
    source_barrier bigint NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT pk_shadow_verifier_barriers PRIMARY KEY (verification_id),
    CONSTRAINT fk_shadow_verifier_barriers_verification FOREIGN KEY (verification_id) REFERENCES silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE,
    CONSTRAINT fk_shadow_verifier_barriers_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE
);

CREATE TABLE silicon_shadow.retention_checkpoints (
    run_id text COLLATE "C" NOT NULL,
    checkpoint_kind text COLLATE "C" NOT NULL,
    source_seq bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT pk_shadow_retention_checkpoints PRIMARY KEY (run_id, checkpoint_kind),
    CONSTRAINT fk_shadow_retention_checkpoints_run FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE
);

REVOKE ALL ON SCHEMA silicon_shadow FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA silicon_shadow FROM PUBLIC;
