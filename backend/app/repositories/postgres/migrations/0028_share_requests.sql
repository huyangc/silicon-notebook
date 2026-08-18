-- Mirror SQLite v50's group knowledge sharing P2 table
-- (notebook_share_requests). Zero behavior change: no service or API code
-- reads this table yet -- this migration only lays the schema down.
--
-- notebook_share_requests lets a notebook member who is not that notebook's
-- owner/admin request that their own notebook be shared with a group they
-- belong to; a group admin later approves or rejects the request.
--
-- Unlike notebook_grants.principal_id (v27), this table has no polymorphic
-- column -- group_id always references groups.id -- so it carries a real
-- foreign key with no park-strategy tradeoff. status (pending|approved|
-- rejected) is validated in the application layer, not by a CHECK
-- constraint, matching every other enumerated column in the group-sharing
-- schema (v27's kind, role, principal_type).
--
-- Both notebook_id and group_id are ON DELETE CASCADE: deleting the
-- notebook or the group being requested-into drops the request, so no
-- request can ever point at a notebook or group that no longer exists.
-- requested_by and decided_by reference users(id) with no ON DELETE clause,
-- mirroring every other REFERENCES users(id) column in this schema (e.g.
-- notebook_grants.created_by). decided_at is a genuinely nullable
-- timestamptz -- it is NULL until a group admin approves or rejects the
-- request, with no legacy SQLite ''-sentinel convention to preserve here
-- (see POSTGRES_EMPTY_TIME_SENTINELS in schema_manifest.py): true NULL on
-- both backends.
--
-- idx_share_requests_group is a plain (non-unique) index on
-- (group_id, status) -- the pending-requests-for-this-group lookup a group
-- admin's review queue will use. It is deliberately NOT unique: a notebook
-- can accumulate more than one request to the same group over time (e.g.
-- rejected, then requested again), and nothing in the design requires
-- collapsing them.
--
-- This table is additive with no backfill: no existing row in any other
-- table implies a share request, so there is nothing to populate.

CREATE TABLE notebook_share_requests (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  group_id text COLLATE "C" NOT NULL,
  requested_by text COLLATE "C" NOT NULL,
  status text COLLATE "C" NOT NULL DEFAULT 'pending',
  decided_by text COLLATE "C",
  decided_at timestamptz,
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_notebook_share_requests PRIMARY KEY (id)
);

CREATE INDEX idx_share_requests_group
  ON notebook_share_requests(group_id, status);

ALTER TABLE notebook_share_requests
  ADD CONSTRAINT fk_notebook_share_requests_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE notebook_share_requests
  ADD CONSTRAINT fk_notebook_share_requests_group_id__groups
  FOREIGN KEY (group_id) REFERENCES groups (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE notebook_share_requests
  ADD CONSTRAINT fk_notebook_share_requests_requested_by__users
  FOREIGN KEY (requested_by) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE notebook_share_requests
  ADD CONSTRAINT fk_notebook_share_requests_decided_by__users
  FOREIGN KEY (decided_by) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;
