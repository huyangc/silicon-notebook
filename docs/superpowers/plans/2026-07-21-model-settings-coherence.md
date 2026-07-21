# Model Settings Coherence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backend effective model settings DB-coherent across reads/processes and make frontend saves sparse enough that stale tabs cannot overwrite untouched fields.

**Architecture:** `IdentityStore.get_user_model_settings` becomes a direct SQLite read; the existing cache objects stay compatibility-only and never affect resolution. `ServiceForm` gains one dirty flag per editable field, a shared form-construction helper resets flags, and `buildPutPayload` emits only dirty roles/fields while the UI disables/guards no-op saves.

**Tech Stack:** Python 3.13, FastAPI, standard-library SQLite, pytest, TypeScript, React 19, Node test runner, Vitest.

## Global Constraints

- Strict RED → GREEN TDD for every production behavior change.
- Preserve the existing atomic `BEGIN IMMEDIATE` settings patch and three-state backend patch semantics.
- Keep the compatibility `model_config_cache` attributes wired by identity; do not use them to serve or fill model settings.
- Keep `/me/model-settings` handlers synchronous so FastAPI runs SQLite work outside the event loop.
- Preserve draft-test role/revision ownership and all provider/status safety behavior.
- Do not modify frozen repository-v9 fixtures or `fangan_done.md`.

---

### Task 1: DB-coherent backend reads and runtime resolution

**Files:**
- Modify: `backend/tests/test_user_model_settings_store.py`
- Modify: `backend/tests/test_model_settings_api.py`
- Modify: `backend/tests/test_model_provider_runtime.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`

**Interfaces:**
- Retains: `IdentityStore.get_user_model_settings(user_id: str) -> dict`.
- Changes: every call reads `user_profiles.model_settings` directly and never reads or writes `model_config_cache`.
- Retains: `IdentityStore.resolve_model_config(user, role)` and runtime client factories, now transitively DB-coherent.

- [x] Add a deterministic test that pauses an old read after SELECT/inside decode, commits a settings patch through a separate repository, resumes the old read, and asserts the next resolve observes the committed model.
- [x] Add a two-repository test that preloads old settings in the reader, commits through the writer, and asserts the reader's next settings/effective resolution is new.
- [x] Add an API test that preloads the API repository, commits an external update, sends `PUT {}`, and asserts the response reflects the committed DB snapshot without reverting it.
- [x] Add a runtime-provider test that preloads an old client/config, rotates through another repository, and asserts the reader constructs/resolves the new model client.
- [x] Run the exact tests and capture RED showing stale cached identities.
- [x] Remove cache serving/filling from `get_user_model_settings`; keep compatibility cache attributes and harmless writer invalidations intact.
- [x] Rerun the exact tests and adjacent identity/provider/API suites for GREEN.
- [x] Run `git diff --check` and commit the backend tests/implementation.

### Task 2: Sparse per-field frontend settings patches

**Files:**
- Modify: `frontend/app/model-settings.ts`
- Modify: `frontend/app/model-settings.test.mjs`
- Modify: `frontend/app/model-service-panel.tsx`
- Modify: `frontend/app/model-service-panel.component.test.tsx`
- Modify: `frontend/app/page.tsx`
- Modify if semantic ownership changes: generated repository contract fixtures only through their designated generator.

**Interfaces:**
- Changes: `ServiceForm` adds `baseUrlDirty: boolean` and `modelDirty: boolean` alongside `keyDirty`.
- Produces: `modelSettingsForms(view: ModelSettingsView) -> Record<ModelRole, ServiceForm>` with all dirty flags false.
- Changes: `buildPutPayload(forms)` returns a sparse `Partial<Record<ModelRole, Partial<{base_url: string; model: string; api_key: string}>>>`.

- [x] Replace the existing payload tests with RED cases for `{}` no-op, one dirty field, empty dirty clear, one dirty role only, and two tabs producing disjoint patches.
- [x] Add component RED cases proving URL/model/key edits set only their matching dirty flag and Save is disabled for an unchanged form.
- [x] Add form-construction/reset RED coverage proving load/success forms clear every dirty flag; retain draft revision invalidation assertions.
- [x] Run the exact Node/Vitest tests and capture RED due missing flags/sparse behavior.
- [x] Implement `modelSettingsForms`, per-field flags, sparse payload construction, field-edit flagging, no-op Save disabling, and an empty-payload guard before configuration/test invalidation.
- [x] Use `modelSettingsForms` for both panel loading and successful save rebasing.
- [x] Rerun exact tests plus orchestration/component/typecheck for GREEN.
- [x] Run `git diff --check` and commit the frontend tests/implementation.

### Task 3: Contracts, documentation, and final verification

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Update: `docs/superpowers/plans/2026-07-21-model-settings-coherence.md`
- Append ignored local report: `.superpowers/sdd/final-review-fixes-report.md`

**Interfaces:** None.

- [x] Run repository ownership/facade/port contracts and use the designated generator only if a demonstrated semantic surface changed.
- [x] Synchronize README, README_zh, and AGENTS with direct-read coherence and sparse per-field saves.
- [x] Mark this plan complete, run `git diff --check`, and commit docs/plan separately.
- [x] Run focused backend and frontend suites from final HEAD.
- [x] Run `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check.sh` and record exact counts/exit code.
- [x] Run `cd frontend && npm run build` separately.
- [x] Run clean-worktree, cumulative diff, frozen-v9, credential/private-key, and raw-provider-payload audits.
- [x] Append exact RED/GREEN, commits, hashes, and remaining-concern status to the ignored final-review report.
