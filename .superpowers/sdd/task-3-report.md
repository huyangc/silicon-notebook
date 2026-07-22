# Task 3 — generic repository facade/runtime and backend factory report

## Starting point and characterization

- Started from review base `329eab521d841f3133fc55b271364990c28d5627` in a clean worktree.
- Before extraction, added a complete public-callable characterization for
  `SQLiteRepository`. The frozen fixture contains 239 public methods/properties
  and their signatures.
- The pre-existing semantic facade fixture still has exactly 208 keys after
  extraction: no keys were added or removed and no signatures changed.

## TDD record

The initial focused factory/injection contract run failed in the six expected
places: there was no SQLite bundle factory, no central backend factory, API
dependencies still constructed SQLite directly, and runtime/facade modules
were not yet backend-neutral.

After implementation, the focused factory and neutral-boundary slice passed:

`7 passed, 19 deselected`

The final required Task 3 contract command passed after the sole-root guard was
added:

`49 passed in 7.63s`

## Implementation decisions

- Extracted the backend-neutral orchestration surface into
  `RepositoryFacade`; the SQLite wrapper now owns only migrations, SQLite
  connection compatibility, and SQLite maintenance adapters.
- `RepositoryRuntime` creates its model-config cache first, requests one
  injected `PersistenceBundle`, and publishes every store from that bundle by
  identity. It no longer imports SQLite or PostgreSQL modules.
- `SqlitePersistenceBundleFactory` is the sole construction root for
  `SqliteDatabase` and all 18 SQLite persistence stores. Late runtime callbacks
  retarget the already-created bundle stores instead of constructing
  replacements. A production-tree AST guard enforces that invariant; it first
  failed on the historical `NotebookSummaryQuery` fallback constructor, which
  was removed in favor of the injected query-store port.
- `create_repository(settings)` selects only from the normalized active
  `DATABASE_URL`; PostgreSQL is imported lazily, and `SHADOW_DATABASE_URL`
  cannot select the formal repository.
- `app.api.deps.repository()` remains cached and now delegates to the central
  factory.
- The neutral import guard now covers ports, bundle, runtime, and facade; the
  Task 2 deferred-module list was removed.
- Historical module monkeypatch behavior remains observable through bounded,
  late compatibility lookups without retaining the facade. This was verified
  by the scale-runtime leak regression.

## Fixture generation and review

The caller-boundary fixture was regenerated with the official script. Its only
formal composition-root change is:

- removed the direct `SQLiteRepository` import from `backend/app/api/deps.py`;
- added the direct import in `backend/app/repositories/factory.py` with the
  explicit backend-selection reason.

No new private SQL or direct-connection caller was introduced. The normal
fixture generator was also repaired to patch the current split route modules
and now completes successfully.

## Verification

- Repository architecture/contract group: `89 passed in 12.29s`.
- Broader runtime, lifecycle, identity, monkeypatch, and leak regression set:
  `141 passed in 20.29s`.
- Required Task 3 contract suite: `49 passed in 7.63s`.
- Full backend gate before the final sole-root audit: `4716 passed in 105.78s`.
- Final full backend gate after adding the construction-root guard:
  `4717 passed in 97.56s`.
- Full `scripts/check.sh`: passed, including contract/smoke checks, 1,323
  frontend node tests, 41 component tests, TypeScript checking, and the
  production frontend build.
- `git diff --check`: passed.

## Remaining concern

Task 3 deliberately adds only the lazy PostgreSQL selection boundary; the
actual `PostgresRepository`, pool, migrations, and PostgreSQL stores arrive in
later tasks. Until then, selecting a PostgreSQL active URL reaches the lazy
import and cannot start a formal repository. SQLite remains behaviorally
unchanged and the shadow URL remains inert.

## Review remediation

The post-Task-3 review identified five important and two minor contract gaps.
Each was reproduced with a focused failing test before the implementation was
changed:

- clean imports of `repository_runtime` and `repository_facade` still loaded
  SQLite stores transitively through service annotations/imports;
- three bundle stores were rebound through undeclared attributes instead of
  executable port methods;
- the construction-root guard missed qualified and aliased constructors;
- the SQLite wrapper and ownership generator did not fail closed on surface
  growth or unmapped owners;
- `close_local` and the TEMP mention-search table leaked SQLite lifecycle
  behavior into neutral orchestration;
- the wrapper no longer exposed the MinerU monkeypatch seams;
- PostgreSQL selection leaked `ModuleNotFoundError` and could mask an error
  raised inside a future adapter module.

The remediation makes neutral imports genuinely backend-free, declares and
signature-pins the late-binding operations, moves mention alias scanning into
`UnifiedKgStore`, and keeps `close_local` only on `SQLiteRepository`. Knowhow
derived-id helpers and source/chunk write records now live in neutral modules,
so importing runtime/facade no longer imports any
`app.repositories.sqlite.*` or PostgreSQL module. The semantic sole-root guard
resolves module aliases, renamed imports, store aliases, and qualified calls.
The fixture generator now validates the exact SQLite wrapper member/import/SQL
allowlists and raises on any unmapped owner; the reviewed owner changes are
`_migrator -> SqliteDatabase`, `_runtime -> RepositoryRuntime`, and
`maintenance -> SQLiteMaintenanceAdapter`.

The factory now raises the stable
`RepositoryBackendUnavailableError("PostgreSQL repository backend is not available")`
only when the PostgreSQL adapter package itself is absent. It never falls back
to SQLite, and unrelated/nested import errors propagate unchanged. MinerU
exports and late monkeypatch resolution were restored. `README.md`,
`README_zh.md`, `AGENTS.md`, and `architecture.md` now describe the same
interim truth: one formal backend factory, SQLite as the available default,
PostgreSQL selection failing closed until its adapter exists, and an inert
shadow URL.

### Review TDD and verification

- Initial review-focused run: 17 expected failures.
- Final focused review suite: `94 passed in 9.93s`.
- Repository/architecture regression group: `182 passed in 18.73s`.
- Full backend: `4734 passed in 56.92s`.
- Full `scripts/check.sh` with
  `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python3.13`: passed,
  including contract/smoke checks, 1,323 frontend Node tests, 41 component
  tests, TypeScript checking, and the production frontend build.

The surface fixture was regenerated with the official generator and reviewed.
It removes the neutral-facade `close_local` entry while the separately frozen
SQLite public-callable contract continues to pin the wrapper method. No caller
boundary fixture change was produced.
