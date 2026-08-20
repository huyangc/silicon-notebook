-- Mirror SQLite v55 (_migration_55): agent_observations + user_profiles.
-- search_profile_json. Agentic Memory P3, T1. Zero behavior change -- the
-- store, port and MCP tool that read/write these land in later tasks of the
-- same feature; this migration only lays the schema down.
--
-- agent_observations is an append-only log of short lines an external Agent
-- writes via the (later-landing) add_observation MCP tool -- one line
-- "I noticed X while working in this notebook". They feed the per-member
-- overlay consolidation job as UNTRUSTED input, never the answer/report
-- path itself.
--
-- Eight already-decided trade-offs (recorded so the next reader can tell a
-- decision from an oversight):
--
-- 1. owner_id is the identity chain -- whose overlay this observation
--    eventually feeds. NOT NULL DEFAULT '' sentinel, not nullable, same
--    reasoning as agent_notebook_profile.owner_id / notebook_grants.
--    principal_id (see 0029_agent_profile.sql / 0027_group_sharing.sql): a
--    nullable owner_id would make every row incomparable under a UNIQUE
--    index that includes it. It is not part of this table's PRIMARY KEY
--    (unlike agent_notebook_profile, whose PK IS (notebook_id, owner_id,
--    label)), but it IS the second column of the idempotency unique index
--    below -- making it nullable would let NULL-owner rows escape that
--    index entirely (duplicate client_request_id writes would land) and
--    would drift the shadow park column off client_request_id.
--
-- 2. agent_profile_id is a bare provenance id with NO foreign key -- same
--    precedent as sources.agent_profile_id (v48/0026) and catalog_
--    candidates.job_id (v39/0017): an out-of-band identifier that is never
--    joined against, only carried for attribution and per-Agent clearing.
--
-- 3. No incoming foreign key on this table at all (it stays a leaf table);
--    the one OUTGOING foreign key to notebooks is kept -- it is the only
--    mechanism that cascades this table's rows away when the notebook
--    itself is deleted, mirroring agent_notebook_profile's own FK to
--    notebooks.
--
-- 4. client_request_id stays NULLABLE, paired with a PARTIAL unique index
--    over (notebook_id, owner_id, agent_profile_id, client_request_id)
--    WHERE client_request_id IS NOT NULL. This nullable column + partial
--    index IS the shadow park strategy itself: a write that carries no
--    client_request_id parks for free by simply not participating in the
--    unique surface. The application layer must NEVER write NULL here --
--    the MCP write path normalizes an empty/missing client_request_id into
--    a rejected request the same way memory_inputs.
--    normalize_client_request_id already does for Memory proposals -- so in
--    practice every row this service ever writes carries a real value; NULL
--    is a shape this column supports for the shadow migration's benefit
--    only. Do NOT change this column to NOT NULL DEFAULT '': that would push
--    every "no request id supplied" row onto the SENTINEL_TEXT park path
--    (there is no such row today, but the column's nullability is the
--    contract, not an accident of the current caller). Do NOT turn the
--    index non-partial either -- a non-partial unique index would make the
--    NULL park strategy inapplicable to this surface.
--
-- 5. No other index is created. There are exactly two read paths --
--    recent_observations/list_observations scan by (notebook_id, owner_id)
--    and the idempotency check point-reads the partial unique index above
--    -- and every read returns at most a bounded sample while the
--    per-(notebook_id, owner_id) ring eviction (AGENT_OBSERVATION_RING_MAX,
--    landing with the store in a later task) bounds each group. The TABLE
--    total is NOT capped -- it grows with notebooks x members -- so this is
--    a registered cost call, not a proof: revisit with measurements if the
--    scan ever shows up, rather than citing this note as "no index needed".
--
-- 6. Deep notebook copy does NOT carry this table -- same as agent_
--    notebook_profile/agent_profile_jobs (see 0029_agent_profile.sql):
--    observations are process state about how an Agent has been using THIS
--    notebook, not knowledge a copy should inherit. A fresh copy starts
--    with an empty observation log.
--
-- 7. scripts/merge_dbs.py unions this table by NOTEBOOK ownership
--    (NOTEBOOK_SCOPED_TABLES), not as a deployment-global table -- unlike
--    retrieval_experiences (v54/0032), which is intentionally
--    notebook-less. Every row here belongs to one notebook via its
--    REQUIRED notebook_id foreign key, so merging two deployments
--    re-parents each notebook's observation rows the same way every other
--    per-notebook table already does.
--
-- 8. created_at accepts ONLY ISO timestamps -- never the empty string.
--    It is deliberately NOT in POSTGRES_EMPTY_TIME_SENTINELS: the SQLite
--    side would accept '' without any symptom, and forward shadow would
--    then hand '' to this timestamptz and poison the whole direction (the
--    notebook_share_requests.decided_at lesson). Same handling as
--    retrieval_experiences.
--
-- 9. user_profiles.search_profile_json is nullable; NULL means "the user
--    has never set a preference and no consolidation job has ever written
--    one" -- same contract as v45/0023's ui_mode. It is NOT backfilled for
--    existing rows, and it does NOT go into POSTGRES_JSON_COLUMNS even
--    though it holds a JSON document: like ui_mode, it is read and written
--    whole (one document per user, no per-key query ever touches it), and
--    the existing ui_mode precedent already stores a comparably-shaped
--    small preference value as plain text rather than jsonb. Keeping it
--    plain text avoids a JSON-parse-on-every-read cost this column's access
--    pattern never needs.

CREATE TABLE agent_observations (
  id                text COLLATE "C" NOT NULL,
  notebook_id       text COLLATE "C" NOT NULL,
  owner_id          text COLLATE "C" NOT NULL DEFAULT '',
  agent_profile_id  text COLLATE "C" NOT NULL DEFAULT '',
  text              text COLLATE "C" NOT NULL DEFAULT '',
  client_request_id text COLLATE "C",
  created_at        timestamptz NOT NULL,
  CONSTRAINT pk_agent_observations PRIMARY KEY (id)
);

ALTER TABLE agent_observations
  ADD CONSTRAINT fk_agent_observations_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

-- agent_observations.owner_id and agent_observations.agent_profile_id
-- deliberately carry NO foreign key -- see the module-level comment above.

CREATE UNIQUE INDEX idx_agent_observations_request
  ON agent_observations(notebook_id, owner_id, agent_profile_id, client_request_id)
  WHERE client_request_id IS NOT NULL;

ALTER TABLE user_profiles ADD COLUMN search_profile_json text COLLATE "C";
