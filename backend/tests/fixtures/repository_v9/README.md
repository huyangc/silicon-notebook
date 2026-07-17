# Repository schema-v9 baseline fixture

`baseline.db` is a **frozen** schema-v9 database, produced once from pre-refactor
runtime commit `3334626` by `scripts/generate_repository_contract_fixtures.py`.
The current runtime (schema v18+) cannot recreate it, so it is never rewritten in
normal operation — it is the ground-truth input that proves live migrations keep
upgrading a real v9 database correctly.

`baseline.db` is written with `sqlite3.Connection.backup()` and needs no WAL
sidecar. `storage/` contains one source file plus minimal loadable scale/viz
artifacts. Database rows are never rewritten.

`expected_snapshot.json` and `manifest.json` are **derived** and living: they are
refreshed by replaying the frozen `baseline.db` through the *current* code
(`main` -> `refresh_v9_snapshot`). IDs, timestamps, credentials, tokens and
absolute fixture paths are normalized only in `expected_snapshot.json`.

To regenerate `baseline.db` itself (rare), run the generator with `--rebaseline`
from a checkout whose `backend/app` matches `3334626`; that path is guarded and
otherwise refuses.
