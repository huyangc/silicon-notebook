-- SQLite v96 parity (_migration_96). The notebook analytics panel and the
-- admin question overview count a global Ask's 👍/👎 beside the notebook
-- feedback table; the rating lives only in payload_json ('feedback', first
-- write wins, see GlobalAskStore.set_feedback) and no existing index can find
-- the rated jobs, so every count would parse every participating payload.
-- This PARTIAL expression index holds only the rated rows: a count that
-- carries the same predicate reads just those rows (a btree condition needs
-- no recheck, so an unrated payload is never detoasted). Index only; no
-- table, column, foreign key, unique-surface or write-path change.
CREATE INDEX idx_global_ask_jobs_feedback
    ON global_ask_jobs ((payload_json::jsonb->>'feedback'))
    WHERE (payload_json::jsonb->>'feedback') IN ('useful', 'not_useful');
