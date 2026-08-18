-- Per-conversation public share tokens + a read watermark (T1 of
-- question-answer session sharing). Mirrors SQLite v52 / _migration_52.
--
-- Same shape as 0021_report_share_tokens.sql: the token lives on the
-- conversation row rather than in a side table, so a deleted conversation
-- takes its public link with it without a second cascade to maintain. The
-- partial unique index only covers issued tokens, so unshared conversations
-- cost nothing and NULLs never collide.
--
-- Unlike a report, a conversation keeps growing after it is shared -- the
-- design doc's decision is "freeze + explicit update" (sharing again pushes
-- the boundary; it never happens implicitly), so this migration adds TWO
-- watermark columns beyond the token:
--
--  * shared_through_at is a literal timestamptz VALUE, not a foreign key to
--    an answer row. Storing an answer id instead would make the watermark
--    meaningless the moment that answer is deleted (no created_at to compare
--    against); a literal value keeps the boundary predicate
--    (answers.created_at <= conversations.shared_through_at) self-contained
--    regardless of whether the referenced row still exists. The comparison
--    is closed (<=): the watermark is captured from the last answer that
--    existed at share time.
--  * shared_through_id is a denormalized copy of that answer's id --
--    recorded for display/audit ("shared through this turn"), NOT consumed
--    by the boundary predicate above (which is self-contained on purpose,
--    see above). It is a plain snapshotted string, not a live reference, so
--    it carries the same delete-safety as shared_through_at.
--
-- Zero behavior change: the store methods that read/write these three
-- columns (share_conversation / unshare_conversation /
-- conversation_share_state / public_conversation_by_token) have no caller
-- anywhere in the codebase yet -- the API surface, the row-level created_by
-- gate and the "must have at least one written answer" policy all land in a
-- later task of the same feature. This migration only lays the schema down.

ALTER TABLE conversations ADD COLUMN share_token text COLLATE "C";
ALTER TABLE conversations ADD COLUMN shared_through_at timestamptz;
ALTER TABLE conversations ADD COLUMN shared_through_id text COLLATE "C";

CREATE UNIQUE INDEX idx_conversations_share_token
  ON conversations(share_token) WHERE share_token IS NOT NULL;
