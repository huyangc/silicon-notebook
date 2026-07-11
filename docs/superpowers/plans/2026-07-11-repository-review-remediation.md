# Repository Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the repository composition boundaries asserted by the project true in production code, close launch/compatibility safety gaps, and harden old-database verification without changing product behavior.

**Architecture:** Strengthen executable architecture contracts first, then fix runtime ownership and launch failure handling, replace private reaches with consumer-owned ports, move product SQL into SQLite stores, reduce `SQLiteRepository` to explicit compatibility delegates, and finally harden snapshot verification and synchronize documentation. Each task is an independent rollback commit in the existing pull request.

**Tech Stack:** Python 3.11+, FastAPI, standard-library `sqlite3`, `ast`, `typing.Protocol`, pytest, Next.js 15/TypeScript.

## Global Constraints

- Keep every HTTP route, request/response schema, frontend behavior, exception mapping, retrieval ordering, and persisted JSON shape unchanged.
- Do not add or modify a database migration; keep `SCHEMA_VERSION = 10` from the branch's master baseline.
- Frozen v9 databases must continue to upgrade to v10 and remain readable.
- Preserve Ask begin/save/finish/cleanup transaction checkpoints and disconnect-versus-explicit-cancel behavior.
- Preserve report plan/generate behavior and process-global report cancellation registry identity.
- Keep compatibility imports and public `SQLiteRepository` method signatures.
- Product database SQL belongs only in `backend/app/repositories/sqlite/` or the SQLite maintenance adapter.
- Do not add runtime dependencies, SQLAlchemy, PostgreSQL, pgvector, mypy, or pyright.
- Work in `/private/tmp/silicon-notebook-repository-composition-refactor` and update the existing pull request.
- Run backend commands with `/opt/homebrew/Caskroom/miniconda/base/bin/python` on this machine.

---

### Task 1: Make architecture guards measure production boundaries

**Files:**
- Modify: `backend/tests/test_repository_callers_static.py`
- Modify: `backend/tests/test_repository_facade_contract.py`
- Modify: `backend/tests/test_repository_surface_manifest.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Create: `backend/tests/test_repository_protocol_coverage.py`

**Interfaces:**
- Consumes: production Python AST and `SURFACE_MEMBERS`.
- Produces: `product_sql_sites()`, `private_repository_sites()`, `protocol_calls()` test helpers and exact failure lists used by later tasks.

- [ ] **Step 1: Add RED SQL and private-access tests**

Add AST helpers that record call sites rather than literal string matches:

```python
def call_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))

def product_sql_sites() -> list[tuple[str, int, str]]:
    hits = []
    for path, rel in _production_files():
        if rel.startswith("backend/app/repositories/sqlite/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node.func).rsplit(".", 1)[-1] in {
                "execute", "executemany", "executescript"
            }:
                hits.append((rel, node.lineno, call_name(node.func)))
    return hits
```

Assert that only the exact independent/read-only exceptions documented in the spec remain. Add a second AST test that rejects `_runtime`, `_connect`, `_write`, and other private facade access from routes and application services, with allowances keyed by exact `(file, line, attribute)` until later tasks remove them.

- [ ] **Step 2: Add RED protocol/facade/ownership tests**

```python
def test_retrieval_port_declares_every_production_retrieval_call():
    missing = protocol_calls("RetrievalPort") - set(RetrievalPort.__dict__)
    assert missing == set()

def test_manifest_owner_matches_facade_delegate_target():
    assert manifest_delegate_mismatches(SQLiteRepository, OWNER_BY_MEMBER) == []

def test_facade_methods_are_properties_adapters_or_one_hop_delegates():
    assert facade_body_violations(SQLiteRepository) == []
```

The one-hop checker permits a single return/call plus argument adaptation, property accessors, and context-manager wrappers `_connect`/`_write`; it rejects SQL, loops, imports, `getattr(self, ...)`, response assembly, and calls to more than one canonical component.

- [ ] **Step 3: Run RED and capture the complete offender lists**

Run:

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_callers_static.py \
  tests/test_repository_facade_contract.py \
  tests/test_repository_protocol_coverage.py -q
```

Expected: failures list service SQL, route/runtime reaches, missing port methods, and multi-body facade methods found in the review.

- [ ] **Step 4: Replace broad allowances with an exact remediation ledger**

Delete file-wide `(file, attribute)` allowances and Task-number patch unions. Keep only exact production composition-root exceptions with a reason string. Record the current RED sites in `EXPECTED_REMEDIATION_SITES` as exact `(file, line, call)` tuples and assert equality, so new sites and stale entries both fail; Tasks 2–6 delete entries as they remove each site, and Task 6 requires the ledger to be empty. Change `validate_ownership_manifest()` to optionally accept delegate evidence:

```python
def validate_ownership_manifest(
    members: tuple[SurfaceMember, ...] = SURFACE_MEMBERS,
    delegates: Mapping[str, str] | None = None,
) -> dict[str, str]:
    owners = _unique_nonempty_owners(members)
    if delegates is not None:
        mismatches = {name: (owners[name], owner) for name, owner in delegates.items()
                      if name in owners and owners[name] != owner}
        if mismatches:
            raise ValueError(f"ownership/delegate mismatch: {mismatches}")
    return owners
```

- [ ] **Step 5: Run the architecture test module**

Run the same pytest command. Expected: all pass with the exact remediation ledger matching the current source; no file-wide allowance is accepted.

- [ ] **Step 6: Commit the executable RED contracts**

```bash
git add backend/tests/test_repository_callers_static.py \
  backend/tests/test_repository_facade_contract.py \
  backend/tests/test_repository_surface_manifest.py \
  backend/tests/test_repository_protocol_coverage.py \
  backend/app/repositories/ownership_manifest.py
git commit -m "test(repository): enforce real composition boundaries"
```

### Task 2: Make runtime state and background launch exception-safe

**Files:**
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/retrieval_service.py`
- Modify: `backend/app/services/ask_execution.py`
- Modify: `backend/app/services/report_execution.py`
- Modify: `backend/tests/test_repository_runtime_identity.py`
- Modify: `backend/tests/test_ask_execution_coordinator.py`
- Modify: `backend/tests/test_report_execution.py`

**Interfaces:**
- Produces runtime-owned `storage_dir`, `embedder`, and `notebook_languages` properties.
- Produces exception-safe `AskExecutionCoordinator.start()` and `ReportExecutionCoordinator.start_plan/start_generate()`.

- [ ] **Step 1: Write RED replacement and submit-failure tests**

```python
def test_storage_and_embedder_replacements_reach_composed_consumers(repo, tmp_path):
    _ = repo.retrieval
    replacement_dir = tmp_path / "replacement"
    replacement_embedder = object()
    repo.storage_dir = replacement_dir
    repo.embedder = replacement_embedder
    assert repo._runtime.source_files.storage_dir is replacement_dir
    assert repo._runtime.source_ingestion.embedder() is replacement_embedder
    assert repo.retrieval.candidates.embedder is replacement_embedder
    assert repo.retrieval.graph.embedder is replacement_embedder

def test_ask_submit_failure_finishes_and_unregisters(coordinator, state, registry):
    coordinator.job_submitter = RaisingSubmitter(RuntimeError("thread start failed"))
    with pytest.raises(RuntimeError, match="thread start failed"):
        coordinator.start("nb", request(), chunk_mode(), user_id="u")
    assert state.status == "failed"
    assert registry.get(state.job_id) is None

def test_report_submit_failure_unregisters(coordinator, registry):
    coordinator.job_submitter = RaisingSubmitter(RuntimeError("thread start failed"))
    with pytest.raises(RuntimeError, match="thread start failed"):
        coordinator.start_plan("nb", "r", "q", user_id="u")
    assert registry.cancel("r") is False
```

- [ ] **Step 2: Run RED**

Run the three modified test files. Expected: facade/runtime state splits and both cancellation leaks reproduce.

- [ ] **Step 3: Move mutable state ownership into runtime**

Add runtime setters that update composed services:

```python
def set_embedder(self, value: Any) -> None:
    self._embedder = value
    if self._retrieval is not None:
        self._retrieval.replace_embedder(value)

def set_storage_dir(self, value: Path) -> None:
    resolved = Path(value)
    self.source_files.storage_dir = resolved
```

Expose facade properties that call these setters. Do not retain ordinary facade attributes for these objects.

- [ ] **Step 4: Compensate synchronous submission failure**

Wrap only the submit call, leaving worker order unchanged:

```python
try:
    self.job_submitter.submit(worker, name=f"ask-{mode.id}")
except BaseException as exc:
    self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
    raise
```

For reports, unregister in the submit exception handler before re-raising. If the report store has a pre-worker durable row, update it to `failed` through the existing report store API without adding a new transaction checkpoint on success.

- [ ] **Step 5: Run focused and regression tests**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_runtime_identity.py \
  tests/test_ask_execution_coordinator.py tests/test_ask_stream_cancel.py \
  tests/test_report_execution.py tests/test_report_api.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py backend/app/services/retrieval_service.py \
  backend/app/services/ask_execution.py backend/app/services/report_execution.py \
  backend/tests/test_repository_runtime_identity.py \
  backend/tests/test_ask_execution_coordinator.py backend/tests/test_report_execution.py
git commit -m "fix(repository): make runtime state and launch cleanup authoritative"
```

### Task 3: Replace private Ask/retrieval/maintenance reaches with executable ports

**Files:**
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/retrieval_service.py`
- Modify: `backend/app/services/ask_service.py`
- Modify: `backend/app/services/reasoning_retrieval.py`
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `backend/app/eval/retrieval_metrics.py`
- Modify: `backend/tests/test_ask_service_boundary.py`
- Modify: `backend/tests/test_repository_callers_static.py`
- Modify: `backend/tests/test_repository_protocol_coverage.py`

**Interfaces:**
- Produces `AskStreamPort.start_ask_stream(...)`.
- Extends `RetrievalPort` with `retrieve_relations_scored` and explicit Ask candidate/graph capabilities.
- Produces complete `SQLiteMaintenancePort` methods used by batch/CLI callers.

- [ ] **Step 1: Write RED minimal-fake tests**

Create fakes implementing only declared Protocol members and execute one chunk and graph request. Assert no `.candidates`, `.graph`, `.identity`, or `._runtime` attribute is required. Add an API route test using an `AskStreamPort` fake without `_runtime`.

```python
class MinimalAskStream:
    def current_user(self): return user()
    def start_ask_stream(self, notebook_id, payload, mode, *, user_id):
        q = queue.Queue(); q.put(None); return q

class MinimalModels:
    def primary_unconfigured(self) -> bool: return True
```

- [ ] **Step 2: Run RED**

Run `test_ask_service_boundary.py`, `test_repository_protocol_coverage.py`, and Ask route tests. Expected: missing public capability methods and private runtime access failures.

- [ ] **Step 3: Define complete consumer-owned protocols**

Add exact methods:

```python
class AskStreamPort(Protocol):
    def current_user(self) -> UserProfile: ...
    def start_ask_stream(self, notebook_id: str, payload: AskRequest, mode: Any,
                         *, user_id: str) -> "queue.Queue[dict[str, Any] | None]": ...

class RetrievalPort(Protocol):
    def retrieve_scored(...): ...
    def retrieve_relations_scored(self, notebook_id: str, query: str): ...
    # retain all existing public retrieval methods

class AskCandidatePort(Protocol):
    def notebook_languages(self, notebook_id: str) -> list[str]: ...
    def chunk_plan(self, notebook_id: str, queries: list[str]) -> Any: ...
    def retrieve_chunk_candidates(...): ...
    def graph_is_large(self, notebook_id: str) -> bool: ...

class AskGraphPort(Protocol):
    def federated_graph(self, notebook_id: str) -> tuple[Any, list[str], dict[str, int]]: ...
    def source_chunks(self, notebook_id: str, object_ids: list[str]) -> list[Any]: ...
```

Declare every maintenance method actually used by `batch_ingest.py` and CLI modules; annotate those functions with the narrow public repository and maintenance ports instead of `Any` or concrete facade types.

- [ ] **Step 4: Implement adapters and remove private reaches**

`SQLiteRepository.start_ask_stream()` delegates to `runtime.ask_execution.start()`. Routes call that method. `RetrievalService` exposes candidate/graph adapters with public names. `AskService` receives these ports directly. `RuntimeModelProvider.primary_unconfigured()` encapsulates identity resolution. Type `ReasoningRetriever.__init__` with `RetrievalPort`, `ModelClientProvider`, `CommunityQueryPort`, and `Settings`.

- [ ] **Step 5: Run focused behavior suites**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_ask_service_boundary.py tests/test_ask_modes.py \
  tests/test_ask_repository_golden.py tests/test_reasoning_retrieval.py \
  tests/test_repository_callers_static.py tests/test_repository_protocol_coverage.py \
  tests/test_batch_ingest.py tests/eval/test_retrieval_metrics.py -q
```

Expected: all pass; static private-access offender list no longer includes routes, AskService, ReasoningRetriever, or evaluation.

- [ ] **Step 6: Commit**

```bash
git add backend/app/repositories/ports.py backend/app/api/routes.py \
  backend/app/services/repository_runtime.py backend/app/services/retrieval_service.py \
  backend/app/services/ask_service.py backend/app/services/reasoning_retrieval.py \
  backend/app/services/batch_ingest.py backend/app/eval/retrieval_metrics.py \
  backend/tests/test_ask_service_boundary.py \
  backend/tests/test_repository_callers_static.py \
  backend/tests/test_repository_protocol_coverage.py
git commit -m "refactor(repository): close executable service ports"
```

### Task 4: Move catalog, sharing, governance, and scale SQL into stores

**Files:**
- Modify: `backend/app/repositories/sqlite/notebook_store.py`
- Modify: `backend/app/repositories/sqlite/query_store.py`
- Modify: `backend/app/repositories/sqlite/sharing_store.py`
- Modify: `backend/app/repositories/sqlite/governance_store.py`
- Modify: `backend/app/repositories/sqlite/knowledge_store.py`
- Modify: `backend/app/repositories/sqlite/index_projection_store.py`
- Modify: `backend/app/services/notebook_catalog.py`
- Modify: `backend/app/services/notebook_sharing.py`
- Modify: `backend/app/services/knowledge_governance.py`
- Modify: `backend/app/services/scale_artifact_runtime.py`
- Modify: `backend/tests/test_notebook_store_component.py`
- Modify: `backend/tests/test_notebook_summary_query.py`
- Modify: `backend/tests/test_notebook_copy_service.py`
- Modify: `backend/tests/test_knowledge_governance_delegation.py`
- Modify: `backend/tests/test_scale_artifact_runtime.py`

**Interfaces:**
- Store methods accept an existing `sqlite3.Connection` when transaction ownership must remain in the service; otherwise they open through their shared `SqliteDatabase`.
- Services retain sequencing, authorization policy, cache invalidation, LLM calls, and progress/error policy.

- [ ] **Step 1: Add RED store-delegation tests**

For every current service `db.execute` site, patch the intended store method, call the public service operation, and assert arguments/return projection. Add a static assertion that the four service modules contain no `execute`, `executemany`, or `executescript` calls.

```python
@pytest.mark.parametrize("module", [
    notebook_catalog, notebook_sharing, knowledge_governance, scale_artifact_runtime,
])
def test_application_service_contains_no_sql_calls(module):
    assert sql_call_sites(inspect.getsource(module)) == []
```

- [ ] **Step 2: Run RED**

Expected: exact current SQL lines fail.

- [ ] **Step 3: Move SQL bodies without changing transaction seats**

Create named store methods matching the projections, for example:

```python
class NotebookStore:
    def catalog_row(self, db, notebook_id: str): ...
    def catalog_source_counts(self, db, notebook_ids: Sequence[str]): ...
    def search_metadata_rows(self, db, notebook_id: str): ...

class GovernanceStore:
    def review_relation_rows(self, db, notebook_id: str, limit: int): ...
    def promotion_object_row(self, db, notebook_id: str, object_id: str): ...

class IndexProjectionStore:
    def artifact_version_row(self, db, notebook_id: str): ...
```

Move the SQL strings and row ordering verbatim. Do not combine writes that previously committed separately.

- [ ] **Step 4: Run component and behavior tests**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_notebook_store_component.py tests/test_notebook_summary_query.py \
  tests/test_notebook_share_copy.py tests/test_notebook_copy_service.py \
  tests/test_knowledge_governance_boundaries.py \
  tests/test_knowledge_governance_delegation.py \
  tests/test_scale_artifact_runtime.py tests/test_repository_callers_static.py -q
```

Expected: all pass and no service SQL offenders for these modules.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/{notebook_store,query_store,sharing_store,governance_store,knowledge_store,index_projection_store}.py \
  backend/app/services/{notebook_catalog,notebook_sharing,knowledge_governance,scale_artifact_runtime}.py \
  backend/tests
git commit -m "refactor(repository): move catalog governance and sharing SQL to stores"
```

### Task 5: Move lifecycle SQL into stores while preserving checkpoints

**Files:**
- Modify: `backend/app/repositories/sqlite/knowledge_store.py`
- Modify: `backend/app/repositories/sqlite/governance_store.py`
- Modify: `backend/app/repositories/sqlite/unified_kg_store.py`
- Modify: `backend/app/repositories/sqlite/index_projection_store.py`
- Modify: `backend/app/services/knowledge_lifecycle.py`
- Modify: `backend/tests/test_knowledge_lifecycle_delegation.py`
- Modify: `backend/tests/test_rebuild_checkpoint.py`
- Modify: `backend/tests/test_rebuild_cache.py`
- Modify: `backend/tests/test_rebuild_streaming.py`
- Modify: `backend/tests/test_rebuild_communities.py`
- Modify: `backend/tests/test_kg_mutation_phase_matrix.py`
- Modify: `backend/tests/test_kg_mutation_failure_boundaries.py`

**Interfaces:**
- Store primitives accept the caller-owned connection for existing atomic phases.
- `KnowledgeLifecycleService` retains the same public methods, progress events, checkpoints, locks, and failure policies.

- [ ] **Step 1: Add RED SQL-free and checkpoint-characterization tests**

Assert `knowledge_lifecycle.py` contains no SQL call nodes. Extend phase tests to record the order of store calls and database write contexts for delete/build/rebuild/relink/merge-review/checkpoint paths.

- [ ] **Step 2: Run RED**

Run lifecycle, rebuild checkpoint, mutation phase, and failure-boundary suites. Expected: SQL-free assertion fails while existing characterization tests pass.

- [ ] **Step 3: Extract cohesive store primitives**

Use explicit names grouped by phase:

```python
class KnowledgeStore:
    def delete_notebook_graph_rows(self, db, notebook_id: str) -> dict[str, int]: ...
    def lifecycle_object_rows(self, db, notebook_id: str): ...
    def lifecycle_relation_rows(self, db, notebook_id: str): ...

class GovernanceStore:
    def merge_review_batch(self, db, notebook_id: str, limit: int): ...
    def apply_merge_decisions(self, db, notebook_id: str, decisions: Sequence[dict]): ...

class UnifiedKgStore:
    def rebuild_seed_rows(self, db, notebook_id: str): ...
    def scratch_replace(self, db, notebook_id: str, run_id: str, rows: Sequence[tuple]): ...
```

Move SQL and row decoding verbatim. Keep LLM review loops, graph algorithms, and progress callbacks in the service.

- [ ] **Step 4: Run focused and scale regression suites**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_knowledge_lifecycle_delegation.py \
  tests/test_rebuild_checkpoint.py tests/test_rebuild_cache.py \
  tests/test_rebuild_streaming.py tests/test_rebuild_communities.py \
  tests/test_kg_mutation_phase_matrix.py tests/test_kg_mutation_failure_boundaries.py \
  tests/test_repository_callers_static.py -q
```

Expected: all pass and `knowledge_lifecycle.py` has zero product SQL sites.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/{knowledge_store,governance_store,unified_kg_store,index_projection_store}.py \
  backend/app/services/knowledge_lifecycle.py backend/tests
git commit -m "refactor(repository): move lifecycle persistence into stores"
```

### Task 6: Reduce SQLiteRepository to explicit compatibility delegates

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Create: `backend/app/services/knowledge_query.py`
- Create: `backend/app/services/pending_actions_service.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Modify: `backend/tests/test_repository_facade_contract.py`
- Modify: `backend/tests/test_repository_surface_manifest.py`
- Modify: `backend/tests/test_repository_monkeypatch_owners.py`
- Modify: `backend/tests/test_repository_api_contract.py`
- Modify: `backend/tests/test_kg_search.py`
- Modify: `backend/tests/test_kg_search_api.py`
- Modify: `backend/tests/test_node_context.py`
- Modify: `backend/tests/test_pending_actions.py`
- Modify: `backend/tests/test_pending_actions_api.py`
- Modify: `backend/tests/test_ask_repository_golden.py`

**Interfaces:**
- Public facade signatures and module re-exports remain frozen.
- Every facade method delegates once to the owner recorded in the manifest.

- [ ] **Step 1: Run the Task 1 facade RED contract and group offenders by owner**

Expected groups include KG search, graph/detail projections, pending actions, Ask dispatch, and maintenance duplicates.

- [ ] **Step 2: Add characterization tests for every moved algorithm**

Freeze response models, ordering, fail-open behavior, cache invalidation, and KeyError behavior before moving bodies. For Ask dispatch, assert all current and retired mode aliases produce the same handler selection.

- [ ] **Step 3: Move algorithms to single canonical services**

Create focused owners:

```python
class KnowledgeQueryService:
    def search(self, notebook_id: str, query: str, limit: int = 30) -> list[dict]: ...
    def graph(self, notebook_id: str) -> KnowledgeGraph: ...
    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict: ...

class PendingActionsService:
    def list_for_user(self, user_id: str) -> list[dict]: ...
```

Ask dispatch calls `runtime.ask_service().ask(...)`; maintenance compatibility methods call `self.maintenance.<method>(...)`. Move `_semantic_search`, hydration, canonical folding, response assembly, and other multi-step facade bodies into the named owner.

- [ ] **Step 4: Make manifest ownership mechanically truthful**

Record the actual runtime component name for each frozen member. Generate delegate evidence from facade AST and require `validate_ownership_manifest(..., delegates)` to pass. Remove patch allowances whose only consumers are tests and retarget those tests to component seams.

- [ ] **Step 5: Run facade and broad domain tests**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_facade_contract.py tests/test_repository_surface_manifest.py \
  tests/test_repository_monkeypatch_owners.py tests/test_repository_api_contract.py \
  tests/test_kg_search.py tests/test_kg_search_api.py tests/test_node_context.py \
  tests/test_pending_actions.py tests/test_pending_actions_api.py \
  tests/test_ask_repository_golden.py tests/test_repository_callers_static.py -q
```

Expected: all pass; facade contract has no violations.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py \
  backend/app/services/knowledge_query.py \
  backend/app/services/pending_actions_service.py \
  backend/app/services/repository_runtime.py \
  backend/app/repositories/ownership_manifest.py \
  backend/tests/test_repository_facade_contract.py \
  backend/tests/test_repository_surface_manifest.py \
  backend/tests/test_repository_monkeypatch_owners.py \
  backend/tests/test_kg_search.py backend/tests/test_kg_search_api.py \
  backend/tests/test_pending_actions.py backend/tests/test_pending_actions_api.py
git commit -m "refactor(repository): finish the explicit compatibility facade"
```

### Task 7: Harden backup-only snapshot verification

**Files:**
- Modify: `scripts/verify_repository_snapshot.py`
- Modify: `backend/tests/test_repository_snapshot_verifier.py`

**Interfaces:**
- Produces exact `MIGRATION_MANIFEST` and `SEED_MANIFEST` constants.
- Keeps CLI arguments and PASS/FAIL summary format.

- [ ] **Step 1: Add RED adversarial verifier tests**

Add tests that inject an unmanifested empty table, column, index-definition change, trigger, view, and fake builtin row and assert FAIL. Patch `shutil.rmtree` to raise and assert verification fails with the temporary path. Add database filenames containing `?`, `#`, and `%`.

- [ ] **Step 2: Run RED**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_snapshot_verifier.py -q
```

Expected: new adversarial cases currently pass incorrectly or fail to open URI paths.

- [ ] **Step 3: Implement exact schema and seed manifests**

```python
MIGRATION_MANIFEST = {
    (9, 10): {
        "tables": {"kg_rebuild_checkpoint": EXPECTED_CREATE_SQL},
        "columns": {},
        "indexes": {},
        "triggers": {},
        "views": {},
    }
}

SEED_MANIFEST = {
    "users": {"user-local": {"role": "admin"}},
    "user_profiles": {"profile-local": {"user_id": "user-local"}},
    "concept_whitelist": frozen_builtin_whitelist(),
    "object_schemas": frozen_builtin_schemas(),
}
```

Snapshot all `sqlite_master` object definitions. Compare each migration hop against its exact manifest; reject every extra object/value.

- [ ] **Step 4: Encode SQLite URIs and make cleanup authoritative**

Use a helper based on `urllib.parse.quote(path.as_posix(), safe="/")`. If cleanup fails, set the result to FAIL and report only `temporary_backup_retained=<path>`; never print row content. Document and test the live-WAL SHM mtime exception.

- [ ] **Step 5: Run verifier, migration, and real fixture tests**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_snapshot_verifier.py tests/test_repository_v9_fixture.py \
  tests/test_legacy_db_compat.py tests/test_schema_version_migration.py -q
cd ..
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/verify_repository_snapshot.py \
  --database backend/tests/fixtures/repository_v9/baseline.db \
  --storage-dir backend/tests/fixtures/repository_v9/storage
```

Expected: tests pass and CLI ends with `PASS schema=v9 changed_tables=0`.

- [ ] **Step 6: Commit**

```bash
git add scripts/verify_repository_snapshot.py \
  backend/tests/test_repository_snapshot_verifier.py
git commit -m "fix(repository): harden old database snapshot verification"
```

### Task 8: Synchronize documentation, verify, review, and update the existing PR

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `fangan_done.md`
- Modify: `docs/superpowers/plans/2026-07-10-repository-composition-refactor.md`
- Modify: `docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md`
- Modify: `backend/tests/test_architecture_documentation.py`

**Interfaces:**
- Documentation claims derive from executable architecture tests.
- Existing PR branch remains `codex/repository-composition-refactor`.

- [ ] **Step 1: Update schema and boundary wording in all synchronized docs**

Use the exact statement:

```text
The refactor does not change the schema version present on its master baseline
(SCHEMA_VERSION = 10). The committed v9 compatibility fixture upgrades through
the existing v10 migration and remains readable.
```

Describe the now-verified store-only product SQL/raw row selection boundary, the
established application/query projection assembly boundary, thin facade, ports,
launch compensation, the process-global `REPORT_CANCELLATIONS` identity exception,
verifier manifests, and live-WAL SHM metadata exception.

- [ ] **Step 2: Replace prose-to-prose documentation tests**

Assert documentation statements together with source-level guard results:

```python
def test_store_only_sql_claim_is_executable():
    assert product_sql_sites() == []
    assert "service does not assemble product SQL" in architecture_text()
```

- [ ] **Step 3: Run all focused architecture and compatibility gates**

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_repository_facade_contract.py \
  tests/test_repository_callers_static.py \
  tests/test_repository_protocol_coverage.py \
  tests/test_repository_api_contract.py \
  tests/test_repository_snapshot_verifier.py \
  tests/test_legacy_db_compat.py tests/test_schema_version_migration.py \
  tests/test_ask_execution_coordinator.py tests/test_report_execution.py \
  tests/test_architecture_documentation.py -q
```

Expected: all pass.

- [ ] **Step 4: Verify frozen and real old databases**

Run the frozen fixture command from Task 7, then run the same verifier against the main checkout's `.local/silicon_notebook.db` and `.local/storage`. Expected: both PASS; no original source/storage metadata change beyond the documented live SHM exception.

- [ ] **Step 5: Run the complete offline gate**

```bash
cd /private/tmp/silicon-notebook-repository-composition-refactor
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: backend, frontend tests, TypeScript, and production build all pass.

- [ ] **Step 6: Merge latest master when required and repeat the complete gate**

```bash
git fetch origin master
git merge --no-ff origin/master
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

If `origin/master` is already an ancestor, record that fact and do not create an empty merge commit.

- [ ] **Step 7: Request whole-branch review and fix all Critical/Important findings**

Review range: `git merge-base origin/master HEAD` through `HEAD`. Re-run the covering focused tests after fixes, then the complete gate.

- [ ] **Step 8: Commit docs and push the same PR branch**

```bash
git add README.md README_zh.md AGENTS.md architecture.md fangan_done.md \
  docs/superpowers/plans/2026-07-10-repository-composition-refactor.md \
  docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md \
  backend/tests/test_architecture_documentation.py
git commit -m "docs(repository): record completed composition boundaries"
git push origin codex/repository-composition-refactor
```

Update the existing PR description with the final test counts and both database-verifier results; do not create a second PR.
