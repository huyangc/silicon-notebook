# Historical Test Baseline Recovery

**Date:** 2026-07-19  
**Status:** Approved design  
**Target branch:** `codex/historical-debt-baseline`

## Context

The repository's local quality gate is no longer trustworthy on current
`master`. Known failures include:

- the MCP smoke still pins the seven Memory tools even though the runtime
  contract now contains those seven tools plus four knowhow tools;
- user-facing copy introduced by the notebook-transfer work violates the
  vocabulary and trusted-user-error guards;
- `README.md`, `README_zh.md`, and `AGENTS.md` still state schema version 15
  while the executable schema is version 20;
- the architecture documentation test hard-codes the same stale version-15
  sentence, so it has allowed five schema bumps to pass without forcing the
  documentation to move.

Because `scripts/check.sh` exits at the first failure, these known defects can
hide later backend, frontend, contract, lint, or build failures. Before adding
GitHub CI, the repository needs one authoritative, reproducible local baseline.

## Goal

Restore `scripts/check.sh` as the complete, hermetic quality gate for the
current repository. Audit every test layer, fix all failures, and correct or
remove tests whose asserted contract is demonstrably obsolete.

The completed pull request must make the following statement true on a clean
worktree based on the latest `origin/master`:

> The backend smoke tests, MCP smoke, static contract guards, complete backend
> pytest suite, recursively collected frontend tests, TypeScript check, and
> production frontend build all pass through `scripts/check.sh`.

## Non-goals

- Do not add GitHub Actions or another hosted CI system in this pull request.
  A follow-up CI pull request will call the trusted local entry point restored
  here.
- Do not implement unrelated runtime debt from the historical PR audit,
  including LLM total timeouts, migration rollback protection, promotion UI
  follow-ups, or asset-GC policy changes.
- Do not rewrite the test framework, reorganize the entire suite, or pursue
  coverage percentage changes.
- Do not mark new product capabilities complete in `fangan_done.md`.

If a test failure exposes a real product defect, the smallest production fix
needed to satisfy the already-approved contract is in scope. A failure that
requires a new product decision or a materially larger behavior change is
recorded for a later debt pull request instead of silently redefining the
contract here.

## Test-Failure Decision Policy

Every failure must first be reproduced and assigned to exactly one category:

| Category | Required treatment |
| --- | --- |
| Product code violates the current contract | Fix product code and retain or strengthen the regression test. |
| Test infrastructure is broken | Fix the fixture, mock, isolation boundary, collector, or runner without weakening the asserted behavior. |
| The approved product contract changed but the test did not | Update the test using merged design documents, synchronized repository docs, and shipped behavior as evidence. |
| Tests duplicate the same implementation detail | Keep the more stable behavior-level protection and remove only the redundant, brittle assertion. |
| The tested feature was formally removed | Remove the test and clean up the directly related stale code or documentation. |
| The failure is timing-dependent | Fix the synchronization, polling, or lifecycle root cause; do not hide it with an arbitrary sleep increase. |
| The failure depends on a developer machine or external service | Restore an offline, deterministic default and require explicit opt-in for external providers. |

The following shortcuts are prohibited:

- adding `skip` or `xfail` merely to make the suite green;
- weakening a security, authorization, migration, user-error, or protocol
  assertion without an approved replacement contract;
- narrowing a scan or collector so that failing source files are no longer
  inspected;
- deleting a test without identifying the replacement contract or the merged
  decision that retired it.

For every deleted or materially rewritten test, the pull request description
must record the previous assertion, the current contract, and the evidence for
the change.

## Execution Design

### 1. Establish a complete baseline

Use a dedicated worktree created from the latest `origin/master`. Run each
layer separately before relying on the fail-fast aggregate script:

1. backend syntax/import preflight;
2. `scripts/smoke_backend.py`;
3. `scripts/smoke_memory_mcp.py`;
4. each static contract script invoked by `scripts/check.sh`;
5. the complete `backend/tests` pytest suite with its default inclusion of
   slow-marked tests;
6. all recursively discovered frontend `*.test.mjs` files;
7. TypeScript `tsc --noEmit`;
8. the production Next.js build.

Record the failing test or command, the observed error, and its classification.
This inventory prevents the first MCP failure from masking failures later in
the gate.

### 2. Repair by responsibility

Apply changes in bounded groups:

- **MCP protocol:** make the smoke assert the exact eleven-tool contract and
  update its success diagnostics. Preserve an independently pinned expected
  set so an accidental runtime tool addition or removal still fails.
- **User-facing wording and error trust boundary:** rewrite the transfer copy
  to the vocabulary contract and use the repository's trusted user-error
  mechanism where Chinese 4xx text is intended for display. Do not relax either
  guard.
- **Schema documentation:** synchronize schema version 20 and its migration
  description across `README.md`, `README_zh.md`, and `AGENTS.md`. Replace the
  brittle version-15 documentation assertion with a guard derived from the
  executable `SCHEMA_VERSION`, so the next migration bump fails until all
  required documents move together.
- **Newly exposed failures:** apply the decision policy above to every
  additional backend, frontend, lint, build, or contract failure discovered
  after the known blockers are removed.

### 3. Audit the suite itself

In addition to red failures, inspect the current collection for:

- `skip` and `xfail` markers and unexpected non-collection;
- fixtures or assertions referring to formally removed product surfaces;
- redundant implementation-detail assertions already protected by a stronger
  behavior-level test;
- fixed sleeps, short polling windows, and machine-speed assumptions;
- mismatch between what `scripts/check.sh` claims to run and what it actually
  collects.

This is a focused integrity audit, not permission to delete old tests merely
because they are inconvenient or slow. Existing slow tests remain part of the
default complete backend run.

## Documentation and Contract Handling

Changes affecting repository constraints must update these three files
together:

- `README.md`
- `README_zh.md`
- `AGENTS.md`

The schema-version guard must validate current executable state rather than
pinning a stale sentence inside the test itself. MCP documentation must
consistently describe the seven Memory tools plus four knowhow tools. Test and
build documentation must continue to state that `scripts/check.sh` is offline,
fail-fast, and complete.

`fangan_done.md` changes are allowed only when correcting a factual
contradiction about already-shipped behavior; this baseline project does not
claim completion of a new product-spec feature.

## Verification

The final evidence is a fresh run from the implementation worktree:

| Layer | Acceptance requirement |
| --- | --- |
| Backend syntax and dependency preflight | Pass |
| `scripts/smoke_backend.py` | Pass offline |
| `scripts/smoke_memory_mcp.py` | Pass with exactly 11 tools |
| Ask-mode, object-label, and UI-vocabulary guards | Pass |
| Complete backend pytest suite | Zero failures and no newly introduced or unexpected skips |
| Recursively collected frontend tests | Pass |
| TypeScript check | Pass |
| Next.js production build | Pass |
| `scripts/check.sh` | Complete successfully from start to finish |

Timing-sensitive defects receive targeted repeated runs after their root-cause
fix. The project does not mechanically repeat every expensive test multiple
times unless evidence points to flakiness in that area.

The pull request description includes a table mapping each baseline failure to
its root cause, resolution, and verification command.

## Delivery Workflow

1. Keep all work in `.worktrees/historical-debt-baseline` on
   `codex/historical-debt-baseline`.
2. Commit this design before implementation.
3. After user review, write a detailed implementation plan.
4. Use test-driven development: reproduce or add a failing guard before each
   behavior fix, then make the minimum correction and rerun the focused test.
5. Run the complete verification matrix before claiming completion.
6. Push the branch and open a draft pull request.
7. Only after the pull request exists, start one independent review subagent.
   Use `gpt-5.6-terra` with high reasoning for test, documentation, and
   low-risk contract changes. Upgrade that reviewer to `gpt-5.6-sol` with high
   reasoning only if the actual patch crosses migration, concurrency, or
   security-sensitive runtime boundaries.
8. Independently verify review findings, fix confirmed issues, rerun the
   required checks, and update the draft pull request.

The separate hosted-CI project follows this pull request and reuses
`scripts/check.sh` instead of inventing a second verification contract.
