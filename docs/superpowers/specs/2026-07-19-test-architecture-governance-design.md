# Test Architecture Governance and 60-Second Quality Gate

**Date:** 2026-07-19
**Status:** Approved design
**Target branch:** `codex/test-architecture-governance`
**Baseline:** `origin/master` at `842de5d2` (merged PR #305)

## Context

PR #305 restored a trustworthy local verification baseline: every test layer
is collected, offline, and green. The restored baseline also exposed two
structural debts.

First, ordinary feature changes frequently require unrelated test maintenance.
In the latest 100 commits, 55 of the 66 commits that changed implementation
also changed tests. The repository contains many valuable behavior and
architecture checks, but some of them identify source constructs by file and
line number, pin source-text order or CSS fragments, or carry historical
`TASKN_ALLOWED_*` exception layers forward indefinitely. These tests make
semantically neutral refactors look like contract changes.

Second, the complete quality gate is too slow for the desired feedback loop.
A clean baseline run on the development machine completed successfully in
`146.13s`:

| Layer | Observed baseline |
| --- | ---: |
| Complete backend pytest suite | approximately `116s` |
| Deterministic Python harness | `3.64s` |
| Frontend Node tests | `1.13s` |
| Remaining smoke, guards, TypeScript, and production build | remainder |
| Complete `scripts/check.sh` | `146.13s` |

An isolated backend profile also completed in `78.09s`, showing material
worker-scheduling and repeated-initialization variance. The slowest tests are
not uniformly slow in their own logic: several KG/index tests cluster around
15–18 seconds, retry tests spend real wall time in backoff, and architecture
tests repeatedly parse or scan the same source tree.

The quality gate must therefore be governed as a product in its own right:
stable contracts, bounded runtime, understandable failures, and no coverage
loss disguised as optimization.

## Goal

Deliver one pull request that:

1. makes tests resilient to harmless source movement and formatting changes;
2. removes source line numbers as test identity or expected state;
3. replaces historical exception accumulation with current-state semantic
   contracts;
4. moves user behavior assertions from source-text inspection to executable
   component or pure-model tests where appropriate;
5. keeps legitimate semantic architecture, security, vocabulary, and
   test-entry guards;
6. makes the complete `scripts/check.sh` finish in less than 60 seconds on
   three consecutive warm runs on the established development environment;
7. preserves zero failures, zero skips, zero xfails, complete collection,
   offline execution, TypeScript checking, and the production frontend build.

The project Python is:

```text
/opt/homebrew/Caskroom/miniconda/base/bin/python
```

All measurements and verification use that interpreter through `PYTHON_BIN`.

## Non-goals

- Do not add GitHub Actions in this pull request. Hosted CI follows after this
  local contract is stable and will call the same entry point.
- Do not reduce coverage by skipping tests, adding xfails, narrowing
  collection, deleting unique behavior assertions, or substituting weaker
  assertions solely to meet the time budget.
- Do not perform a wholesale frontend test-runner rewrite. `node:test` remains
  the fast default for pure logic and protocol contracts.
- Do not claim browser geometry or pixel fidelity from jsdom. Browser visual
  regression is a later project.
- Do not change user-facing product behavior except for the minimum extraction
  needed to make existing behavior executable and testable.
- Do not rewrite the frozen v9 database compatibility fixture. It is historical
  persisted data, not a source-position contract.
- Do not add machine-dependent sub-60-second assertions to ordinary pytest or
  frontend tests.

## Governing Principle

Tests protect observable behavior and durable architectural boundaries, not
the current textual arrangement of an implementation.

A source scan remains appropriate when the repository intentionally treats a
semantic property as a contract that is difficult or unsafe to discover by
runtime execution, for example:

- dependency direction and forbidden imports;
- repository ownership and SQL boundaries;
- public compatibility symbols and one-hop delegation;
- security-sensitive API or vocabulary constraints;
- exact test collection and verification-entry coverage.

Even in those cases the test must identify a finding semantically. A line
number may appear in a failure message to help the developer navigate, but it
must never be part of the identity, allowlist key, fixture payload, generated
manifest, or expected assertion.

## Backend Semantic Contract Design

### Semantic identity

Architecture scanners will describe a finding with a semantic identity such
as:

- repository-relative module path;
- qualified enclosing scope;
- finding kind;
- referenced module, symbol, attribute, or call target;
- occurrence count when duplicates inside the same semantic scope matter.

The scanner may attach source position only as non-comparable diagnostic
metadata. Moving a valid call up or down in the same function must stay green.
Moving it across an ownership scope may correctly fail because the semantic
boundary changed.

### Current-state manifests

The accumulated `TASKN_ALLOWED_*` structures in
`test_repository_surface_manifest.py` and related tests will be collapsed into
the current repository contract:

- supported compatibility exports;
- facade consumers and one-hop delegates;
- explicitly supported private seams;
- store/service/facade ownership;
- patch targets that are part of compatibility behavior.

The final manifest describes what is valid now, not the chronological sequence
of exceptions that produced it. Obsolete allowances are removed; live
allowances receive a semantic name and reason.

`backend/app/repositories/ownership_manifest.py` and
`scripts/generate_repository_contract_fixtures.py` must stop producing or
consuming `path:line` identities. Generated fixtures use the same semantic
schema as the assertions.

### Scanner organization

The 4,619-line repository surface test will be separated into:

1. a reusable semantic scanner/index;
2. a declarative current-state manifest;
3. focused tests for dependency, ownership, compatibility, and delegation
   contracts.

The semantic index parses each relevant Python source file once per pytest
session and exposes normalized imports, definitions, scopes, calls,
attributes, SQL-bearing constructs, and compatibility exports. Tests query
that index rather than rereading and reparsing the tree independently.

Existing scanners outside the large surface test will reuse this index where
their contract overlaps. A distinct security or documentation scan remains
separate when combining it would make ownership less clear.

### Mutation-style self-tests

The scanner itself must prove its intended sensitivity:

- inserting blank lines or moving a finding within the same scope stays green;
- a new forbidden import is detected;
- a new service-side SQL or forbidden private access is detected;
- moving a consumer into a disallowed scope is detected;
- same-scope duplicates are detected when the contract limits occurrence
  count.

These are small synthetic-source tests and do not mutate the real worktree.

## Frontend Test Design

### Two complementary runners

The frontend will use:

- `node:test` for pure functions, protocol registries, serializable state
  models, static semantic guards, and other DOM-free contracts;
- Vitest with jsdom, React Testing Library, and `user-event` for executable
  component behavior.

Component tests use a dedicated `*.component.test.tsx` suffix. The canonical
`npm run test` command runs both suites and fails if either runner fails.

### Behavior migration

Tests that currently read `page.tsx`, `globals.css`, or another production file
to infer user behavior will be classified:

- state transitions, button availability, keyboard behavior, modal lifecycle,
  labels, and navigation become component or pure-model tests;
- protocol and security vocabulary remain centralized static guards when
  source-wide enforcement is the actual contract;
- exact CSS values, class order, source order, source slices, and
  `max-width`-style pins are removed unless they map to an accessibility or
  component-state behavior that can be executed.

Only the production components, hooks, or view models necessary for testing
are extracted from large orchestrator files. No test-only props, alternate
runtime APIs, or duplicate component implementations are introduced.

jsdom tests assert DOM structure, accessible roles/names, state, and meaningful
class/state selection. They do not pretend to validate computed layout. The
future browser visual suite will own responsive geometry and screenshot
fidelity.

### Static-source registry

Remaining frontend source scans are registered centrally with:

- scan category;
- protected semantic contract;
- reason runtime execution is insufficient;
- owned source roots.

No registered scan may depend on line numbers, text order, arbitrary slices,
or a duplicate assertion already protected by a component or pure-model test.

The enforcement stays deliberately bounded. Test entrypoints and test-only
helpers may not read production files directly; the registered
`semantic-source.mjs` adapter exposes AST semantics only. A syntax-only guard
rejects AST position/collection-order APIs and source-named text slicing,
splitting, indexing, or length. It does not attempt whole-JavaScript data-flow
or closure interpretation, so its runtime is linear in the parsed syntax and
ordinary array operations remain valid.

## Performance Design

### Complete-gate budget

The 60-second target applies to the complete authoritative entry point, not to
an artificially reduced “fast” subset:

```bash
/usr/bin/time -p env \
  PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python \
  bash scripts/check.sh
```

The final acceptance evidence is three consecutive warm runs, each with:

- exit status zero;
- `real < 60.00`;
- zero skipped tests;
- zero xfailed tests;
- all smoke tests, guards, backend tests, deterministic harness tests,
  frontend Node tests, frontend component tests, TypeScript checks, and the
  Next.js production build executed.

The pull request records the three total times and per-lane durations. Timing
is an external acceptance observation because a wall-clock assertion embedded
in pytest would be machine-specific and flaky.

### Controlled parallel lanes

`scripts/check.sh` will preserve one entry point while scheduling independent
work in bounded lanes:

1. backend pytest;
2. deterministic smoke/contract/harness checks;
3. frontend Node/Vitest/type/build checks.

Each lane writes its own log and timing record. The parent waits for all lanes,
prints readable output in a stable order, and returns failure if any lane
fails. A fast failure may terminate work that can be safely stopped, but no
successful run may omit a lane. Temporary files are cleaned reliably.

The lane count is deliberately bounded to avoid CPU oversubscription with
pytest-xdist and the Next.js build. Worker counts are tuned against measured
end-to-end time, not chosen from logical CPU count alone.

### Backend critical path

Backend optimization proceeds from profiles:

- mark heavy KG/index/rebuild tests by semantic resource profile and schedule
  them early to avoid an xdist tail;
- eliminate real retry/backoff sleeps by injecting a fake clock or sleep
  boundary while retaining retry count and error behavior assertions;
- reuse the session semantic source index across architecture tests;
- merge duplicate tree scans and document/surface parses;
- identify repeated expensive application/index initialization and replace it
  with safe session/module fixtures only when isolation tests prove no state
  leakage;
- tune xdist worker count and distribution mode together with the outer lanes.

The optimizer must not hide a slow product path that the test intentionally
measures. Performance tests retain explicit product budgets where those
budgets are themselves the contract.

### Frontend critical path

Component tests remain deterministic and local:

- fake timers only where the production behavior intentionally uses a delay;
- no real network, polling, or sleep;
- no browser startup inside the 60-second unit/component gate;
- runner startup is amortized through a single Vitest invocation;
- TypeScript and production build remain mandatory.

## Guardrails Against Optimization Regressions

The repository will contain executable guards that fail if:

- a test manifest, fixture, or generator introduces source line number as
  identity or expected state;
- a new historical `TASKN_ALLOWED_*` layer is added;
- a frontend behavior test uses raw source position/order instead of the
  approved semantic-scan registry;
- `scripts/check.sh` stops collecting a required test suffix or omits a
  verification layer;
- a skip or xfail is introduced without an explicitly approved repository
  policy.

Line numbers remain allowed only in failure diagnostics. A simple variable
named `line` is not banned globally; the guard validates the relevant identity
and expectation paths to avoid false positives.

## Documentation

Because this change modifies development constraints and the canonical test
workflow, implementation must update all three synchronized documents:

- `README.md`;
- `README_zh.md`;
- `AGENTS.md`.

They will document:

- the two frontend test runners and suffix contract;
- the semantic-static-scan rule;
- the line-number prohibition;
- the complete sub-60-second local gate and its measurement method;
- the project Homebrew/Miniconda Python path as the verified local interpreter
  example without making that absolute path a cross-platform runtime
  requirement.

No product-spec feature is completed, so `fangan_done.md` does not need a new
feature entry.

## Implementation and Test Policy

Every refactor follows test-driven development:

1. add or isolate a failing semantic or behavior contract;
2. make the minimum production/test-infrastructure change;
3. run the focused test;
4. run the affected layer;
5. profile before and after any performance claim.

For deletion or material rewrite of a brittle test, the pull request records:

- previous assertion;
- replacement durable contract;
- why the old assertion was implementation-specific or obsolete;
- the test that now protects the behavior.

If an optimization changes fixture scope or process scheduling, it requires an
isolation regression test or repeated targeted verification appropriate to the
risk.

## Verification

Before delivery:

1. run focused scanner mutation tests;
2. run all backend architecture tests;
3. run the complete backend suite with durations reported;
4. run both frontend test runners;
5. run TypeScript checking and the production build;
6. run `scripts/check.sh` once from a clean worktree;
7. run it three more consecutive warm times under `/usr/bin/time -p`;
8. inspect collection output for failures, skips, xfails, or omitted layers;
9. inspect the final diff for accidental product behavior changes and generated
   artifacts.

The complete gate remains offline. MinerU, remote LLMs, external MCP servers,
and hosted services are not required.

## Delivery Workflow

1. Keep all implementation in
   `.worktrees/test-architecture-governance` on
   `codex/test-architecture-governance`.
2. Commit this design before implementation.
3. Write and commit a detailed implementation plan.
4. Implement locally without implementation subagents; this keeps the
   cross-cutting test contract under one coherent owner.
5. Verify and push the branch.
6. Open one draft pull request containing the entire governance change.
7. Only after the pull request exists, create one independent review subagent
   using `gpt-5.6-sol` with high reasoning because the patch crosses test
   infrastructure, process scheduling, architecture contracts, and broad
   frontend extraction.
8. Independently validate every review finding, fix confirmed issues, rerun
   affected and complete verification, and ask the same reviewer to re-review
   until it reports `Ready to merge: Yes`.

The follow-up GitHub Actions pull request will reuse `scripts/check.sh`; it will
not define a second, divergent test contract.
