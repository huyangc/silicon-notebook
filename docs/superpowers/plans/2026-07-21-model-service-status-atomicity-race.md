# Model Service Status Atomicity Race Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make model-settings patches, effective-status invalidation, and identity-conditional status recording serializable across independent SQLite connections.

**Architecture:** `IdentityStore` owns both atomic write boundaries. A settings patch starts `BEGIN IMMEDIATE`, reads the persisted JSON directly, applies three-state field semantics, compares all six resolved fingerprints, writes settings, and deletes changed statuses before one commit. Status persistence independently starts `BEGIN IMMEDIATE`, resolves the current persisted identity inside that transaction, and runs the existing monotonic UPSERT only when the originating fingerprint still matches.

**Tech Stack:** Python 3.13, FastAPI, standard-library SQLite, pytest, threading primitives.

## Global Constraints

- Use strict RED → GREEN TDD for each behavior change.
- Interleaving tests must use separate `SQLiteRepository` instances and therefore separate process-local locks/connections.
- Transactional reads must bypass `model_config_cache`; invalidate the writing store's cache only after commit.
- Preserve `None` = unchanged, `""` = clear, non-empty = set patch semantics.
- Preserve fixed-precision UTC occurrence ordering and observed/error tie precedence.
- Do not regenerate the frozen repository-v9 database or storage.

---

### Task 1: Atomic settings patch and status invalidation

**Files:**
- Modify: `backend/tests/test_model_status_store.py`
- Modify: `backend/tests/test_user_model_settings_store.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/api/routes.py`

**Interfaces:**
- Produces: `IdentityRepository.patch_user_model_settings_atomic(user_id, patch) -> dict`.
- Patch shape: role keys map to optional field dictionaries; missing/`None` roles and `None` fields are unchanged, empty strings clear, other strings set.

- [x] Add a separate-repository concurrent patch test that preloads stale caches, starts two role-disjoint patches together, and asserts neither update is lost and both local caches reload the final persisted value.
- [x] Add a transaction trace/lock test proving `BEGIN IMMEDIATE` precedes the persisted settings read and remains held through settings update plus changed-status deletion.
- [x] Run the focused tests and verify RED because the atomic patch operation does not exist.
- [x] Implement direct JSON read, patch application, all-six before/after resolution, settings update, and changed-role deletion in one `database.write()` block with `begin_immediate()` first; invalidate cache after successful commit.
- [x] Replace the route's cached read / set / clear sequence with `payload.model_dump(exclude_unset=True)` passed to the atomic operation.
- [x] Rerun store/API tests and verify GREEN.

### Task 2: Identity-conditional monotonic status record

**Files:**
- Modify: `backend/tests/test_model_status_store.py`
- Modify: `backend/tests/test_model_status_service.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/model_status.py`

**Interfaces:**
- Produces: `IdentityRepository.record_model_service_status_if_current(..., expected_fingerprint, ...) -> bool`.
- Retains: existing normalized `checked_at` and monotonic/tie UPSERT predicate.

- [x] Add deterministic separate-repository interleavings proving: old observed status cannot write after a settings commit; old manual status written first is deleted by the later settings commit; and a valid new manual status waiting behind the settings transaction survives.
- [x] Add a service-level probe interleaving proving `test_one` declines an old descriptor after settings rotate and returns the current snapshot rather than persisting stale success.
- [x] Run the focused tests and verify RED because status check and write are not one transaction.
- [x] Implement the conditional record with `BEGIN IMMEDIATE`, direct persisted-settings resolution, configured/fingerprint validation, and the existing UPSERT in one transaction.
- [x] Route manual and observed status persistence through the conditional operation and propagate its boolean result where the response semantics require it.
- [x] Rerun status store/service/provider/API tests and verify GREEN.

### Task 3: Contracts, documentation, commits, and full verification

**Files:**
- Modify generated repository semantic fixtures only if their focused checks fail.
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Append: `.superpowers/sdd/final-review-fixes-report.md`

**Interfaces:** None.

- [x] Run repository port/facade/ownership contracts; use the designated generator only for demonstrated semantic-surface changes.
- [x] Update all three synchronized documentation files with the transaction and serialization invariants.
- [x] Commit the atomic implementation/tests and documentation as logical changes.
- [x] Run `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check.sh` successfully from the final committed state.
- [x] Run `cd frontend && npm run build` separately.
- [x] Run cumulative diff, frozen-v9, credential/provider-payload, and clean-worktree audits; append exact RED/GREEN evidence to the final-review report.
