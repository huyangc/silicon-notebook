# Repository Review Remediation Design

**Date:** 2026-07-11  
**Branch:** `codex/repository-composition-refactor`  
**Delivery:** Continue in the existing repository-composition pull request

## 1. Objective

Close every Critical/Important architecture gap found after Tasks 22–28 while
preserving the behavior of the current code and the data compatibility proven by
the frozen v9 fixture and the real schema-v10 database.

Completion means the implementation, tests, ownership manifest, and documentation
all describe the same dependency direction:

```text
API / CLI composition roots
        ↓ consumer-owned ports
application services / coordinators
        ↓ store ports
SQLite stores + filesystem adapters
        ↓
SqliteDatabase / filesystem
```

`SQLiteRepository` remains a compatibility surface for existing callers, but it
does not own SQL, retrieval algorithms, answer synthesis, job lifecycle, or
cross-domain business orchestration.

## 2. Constraints

- Keep every HTTP route, request/response schema, frontend behavior, exception
  mapping, retrieval ordering, and persisted JSON shape unchanged.
- Do not add or modify a database migration. The branch keeps the schema version
  already present on its master baseline: `SCHEMA_VERSION = 10`.
- Frozen v9 databases must continue to upgrade to v10 and remain readable.
- Preserve Ask transaction checkpoints: durable begin, answer save, terminal job
  update, and failed/cancelled empty-conversation cleanup remain separate writes.
- Preserve Ask disconnect behavior: transport loss does not cancel detached work;
  explicit cancellation remains the only cancellation entry point.
- Preserve report plan/generate behavior and process-global report cancellation
  registry identity.
- Keep compatibility imports and public `SQLiteRepository` method signatures.
- Do not add SQLAlchemy, PostgreSQL, pgvector, mypy, pyright, or new runtime
  dependencies.
- Work only in the existing isolated worktree and update the existing pull request.

## 3. Chosen Approach

Use incremental review gates in one pull request. A single large rewrite would
make behavioral equivalence difficult to prove; a risk-only patch would leave the
confirmed architecture defects in place. Each gate therefore introduces a failing
contract test first, implements one coherent boundary, runs its focused regression
suite, and creates a rollback commit.

## 4. Gate A — Architecture Tests That Measure Real Boundaries

Replace string-based and self-referential guards with AST-derived contracts.

The production SQL audit scans `backend/app/**/*.py` and `scripts/*.py`. Product
database SQL statements and calls such as `execute`, `executemany`, and
`executescript` are allowed only under `backend/app/repositories/sqlite/` and the
documented independent/read-only tools. The audit follows injected connection
members such as `self._connect()` and must not rely only on the literal text
`sqlite3.connect(`.

The private-access audit recognizes direct variables, annotated parameters,
attributes such as `self.repo`, aliases, and composition-root calls. Allowances are
exact call sites, not a whole `(file, attribute)` pair. No production application
service may depend on `SQLiteRepository._runtime`.

Protocol coverage tests compare production calls against the declared consumer
port. They must fail when a caller uses a method absent from its Protocol.

Facade tests classify every frozen method as one of:

- a property forwarding one runtime-owned object;
- a compatibility adapter that performs only identity/signature adaptation; or
- a one-hop delegate to one canonical component.

Methods containing persistence, retrieval algorithms, response assembly, dynamic
dispatch across facade methods, or multiple component calls fail the contract.

## 5. Gate B — Runtime State and Launch Failure Safety

Runtime is the only owner of mutable operational state. `storage_dir`, embedder,
retrieval/evidence services, language caches, build sets, locks, cancellation
registries, and artifact caches are exposed through explicit facade properties.
Every setter updates the canonical runtime component and all already-composed
consumers that retain the value. Tests perform post-composition replacement and
assert that source files, retrieval, ingestion, and compatibility callers observe
the replacement.

Ask and report launch become exception-safe:

1. create/register durable state;
2. attempt synchronous submission;
3. if submission raises before the worker starts, unregister cancellation state;
4. record the appropriate failed terminal state using the existing transaction
   boundary;
5. perform the existing later empty-conversation cleanup where applicable;
6. re-raise the submission error to the caller.

Worker completion order remains unchanged. New tests use a submitter that raises
synchronously and assert database state plus registry identity, not only mock call
order.

## 6. Gate C — Executable Consumer-Owned Ports

Ports describe the operations production code actually uses.

- `AskStreamPort` exposes a streaming-start operation; routes never reach
  `repo._runtime.ask_execution`.
- `RetrievalPort` contains all public retrieval operations used by Ask, reports,
  evaluation, and diagnostics, including relation scoring.
- Chunk candidate generation and graph traversal needed by Ask become explicit
  capability ports instead of `retrieval.candidates._...` and
  `retrieval.graph._...` reaches.
- Model configuration state needed by Ask is exposed as a provider operation;
  AskService never reaches an undeclared `.identity` object.
- Maintenance callers depend on a complete maintenance Protocol rather than an
  `Any`-typed `SQLiteRepository` surface.
- `ReasoningRetriever` constructor parameters use the declared retrieval, model,
  community, and settings types.

Port tests instantiate services with minimal structural fakes that implement only
the Protocol. Chunk, reasoning, graph, streaming, report, and evaluation paths must
execute without a facade or private runtime.

## 7. Gate D — Store-Only Product SQL and Thin Facade

Move remaining product-database SQL out of application services:

- notebook catalog projections move to `NotebookStore` / `QueryStore`;
- sharing and copy persistence primitives move to `SharingStore`;
- governance SQL moves to `GovernanceStore` / `KnowledgeStore`;
- lifecycle rebuild/checkpoint/cluster projections move to `KnowledgeStore`,
  `UnifiedKgStore`, and `IndexProjectionStore`;
- scale artifact database projections move to `IndexProjectionStore`.

Services continue to own business sequencing, error policy, progress reporting,
LLM decisions, cache invalidation, and transaction selection. Stores own SQL and
row-to-domain projection. Existing atomic operations remain in one store call;
existing multi-transaction checkpoints remain separate service calls.

Move remaining facade algorithms to canonical services:

- KG lexical/semantic search and canonical folding;
- knowledge graph/detail projection;
- pending-action aggregation;
- Ask mode dispatch;
- duplicated maintenance operations.

Compatibility methods retain their signatures and delegate once. The ownership
manifest is generated or validated against actual delegate targets; a non-empty
owner string is insufficient. Historical test-only monkeypatches move to the
canonical component unless a production late-binding requirement is demonstrated
by a real production call path.

## 8. Gate E — Snapshot Verifier Hardening

The verifier remains backup-only and offline, but its accepted differences become
explicit.

- A per-version migration manifest lists the exact tables, columns, indexes,
  triggers, and views allowed when upgrading v9 to v10.
- Schema snapshots include full SQL definitions for indexes, triggers, and views.
- Added columns participate in the post-migration snapshot according to the
  migration manifest; arbitrary additions fail.
- Seed normalization uses an exact manifest of built-in primary keys and stable
  values. A row is not accepted merely because `source` or `note` says `builtin`.
- Admin and interrupted-job normalization remains field-bounded as today.
- SQLite URI paths are percent-encoded so valid paths containing URI punctuation
  work correctly.
- Failure to remove the temporary database backup is a verification failure and
  reports the retained path without printing private row data.
- When a live WAL database requires attaching to existing sidecars, the report and
  documentation state that the rebuildable SHM mtime may change; other original
  database/storage metadata remains guarded.

Tests include malicious extra migration objects, altered index definitions,
noncanonical built-ins, cleanup failure, URI punctuation paths, WAL sidecars, and
secret-output assertions.

## 9. Gate F — Documentation and Delivery

Synchronize `README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`,
`fangan_done.md`, the repository design, and implementation plan with verified
code. Historical references use this wording:

> The refactor does not change the schema version present on its master baseline
> (`SCHEMA_VERSION = 10`). The committed v9 compatibility fixture upgrades through
> the existing v10 migration and remains readable.

Documentation tests derive claims from source-level architecture checks rather
than comparing one prose file with another.

Before updating the existing pull request:

1. confirm `origin/master` is an ancestor or merge the latest master;
2. run focused architecture, Ask, report, migration, legacy-database, and verifier
   tests;
3. run the frozen v9 verifier and the real old-database verifier;
4. run `PYTHON_BIN=/path/to/python bash scripts/check.sh`;
5. request final whole-branch code review and fix every Critical/Important item;
6. push the same branch and update the same pull request.

## 10. Completion Criteria

- No product database SQL exists outside SQLite stores/maintenance and documented
  independent read-only tools.
- No application service or route reaches `SQLiteRepository._runtime` or another
  private facade member.
- Minimal Protocol-only fakes can execute every service path they claim to support.
- `SQLiteRepository` contains only explicit properties, identity/signature adapters,
  and one-hop delegates.
- Mutable compatibility state is runtime-owned and replacement-safe after service
  composition.
- Synchronous Ask/report submission failure leaves no running job or registered
  cancellation event.
- Snapshot verification rejects every unmanifested schema/seed change and never
  silently leaves a private database backup.
- Frozen v9 and real pre-refactor databases verify successfully.
- Full backend, frontend, TypeScript, and production-build gates pass.
- All fixes are delivered through the existing pull request.
