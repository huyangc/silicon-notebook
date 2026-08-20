-- Mirror SQLite v56 (_migration_56): one transferable owner per group.
--
-- groups.created_by remains immutable creation audit. owner_id is the live
-- authority used for transfer, delete, and owner membership protection.
-- Existing groups choose a current admin deterministically, preferring the
-- creator only when that creator is still an admin. This preserves the current
-- membership state instead of reviving a creator who already left or was
-- demoted.
--
-- The value intentionally has no foreign key. Ownership must imply an existing
-- group_members row; a users FK cannot express that invariant, while a
-- composite FK would duplicate group_members' existing primary-key surface.
-- Stores enforce membership and ownership together under the group root lock.
-- This adds no table, index, foreign key, or unique surface.

ALTER TABLE groups
  ADD COLUMN owner_id text COLLATE "C" NOT NULL DEFAULT '';

UPDATE groups AS g
SET owner_id = COALESCE((
  SELECT gm.user_id
  FROM group_members AS gm
  WHERE gm.group_id = g.id AND gm.role = 'admin'
  ORDER BY
    CASE WHEN gm.user_id = g.created_by THEN 0 ELSE 1 END,
    gm.added_at ASC,
    gm.user_id COLLATE "C" ASC
  LIMIT 1
), '')
WHERE g.owner_id = '';

-- The store has always protected the last admin. These three statements are a
-- fail-safe for manually corrupted historical rows with no current admin.
UPDATE group_members AS gm
SET role = 'admin'
FROM groups AS g
WHERE g.owner_id = ''
  AND gm.group_id = g.id
  AND gm.user_id = g.created_by;

INSERT INTO group_members (group_id, user_id, role, added_at, added_by)
SELECT g.id, g.created_by, 'admin', g.created_at, g.created_by
FROM groups AS g
WHERE g.owner_id = ''
ON CONFLICT (group_id, user_id) DO NOTHING;

UPDATE groups SET owner_id = created_by WHERE owner_id = '';
