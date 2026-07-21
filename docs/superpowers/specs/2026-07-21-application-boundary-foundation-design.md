# Application Boundary Foundation Design

**Date:** 2026-07-21
**Status:** Implemented
**Scope:** FastAPI route boundaries, Pydantic model boundaries, and the shared frontend API client
**Delivery:** One pull request from an isolated `codex/` worktree branch

## 1. Context

At the design baseline, the historical-debt programme had already established a
more trustworthy test baseline and improved test architecture. The next debt item
was the application boundary itself.

Three composition hotspots were present at baseline and made unrelated feature
work collide:

- `backend/app/api/routes.py` contained 126 route decorators across otherwise
  separate product domains.
- `backend/app/models/schemas.py` contained 147 Pydantic classes and also reached
  into service-layer validation code.
- eight frontend production modules defined a local `apiFetch`, so authentication,
  trusted error handling, downloads, cancellation, and streaming could drift between
  features.

The baseline already contained useful boundaries that the implementation needed
to preserve, including dedicated auth, Memory, content-overview, debug-log, and
Agent Knowhow routers; storage/runtime composition; shared workspace view models;
and Ask stream parsing. This design extended those boundaries instead of replacing
them.

At the design baseline, commit `6f4170b347dd8f715023406402f056a03e73f8b6`
passed the full functional gate with the Homebrew Python runtime. A warm run
reported `contracts=23s`, `backend=95s`, and `frontend=56s`. The test results are
green, but the backend lane meant that observed baseline did not meet the
existing local warm-gate target of 60 seconds. The implementation therefore had
to remeasure before broad movement without hiding or worsening a reproducible
timing regression.

After rebasing through #315, exact-head publication verification recorded two
consecutive complete warm gates with every lane at or below the local 60-second
target. The authoritative per-lane seconds belong in the PR verification record,
because committing them here would create a replacement SHA and make them stale.
These are local Apple Silicon measurements; CI lane timing remains observational
rather than a portable timeout.

## 2. Goals

1. Make HTTP endpoint ownership visible by product domain.
2. Make request and response model ownership visible by product domain.
3. Give the frontend one transport policy while keeping endpoint semantics in
   domain modules.
4. Preserve every current public URL, method, dependency, response shape, status
   code, trusted error, and cancellation/streaming rule.
5. Keep old Python model imports working during the migration.
6. Replace tests coupled to the monolithic module's private implementation with
   tests of public behaviour or stable domain seams.
7. Complete the work as one reviewable PR with staged commits, full verification,
   and an independent review of the exact green head.

## 3. Non-goals

This change does not:

- split `frontend/app/page.tsx` state into workspace hooks;
- change FastAPI lifespan or application startup/shutdown ownership;
- introduce OpenAPI client generation;
- introduce a frontend state-management framework;
- change API paths, payloads, permissions, user-visible behaviour, or product flow;
- delete endpoints merely because they appear old;
- redesign repository/store boundaries;
- add tests based on line counts, source offsets, route counts, or exact file size;
- turn a temporary OpenAPI comparison into a permanent golden snapshot.

Workspace-state extraction and application-lifecycle composition remain separate
historical-debt items. Endpoint retirement also requires separate evidence and a
separate product decision.

## 4. Chosen approach

Use a contract-first strangler migration inside one PR.

- Establish boundary tests and compatibility seams first.
- Move one coherent domain at a time.
- Keep the aggregate router as the composition boundary and `schemas.py` as the
  compatibility facade while consumers migrate.
- Centralize frontend transport policy, then move endpoint calls by domain.
- Verify focused behaviour after every domain and the complete contract at the
  end.

This is preferred over a one-shot file move because every intermediate commit can
remain executable and reviewable. It is preferred over generated clients because
the repository does not currently need the additional generator, checked-in
artifacts, or CI tooling.

## 5. Target backend route architecture

`backend/app/api/routes.py` remains the authenticated aggregate boundary. It owns
router composition order only, not product endpoint bodies or compatibility
exports.

The migration must not assume that grouping routes by domain automatically
preserves FastAPI matching order. Before moving endpoints, build a path-shape
collision table for static and dynamic patterns that could accept the same request.
Every potentially competing pair must retain its current relative order and be
covered by a route-resolution probe. The aggregate may include whole domain routers
where no ambiguity exists; otherwise it uses ordered subrouters/composition until
the same resolution is proven. OpenAPI operation precedence must also remain
unchanged.

### 5.1 Domain ownership

| Module | Responsibilities |
| --- | --- |
| `system_routes.py` | health-adjacent authenticated system endpoints, current-user metadata, model settings, document types, and notebook templates |
| `notebook_routes.py` | notebook CRUD, summaries/analytics, tier, mounted bases, sharing, membership, and collection-facing notebook operations |
| `source_routes.py` | upload, URL import, listing, detail, parsing/reparsing, source elements/assets, and deletion |
| `knowhow_routes.py` | session-facing Knowhow tables, import/append, editing, formatting, code attachments, transfer, and projection controls |
| `knowledge_routes.py` | knowledge-object browse/update, dynamic types, schemas/proposals, duplicates, merge, and object-level governance |
| `ask_routes.py` | search, Ask modes, Ask execution/streaming, jobs and cancellation, conversations, and feedback |
| `report_routes.py` | report creation, outline workflow, progress, export, cancellation, and deletion |
| `kg_routes.py` | graph/unified-graph reads, build/rebuild/relink, index/status, conflicts, transitive policy, edge review, and graph-specific administration |
| `admin_routes.py` | promotion queues and other remaining authenticated administrative governance endpoints |

The exact placement of a borderline endpoint follows the service it orchestrates
and the permission boundary it enforces, not the shape of its URL alone. When a
route spans domains, its owning router imports an application service; routers do
not import endpoint functions from one another.

### 5.2 Existing specialized routers

Existing routers for auth, Memory, content overview, debug logs, and Agent Knowhow
remain independent. The work must not fold them back into the new aggregate or
create parallel implementations.

### 5.3 Dependency and execution rules

- Access dependencies remain in the established dependency modules.
- Dependency declaration order is preserved where it affects authentication or
  existence-oracle behaviour.
- Synchronous SQLite authorization and repository calls remain off the event loop.
- Domain routers orchestrate services; they do not assemble product SQL.
- Error translation stays at its current stable application/API seam.
- Router composition must not introduce import-time construction of repositories,
  settings, or model clients.

### 5.4 Aggregate compatibility boundary

Throughout this PR, imports of the aggregate `router` continued to work.
`routes.py` retains that composition surface only; it does not re-export endpoint
functions, private helpers, repositories, or other legacy monolith symbols.

Tests and production code migrated away from patching private monolith helpers and
now use public HTTP behaviour or explicit domain seams. The aggregate therefore
cannot become a second private API by accident.

## 6. Target Pydantic model architecture

Model definitions move into domain-owned modules under `backend/app/models/`:

| Module | Model responsibility |
| --- | --- |
| `common.py` | small cross-domain value types and transport-safe shared primitives |
| `identity.py` | user, auth-session-facing, Agent profile, and Agent token contracts |
| `memory.py` | existing storage-neutral Memory dataclasses plus Memory API/retrieval contracts |
| `notebooks.py` | notebook, tier, membership, base-mount, and sharing contracts |
| `sources.py` | source import, parse, element, asset, and source lifecycle contracts |
| `knowledge.py` | knowledge objects, schemas, relations, dedupe/merge, and governance contracts |
| `kg.py` | graph build/index/status, unified-graph, conflict, and merge-review contracts |
| `ask.py` | search, Ask, stream/job, conversation, citation, trace, and feedback contracts |
| `reports.py` | report outline, generation, progress, export metadata, and cancellation contracts |
| `knowhow.py` | session-facing Knowhow table/import/edit/format/transfer contracts |
| `content_overview.py` | notebook-level Memory/Knowhow overview and content-summary contracts |
| `admin.py` | promotion and administrative queue contracts not owned by another domain |
| `model_services.py` | model configuration/test/status transport contracts |

The final filenames may be adjusted only to avoid a real collision with an existing
domain module. Ownership and dependency direction are the invariant decisions.

### 6.1 `schemas.py` compatibility

`backend/app/models/schemas.py` becomes a compatibility facade that re-exports the
same model objects from their domain modules. Existing import paths therefore keep
working while new and migrated routers import their domain model modules directly.

There must be one class definition for each model. The facade may not subclass,
copy, wrap, or redefine a model simply to retain an old import.

### 6.2 Neutral validation boundary

Pydantic models must not import service-layer modules. Shared validation rules,
including the established Memory input limits and JSON safety policy, belong in a
neutral core/domain module that may be imported by models, services, adapters, and
stores without reversing the dependency graph.

Moving a validator must preserve its exact fail-closed semantics. This PR does not
relax caps, error locations, HTTP 422 behaviour, or JSON handling.

### 6.3 Import-cycle rule

Domain model modules may depend on `common.py` and neutral core/domain helpers.
They may not depend on routers, services, repositories, stores, or the compatibility
facade. `schemas.py` may depend on domain model modules, but domain modules may not
import `schemas.py`.

## 7. Target frontend API architecture

The frontend separates transport policy from endpoint ownership.

### 7.1 Transport core

`frontend/app/api-client.ts` owns only shared HTTP mechanics:

- API base URL resolution;
- bearer-session headers through the existing auth-session boundary;
- JSON, empty, and Blob response handling;
- trusted `X-User-Message`/error payload interpretation through the established
  error module;
- network-failure normalization;
- caller-provided `AbortSignal` propagation;
- request headers and body encoding.

The core must be usable for unauthenticated login/register requests without
creating an `auth.ts` import cycle. Session-token storage remains independently
owned; the client consumes that boundary rather than owning login policy.

### 7.2 Domain API modules

Endpoint paths, bodies, and response types live in domain modules such as:

- `notebook-api.ts`
- `source-api.ts`
- `knowhow-api.ts`
- `knowledge-api.ts`
- `ask-api.ts`
- `report-api.ts`
- `kg-api.ts`

Where an existing file already cleanly owns one domain, it may consume the shared
client without being renamed merely for symmetry.

`workspace-api.ts`, if needed, is a compatibility barrel only. It must not become a
new frontend god module.

### 7.3 Special transport semantics

Centralization must preserve these intentional differences:

- login/register retain their current unauthenticated and 401-facing behaviour;
- authenticated 401 handling retains current session cleanup/navigation policy;
- fail-open and fallback behaviour remains local to the feature that defines it;
- Blob downloads retain filename/content-disposition handling;
- Ask NDJSON decoding and progress/final-event semantics remain Ask-owned;
- transport disconnect remains distinct from explicit Ask cancellation;
- navigation or refresh must not start sending cancellation requests;
- every caller's `AbortSignal`, polling cadence, request ordering, and stale-result
  guard is preserved.

The shared client provides mechanics, not product policy.

## 8. Migration sequence inside the PR

The PR is delivered in independently testable commits:

1. Add architectural/compatibility tests and capture a temporary normalized API
   baseline.
2. Establish neutral validation and domain model modules, with `schemas.py`
   re-exporting identical objects.
3. Establish the shared frontend transport core without changing endpoint call
   sites.
4. Move backend routes one domain at a time, preserving aggregate order and
   behaviour.
5. Migrate frontend calls one domain at a time and remove local transport clones.
6. Remove compatibility code that has no remaining consumer, while retaining the
   explicitly required facades.
7. Prove functional and API equivalence, then audit every migration-time test and
   remove temporary, duplicate, or implementation-coupled coverage only after its
   durable replacement is identified.
8. Synchronize architecture and development documentation.
9. Run final contract comparison, timed full verification, and independent review.

No commit may intentionally leave a backend capability without its existing
frontend surface or a frontend action without its backend endpoint.

## 9. Test design

### 9.1 Permanent tests

Permanent tests protect behaviour and stable dependency direction:

- aggregate routers expose representative endpoints from every domain;
- every static/dynamic route pair identified by the collision analysis resolves to
  the same handler before and after migration;
- domain router permissions preserve 401/403/404 and no-existence-oracle rules;
- representative response models serialize exactly as before;
- old `schemas.py` imports are object-identical to domain model imports;
- model modules do not import service/router/store layers;
- domain routers do not import the monolithic compatibility facade;
- frontend domain clients preserve auth, trusted errors, Blob handling, abort
  propagation, and Ask disconnect-versus-cancel semantics;
- production frontend code does not introduce another local `apiFetch` transport
  implementation.

Architecture tests may use AST/import-graph semantics to express module ownership.
They must not depend on source offsets, line counts, exact function positions,
formatting, or total route/model counts.

### 9.2 Existing test migration

Tests that patch `app.api.routes` private helpers move to one of two stable seams:

1. `TestClient` assertions against the public HTTP contract; or
2. patching an explicit domain dependency/service boundary when isolation is
   necessary.

Tests should not patch a symbol merely because it happened to be in the old large
file.

### 9.3 Temporary migration evidence

Before route movement, generate a normalized representation of the current
OpenAPI surface covering paths, methods, operation identity, request/response
schemas, and security metadata. Compare the final application against it and
explain any difference.

This artifact is PR evidence, not a committed permanent golden. Normal product
endpoint additions should not require unrelated snapshot maintenance later.

### 9.4 Verification gates

After each domain:

- focused backend or frontend tests for that domain;
- relevant architecture/import tests;
- type checking for affected frontend modules.

Before publishing:

- `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`;
- `cd frontend && npm run build` if not already covered by the final gate;
- normalized OpenAPI comparison;
- clean `git status` and exact-head verification.

The local warm gate remains at most 60 seconds. CI duration is observed but not a
hard merge gate. Because the recorded starting run exceeded 60 seconds, the first
implementation checkpoint must remeasure under an uncontended warm environment.
If backend timing remains above 60 seconds, diagnose and restore the existing gate
before broad route movement, without weakening or deleting meaningful coverage.

### 9.5 Post-equivalence test optimization

Test cleanup happens only after the migrated application has passed the normalized
API comparison and focused behavioural equivalence checks. At that point, inventory
every test added or modified by the migration and classify it as:

- durable public-behaviour coverage;
- durable architecture/dependency coverage;
- temporary migration evidence;
- duplicate coverage; or
- implementation-coupled coverage.

Temporary OpenAPI artifacts and one-off migration probes are removed rather than
turned into permanent goldens. Duplicate tests may be consolidated only when the
retained test names the same behaviour and failure mode. Tests coupled to the old
monolith move to `TestClient` or explicit domain seams. Every deleted test must have
a recorded retained-coverage mapping; test deletion may not be used to obtain the
60-second result.

After cleanup, use per-lane timing plus backend slow-test and frontend test timing
to optimize fixture setup, repeated application/database construction, duplicated
build/type-check work, and other measured overhead. Do not reduce assertion depth,
permission coverage, cancellation coverage, or supported environment coverage.
The final evidence contains two consecutive uncontended warm `scripts/check.sh`
runs, each completing in at most 60 seconds.

## 10. Error handling and rollback

Migration stops at the first unexplained change in status code, dependency order,
serialization, trusted error message, or asynchronous behaviour. The current
domain is corrected before another domain moves.

Because commits follow domain boundaries, a problematic migration can be reverted
without discarding the rest of the PR. There is no database migration and no
deployment-time dual-write, so rollback is a code rollback.

Compatibility facades are removed only when repository-wide search proves there is
no consumer and the relevant tests remain green.

## 11. Documentation

Architecture/development constraints are synchronized in:

- `README.md`
- `README_zh.md`
- `AGENTS.md`
- `architecture.md`
- the existing historical-debt/architecture remediation record

This work is architecture debt repayment, not a new product capability.
`fangan_done.md` changes only if the implementation completes a concrete product
spec item; file movement alone must not be recorded as a new feature.

## 12. PR and review protocol

All implementation ships in one PR from the isolated
`codex/application-boundary-foundation` branch. The PR body records:

- baseline and final gate results and timings;
- normalized API comparison results;
- route/model/frontend ownership changes;
- model/repository compatibility facades retained;
- explicitly deferred debt.

After the PR exists and the exact head is green, an independent subagent reviews
that exact SHA for contract drift, permission regressions, import cycles, async
blocking, error-policy drift, and brittle tests. Use `gpt-5.6-terra` with high
reasoning by default to control cost; escalate the final cross-cutting review model
only if the observed change complexity warrants it.

Critical and Important findings are fixed, fully reverified, pushed, and reviewed
against the new exact head before handoff.

## 13. Acceptance criteria

The work is complete when all of the following are true:

1. Product endpoints are implemented in the agreed domain routers and composed by
   a thin aggregate router.
2. Pydantic definitions are domain-owned, with old imports resolving to the same
   class objects through `schemas.py`.
3. No model module imports service, router, repository, or store code.
4. Frontend domain calls share one transport policy, with special authentication,
   download, streaming, and cancellation semantics preserved.
5. Public API and user-visible behaviour have no unexplained difference from the
   baseline.
6. Tests rely on public behaviour or stable semantic boundaries, never source line
   counts or positions.
7. The full gate and production frontend build pass with the Homebrew Python
   runtime; two consecutive uncontended warm full-gate runs each complete in at
   most 60 seconds.
8. Required documentation is synchronized.
9. Migration-time tests have been audited after equivalence is proven; temporary,
   duplicate, and implementation-coupled cases are removed only with an explicit
   retained-coverage mapping.
10. The PR's exact green head has received independent subagent review with no open
   Critical or Important findings.
