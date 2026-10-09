-- Add output: what an ask_jobs row produced -- 'answer' (a stored answer in a
-- conversation) or 'evidence' (a retrieval-only MCP ask_notebook call that
-- returned the synthesis evidence and stored no answer and no conversation).
-- Mirrors SQLite v90 (_migration_90).
--
-- NOT NULL DEFAULT 'answer' is the whole backfill: every row that exists
-- before this version produced an answer. The allowed values are pinned by the
-- API model (app.models.ask.AskOutput), not a CHECK constraint, matching how
-- ``submitted_via`` is handled.
--
-- No index: the learning samplers filter ``output = 'answer'`` on top of
-- predicates that already drive their plans, and a two-valued column is not
-- selective. No FK or unique-surface change.
ALTER TABLE ask_jobs ADD COLUMN IF NOT EXISTS output text COLLATE "C" NOT NULL DEFAULT 'answer';
-- retained_user_activity's existing text columns were created (0044) without
-- an explicit COLLATE, so this column matches its table rather than ask_jobs.
ALTER TABLE retained_user_activity ADD COLUMN IF NOT EXISTS output text NOT NULL DEFAULT 'answer';
