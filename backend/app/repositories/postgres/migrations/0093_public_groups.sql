-- Add public-visibility flag to groups (paired with SQLite v111).
--
-- Public groups (is_public = 1) are visible to every authenticated user without
-- requiring an explicit group_members row, and are managed by system
-- administrators. Regular groups keep is_public = 0 and retain their strict
-- member-only 404 visibility contract.

ALTER TABLE groups
  ADD COLUMN IF NOT EXISTS is_public INTEGER NOT NULL DEFAULT 0 CHECK (is_public IN (0, 1));
