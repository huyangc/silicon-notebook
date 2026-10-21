-- Notebook deletion grace window. Mirrors SQLite v100 / _migration_100.
--
-- DELETE /api/notebooks/{id} no longer tombstones the notebook as 'deleting'
-- at once: it moves the row to status='pending_delete' for one day
-- (app.domain.notebook_delete_grace.NOTEBOOK_DELETE_GRACE_SECONDS). During that
-- day the notebook is invisible to everyone (NOTEBOOK_LIVE_SQL lists the new
-- value); the owner can cancel, and readers keep exporting or moving their own
-- Memory out. The delete sweep's driver C moves a due row to 'deleting' and
-- inserts its delete job in one transaction; the six-phase delete is unchanged.
--
-- delete_after         the instant the window closes (NULL outside a window)
-- delete_requested_at  when the owner asked for the deletion
-- delete_requested_by  who asked (audit only: no FK, registered audit_kept in
--                      app/repositories/user_references.py)
-- status_before_delete the status a cancel restores ('' outside a window; an
--                      empty value restores 'draft', the column default)
--
-- No backfill: every existing row is outside a window, which NULL / '' record.
-- The status values are enforced by the three write paths (request, cancel,
-- promote) and their tests, not by a CHECK -- like every other status column
-- of this schema. All four are target-owned in app/migration/sync/manifest.py:
-- a mirror never takes the source's deletion window. The partial index holds
-- only the rows inside a window (normally none), so the due scan and the
-- notice projection read an empty index when nothing is pending. The index is
-- spelled without IF NOT EXISTS: app/migration/shadow/postgres_catalog.py reads
-- it out of this file for the catalog drift expectation and does not parse
-- that clause.
ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS delete_after timestamp with time zone;
ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS delete_requested_at timestamp with time zone;
ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS delete_requested_by text COLLATE "C";
ALTER TABLE notebooks ADD COLUMN IF NOT EXISTS status_before_delete text COLLATE "C" NOT NULL DEFAULT '';
CREATE INDEX idx_notebooks_pending_delete_due
    ON notebooks (delete_after) WHERE status = 'pending_delete';
