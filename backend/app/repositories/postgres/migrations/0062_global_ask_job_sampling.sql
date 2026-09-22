-- SQLite v82 parity (_migration_82). The post-completion learning chains now
-- sample global Ask jobs too: three background readers walk global_ask_jobs by
-- actor (user_id) or by status + engine, ordered by recency, and the table's
-- existing indexes all key on conversation_id. mode comes out of payload_json
-- into a real column (backfilled from the payload; only empty rows are
-- touched) so the "done reasoning runs" read narrows on the index instead of
-- parsing every finished job's payload. Two non-unique indexes; no table,
-- foreign key or unique-surface change.
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS mode text COLLATE "C" NOT NULL DEFAULT '';
UPDATE global_ask_jobs SET mode = COALESCE(payload_json::jsonb->>'mode', '') WHERE mode = '';
CREATE INDEX IF NOT EXISTS idx_global_ask_jobs_user_created
    ON global_ask_jobs (user_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_global_ask_jobs_status_mode_created
    ON global_ask_jobs (status, mode, created_at, id);
-- PostgreSQL-only (SQLite has no GIN and its json_each EXISTS walks the
-- (user_id, created_at, id) index in order already): the overlay sampler asks
-- "this member's global jobs whose participant list CONTAINS this notebook".
-- As an EXISTS over jsonb_array_elements_text that predicate cannot use an
-- index, so the planner read every one of the member's global jobs (and
-- parsed every payload) before sorting; as a jsonb containment (@>) over this
-- expression index it is answered from the index and only the matching rows
-- are read. text::jsonb is IMMUTABLE, so the expression is indexable.
CREATE INDEX IF NOT EXISTS idx_global_ask_jobs_participants
    ON global_ask_jobs USING GIN ((payload_json::jsonb->'resolved_notebook_ids'));
