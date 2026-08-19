-- Mirror SQLite v54: retrieval_experiences, Agentic Memory P2's
-- DEPLOYMENT-GLOBAL retrieval-strategy experience library.
--
-- One entry = "in THIS SHAPE of question, THIS retrieval action is / is not
-- worth reaching for", plus one short model-written rationale. Entries are
-- shared across users and across notebooks, which is why this table
-- deliberately has NO notebook_id and NO owner column -- that is the table's
-- definition, not an omission: it stores general tactics for HOW to search,
-- never anyone's content and never any one library's content.
--
-- Seven already-decided trade-offs (recorded so the next reader can tell a
-- decision from an oversight):
--
-- 1. `id` is a CONTENT-ADDRESSED synthesised primary key: the deterministic
--    hash of (situation fingerprint, action) computed by
--    retrieval_experience_projection.experience_id. That buys three things at
--    once:
--    (a) "one row per (situation, action)" is enforced by the primary key
--        itself, so no second unique surface is needed;
--    (b) scripts/merge_dbs.py unions this table across deployments with
--        INSERT OR IGNORE, which requires ids that are stable across
--        independent deployments and do not collide -- a content hash is
--        exactly that, while an incrementing integer id would SILENTLY DROP
--        ROWS (two deployments' "entry 1" collide on the primary key and the
--        later one is discarded without a word);
--    (c) parking: replicator.py's _build_unique_surfaces resolves a surface to
--        REPLICATION_KEY when the surface's columns equal the spec's
--        replication key. A single-column primary key equal, verbatim, to the
--        shadow manifest's declared replication key means no sentinel column,
--        no nullable column and no leaf delete/reinsert.
--
-- 2. Do NOT add a separate UNIQUE index on the situation fingerprint.
--    De-duplication is already item 1's primary key; a second unique surface
--    is one more surface whose park strategy has to be hand-derived -- and a
--    bare text surface would fall onto the more expensive sentinel/candidate
--    search path.
--
-- 3. No foreign key, no CHECK. `action` and `polarity` draw on closed
--    application-layer vocabularies (RETRIEVAL_ACTIONS / EXPERIENCE_POLARITIES
--    in retrieval_experience_projection.py), the same convention v29's tables
--    use for label/status; a CHECK would also break item 1(c)'s park
--    precondition. No incoming foreign key either, so this stays a leaf table.
--
-- 4. Both timestamps are NOT NULL, so this table needs NO entry in
--    schema_manifest.POSTGRES_EMPTY_TIME_SENTINELS -- that set exists only for
--    the "SQLite TEXT NOT NULL DEFAULT '' optional timestamp <-> PostgreSQL
--    nullable timestamptz" shape. Every write path here fills both columns.
--
-- 5. provenance_json holds OPAQUE RUN IDS ONLY -- never notebook_id, never
--    user_id. Either of those would let "this person asked this kind of
--    question in that library" be reassembled out of a table every deployment
--    reads. Its purpose is idempotence: when the same batch of runs is
--    distilled twice, the run-id set de-duplicates so `support` is not counted
--    twice. It is bounded (RETRIEVAL_EXPERIENCE_PROVENANCE_MAX), and "one
--    distillation batch reads no more runs than that bound" is an invariant
--    pinned by a test -- a batch larger than the bound would evict run ids that
--    the next batch then re-counts.
--
-- 6. `support` and `adopted` are two different numbers and must not be merged.
--    `support` counts runs backing the conclusion (incremented by
--    distillation); `adopted` counts times the model actually picked that
--    action after the entry was injected (incremented by the injection side).
--    Eviction is ascending (adopted, support, updated_at): unused entries go
--    first, then thinly supported ones, and only then by age. One combined
--    number could not express "plenty of evidence, never once adopted".
--
-- 7. No secondary index on purpose. The row count is hard-capped
--    (RETRIEVAL_EXPERIENCE_MAX_ENTRIES) and there are exactly two read paths:
--    a primary-key point lookup, and "read the whole table and score it in
--    memory against the current situation" (a bounded full scan in the
--    hundreds of rows). The (adopted, support, updated_at) eviction ordering
--    rides that same bounded scan. An index on a table that stays a few
--    hundred rows buys nothing.

CREATE TABLE retrieval_experiences (
  id text COLLATE "C" NOT NULL,
  situation_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  action text COLLATE "C" NOT NULL DEFAULT '',
  polarity text COLLATE "C" NOT NULL DEFAULT '',
  rationale text COLLATE "C" NOT NULL DEFAULT '',
  support bigint NOT NULL DEFAULT 0,
  adopted bigint NOT NULL DEFAULT 0,
  provenance_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_retrieval_experiences PRIMARY KEY (id)
);
