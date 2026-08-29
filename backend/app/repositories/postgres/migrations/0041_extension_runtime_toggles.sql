-- Mirror SQLite v63 (_migration_63): extension_runtime_toggles.
--
-- The deployment-plugin runtime enable/disable switch + audit (who, when).
-- "Off" is a global admission gate layered OVER the TOML `enabled` field, not
-- a replacement for it: the plugin is still loaded at startup exactly as
-- TOML says, but a disabled row makes every contribution/capability
-- availability evaluation return DISABLED ("admin_disabled") from then on,
-- with no restart.
--
-- THIS TABLE IS NOT READ ON EVERY EVALUATION. Every contribution/capability
-- check reads only an in-process snapshot; this table is the durable layer
-- behind that snapshot. The write path invalidates and rebuilds the snapshot
-- immediately after writing here; every other process converges its own
-- snapshot through a low-frequency poll of this table. The admission check
-- therefore stays zero-I/O -- it never turns into one query per evaluation
-- (which would be both an N+1 and a violation of the existing zero-I/O probe
-- contract).
--
-- TOML `enabled = false` / an unlisted plugin id is a strictly earlier gate
-- ("does it load at all") that this table cannot reach -- the two layers are
-- deliberately kept apart.
--
-- NO ROW MEANS ENABLED. That is the one property that keeps a fresh
-- deployment, or an existing one where no administrator has ever touched
-- this table, behaving exactly as it did before this table existed:
-- `extension_runtime_disabled_ids()` returns the empty set until an admin
-- writes a row.
--
-- `enabled` is declared `boolean` here even though this repository's
-- established convention for a SQLite INTEGER 0/1 flag is a PostgreSQL
-- `bigint` (see shadow/manifest.py's COLUMN_TRANSFORMS comment) -- that
-- convention exists for flags with an internal-only, backend-neutral
-- contract; this column's only contract is the JSON `runtime_enabled: bool`
-- field the admin API layer reads straight off it, so `boolean` is the
-- right native type and there is no shared internal reader to keep aligned
-- with the bigint convention instead. SQLite's twin `enabled INTEGER` column
-- deliberately carries no `CHECK (enabled IN (0,1))` -- this repository does
-- not add one to any of its other INTEGER flag columns either -- so a
-- 0/1-violating value is accepted there. That is an accepted tradeoff, not
-- an oversight: such a value hard-fails the `bool` branch of
-- `sqlite_to_postgres.py`'s `_transform_sqlite_value` (raises
-- `SqliteToPostgresMigrationError`) rather than silently coercing to `true`
-- or `false` during the offline SQLite-to-PostgreSQL migration.
--
-- Rows are NOT deleted when a plugin is removed from EXTENSIONS_CONFIG: an
-- administrator's decision must survive a TOML edit, so the row stays and is
-- picked back up verbatim if the same plugin_id is reconfigured later.
--
-- No secondary index: the table only ever holds a few dozen rows (one per
-- deployment plugin an administrator has ever toggled), so both the admin
-- page's full listing and the admission-refresh read (`enabled = false`)
-- are a cheap sequential scan -- `enabled` is not part of the primary key,
-- so the primary key index cannot serve that filter, and a scan over a few
-- dozen rows needs no index to be fast.

CREATE TABLE extension_runtime_toggles (
  plugin_id text COLLATE "C" NOT NULL,
  enabled boolean NOT NULL,
  updated_by text COLLATE "C" NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_extension_runtime_toggles PRIMARY KEY (plugin_id)
);
