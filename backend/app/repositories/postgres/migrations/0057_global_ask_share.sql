-- Per-conversation public share tokens + a read watermark for the GLOBAL
-- question-answer sessions. Mirrors SQLite v77 / _migration_77, and is the
-- same three-column shape 0030_conversation_share.sql laid on `conversations`.
--
-- Why a second copy instead of reusing `conversations`: a global session
-- belongs to no notebook, and `conversations.notebook_id` is NOT NULL with a
-- foreign key into notebooks. The two tables therefore carry the same columns
-- independently; read 0030's header for the full design rationale, which
-- applies here verbatim:
--
--  * the token lives on the conversation row rather than in a side table, so a
--    deleted conversation takes its public link with it (global_ask_jobs
--    already cascades from here) without a second cascade to maintain;
--  * the partial unique index only covers issued tokens, so unshared sessions
--    cost nothing and NULLs never collide;
--  * shared_through_at is a literal timestamp VALUE, not a foreign key to a
--    job row, so the boundary predicate stays self-contained when the job it
--    was captured from is gone; the comparison is closed (<=);
--  * shared_through_id is a denormalized copy of that job's id, recorded for
--    display/audit and for the keyset tie-break, never a live reference.
--
-- TWO deliberate departures from 0030's literal spelling, both forced by the
-- host table rather than by a change of design:
--
--  * shared_through_at is `text`, not `timestamptz`. Every timestamp in the
--    global-ask family (global_ask_conversations.created_at/updated_at,
--    global_ask_jobs.created_at) is stored as text on PostgreSQL too -- see
--    0056_global_ask.sql -- and the watermark has to be comparable to
--    global_ask_jobs.created_at under exactly the canonical order the store
--    already sorts jobs by (created_at, id). A timestamptz watermark would
--    need a cast on every comparison and could drift from that order.
--  * the watermark keyset tie-break is the job `id`, not a rowid/ordinal
--    companion column: global_ask_jobs has no rowid mirror on PostgreSQL, and
--    (created_at, id) is already the canonical order every existing read of
--    that table uses. Nothing extra to add here.
--
-- COLLATE "C" is kept from 0030 for the two identifier columns: the token is a
-- credential compared for exact equality through a unique index, and the
-- database default collation is allowed to be non-C (there is a dedicated
-- regression test migrating onto such a database). Neither column is ever
-- compared against another column in SQL, so no implicit-collation conflict
-- with the surrounding default-collation columns can arise. shared_through_at
-- carries it for the same byte-exact reason and is likewise only ever compared
-- against a bound parameter.
--
-- Deep copy needs no handling here, same as 0030: the notebook deep-copy
-- validated-table set does not include the global-ask tables at all (a global
-- session belongs to no notebook), so these three columns never travel with a
-- copy and there is nothing to clear.

ALTER TABLE global_ask_conversations ADD COLUMN share_token text COLLATE "C";
ALTER TABLE global_ask_conversations ADD COLUMN shared_through_at text COLLATE "C";
ALTER TABLE global_ask_conversations ADD COLUMN shared_through_id text COLLATE "C";

CREATE UNIQUE INDEX idx_global_conversations_share_token
  ON global_ask_conversations(share_token) WHERE share_token IS NOT NULL;
