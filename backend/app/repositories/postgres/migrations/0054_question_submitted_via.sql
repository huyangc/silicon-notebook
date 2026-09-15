-- Add submitted_via: which submission surface created a question/report row
-- (the web app, or the MCP tool ask_notebook). Mirrors SQLite v74
-- (_migration_74).
--
-- NOT NULL DEFAULT '': deliberately not a backfill. Historical rows (and
-- any in-process caller that does not pass the keyword, e.g.
-- app.eval.inference calling repo.ask directly) have no reliable signal to
-- reconstruct "web" vs "mcp" from -- mixing an inferred value with a
-- recorded one would make the column unexplainable -- so they stay ''
-- ("not recorded") forever. The allowed values are pinned by the API model
-- (app.models.ask.StoredSubmittedVia), not a CHECK constraint, matching how
-- ``wishes.status`` is already handled.
--
-- No index: the admin questions CTE already does a three-way UNION full
-- scan; an equality filter on submitted_via does not change the plan
-- shape. No FK or unique-surface change.
ALTER TABLE ask_jobs ADD COLUMN IF NOT EXISTS submitted_via text COLLATE "C" NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS submitted_via text COLLATE "C" NOT NULL DEFAULT '';
-- retained_user_activity's existing text columns were created (0044) without
-- an explicit COLLATE, so this column matches its table rather than its
-- ask_jobs/reports siblings.
ALTER TABLE retained_user_activity ADD COLUMN IF NOT EXISTS submitted_via text NOT NULL DEFAULT '';
