# Model Service Status Cumulative Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the cumulative model-service-status review findings without allowing stale identities, whitespace-only rerank settings, endpoint-shaped labels, modal focus escape, or misleading progress text.

**Architecture:** Effective model fingerprints remain the single identity primitive. Settings updates compare all six effective identities before and after persistence; observed failures carry the originating real client's fingerprint and persist only while it still matches the current effective identity. Frontend accessibility and wording remain component-owned while request ownership remains page-owned.

**Tech Stack:** FastAPI, Pydantic, SQLite, pytest, React/TypeScript, Testing Library, Vitest, Next.js.

## Global Constraints

- Use strict RED → GREEN TDD for every behavior change.
- Preserve sanitized client responses and logs-only raw provider diagnostics.
- Preserve test doubles by treating a missing real-client fingerprint as non-persistable, never by substituting the current identity.
- Keep `README.md`, `README_zh.md`, and `AGENTS.md` synchronized.
- Do not regenerate the frozen repository-v9 database or storage.

---

### Task 1: Effective settings invalidation

**Files:**
- Modify: `backend/tests/test_model_settings_api.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/services/model_config.py`

**Interfaces:**
- Consumes: `STATUS_SERVICE_ROLES`, `resolve_effective_config()`, `model_config_fingerprint()`.
- Produces: `changed_effective_model_roles(before, after, policy, system_settings) -> set[str]`.

- [x] Add API tests proving a no-op all-role save preserves statuses, rerank-only edits preserve LLM statuses, and primary edits preserve independently configured variants while invalidating inheriting variants.
- [x] Run the API tests and verify the new assertions fail because invalidation currently follows supplied payload roles.
- [x] Add an all-six-role before/after fingerprint comparison and use its result after saving settings.
- [x] Rerun API and resolver tests and verify they pass.

### Task 2: Stale observed-failure identity

**Files:**
- Modify: `backend/tests/test_model_status_service.py`
- Modify: `backend/tests/test_model_provider_runtime.py`
- Modify: `backend/app/services/model_config.py`
- Modify: `backend/app/services/model_status.py`
- Modify: `backend/app/services/model_provider.py`
- Modify: `backend/app/services/embedding.py`
- Modify: provider-error call sites and repository port/forwarder signatures.

**Interfaces:**
- Produces: `bind_model_status_identity(client, config)` and `model_client_fingerprint(client) -> str`.
- Changes: `record_observed_failure(user, service, failed_fingerprint) -> bool`.
- Changes: `note_model_error(..., provider_failure=False, failed_fingerprint="")`.

- [x] Add a concurrent-save regression proving an old client's error remains named with the old safe model in Ask output but cannot persist against the newly saved identity.
- [x] Run the focused status/provider tests and verify stale failures currently overwrite status for the new effective identity.
- [x] Stamp every real runtime LLM/rerank/embedding client with its resolved fingerprint, propagate it from actual provider-call catch sites, and decline persistence when absent or stale.
- [x] Update protocols, forwarding lambdas, and test fakes without deriving a missing fingerprint from current configuration.
- [x] Rerun the focused status/provider/caller suites and verify they pass.

### Task 3: Rerank normalization and sanitizer hardening

**Files:**
- Modify: `backend/tests/test_model_status_resolution.py`
- Modify: `backend/tests/test_user_rerank_resolve.py`
- Modify: `backend/tests/test_model_status_service.py`
- Modify: `backend/tests/test_model_safety.py`
- Modify: `backend/app/services/model_config.py`
- Modify: `backend/app/services/rerank_client.py`
- Modify: `backend/app/core/model_safety.py`

**Interfaces:**
- Produces canonical trimmed rerank URL/key/model values in resolver, runtime, probe, and fingerprint paths.
- Preserves the existing supported dynamic model-label allowlist behavior.

- [x] Add tests for whitespace-only user/system rerank fields, no-probe behavior, canonical fingerprints, two-label ASCII/internal/Unicode/punycode host rejection, and pinned dynamic IDs.
- [x] Run the focused tests and verify they fail at the current inconsistent normalization/hostname boundaries.
- [x] Canonicalize rerank descriptors and client activation; extend bare-host detection to valid two-label public/internal/Unicode/punycode shapes while retaining slashed/tagged/versioned IDs.
- [x] Rerun resolver/runtime/probe/safety tests and verify they pass.

### Task 4: Dialog focus fallback and precise button labels

**Files:**
- Modify: `frontend/app/model-service-panel.component.test.tsx`
- Modify: `frontend/app/model-service-panel.tsx`

**Interfaces:**
- Changes: `EffectiveStatusRow` receives independent `testing` and `disabled` booleans.
- Produces: focusable dialog fallback through `tabIndex={-1}` and `dialogRef`.

- [x] Add component tests for Tab/Shift+Tab with saving and zero enabled controls, plus save/draft locks that retain `测试当前使用` instead of announcing `测试中…`.
- [x] Run the component test and verify both behaviors fail.
- [x] Prevent default and focus the dialog when no enabled control exists; separate testing text from generic disabled state.
- [x] Rerun component, orchestration, accessibility, and TypeScript tests and verify they pass.

### Task 5: Contracts, documentation, and final verification

**Files:**
- Modify generated semantic contracts only when focused contract tests demonstrate a signature change.
- Modify: `README.md`, `README_zh.md`, `AGENTS.md`
- Append: `.superpowers/sdd/final-review-fixes-report.md`

**Interfaces:** None.

- [x] Run focused backend/frontend/security/golden/contract suites and refresh only living generated contracts whose focused tests fail.
- [x] Commit backend and frontend logical groups with fresh verification evidence.
- [x] Update synchronized documentation and append the final-review report with RED/GREEN evidence and fixture hashes.
- [x] Run `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check.sh`.
- [x] Run `cd frontend && npm run build` separately.
- [x] Run `git diff --check`, tracked-status, frozen-fixture hash/diff, and credential/provider-payload scans before handoff.
