# Model Settings Coherence Design

## Goal

Eliminate stale effective model configuration across concurrent reads,
independent repository/process instances, API save responses, runtime client
resolution, and multiple browser tabs.

## Backend design

`IdentityStore.get_user_model_settings(user_id)` always reads and decodes the
small `user_profiles.model_settings` JSON value from SQLite. It never serves or
fills `model_config_cache`. Existing runtime/facade cache attributes remain as
inert compatibility seams because repository identity contracts and older test
helpers expose them; existing cache `pop` calls may remain harmless, but
effective resolution cannot depend on that dictionary.

This intentionally chooses direct reads instead of a DB-visible cache version.
It avoids a schema migration and removes the cache-coherence protocol rather
than making it more complex. The existing atomic `BEGIN IMMEDIATE` settings
patch remains the only write path used by the PUT route.

The API model-settings routes stay synchronous `def` handlers. FastAPI runs
those handlers in its threadpool, preserving the rule that synchronous SQLite
work does not block the event loop. A PUT response reads the committed snapshot
directly, and the next runtime client resolution does the same.

## Frontend design

`ServiceForm` tracks `baseUrlDirty`, `modelDirty`, and `keyDirty` independently.
Loading the settings panel and accepting a successful save initialize every
flag to `false`. Editing a field updates its matching flag to `true`; an empty
dirty field remains meaningful and clears that field on the backend.

`buildPutPayload` emits only roles containing at least one dirty field and only
the dirty fields within each emitted role. It trims Base URL and model values,
preserves API-key input semantics, and returns `{}` when nothing changed. Thus
two tabs editing disjoint roles or fields produce disjoint patches and cannot
replay untouched stale values.

The Save button is disabled when no field is dirty. The page save handler also
guards an empty payload so programmatic/no-op saves make no request and do not
invalidate model-test ownership or configuration epochs. Existing field-change
callbacks continue invalidating the exact role's draft-test revision.

## Serialization and error behavior

Backend direct reads inherit SQLite snapshot behavior for one read: a read that
already selected an old row may return that old value once. It cannot cache that
value, so every subsequent resolution observes the latest committed row. The
atomic writer continues to serialize concurrent patches and status
invalidation.

Frontend failed saves retain dirty form state for retry. Successful saves rebase
the form from the server response and clear all dirty flags. Existing humanized
HTTP error handling and draft-test locking remain unchanged.

## Verification

Backend deterministic tests pause an old read after its SELECT, commit a write,
resume the old read, and prove the following resolution is new. A separate
repository preloads old settings, another commits, and the reader's next
resolution must be new. API tests cover a PUT response and subsequent runtime
client resolution from the committed identity.

Frontend pure tests cover empty/no-op payloads, dirty empty clears, sparse
fields, and disjoint tab patches. Component/orchestration tests cover setting
each dirty flag, disabling no-op Save, resetting flags after load/success, and
preserving per-role draft-test invalidation.

## Non-goals

- No new database column, migration, or cross-process invalidation channel.
- No removal of compatibility cache attributes in this change.
- No change to model-test request identity, status monotonicity, or provider
  client caches keyed by effective fingerprints.
