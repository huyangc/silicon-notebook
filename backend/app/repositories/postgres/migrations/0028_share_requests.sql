-- Mirror SQLite v50's group knowledge sharing P2 table
-- (notebook_share_requests). Zero behavior change: no service or API code
-- reads this table yet -- this migration only lays the schema down.
--
-- notebook_share_requests's axis is easy to get backwards, so it is spelled
-- out explicitly: requested_by is someone who already has admin/owner
-- (manage) rights on notebook_id -- they can share it unilaterally to any
-- group whose *admin* they also are -- but is merely an ordinary member (not
-- necessarily an admin) of group_id, the group they are asking to
-- contribute the notebook to. A group admin sharing into their own group
-- never goes through this table at all; they write a notebook_grants row
-- directly. This table exists only for the other half: a manage-rights
-- holder on a notebook who is *not* an admin of the group they want to
-- share into has to ask, and a group admin later approves or rejects the
-- request.
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
-- uq_share_requests_one_pending is a *partial* unique index on
-- (notebook_id, group_id, status) scoped to WHERE status = 'pending' -- it
-- collapses to a plain two-column uniqueness constraint on
-- (notebook_id, group_id) for pending rows only, so the same notebook can
-- never accumulate two simultaneously-pending requests into the same group.
-- The third column (status) is not needed for that semantics alone; it is
-- added deliberately so the forward-shadow UNIQUE park strategy has a
-- non-FK TEXT column to park a poisoned row into (SENTINEL_TEXT) without
-- having to fall back to requiring this table forever have no incoming
-- foreign key (LEAF_DELETE) -- the same tradeoff already made for
-- uq_notebook_grants_principal in v27. Consumers must match status against
-- its known values with exact equality and never write something like
-- status != 'pending' to mean "already decided" -- mirroring the
-- principal_type red line in v27: a row temporarily parked by the shadow
-- replicator carries a sentinel string in status, and exact matching
-- against 'pending'/'approved'/'rejected' makes such a row fail safe (it
-- matches none of them) instead of being silently miscounted as decided.
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

CREATE UNIQUE INDEX uq_share_requests_one_pending
  ON notebook_share_requests(notebook_id, group_id, status)
  WHERE status = 'pending';

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
