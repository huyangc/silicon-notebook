-- Content-minimal user-analysis projection retained after notebook deletion.
-- Deliberately no notebook FK: the notebook aggregate remains fully deletable.
-- The delete path stamps expires_at from USER_ACTIVITY_RETENTION_DAYS and
-- archives no answer/source/report body or reasoning trace.
CREATE TABLE retained_user_activity (
  activity_type text NOT NULL,
  record_id text NOT NULL,
  actor_id text NOT NULL DEFAULT '',
  notebook_id text NOT NULL DEFAULT '',
  notebook_owner_id text NOT NULL DEFAULT '',
  notebook_name text NOT NULL DEFAULT '',
  created_at timestamptz,
  updated_at timestamptz,
  asked_at text NOT NULL DEFAULT '',
  conversation_id text NOT NULL DEFAULT '',
  question text NOT NULL DEFAULT '',
  mode text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT '',
  display_title text NOT NULL DEFAULT '',
  file_name text NOT NULL DEFAULT '',
  source_type text NOT NULL DEFAULT '',
  parse_status text NOT NULL DEFAULT '',
  parse_failed boolean NOT NULL DEFAULT false,
  depth integer NOT NULL DEFAULT 0,
  generation_started_at text NOT NULL DEFAULT '',
  deleted_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  CONSTRAINT pk_retained_user_activity
    PRIMARY KEY (activity_type, record_id)
);

CREATE INDEX idx_retained_activity_actor_type_created
  ON retained_user_activity (
    actor_id,
    activity_type,
    (COALESCE(created_at, TIMESTAMPTZ '0001-01-01T00:00:00+00:00')) DESC,
    record_id DESC
  );

CREATE INDEX idx_retained_activity_owner_created
  ON retained_user_activity (
    notebook_owner_id,
    (COALESCE(created_at, TIMESTAMPTZ '0001-01-01T00:00:00+00:00')) DESC,
    record_id DESC
  );

CREATE INDEX idx_retained_activity_expires
  ON retained_user_activity (expires_at);

CREATE INDEX idx_retained_activity_notebook
  ON retained_user_activity (notebook_id);
