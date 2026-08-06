-- Per-report public share tokens.
-- The token lives on the report rather than in a side table: it is one nullable
-- value with the same lifetime as its row, so deleting a report takes its public
-- link with it without a second cascade to maintain. The partial unique index
-- only covers issued tokens, so unshared reports cost nothing and NULLs never
-- collide.

ALTER TABLE reports ADD COLUMN share_token text COLLATE "C";
ALTER TABLE reports
  ADD COLUMN shared_at timestamptz;

CREATE UNIQUE INDEX idx_reports_share_token
  ON reports(share_token) WHERE share_token IS NOT NULL;
