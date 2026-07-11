# Repository schema-v9 baseline fixture

Generated once from runtime commit `3334626` by
`scripts/generate_repository_contract_fixtures.py`.

`baseline.db` is written with `sqlite3.Connection.backup()` and needs no WAL
sidecar. `storage/` contains one source file plus minimal loadable scale/viz
artifacts. IDs, timestamps, credentials, tokens, and absolute fixture paths are
normalized only in `expected_snapshot.json`; database rows remain untouched.
Regeneration is refused after any `backend/app/**/*.py` byte or path changes.
