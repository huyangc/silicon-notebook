-- Mirror SQLite v49's group knowledge sharing P1 tables (groups,
-- group_members, notebook_grants). Zero behavior change: no service or API
-- code reads these tables yet -- this migration only lays the schema down.
--
-- groups is the group row itself. kind (project|department|domain) and every
-- other enumerated column added by this migration is validated in the
-- application layer, not by a CHECK constraint -- the design doc's
-- already-decided list requires this, because the forward-shadow UNIQUE park
-- strategy on notebook_grants (see below) depends on at least one of
-- principal_type/principal_id staying free of both CHECK and FK so it
-- remains a legal park column; a CHECK on principal_type would remove that
-- candidate.
--
-- group_members is deliberately shaped like notebook_members: composite
-- (group_id, user_id) primary key, ON DELETE CASCADE on group_id (deleting a
-- group drops its membership rows), and NO ON DELETE clause on user_id --
-- the same asymmetry notebook_members already uses for its own user_id FK.
--
-- notebook_grants is the polymorphic authorization edge: one row grants role
-- (viewer|admin) to principal_type (user|group|group_admins|everyone)
-- identified by principal_id. principal_id is deliberately a bare,
-- unconstrained text column with NO foreign key -- it references either
-- users.id or groups.id depending on principal_type, or is NULL for the
-- everyone principal (no single-table FK can express that). This mirrors the
-- catalog_candidates.job_id precedent (v17): a real FK here would make
-- whichever table it points to a non-leaf table for the forward-shadow
-- replicator's UNIQUE-conflict park strategy, and the
-- UNIQUE (notebook_id, principal_type, principal_id) index below needs a
-- column it CAN safely park a poison value into during conflict resolution.
--
-- All three tables are additive with no backfill: no existing row in any
-- other table implies a group or a grant, so there is nothing to populate.

CREATE TABLE groups (
  id text COLLATE "C" NOT NULL,
  name text COLLATE "C" NOT NULL,
  kind text COLLATE "C" NOT NULL DEFAULT 'project',
  description text COLLATE "C" NOT NULL DEFAULT '',
  created_by text COLLATE "C",
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_groups PRIMARY KEY (id)
);

CREATE TABLE group_members (
  group_id text COLLATE "C" NOT NULL,
  user_id text COLLATE "C" NOT NULL,
  role text COLLATE "C" NOT NULL DEFAULT 'member',
  added_at timestamptz NOT NULL,
  added_by text COLLATE "C",
  CONSTRAINT pk_group_members PRIMARY KEY (group_id, user_id)
);

CREATE INDEX idx_group_members_user ON group_members(user_id);

CREATE TABLE notebook_grants (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  principal_type text COLLATE "C" NOT NULL,
  principal_id text COLLATE "C",
  role text COLLATE "C" NOT NULL,
  created_by text COLLATE "C",
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_notebook_grants PRIMARY KEY (id),
  CONSTRAINT uq_notebook_grants_principal
    UNIQUE (notebook_id, principal_type, principal_id)
);

CREATE INDEX idx_notebook_grants_nb ON notebook_grants(notebook_id);
CREATE INDEX idx_notebook_grants_principal
  ON notebook_grants(principal_type, principal_id);

ALTER TABLE groups
  ADD CONSTRAINT fk_groups_created_by__users
  FOREIGN KEY (created_by) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE group_members
  ADD CONSTRAINT fk_group_members_group_id__groups
  FOREIGN KEY (group_id) REFERENCES groups (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE group_members
  ADD CONSTRAINT fk_group_members_user_id__users
  FOREIGN KEY (user_id) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE group_members
  ADD CONSTRAINT fk_group_members_added_by__users
  FOREIGN KEY (added_by) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;

ALTER TABLE notebook_grants
  ADD CONSTRAINT fk_notebook_grants_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks (id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

ALTER TABLE notebook_grants
  ADD CONSTRAINT fk_notebook_grants_created_by__users
  FOREIGN KEY (created_by) REFERENCES users (id)
  ON UPDATE NO ACTION ON DELETE NO ACTION;

-- notebook_grants.principal_id deliberately carries NO foreign key -- see the
-- module-level comment above. Do not add one: it references two different
-- tables depending on principal_type and would also make groups/users a
-- non-leaf table for the forward-shadow UNIQUE park strategy.
