-- Creator-wide question overview: match query_store._absolute_instant and
-- its (created_at DESC, id DESC) keyset order so LIMIT can stop in the index.
CREATE INDEX idx_ask_jobs_creator_activity
  ON ask_jobs (
    created_by,
    (COALESCE(created_at, TIMESTAMPTZ '0001-01-01T00:00:00+00:00')) DESC,
    id DESC
  );
