-- Mirror SQLite v57 (_migration_57): one reusable, revocable invitation
-- capability per group. NULL means no active link. The raw token is retained
-- because an authorized group admin must be able to reopen the workspace and
-- copy the same live link; this follows the existing notebook/report/
-- conversation share-token contract.

ALTER TABLE groups
  ADD COLUMN invite_token text COLLATE "C";

ALTER TABLE groups
  ADD COLUMN invite_created_at timestamptz;

ALTER TABLE groups
  ADD COLUMN invite_created_by text COLLATE "C";

CREATE UNIQUE INDEX idx_groups_invite_token
  ON groups (invite_token)
  WHERE invite_token IS NOT NULL;
