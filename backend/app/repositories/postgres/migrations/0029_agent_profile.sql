-- Mirror SQLite v51's Agentic Memory P1 tables (agent_notebook_profile,
-- agent_profile_jobs). Zero behavior change: no service or API code reads or
-- writes these tables yet -- this migration only lays the schema down.
--
-- agent_notebook_profile holds the understanding blocks themselves. owner_id =
-- '' is this notebook's shared base layer; a non-empty owner_id is that one
-- member's private overlay. agent_profile_jobs is one row per chain
-- (notebook / member) carrying the consolidation state and doubling as the
-- threshold counter.
--
-- Six already-decided trade-offs (recorded so the next reader can tell a
-- decision from an oversight):
--
-- 1. owner_id is a NOT NULL DEFAULT '' sentinel, not a nullable column. Same
--    reasoning, verbatim, as v27's notebook_grants.principal_id (see
--    0027_group_sharing.sql): both backends treat NULLs as distinct in UNIQUE
--    comparisons, so a nullable owner_id would silently exempt every base-layer
--    row from the primary key -- duplicate base rows could accumulate, clearing
--    would not clear them all, and the consolidation job's optimistic
--    concurrency would stop working. The design doc's remark that a nullable
--    column is the cheapest forward-shadow park shape addresses parking, and
--    parking is solved more thoroughly by item 2 below.
--
-- 2. The unique surface is a composite PRIMARY KEY, not a separate UNIQUE
--    constraint (mirroring v25's notebook_object_schemas
--    PRIMARY KEY (notebook_id, object_type), DECLARED_PK in the shadow
--    manifest). This IS the park strategy: replicator.py's
--    _build_unique_surfaces takes its first branch when the surface's columns
--    equal the table's replication key, yielding REPLICATION_KEY -- so as long
--    as the shadow manifest declares these exact columns as the replication
--    key, no sentinel column, no nullable column and no leaf delete/reinsert is
--    needed.
--
-- 3. owner_id deliberately carries NO foreign key to users: the '' sentinel is
--    incompatible with an FK, and the same precedent already exists for
--    notebook_grants.principal_id, catalog_candidates.job_id and
--    sources.agent_profile_id -- provenance/ownership columns store a bare id.
--    It also keeps both tables free of any incoming foreign key (they stay leaf
--    tables). Do NOT add a CHECK or an FK to owner_id or label: either would
--    break item 2's park precondition, and every enumerated column here
--    (label, updated_origin, status) is validated in the application layer.
--
-- 4. Every primary-key column is explicitly NOT NULL, so neither table needs an
--    entry in the shadow migration's _SQLITE_NULL_GUARD_KEYS (that set exists
--    only for SQLite rowid tables whose declared PK is nullable).
--
-- 5. No partial unique index, so no _UNIQUE_PREDICATES pin is required in
--    replicator.py. Single-flight is a primary-key row CAS on
--    agent_profile_jobs (UPDATE ... WHERE (notebook_id, owner_id) = ... AND
--    status NOT IN ('queued','running'), decided on rowcount): cross-process
--    semantics equal to idx_catalog_jobs_one_active /
--    idx_kg_build_jobs_one_running, and stronger -- one PK row lock, no second
--    unique surface. Why not copy catalog_jobs' "one row per run + partial
--    unique index" shape: that shape exists because a candidate review queue and
--    per-window progress need per-run history rows; here the UI only polls
--    "is this chain busy right now", while the threshold counter has to hang off
--    the chain -- one per-chain state row satisfies both and costs one fewer
--    partial unique surface to hand-pin in _UNIQUE_PREDICATES (a missed pin
--    fails the ShadowReplicator's construction closed for the whole schema
--    generation).
--
-- 6. Block history lives in the history_json ring column rather than a separate
--    agent_profile_changes table. P1's interface is only view/edit/clear/manual
--    rebuild -- no history browsing and no rollback -- so a queryable audit
--    table buys no P1 capability, while each new table costs a manifest entry, a
--    copy_rank, a park surface and a deep-copy registration. The knowhow
--    record_change shape exists to serve whole-table reverse delta replay with
--    before/after fingerprints; a block rollback is a single-row value swap.
--    Block values are hard-capped in characters, so a 20-entry ring stays a few
--    KB per row. The half of the knowhow contract that DOES carry over: a
--    history entry must be appended inside the same write transaction, after the
--    block UPDATE.
--
-- No secondary indexes on purpose: the primary key covers every read path
-- (blocks are read by the (notebook_id, owner_id) prefix and written by exact
-- PK; the startup sweep over agent_profile_jobs.status runs once per process
-- over a table sized by "chains that have ever fired").

CREATE TABLE agent_notebook_profile (
  notebook_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL DEFAULT '',
  label text COLLATE "C" NOT NULL,
  value text COLLATE "C" NOT NULL DEFAULT '',
  evidence_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  history_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  revision bigint NOT NULL DEFAULT 1,
  updated_by text COLLATE "C" NOT NULL DEFAULT '',
  updated_origin text COLLATE "C" NOT NULL DEFAULT 'job',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_agent_notebook_profile PRIMARY KEY (notebook_id, owner_id, label)
);

CREATE TABLE agent_profile_jobs (
  notebook_id text COLLATE "C" NOT NULL,
  owner_id text COLLATE "C" NOT NULL DEFAULT '',
  status text COLLATE "C" NOT NULL DEFAULT 'idle',
  pending_signal bigint NOT NULL DEFAULT 0,
  runs bigint NOT NULL DEFAULT 0,
  blocks_written bigint NOT NULL DEFAULT 0,
  failure_reason text COLLATE "C" NOT NULL DEFAULT '',
  diagnostic text COLLATE "C" NOT NULL DEFAULT '',
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_agent_profile_jobs PRIMARY KEY (notebook_id, owner_id)
);

ALTER TABLE agent_notebook_profile
  ADD CONSTRAINT fk_agent_notebook_profile_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE agent_profile_jobs
  ADD CONSTRAINT fk_agent_profile_jobs_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

-- agent_notebook_profile.owner_id and agent_profile_jobs.owner_id deliberately
-- carry NO foreign key -- see the module-level comment above. Do not add one:
-- the '' base-layer sentinel matches no users row, and an incoming FK would also
-- add an edge to the forward-shadow parent closure.
