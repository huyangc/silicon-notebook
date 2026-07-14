# Agent Memory ↔ master conflict-resolution report

## Status

DONE

## Root cause

The Agent Memory branch and `origin/master` evolved independently from the same
baseline and both changed repository composition contracts, migration fixtures,
and static boundary ledgers. The substantive collision was that both histories
claimed schema version 11: Memory used it for its private Memory/Agent tables,
while master used v11 and v12 for SQLite hot-path indexes. Master also added a
startup readiness lifecycle and thread-local SQLite connection reuse, which
invalidated two test/smoke assumptions made before those contracts existed.

## Conflict files

- `backend/app/main.py`
- `backend/app/repositories/sqlite/migrations.py`
- `backend/app/services/sqlite_repository.py`
- `backend/tests/fixtures/repository_v9/expected_snapshot.json`
- `backend/tests/fixtures/repository_v9/manifest.json`
- `backend/tests/test_follow_chain_repository.py`
- `backend/tests/test_repository_callers_static.py`
- `backend/tests/test_repository_surface_manifest.py`
- `backend/tests/test_repository_v9_fixture.py`
- `backend/tests/test_sqlite_migrator_component.py`
- `scripts/verify_repository_snapshot.py`

## Key decisions

1. Preserved master's v11/v12 index migrations and moved Memory/Agent schema to
   v13. The v13 migration idempotently ensures master's four indexes so both
   historical v11 lineages (Memory-v11 and master-v11) converge safely.
2. Composed the stateful MCP lifespan with master's asynchronous startup warmup
   and readiness lifecycle; neither lifecycle replaces the other.
3. Regenerated/merged the frozen v9 snapshot and exact verifier manifests for
   v9, v10, both v11 lineages, and v12 upgrading to v13.
4. Kept the union of Memory repository/API consumers and master's connection
   reuse, cache warmup, diagnostics, readiness, and offline-packaging contracts.
5. Updated `README.md`, `README_zh.md`, `AGENTS.md`, architecture/spec tracking,
   and the Memory implementation plan to describe schema v13 consistently.
6. Adapted isolated route/MCP smoke tests to mark their throwaway apps ready;
   dedicated readiness tests continue to exercise the real startup gate.
7. Made the Memory embedding idempotency test deterministic and traced the
   reused SQLite connection directly, matching the new runtime contract without
   weakening production behavior.

## Verification

- Focused backend migration/readiness/connection/legacy suite: 51 passed.
- Focused repository v9/surface/runtime suite: 120 passed.
- Focused Memory/MCP/API/retrieval/promotion suite: 170 passed.
- Focused migration-lineage/v9/index suite: 31 passed.
- Focused repository static/surface suite: 27 passed.
- Focused frontend Memory tests: 40 passed.
- Final documentation + migration regression: 24 passed.
- `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`:
  backend 2990 passed, 1 skipped; frontend 189 passed; TypeScript and production
  build passed.
- `cd frontend && npm run build`: passed independently.
- `git diff --check`, unmerged-index check, and conflict-marker scan: clean.

## Merge commit

`a275ec0ea1a2f1b1c321b670bac13832d7ecf684`

Parents:

- Agent Memory: `8dfc70aa0077b9aa31d4290f14bde2a97098e468`
- `origin/master`: `af4e93b0dd77d1814cf1b7ce7904c8d42eee8dd0`

## Concerns

None. The branch was not pushed. This report is committed separately so it can
record the immutable merge commit SHA accurately.
