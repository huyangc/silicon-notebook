-- Mirror SQLite v70 (_migration_70): ask_jobs.client_request_id + the partial
-- unique index idx_ask_jobs_client_request. The browser submission's
-- idempotency key: the official UI reuses the per-tab mirror id of the
-- reasoning preflight (frontend/app/ask-intent-persist.ts), so a reload that
-- lands between the preflight's hand-off and the server's `started` event can
-- re-POST the same submission and ATTACH to the job it already created instead
-- of creating a second job (AskExecutionCoordinator.start's attach/follow
-- path). The AskStateStorePort method that honours it is
-- begin_or_attach_durable_job.
--
-- Already-decided trade-offs (recorded so the next reader can tell a decision
-- from an oversight):
--
-- 1. client_request_id stays NULLABLE with NO sentinel default, paired with a
--    PARTIAL unique index WHERE client_request_id IS NOT NULL. This is the same
--    nullable-column-plus-partial-index shape 0033_agent_observations.sql chose
--    for agent_observations.client_request_id, for the same reason: the shape
--    IS the forward-shadow park strategy (a row that carries no key parks for
--    free by not participating in the unique surface; the replicator resolves
--    this surface to the NULL park with park_column == "client_request_id").
--    Do NOT change the column to NOT NULL DEFAULT '' -- every keyless row would
--    then collide on '' under the index, or (with a sentinel exclusion) push
--    the surface onto the SENTINEL_TEXT park path. Do NOT make the index
--    non-partial either.
--
-- 2. created_by leads the index, not notebook_id. The key is minted per
--    browser submission by ONE user; scoping the surface by user means a forged
--    or colliding key from another account can never attach to this user's
--    job. A key already spent by the same user in ANOTHER notebook is a
--    conflict (AskRequestKeyConflict), never an attach -- see the store.
--
-- 3. Existing rows stay NULL: nothing is backfilled. A NULL key never attaches
--    to anything, so every pre-migration job behaves exactly as before, and a
--    compatibility caller that omits the key (MCP, scripts, the synchronous
--    /ask route) keeps the "always create" semantics verbatim.
--
-- 4. ADD COLUMN IF NOT EXISTS keeps the column half re-runnable; the index is
--    a plain CREATE UNIQUE INDEX (no IF NOT EXISTS) because the shadow
--    catalog parser (app/migration/shadow/postgres_catalog.py) only registers
--    that exact form as an expected unique surface -- the same form 0049 used
--    for idx_notebook_delete_jobs_one_active. The migrator's ledger is what
--    makes an applied file a no-op on re-run. ask_jobs is small per user and
--    the index is a narrow two-column btree over already-COLLATE-"C" text
--    (0001_initial.sql for created_by; the new column is declared COLLATE "C"
--    below), so a bare (unqualified) index inherits that collation with no
--    explicit opclass -- the same reasoning migration 0049 recorded for its
--    three btree indexes. No CONCURRENTLY: the table is far too small for the
--    build to matter and the migrator runs every file inside one transaction.
--
-- Rollback: `DROP INDEX idx_ask_jobs_client_request; ALTER TABLE ask_jobs DROP
-- COLUMN client_request_id;` -- purely additive; the store's INSERT names the
-- column, so the application must be rolled back first.

ALTER TABLE ask_jobs ADD COLUMN IF NOT EXISTS client_request_id text COLLATE "C";

CREATE UNIQUE INDEX idx_ask_jobs_client_request
  ON ask_jobs(created_by, client_request_id)
  WHERE client_request_id IS NOT NULL;
