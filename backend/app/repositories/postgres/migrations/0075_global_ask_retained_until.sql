-- Global Ask turn retention. Mirrors SQLite v95 / _migration_95.
--
-- retained_until is NULL for a turn none of whose participant libraries was
-- ever deleted. Deleting a library stamps every turn whose
-- resolved_notebook_ids contains it with that deletion's retained_user_activity
-- expiry (app/domain/global_ask_retention.py): the owner can still open the
-- turn until then; startup recovery or the next deletion purges it afterwards.
-- text COLLATE "C" holding an aware ISO-8601 instant, like every other instant
-- of this table family (0056/0061); the readers cast it to timestamptz at the
-- point of comparison. No backfill: a turn whose library was deleted before
-- this column existed has no deletion instant to start a window from and keeps
-- the strict "every participant readable" rule. The partial index over the few
-- stamped rows, keyed by conversation, serves both the purge and the
-- conversation read gate's "does this conversation hold an expired turn" probe
-- (app/domain/global_ask_retention.py::conversation_live_sql), which on an
-- unstamped conversation is answered from an index with no entry for it. The index is spelled
-- without IF NOT EXISTS: app/migration/shadow/postgres_catalog.py reads it out
-- of this file for the catalog drift expectation and does not parse that clause.
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS retained_until text COLLATE "C";
CREATE INDEX idx_global_ask_jobs_retained
    ON global_ask_jobs (conversation_id, retained_until) WHERE retained_until IS NOT NULL;
