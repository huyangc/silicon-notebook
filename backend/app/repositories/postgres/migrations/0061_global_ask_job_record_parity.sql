-- SQLite v81 parity (_migration_81). A global Ask job must leave the same
-- record a notebook ask_jobs row leaves. The four per-job facts that only
-- ask_jobs carried become plain columns on global_ask_jobs -- never fields of
-- payload_json, which is the owner-facing response body:
--
-- * submitted_via  -- the submission surface of THIS turn ("web" / "mcp").
--   It was only ever stamped on the conversation, so a web-started
--   conversation continued over MCP read "web" for every turn. Backfilled
--   from the conversation: that value was recorded by a server entry point at
--   the time (not inferred), and it is exactly what the product attributed
--   those turns to until now. Only empty rows are touched.
-- * asked_at       -- the browser-captured submission instant
--   (ask_jobs.asked_at); display metadata, never an ordering key.
-- * updated_at     -- the last transition instant; the finish time of a
--   terminal job, which ask_jobs.updated_at always recorded and a global job
--   could not recover at all. '' for historical rows: their finish instant was
--   never written anywhere and is not reconstructed.
-- * error_detail   -- the raw "<ExceptionType>: <message>" of a failed run,
--   the administrator-only diagnostic ask_jobs.error keeps. payload_json.error
--   stays the fixed owner-facing sentence; this column is read by the admin
--   detail endpoint only.
--
-- text, not timestamptz, for the two instants: every timestamp in this table
-- family is text on PostgreSQL too (0056), and the admin readers cast at the
-- point of comparison. COLLATE "C" like every other id-like text column here.
-- No index, table, foreign key or unique-surface change.
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS submitted_via text COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS asked_at text COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS updated_at text COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE global_ask_jobs ADD COLUMN IF NOT EXISTS error_detail text COLLATE "C" NOT NULL DEFAULT '';
UPDATE global_ask_jobs j SET submitted_via = COALESCE(
    (SELECT c.submitted_via FROM global_ask_conversations c WHERE c.id = j.conversation_id), '')
WHERE j.submitted_via = '';
