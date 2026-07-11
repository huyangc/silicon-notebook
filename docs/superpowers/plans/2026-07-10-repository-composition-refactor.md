# Repository Composition Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在一个 PR 内把 SQLiteRepository 重构为显式组合 facade，并将 SQLite persistence、业务服务、检索、Ask、报告、缓存与索引 runtime 拆到明确边界，同时保证 API、schema v9、旧数据库、排序、事务 checkpoint 和异步行为完全一致。

**Architecture:** 采用 contract-first strangler：先冻结基线 facade、旧库和行为 goldens，再逐域把实现移动到共享 SqliteDatabase、SQLite stores、application services 和 runtime coordinators，原 facade 在每一步都显式委托且保持可用。RepositoryRuntime 是 cached SQLiteRepository 内部的 composition root；本 PR 不引入 FastAPI lifespan 或新的 shutdown 语义。

**Tech Stack:** Python 3.11+、FastAPI、标准库 sqlite3、numpy、pytest、Next.js 15 / TypeScript（仅做兼容验证）。

## Global Constraints

- 实现基线是 origin/master 3334626；已通过的 characterization tests 和生产代码是行为真相。
- 所有实现位于分支 `codex/repository-composition-refactor` 的隔离 worktree；最终只创建一个 PR。
- endpoint、请求/响应 model、HTTP 映射、前端行为、SQLite 表/列/index/foreign key 和 SCHEMA_VERSION=9 均不得改变。
- 不新增 migration，不更新 schema golden，不做自动 table rebuild、全库 JSON/BLOB 转码、ID 重映射或数据清洗。
- 必须继续读取 JSON TEXT 与 little-endian float32 BLOB 混合向量，以及已有 source/scale/viz artifacts。
- 每个 RepositoryRuntime 只有一个 SqliteDatabase 和一个实例级 RLock；同一 runtime 的 stores 共享它，不把多个 repository 实例改成跨实例全局锁。
- 保留原有 transaction/compensation checkpoints；既有原子操作不拆分，既有 chunked/best-effort checkpoint 不合并。
- Ask transport disconnect 不取消 detached worker；显式 cancel 只保留基线 final-save checkpoint，不能在本 PR 中宣称消除 checkpoint 后的竞态。
- 非流式 Ask 不创建 ask_jobs；streaming 的 begin、answer save、job finish 和 failed/cancel cleanup 继续是独立短事务。
- Synthetic `progress/start` 只发送不持久化；真实 reasoning trace 先持久化再发送；answer commit 早于 job finish。
- 每次请求动态解析当前用户模型配置；Ask、KG scheduler 和 report worker 继续通过 contextvars.copy_context() 传播用户上下文。
- SQLiteRepository 不继承 Protocol，不使用 `__getattr__`，不得把领域 SQL、检索算法、answer synthesis 或 job lifecycle 留在 facade。
- 主业务数据库 SQL 只能位于 `backend/app/repositories/sqlite/` 或明确的 SQLite maintenance adapter；仅 baseline fixture/snapshot backup 工具与必须保持 stdlib 的 `diag_slow.py` 可走经过 AST 审计的窄只读/backup 例外，独立 LLM cache/eval DB 与合成 SQLite benchmark 使用另一份精确 allowlist。
- 不新增 SQLAlchemy、PostgreSQL、pgvector、mypy/pyright、容器或其他依赖。
- 手工文件修改使用 `apply_patch`；机械搬移必须保持方法体语义，随后再单独替换依赖。
- 每个 RED 测试必须先观察到预期失败；每个小任务先跑定向测试，每个 review gate 再跑 `scripts/check.sh`。
- 不接触主 checkout 的用户文件；真实旧库只通过 SQLite backup 快照验证，storage 使用隔离目录，禁止 symlink 回原目录。
- 架构落地后同步 `README.md`、`README_zh.md`、`AGENTS.md`、`architecture.md`、`fangan_done.md` 和文档契约测试。

---

## Final File Responsibilities

### Repository contracts and composition

- Create `backend/app/repositories/__init__.py`: repository package exports.
- Create `backend/app/repositories/ports.py`: consumer-driven Protocols、application ports 与 compatibility NotebookRepository aggregate。
- Create `backend/app/repositories/ownership_manifest.py`: one canonical port owner for every frozen consumer-visible member.
- Modify `backend/app/services/repository.py`: re-export ports、UploadedSourceFile 与旧 import。
- Create `backend/app/core/request_context.py`: request-user ContextVar 与 copied-context helper。
- Create `backend/app/core/ask_context.py`: request-scoped Ask model-error and query-embedding memo ContextVars.
- Create `backend/app/services/model_provider.py`: dynamic per-user/system chat/rerank clients and ModelErrorSink.
- Create `backend/app/services/repository_runtime.py`: constructs one database, stores, services, caches, model providers and Ask cancellation registry.
- Modify `backend/app/services/sqlite_repository.py`: explicit compatibility facade and legacy re-exports only.

### SQLite adapters

- Create `backend/app/repositories/sqlite/__init__.py`: SQLite adapter exports.
- Create `backend/app/repositories/sqlite/database.py`: path resolution, connection PRAGMAs, one runtime-local RLock and write context.
- Create `backend/app/repositories/sqlite/migrations.py`: SCHEMA_VERSION=9、migration registry、recovery 与 seed。
- Create `backend/app/repositories/sqlite/identity_store.py`: users、profiles、sessions、model settings。
- Create `backend/app/repositories/sqlite/notebook_store.py`: notebook rows、tier、access lookups and summary/analytics queries.
- Create `backend/app/repositories/sqlite/sharing_store.py`: share/member rows and copy persistence primitives.
- Create `backend/app/repositories/sqlite/source_store.py`: sources、elements、source projections and source cleanup primitives.
- Create `backend/app/repositories/sqlite/chunk_store.py`: chunk rows、FTS rows and source-chunk replacement.
- Create `backend/app/repositories/sqlite/embedding_store.py`: element/object/relation/chunk embeddings and chunks.
- Create `backend/app/repositories/sqlite/knowledge_store.py`: object/relation/schema/provenance/FTS persistence.
- Create `backend/app/repositories/sqlite/governance_store.py`: review/promotion/merge/conflict/cluster rows.
- Create `backend/app/repositories/sqlite/unified_kg_store.py`: unified state、canonical graph、mention bridge and communities.
- Create `backend/app/repositories/sqlite/ask_state_store.py`: conversations、answers、jobs、traces and feedback.
- Create `backend/app/repositories/sqlite/report_store.py`: report CRUD、progress and export rows.
- Create `backend/app/repositories/sqlite/query_store.py`: pending actions, admin and notebook/source/element/KG search read projections.
- Create `backend/app/repositories/sqlite/index_projection_store.py`: scale/index version, graph and embedding projections.
- Create `backend/app/repositories/sqlite/maintenance.py`: SQLite-only batch/backfill/diagnostic operations excluded from portable ports.
- Create `backend/app/repositories/source_files.py`: original source file read/write/delete adapter.
- Create `backend/app/repositories/filesystem/scale_artifact_store.py`: scale/viz manifest and atomic directory operations.

### Application services and runtime state

- Create `backend/app/services/notebook_scale.py`: lazy copy/index predicates with preserved short-circuit/query-count semantics.
- Create `backend/app/services/notebook_catalog.py`: notebook CRUD/tier application service and summary projection.
- Create `backend/app/services/notebook_sharing.py`: share/access/copy application services.
- Create `backend/app/services/source_ingestion.py`: upload/parse/status/background embedding/extraction orchestration.
- Create `backend/app/services/source_embedding.py`: source/object/relation/chunk embedding coordination.
- Create `backend/app/services/source_chunking.py`: chunk generation and source-chunk replacement.
- Create `backend/app/services/knowledge_contracts.py`: facade-independent status constants and knowledge exceptions.
- Create `backend/app/services/schema_registry.py`: schema CRUD/induction orchestration over store, source and model ports.
- Create `backend/app/services/knowledge_lifecycle.py`: store/relink/rebuild/source-derived cleanup orchestration.
- Create `backend/app/services/knowledge_governance.py`: dedupe/merge/conflict/promotion/review orchestration.
- Create `backend/app/services/kg_mutation.py`: call-site phase matrix, dirty/version/cache hooks and documented exemptions.
- Create `backend/app/services/retrieval_snapshot_cache.py`: vector/token/membership/federated graph/PPR snapshot cache ownership.
- Create `backend/app/services/scale_artifact_catalog.py`: version-aware artifact loading.
- Create `backend/app/services/scale_index_builder.py`: full build/fold/viz build orchestration.
- Create `backend/app/services/scale_artifact_runtime.py`: scale/viz LRU、HNSW handles、artifact load/build/fold/scheduler state.
- Modify `backend/app/services/retrieval_service.py`: real retrieval composition service with no facade backreference.
- Create `backend/app/services/retrieval_candidates.py`: element/object/relation/chunk candidate retrieval.
- Create `backend/app/services/graph_retrieval.py`: PPR、graph snapshots、bounded traversal and follow-chain.
- Create `backend/app/services/evidence_context.py`: evidence rendering、anchors、citations and grounding.
- Create `backend/app/services/ask_service.py`: Ask mode orchestration, synthesis and answer persistence.
- Create `backend/app/services/ask_execution.py`: detached Ask execution and Ask cancellation registry.
- Modify `backend/app/services/reasoning_retrieval.py`: consumes narrow ports only.
- Modify `backend/app/services/report_engine.py`: consumes report/retrieval/evidence/source/model/observability ports.
- Create `backend/app/services/report_execution.py`: report launch and process-global cancellation compatibility.

### Frozen facade ownership map

Task 1's machine-readable manifest is exhaustive, including private wrappers and mutable properties. The implementation must assign every current public family to exactly one canonical owner before Gate 26:

| Canonical owner | Frozen facade members/families |
|---|---|
| Runtime model provider + ModelErrorSink | `llm_client`, `reasoning_llm_client`, `rewrite_llm_client`, `kg_llm_client`, `rerank_client`, `_note_model_error` |
| IdentityStore | current user, user/session/auth/model-settings methods |
| NotebookCatalogService + QueryStore | notebook templates/CRUD/tier, `notebook_analytics`, `pending_actions`, `search_notebook` |
| NotebookSharingService + SharingStore | access guards, share/member/copy methods and ownership lookups |
| SQLiteMaintenanceAdapter | FTS/backfill/eval-only and CLI maintenance operations |
| SourceStore + SourceIngestionService | source list/detail/elements, import/upload/URL/process/parse/delete/extract and metadata/local-URL/extraction helpers |
| SchemaRegistryService + KnowledgeStore | `effective_schemas`, schema CRUD and `propose_schemas` |
| KnowledgeStore / UnifiedKgStore | knowledge type/list/legacy graph/search, raw relation/cluster/community reads, `concept_detail`, `node_context` and simple compatibility row primitives |
| KnowledgeLifecycleService | `delete_notebook_kg`, full-notebook build/rebuild, KG store/relink/clusters/fusion, unified graph/status/neighbors/rebuilds, canonical/mention/community rebuild and community summary/report orchestration |
| KnowledgeGovernanceService + GovernanceStore | review, merge/conflict/promotion/dedupe/update/whitelist and decision compatibility primitives |
| RetrievalSnapshotCache + ScaleArtifactRuntime | retrieval caches plus scale/viz version/load/build/fold/status/trigger/cancel/auto-index |
| RetrievalService + EvidenceContextService | object/relation/chunk/element/federated/PPR/follow-chain retrieval and evidence/context/citation helpers |
| AskStateStore + AskService + AskExecutionCoordinator | conversation/answer/feedback/job/trace persistence, mode synthesis and detached streaming/cancel lifecycle |
| ReportStore + ReportEngine + ReportExecutionCoordinator | report CRUD/export, planning/generation and background cancellation lifecycle |

An ownership-manifest test must fail as soon as a frozen member has zero/multiple owners or a Gate leaves its implementation body in the facade. Compatibility wrappers may remain, but they are not canonical owners.

### New contract and compatibility tests

- Create `backend/tests/fixtures/repository_contract/facade_surface.json`: frozen callable/property/import surface plus canonical port owner.
- Create `backend/tests/fixtures/repository_contract/{transaction_phases,mutation_phases,error_policies,ask_responses,api_contract}.json`: frozen transaction/error/Ask/OpenAPI behavior contracts.
- Create `backend/tests/fixtures/repository_v9/baseline.db`: immutable database created by the baseline runtime.
- Create `backend/tests/fixtures/repository_v9/expected_snapshot.json`: normalized rows/API/context/Ask golden metadata.
- Create `backend/tests/fixtures/repository_v9/manifest.json`: source commit, schema version and artifact hashes.
- Create `backend/tests/fixtures/repository_v9/storage/`: minimal copied source/scale/viz artifacts.
- Create focused tests named in the tasks below; keep all existing tests as the behavior oracle.
- Create `scripts/verify_repository_snapshot.py`: backup-only real old-database verifier with private-content-safe output.

## Review-Gate Execution Rule

Each numbered task below is a fresh TDD/review unit. Within a gate, commit after every task whose file ownership can be reverted independently. At the end of each gate:

~~~bash
git diff --check
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

Expected: exit 0; backend suite, frontend tests, TypeScript and Next.js build all pass. Do not regenerate any frozen fixture or golden after refactored code exists.

Task 1 records every production and test monkeypatch consumer of facade/private members. Before a body moves, the owning task must update each affected test to patch the canonical store/service/runtime seam and stage that test in the same commit. Never retain a facade backreference merely to keep an old patch target alive; the ownership test fails when a moved private wrapper remains a test's active seam. Module/class constants that are true production compatibility seams stay late-bound through `RepositoryCompatibilitySeams`; test-only probes migrate to the component.

---

## Review Gate 1 — Baseline Contracts, Ports and Scale Policy

### Task 1: Freeze the master facade, phase contracts, Ask goldens and v9 database

**Files:**
- Create: `scripts/generate_repository_contract_fixtures.py`
- Create: `backend/tests/test_repository_surface_manifest.py`
- Create: `backend/tests/test_repository_phase_contracts.py`
- Create: `backend/tests/test_repository_v9_fixture.py`
- Create: `backend/tests/test_ask_repository_golden.py`
- Create: `backend/tests/test_repository_api_contract.py`
- Create: `backend/tests/fixtures/repository_contract/facade_surface.json`
- Create: `backend/tests/fixtures/repository_contract/transaction_phases.json`
- Create: `backend/tests/fixtures/repository_contract/mutation_phases.json`
- Create: `backend/tests/fixtures/repository_contract/error_policies.json`
- Create: `backend/tests/fixtures/repository_contract/ask_responses.json`
- Create: `backend/tests/fixtures/repository_contract/api_contract.json`
- Create: `backend/tests/fixtures/repository_v9/baseline.db`
- Create: `backend/tests/fixtures/repository_v9/expected_snapshot.json`
- Create: `backend/tests/fixtures/repository_v9/manifest.json`
- Create: `backend/tests/fixtures/repository_v9/storage/`
- Create: `backend/tests/fixtures/repository_v9/README.md`

**Interfaces:**
- Consumes: unmodified runtime code from baseline 3334626.
- Produces:

The generator must implement these fixed callables: `collect_facade_surface() -> dict[str, dict[str, object]]`, `generate_v9_fixture(output_dir: Path) -> None`, `normalized_repository_snapshot(repo: SQLiteRepository, notebook_id: str) -> dict[str, object]`, `generate_ask_goldens(output_path: Path) -> None`, `generate_api_contract(output_path: Path) -> None`, and `main(argv: Sequence[str] | None = None) -> int`. `facade_surface.json` records `kind`, `signature`, `consumers` and one later-assigned `owner` for each method/property/mutable property/constant/private wrapper referenced from:

~~~text
backend/app/api
backend/app/main.py
backend/app/services
backend/app/eval
backend/app/scripts
scripts
backend/tests
ASK_MODES[*].handler
~~~

For every `monkeypatch.setattr`/`patch.object` target on the facade instance, class or compatibility module, also record the test file, line, target name and whether it is production-compatible or test-only; later gates consume this list when migrating patch targets.

- [ ] **Step 1: Write the failing fixture-presence and phase-contract tests**

~~~python
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures"
REQUIRED_PHASES = {
    "process_source",
    "store_kg",
    "delete_source",
    "parse_source",
    "update_knowledge",
    "merge_knowledge",
    "approve_promotion",
    "confirm_conflict",
    "set_edge_review",
    "copy_notebook",
    "streaming_ask",
    "migration_recovery_seed",
}
REQUIRED_ERROR_POLICIES = {
    "append_ask_trace",
    "report_corpus_map",
    "model_error_recording",
    "update_report_missing",
    "delete_report_missing",
    "source_chunk_build",
    "source_embedding",
    "source_extraction",
}


def test_committed_v9_fixture_exists_and_is_self_contained():
    root = FIXTURES / "repository_v9"
    assert (root / "baseline.db").is_file()
    assert (root / "expected_snapshot.json").is_file()
    assert (root / "manifest.json").is_file()
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["source_commit"] == "3334626"
    assert manifest["schema_version"] == 9
    assert manifest["storage_files"]


def test_phase_contracts_list_every_required_operation():
    root = FIXTURES / "repository_contract"
    tx = json.loads((root / "transaction_phases.json").read_text())
    err = json.loads((root / "error_policies.json").read_text())
    assert set(tx) == REQUIRED_PHASES
    assert set(err) == REQUIRED_ERROR_POLICIES


def test_api_contract_fixture_records_openapi_and_serialization():
    contract = json.loads(
        (FIXTURES / "repository_contract" / "api_contract.json").read_text()
    )
    assert contract["source_commit"] == "3334626"
    assert contract["openapi"]["paths"]
    assert contract["openapi"]["components"]["schemas"]
    assert set(contract["serialization"]) >= {
        "notebook_summary",
        "source_detail",
        "knowledge_page",
        "ask_job_detail",
        "conversation_detail",
        "report",
    }
~~~

- [ ] **Step 2: Run the new tests and confirm RED**

Run:

~~~bash
cd backend
python -m pytest \
  tests/test_repository_surface_manifest.py \
  tests/test_repository_phase_contracts.py \
  tests/test_repository_v9_fixture.py \
  tests/test_ask_repository_golden.py \
  tests/test_repository_api_contract.py -q
~~~

Expected: FAIL with missing `facade_surface.json` and `baseline.db`.

- [ ] **Step 3: Implement the fixture generator against the untouched runtime**

The generator must:

1. Refuse generation unless `git rev-parse 3334626^{commit}` resolves and the complete `backend/app/**/*.py` path/content set matches that baseline; design/plan/fixture-generator commits may differ.
2. Run with offline settings, deterministic IDs/time and deterministic fake chat/reasoning/rewrite/KG/rerank/embed adapters; instantiate no real provider client, make no network call and disable auto-index background scheduling.
3. Build schema through `SQLiteRepository(Settings(...))`, then create one user-owned notebook containing one Markdown source, source elements/chunks, Concept and Claim objects, one relation, mixed JSON TEXT/float32 BLOB vectors, one conversation/answer, one Ask job/trace, one report, one share token/member and one minimal valid scale/viz artifact.
4. Use `sqlite3.Connection.backup()` to write `baseline.db`; do not copy a live WAL file.
5. Normalize only IDs, timestamps, password salt/hash, auth tokens and absolute fixture paths in JSON goldens; never rewrite the database rows.
6. Record SHA-256, size and relative path for every storage artifact in `manifest.json`.
7. Generate `chunk`, `reasoning` and `graph` AskResponse/answers.payload goldens, including every early exit: unconfigured model, no KG, no hits and large-graph refusal.
8. Freeze canonical `app.openapi()` output (including paths, operation IDs, request/response refs, status codes, security and component schemas) plus representative baseline endpoint/Pydantic serialization for notebook, source, knowledge, Ask job, conversation, report, sharing and error bodies. Sort mapping keys only; do not normalize away route names, defaults, required fields or response shapes.

Use this guard and manifest layout:

~~~python
SOURCE_COMMIT = "3334626"


def _assert_baseline_sources(repo_root: Path) -> None:
    subprocess.check_call(
        ["git", "rev-parse", "--verify", f"{SOURCE_COMMIT}^{{commit}}"],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
    )
    baseline_paths = {
        line
        for line in subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", SOURCE_COMMIT, "backend/app"],
            cwd=repo_root,
            text=True,
        ).splitlines()
        if line.endswith(".py")
    }
    current_paths = {
        str(path.relative_to(repo_root))
        for path in (repo_root / "backend" / "app").rglob("*.py")
    }
    if current_paths != baseline_paths:
        raise SystemExit("refuse fixture regeneration after backend/app path changes")
    for relative in sorted(baseline_paths):
        baseline = subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:{relative}"],
            cwd=repo_root,
        )
        if (repo_root / relative).read_bytes() != baseline:
            raise SystemExit(f"refuse fixture regeneration after runtime change: {relative}")


def _storage_manifest(storage: Path) -> list[dict[str, object]]:
    return [
        {
            "path": str(path.relative_to(storage)),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(p for p in storage.rglob("*") if p.is_file())
    ]
~~~

- [ ] **Step 4: Generate once, then prove the frozen artifacts are stable**

Run:

~~~bash
python scripts/generate_repository_contract_fixtures.py
cd backend
python -m pytest \
  tests/test_repository_surface_manifest.py \
  tests/test_repository_phase_contracts.py \
  tests/test_repository_v9_fixture.py \
  tests/test_ask_repository_golden.py \
  tests/test_repository_api_contract.py -q
~~~

Expected: PASS; rerunning the generator before runtime changes produces byte-identical JSON and normalized snapshots.

- [ ] **Step 5: Commit the immutable oracle**

~~~bash
git add scripts/generate_repository_contract_fixtures.py \
  backend/tests/test_repository_surface_manifest.py \
  backend/tests/test_repository_phase_contracts.py \
  backend/tests/test_repository_v9_fixture.py \
  backend/tests/test_ask_repository_golden.py \
  backend/tests/test_repository_api_contract.py \
  backend/tests/fixtures/repository_contract \
  backend/tests/fixtures/repository_v9
git commit -m "test(repository): freeze facade and database contracts"
~~~

### Task 2: Add consumer-driven Protocols, ownership manifest and typed API accessors

**Files:**
- Create: `backend/app/repositories/__init__.py`
- Create: `backend/app/repositories/ports.py`
- Create: `backend/app/repositories/ownership_manifest.py`
- Modify: `backend/app/services/repository.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/routes.py:593-650`
- Modify: `backend/app/eval/speed.py:77-125`
- Modify: `backend/tests/test_trackA_eval_connect.py`
- Create: `backend/tests/test_repository_ports.py`
- Create: `backend/tests/test_repository_ownership.py`
- Create: `backend/tests/test_eval_speed_public_path.py`

**Interfaces:**

`ports.py` owns `UploadedSourceFile`, `SourceScheduler = Callable[[str], None]`, the following Protocols, and the compatibility aggregate. Use the exact baseline signatures recorded by Task 1:

~~~python
@dataclass
class UploadedSourceFile:
    file_name: str
    content_type: str
    content: bytes
    doc_type: str = ""


class IdentityRepository(Protocol):
    def current_user(self) -> UserProfile: ...
    def get_user_model_settings(self, user_id: str) -> dict: ...
    def set_user_model_settings(self, user_id: str, settings: dict) -> None: ...
    def resolve_model_config(
        self, user: UserProfile, role: str
    ) -> ResolvedModelConfig: ...
    def create_user(self, username: str, password: str) -> UserProfile: ...
    def authenticate_user(
        self, username: str, password: str
    ) -> UserProfile | None: ...
    def create_session(self, user_id: str) -> str: ...
    def resolve_session(self, token: str) -> UserProfile | None: ...
    def delete_session(self, token: str) -> None: ...


class NotebookAccessRepository(Protocol):
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def is_member(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool: ...
    def user_can_read_source(self, source_id: str, user_id: str) -> bool: ...
    def source_owner(self, source_id: str) -> str | None: ...
    def conversation_owner(self, conversation_id: str) -> str | None: ...
    def answer_owner(self, answer_id: str) -> str | None: ...
    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool: ...


class NotebookCatalogRepository(Protocol):
    def list_notebook_templates(self) -> list[NotebookTemplate]: ...
    def list_notebooks(self) -> list[NotebookSummary]: ...
    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...
    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...
    def update_notebook(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> NotebookSummary: ...
    def delete_notebook(self, notebook_id: str) -> None: ...
    def mark_notebook_base(self, notebook_id: str) -> None: ...
    def set_notebook_personal(self, notebook_id: str) -> None: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse: ...


class NotebookSharingRepository(Protocol):
    def share_notebook(self, notebook_id: str) -> dict: ...
    def unshare_notebook(self, notebook_id: str) -> None: ...
    def find_notebook_by_share_token(self, token: str) -> str | None: ...
    def notebook_copy_stats(self, notebook_id: str) -> dict: ...
    def shared_preview(self, notebook_id: str) -> dict: ...
    def shared_by_me(self, user_id: str) -> list: ...
    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: str | None = None,
    ) -> NotebookSummary: ...
    def add_member(self, notebook_id: str, user_id: str) -> None: ...
    def remove_member(self, notebook_id: str, user_id: str) -> None: ...
    def kick_all_members(self, notebook_id: str) -> None: ...
    def list_members(self, notebook_id: str) -> list: ...
    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary: ...
    def leave_notebook(self, notebook_id: str, user_id: str) -> None: ...


class SourceRepository(Protocol):
    def list_sources(self, notebook_id: str) -> list[SourceSummary]: ...
    def list_sources_page(
        self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = ""
    ) -> PaginatedSources: ...
    def import_sources(
        self, notebook_id: str, payload: SourceImportRequest
    ) -> list[SourceSummary]: ...
    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: SourceScheduler | None = None,
    ) -> AddUrlSourcesResult: ...
    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: SourceScheduler | None = None,
    ) -> list[SourceSummary]: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    def process_source(self, source_id: str) -> SourceSummary: ...
    def parse_source(self, source_id: str) -> SourceSummary: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def delete_source(self, source_id: str) -> None: ...
    def extract_source(self, source_id: str) -> None: ...


class KnowledgeReadRepository(Protocol):
    def knowledge_types(self, notebook_id: str) -> list[KnowledgeTypeCount]: ...
    def list_knowledge(
        self,
        notebook_id: str,
        object_type: str,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedKnowledge: ...
    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph: ...
    def kg_search(self, notebook_id: str, q: str, k: int = 30) -> list: ...
    def unified_graph(
        self,
        notebook_id: str,
        level: str = "concept",
        limit: int | None = None,
    ) -> dict: ...
    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict: ...
    def node_context(self, notebook_id: str, object_id: str) -> dict: ...
    def kg_neighbors(
        self, notebook_id: str, object_id: str, cap: int = 50
    ) -> dict: ...


class SchemaRegistryRepository(Protocol):
    def list_object_schemas(self) -> list[ObjectSchemaModel]: ...
    def create_object_schema(
        self, payload: ObjectSchemaCreate
    ) -> ObjectSchemaModel: ...
    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel: ...
    def delete_object_schema(self, object_type: str) -> None: ...
    def propose_schemas(self, notebook_id: str) -> list[ObjectSchemaModel]: ...


class KnowledgeGovernanceRepository(Protocol):
    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard: ...
    def find_duplicates(
        self, notebook_id: str, object_type: str
    ) -> list[DuplicateGroup]: ...
    def merge_knowledge(
        self, notebook_id: str, source_id: str, payload: MergeRequest
    ) -> RuleCard: ...
    def review_queue(
        self, notebook_id: str, limit: int = 200
    ) -> list[dict]: ...
    def set_edge_review(
        self, notebook_id: str, rel_id: str, status: str
    ) -> None: ...
    def pending_merges(self, notebook_id: str) -> list[dict]: ...
    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None: ...
    def reject_merge(self, notebook_id: str, candidate_id: str) -> None: ...
    def pending_conflicts(self, notebook_id: str) -> list[dict]: ...
    def resolve_notebook_conflicts(self, notebook_id: str) -> dict: ...
    def confirm_conflict(self, notebook_id: str, candidate_id: str) -> dict: ...
    def reject_conflict(self, notebook_id: str, candidate_id: str) -> None: ...
    def concept_whitelist_list(self) -> list[dict]: ...
    def concept_whitelist_add(self, term: str, note: str = "") -> dict: ...
    def concept_whitelist_remove(self, term: str) -> None: ...
    def review_pending_merges(
        self,
        notebook_id: str,
        limit: int = 50,
        confirm_threshold: float | None = None,
        separate_threshold: float | None = None,
    ) -> dict: ...
    def merge_review_job_status(self, notebook_id: str) -> dict: ...
    def run_merge_review_job(
        self, notebook_id: str, *, batch: int = 100
    ) -> dict: ...
    def propose_promotion(self, notebook_id: str, object_id: str) -> dict: ...
    def list_promotion_queue(
        self, status_filter: str | None = None
    ) -> list[dict]: ...
    def approve_promotion(self, candidate_id: str) -> dict: ...
    def reject_promotion(
        self, candidate_id: str, reason: str = ""
    ) -> dict: ...


class KnowledgeLifecycleRepository(Protocol):
    def build_notebook_kg(
        self, notebook_id: str, *, progress: Callable | None = None
    ) -> dict: ...
    def rebuild_notebook_kg(self, notebook_id: str) -> dict: ...
    def relink_notebook_kg(self, notebook_id: str) -> dict: ...
    def rebuild_unified_kg(
        self,
        notebook_id: str,
        progress: Callable[[str, int, int], None] | None = None,
        force: bool = False,
    ) -> int: ...
    def unified_kg_status(self, notebook_id: str) -> dict: ...


class IndexLifecycleRepository(Protocol):
    def trigger_scale_index_rebuild(
        self, notebook_id: str, when: str = "now", mode: str = "auto"
    ) -> dict: ...
    def cancel_scale_index(self, notebook_id: str) -> dict: ...
    def scale_index_status(self, notebook_id: str) -> dict: ...
    def index_status(self, notebook_id: str) -> dict: ...


class AskStateRepository(Protocol):
    def begin_ask_job(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: str,
        cancel_event: threading.Event,
    ) -> tuple[str, str]: ...
    def finish_ask_job(
        self,
        job_id: str,
        status: str,
        *,
        answer_id: str = "",
        error: str = "",
    ) -> None: ...
    def cancel_ask_job(self, job_id: str, user_id: str) -> dict: ...
    def ask_job_status(self, job_id: str) -> dict: ...
    def append_ask_trace(self, job_id: str, step: dict) -> None: ...
    def ask_job_detail(self, job_id: str) -> dict: ...
    def list_conversations(
        self, notebook_id: str
    ) -> list[ConversationSummary]: ...
    def get_conversation(self, conversation_id: str) -> ConversationDetail: ...
    def rename_conversation(self, conversation_id: str, title: str) -> None: ...
    def delete_conversation(self, conversation_id: str) -> None: ...
    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int
    ) -> int: ...
    def submit_feedback(
        self, answer_id: str, payload: FeedbackRequest
    ) -> FeedbackResponse: ...


class ReportRepository(Protocol):
    def create_report(
        self, notebook_id: str, question: str, depth: int = 2
    ) -> str: ...
    def update_report(
        self,
        notebook_id: str,
        report_id: str,
        *,
        status=None,
        progress=None,
        error=None,
        outline=None,
        sections=None,
        gaps=None,
        references=None,
        content_md=None,
        section_status=None,
    ) -> None: ...
    def get_report(self, notebook_id: str, report_id: str) -> dict: ...
    def list_reports(self, notebook_id: str) -> list: ...
    def delete_report(self, notebook_id: str, report_id: str) -> None: ...
    def export_reports(self, notebook_id: str, report_ids: list) -> list: ...


class AdminQueryRepository(Protocol):
    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def pending_actions(self, user_id: str) -> dict: ...


class AskExecutionPort(Protocol):
    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse: ...
    def ask_chunk(
        self,
        notebook_id: str,
        payload: AskRequest,
        cancel_event: CancelEvent = None,
    ) -> AskResponse: ...
    def ask_reasoning(
        self,
        notebook_id: str,
        payload: AskRequest,
        on_trace: Callable[[Any], None] | None = None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse: ...
    def ask_graph(
        self,
        notebook_id: str,
        payload: AskRequest,
        seed_ids: list[str] | None = None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse: ...


class RetrievalPort(Protocol):
    def retrieve_scored(
        self,
        notebook_id: str,
        query: str,
        types: Iterable[str] | None = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
    ) -> list[RetrievedKnowledge]: ...
    def federated_retrieve(
        self,
        active_notebook_id: str,
        query: str,
        types: Iterable[str] | None = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
    ) -> list[RetrievedKnowledge]: ...
    def federated_retrieve_relations(
        self,
        active_notebook_id: str,
        query: str,
    ) -> list[RetrievedRelation]: ...
    def retrieve_neighbors(
        self,
        notebook_id: str,
        object_id: str,
        edge_type: str | None = None,
        direction: str = "both",
    ) -> list[RetrievedKnowledge]: ...
    def retrieve_elements(
        self, notebook_id: str, query: str, limit: int = 8
    ) -> list[RetrievedElement]: ...
    def ppr_retrieve(
        self, notebook_id: str, question: str
    ) -> list[RetrievedChunk]: ...
    def follow_chain(
        self,
        active_notebook_id: str,
        start_object_id: str,
        edge_type: str | None = None,
        target_object_id: str = "",
        direction: str = "out",
        max_fan_out: int = 8,
        max_results: int = 4,
    ) -> FollowChainResult: ...
    def node_context(self, notebook_id: str, object_id: str) -> dict[str, Any]: ...


class ModelClientProvider(Protocol):
    @property
    def llm_client(self) -> Any: ...
    @llm_client.setter
    def llm_client(self, value: Any) -> None: ...
    @property
    def reasoning_llm_client(self) -> Any: ...
    @property
    def rewrite_llm_client(self) -> Any: ...
    @property
    def kg_llm_client(self) -> Any: ...
    @property
    def rerank_client(self) -> Any: ...
    @rerank_client.setter
    def rerank_client(self, value: Any) -> None: ...


class ModelErrorSink(Protocol):
    def note_model_error(
        self, stage: str, model: str, error: Exception
    ) -> None: ...


class FacadePropertyContract(ModelClientProvider, Protocol):
    settings: Settings
    storage_dir: Path
    embedder: Any
    retrieval: RetrievalPort


class AskStreamPort(
    AskExecutionPort, AskStateRepository, IdentityRepository, Protocol
):
    pass


class NotebookRepository(
    IdentityRepository,
    NotebookAccessRepository,
    NotebookCatalogRepository,
    NotebookSharingRepository,
    SourceRepository,
    KnowledgeReadRepository,
    SchemaRegistryRepository,
    KnowledgeGovernanceRepository,
    KnowledgeLifecycleRepository,
    IndexLifecycleRepository,
    AskStateRepository,
    ReportRepository,
    AdminQueryRepository,
    AskExecutionPort,
    FacadePropertyContract,
    Protocol,
):
    pass


class SQLiteMaintenancePort(Protocol):
    def delete_notebook_kg(self, notebook_id: str) -> dict: ...
    def backfill_kg_fts(self, notebook_id: str) -> int: ...
    def backfill_chunk_fts(self, notebook_id: str) -> int: ...
    def build_scale_index(
        self,
        notebook_id: str,
        on_stage: Callable[[str, int], None] | None = None,
    ) -> dict: ...
    def fold_scale_index_delta(
        self, notebook_id: str, _assume_locked: bool = False
    ) -> dict: ...
~~~

`ownership_manifest.py` exposes:

~~~python
SurfaceKind = Literal[
    "method", "property", "mutable_property", "constant", "private_wrapper"
]


@dataclass(frozen=True)
class SurfaceMember:
    name: str
    owner: str
    kind: SurfaceKind
    consumers: tuple[str, ...]


SURFACE_MEMBERS: tuple[SurfaceMember, ...]
OWNER_BY_MEMBER: Mapping[str, str]
~~~

- [ ] **Step 1: Write failing aggregate, ownership and eval-path tests**

~~~python
def test_production_protocol_excludes_eval_helper():
    assert "eval_insert_source_for_test" not in NotebookRepository.__dict__


def test_every_production_member_has_exactly_one_owner():
    manifest = json.loads(SURFACE_FIXTURE.read_text())
    for name, record in manifest.items():
        if record["consumers"]:
            assert OWNER_BY_MEMBER[name]


def test_sqlite_repository_does_not_inherit_protocol():
    assert NotebookRepository not in SQLiteRepository.__mro__
~~~

- [ ] **Step 2: Run tests and confirm RED**

Run:

~~~bash
cd backend
python -m pytest \
  tests/test_repository_ports.py \
  tests/test_repository_ownership.py \
  tests/test_eval_speed_public_path.py -q
~~~

Expected: missing `app.repositories.ports`, then the old aggregate still exposes `eval_insert_source_for_test`.

- [ ] **Step 3: Implement the exact Protocol catalogue and compatibility re-export**

Create the Protocols above, import every referenced schema type, and replace `services/repository.py` with explicit re-exports. Populate `SURFACE_MEMBERS` from Task 1 and reject duplicate owners at import time:

~~~python
def _owner_map(members: tuple[SurfaceMember, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for member in members:
        if member.name in result:
            raise RuntimeError(f"duplicate repository owner: {member.name}")
        result[member.name] = member.owner
    return result


OWNER_BY_MEMBER = _owner_map(SURFACE_MEMBERS)
~~~

- [ ] **Step 4: Add zero-state typed accessors without changing FastAPI dependencies**

Add one accessor per domain to `api/deps.py`; each returns the cached `repository()`. Type `_stream_ask_events` as `AskStreamPort`, and leave its control flow unchanged.

- [ ] **Step 5: Migrate speed eval without changing its timed interval**

~~~python
def _insert_source(repo, nb_id, name, text, tmpdir):
    scheduled: list[str] = []
    created = repo.upload_sources(
        nb_id,
        [
            UploadedSourceFile(
                file_name=f"{name}.md",
                content_type="text/markdown",
                content=text.encode("utf-8"),
                doc_type="textbook",
            )
        ],
        scheduler=scheduled.append,
    )
    source_id = created[0].id
    repo.parse_source(source_id)
    return source_id
~~~

Time `repo.extract_source(source_id)`, not `process_source` or `_run_extraction`. Keep result keys and values unchanged.

- [ ] **Step 6: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_ports.py \
  tests/test_repository_ownership.py \
  tests/test_eval_speed_public_path.py \
  tests/test_trackA_eval_connect.py \
  tests/test_ask_jobs.py -q
~~~

- [ ] **Step 7: Commit**

~~~bash
git add backend/app/repositories \
  backend/app/services/repository.py \
  backend/app/api/deps.py \
  backend/app/api/routes.py \
  backend/app/eval/speed.py \
  backend/tests/test_trackA_eval_connect.py \
  backend/tests/test_repository_ports.py \
  backend/tests/test_repository_ownership.py \
  backend/tests/test_eval_speed_public_path.py
git commit -m "refactor(repository): define consumer-driven ports"
~~~

### Task 3: Centralize lazy notebook scale policy without adding hot-path queries

**Files:**
- Create: `backend/app/services/notebook_scale.py`
- Modify: `backend/app/services/sqlite_repository.py:9335-9359,9674-9728,13149-13160`
- Modify: `backend/app/services/sqlite_notebook_sharing.py:100-139`
- Create: `backend/tests/test_notebook_scale_profile.py`
- Modify: `backend/tests/test_notebook_copy_stats_memo.py`
- Modify: `backend/tests/test_large_lib_index_required.py`
- Modify: `backend/tests/test_auto_scale_index.py`
- Modify: `backend/tests/test_scale_index_repo.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class NotebookScaleFacts:
    bytes: int
    sources: int
    chunks: int
    nodes: int
    edges: int

    def as_size_dict(self) -> dict[str, int]:
        return {
            "bytes": self.bytes,
            "sources": self.sources,
            "chunks": self.chunks,
            "nodes": self.nodes,
            "edges": self.edges,
        }


class NotebookScaleFactsRepository(Protocol):
    def load_notebook_scale_facts(
        self, notebook_id: str
    ) -> NotebookScaleFacts: ...


class NotebookScaleProfile:
    def __init__(
        self,
        settings: Settings,
        facts: NotebookScaleFactsRepository,
        version_for: Callable[[str], Hashable],
        cache: VectorCache,
    ) -> None: ...

    def facts(self, notebook_id: str) -> NotebookScaleFacts: ...
    def copy_stats(self, notebook_id: str) -> dict: ...
    def is_copyable(self, notebook_id: str) -> bool: ...
    def requires_index(
        self, notebook_id: str, *, has_disk_index: bool
    ) -> bool: ...
    def index_eligible(
        self,
        notebook_id: str,
        *,
        tier: str,
        has_disk_index: bool,
        total_chunks: int,
    ) -> bool: ...
~~~

- [ ] **Step 1: Write RED tests for predicates and short-circuit order**

~~~python
def test_index_eligible_short_circuits_without_loading_copy_facts(settings):
    facts = Mock()
    profile = NotebookScaleProfile(
        settings, facts, lambda notebook_id: ("v", notebook_id), VectorCache()
    )

    assert profile.index_eligible(
        "nb", tier="base", has_disk_index=False, total_chunks=0
    )
    assert profile.index_eligible(
        "nb", tier="personal", has_disk_index=True, total_chunks=0
    )
    assert profile.index_eligible(
        "nb",
        tier="personal",
        has_disk_index=False,
        total_chunks=settings.index_suggest_chunk_threshold + 1,
    )
    facts.load_notebook_scale_facts.assert_not_called()
~~~

- [ ] **Step 2: Run tests and confirm RED**

Run:

~~~bash
cd backend
python -m pytest \
  tests/test_notebook_scale_profile.py \
  tests/test_notebook_copy_stats_memo.py \
  tests/test_large_lib_index_required.py \
  tests/test_auto_scale_index.py -q
~~~

Expected: missing `app.services.notebook_scale`.

- [ ] **Step 3: Implement the lazy policy and existing cache key**

`copy_stats()` uses the existing `:copystats` VectorCache key/version and returns exactly:

~~~python
{
    "copyable": (
        facts.bytes <= settings.notebook_copy_max_bytes
        and facts.chunks + facts.nodes <= settings.notebook_copy_max_rows
    ),
    "size": facts.as_size_dict(),
}
~~~

`index_eligible()` preserves this order:

~~~python
if tier == "base" or has_disk_index:
    return True
if total_chunks > self.settings.index_suggest_chunk_threshold:
    return True
return not self.is_copyable(notebook_id)
~~~

- [ ] **Step 4: Delegate only the existing policy decisions**

Keep the five aggregate queries temporarily in `SQLiteRepository.load_notebook_scale_facts`; Task 7 moves them to QueryStore. Delegate `notebook_copy_stats`, `_needs_index`, and the final “large notebook” branch of `_scale_index_eligible`. Preserve the auto-index once-set before any facts query.

- [ ] **Step 5: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_notebook_scale_profile.py \
  tests/test_notebook_copy_stats_memo.py \
  tests/test_large_lib_index_required.py \
  tests/test_auto_scale_index.py \
  tests/test_scale_index_repo.py -q
~~~

- [ ] **Step 6: Commit**

~~~bash
git add backend/app/services/notebook_scale.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/sqlite_notebook_sharing.py \
  backend/tests/test_notebook_scale_profile.py \
  backend/tests/test_notebook_copy_stats_memo.py \
  backend/tests/test_large_lib_index_required.py \
  backend/tests/test_auto_scale_index.py \
  backend/tests/test_scale_index_repo.py
git commit -m "refactor(repository): centralize notebook scale policy"
~~~

- [ ] **Step 7: Run Review Gate 1**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_surface_manifest.py \
  tests/test_repository_phase_contracts.py \
  tests/test_repository_v9_fixture.py \
  tests/test_ask_repository_golden.py \
  tests/test_repository_api_contract.py \
  tests/test_repository_ports.py \
  tests/test_repository_ownership.py \
  tests/test_eval_speed_public_path.py \
  tests/test_notebook_scale_profile.py \
  tests/test_notebook_share_copy.py \
  tests/test_ask_jobs.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

## Review Gate 2 — Request Context, SqliteDatabase and Startup

### Task 4: Move request context to a facade-independent owner and introduce the runtime shell

**Files:**
- Create: `backend/app/core/request_context.py`
- Create: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_identity.py:1-34`
- Modify: `backend/app/services/sqlite_repository.py:276-399`
- Modify: `backend/app/services/background_jobs.py:24-35`
- Modify: `backend/app/api/deps.py:10-15`
- Create: `backend/tests/test_repository_context.py`
- Create: `backend/tests/test_repository_runtime.py`
- Modify: `backend/tests/test_request_user_ctx.py`
- Modify: `backend/tests/test_architecture_module_boundaries.py`

**Interfaces:**

~~~python
RequestUserToken = tuple[contextvars.Token, contextvars.Token]

_REQUEST_USER: ContextVar[UserProfile | None] = ContextVar(
    "request_user", default=None
)


def get_request_user() -> UserProfile | None:
    return _REQUEST_USER.get()


def request_user_id() -> str | None:
    user = get_request_user()
    return user.id if user is not None else None


def set_request_user(user: UserProfile | None) -> RequestUserToken:
    tok_user = _REQUEST_USER.set(user)
    tok_owner = set_log_owner(user.id if user is not None else None)
    return tok_user, tok_owner


def reset_request_user(token: RequestUserToken) -> None:
    tok_user, tok_owner = token
    _REQUEST_USER.reset(tok_user)
    reset_log_owner(tok_owner)


@dataclass(frozen=True)
class RepositoryCompatibilitySeams:
    new_id: Callable[[str], str]
    now: Callable[[], str]
    copy_chunk_size: Callable[[], int]
    remap_json_ids: Callable[[Any, dict], Any]


class RepositoryRuntime:
    def __init__(
        self,
        settings: Settings,
        root_dir: Path,
        seams: RepositoryCompatibilitySeams,
    ) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.seams = seams
~~~

- [ ] **Step 1: Write RED identity, import and late-binding tests**

~~~python
def test_request_identity_exports_are_the_same_objects():
    from app.core import request_context
    from app.services import sqlite_identity, sqlite_repository

    assert sqlite_identity._REQUEST_USER is request_context._REQUEST_USER
    assert sqlite_repository._REQUEST_USER is request_context._REQUEST_USER
    assert sqlite_repository.set_request_user is request_context.set_request_user
    assert sqlite_repository.reset_request_user is request_context.reset_request_user


def test_repository_constructs_one_internal_runtime(repo):
    assert repo._runtime.settings is repo.settings


def test_runtime_clock_remains_late_bound(repo, monkeypatch):
    from app.services import sqlite_repository

    monkeypatch.setattr(sqlite_repository, "_now", lambda: "clock-sentinel")
    assert repo._runtime.seams.now() == "clock-sentinel"
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_context.py \
  tests/test_repository_runtime.py \
  tests/test_request_user_ctx.py -q
~~~

Expected: missing `app.core.request_context` and `SQLiteRepository._runtime`.

- [ ] **Step 3: Move ContextVar code byte-for-byte and remove reverse import**

Move the existing user/log-owner token pair into `core/request_context.py`; re-export it from both old modules. Change `background_jobs._resolve_job_user()` to call `request_user_id()` directly, while keeping its fail-open `except Exception: return None`.

- [ ] **Step 4: Construct the runtime with late-bound compatibility seams**

~~~python
self._runtime = RepositoryRuntime(
    settings=self.settings,
    root_dir=self.root_dir,
    seams=RepositoryCompatibilitySeams(
        new_id=lambda prefix: _new_id(prefix),
        now=lambda: _now(),
        copy_chunk_size=lambda: _COPY_CHUNK,
        remap_json_ids=lambda value, maps: _remap_json_ids(value, maps),
    ),
)
~~~

Do not call any seam while constructing the runtime. Every moved store/service body that previously called module `_now()` receives this clock callable; no component captures the function/value at construction. Keep `test_rebuild_cache.py` in the final compatibility gate so post-construction monkeypatches still affect governance/unified writes.

- [ ] **Step 5: Run context propagation tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_context.py \
  tests/test_repository_runtime.py \
  tests/test_request_user_ctx.py \
  tests/test_background_jobs.py \
  tests/test_kg_job_user_context.py \
  tests/test_user_llm_client_resolve.py -q
~~~

- [ ] **Step 6: Commit**

~~~bash
git add backend/app/core/request_context.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_identity.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/background_jobs.py \
  backend/app/api/deps.py \
  backend/tests/test_repository_context.py \
  backend/tests/test_repository_runtime.py \
  backend/tests/test_request_user_ctx.py \
  backend/tests/test_architecture_module_boundaries.py
git commit -m "refactor(repository): isolate runtime request context"
~~~

### Task 5: Extract the SQLite connection and write-lock boundary

**Files:**
- Create: `backend/app/repositories/sqlite/__init__.py`
- Create: `backend/app/repositories/sqlite/database.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:468-493`
- Create: `backend/tests/test_sqlite_database_component.py`
- Modify: `backend/tests/test_db_concurrency.py`
- Modify: `backend/tests/test_sqlite_write_optimization.py`

**Interfaces:**

~~~python
class SqliteDatabase:
    def __init__(self, settings: Settings, root_dir: Path) -> None:
        self.settings = settings
        self.root_dir = root_dir
        self.db_path = self.resolve_path(settings.sqlite_path)
        self.write_lock = threading.RLock()

    def resolve_path(self, value: str) -> Path: ...
    def connect(self) -> sqlite3.Connection: ...

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]: ...
~~~

- [ ] **Step 1: Write RED PRAGMA, rollback, identity and two-instance tests**

~~~python
def test_runtime_components_share_exact_write_lock_object(repo):
    assert repo._write_lock is repo._runtime.database.write_lock


def test_two_repository_instances_keep_independent_locks_for_same_db(settings):
    first = SQLiteRepository(settings)
    second = SQLiteRepository(settings)
    assert first._write_lock is not second._write_lock
~~~

Also assert `row_factory=sqlite3.Row`, foreign keys, WAL, busy_timeout, NORMAL synchronous, cache_size, MEMORY temp_store and mmap_size.

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_sqlite_database_component.py \
  tests/test_db_concurrency.py -q
~~~

Expected: missing `app.repositories.sqlite.database`.

- [ ] **Step 3: Move connection code without changing PRAGMAs**

Implement `connect()` from the existing `_connect` body and `write()` from `_write`. Keep compatibility wrappers:

~~~python
def _resolve_path(self, value: str) -> Path:
    return self._runtime.database.resolve_path(value)


def _connect(self) -> sqlite3.Connection:
    return self._runtime.database.connect()


def _write(self):
    return self._runtime.database.write()
~~~

Make `db_path` and `_write_lock` write-through compatibility properties pointing to the runtime objects, not copied values.

- [ ] **Step 4: Run tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_sqlite_database_component.py \
  tests/test_db_concurrency.py \
  tests/test_sqlite_write_optimization.py \
  tests/test_settings_path_anchor.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/repositories/sqlite \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_sqlite_database_component.py \
  backend/tests/test_db_concurrency.py \
  backend/tests/test_sqlite_write_optimization.py
git commit -m "refactor(repository): extract sqlite database boundary"
~~~

### Task 6: Move schema v9 migrations, recovery and seed as one unit

**Files:**
- Create: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:495-1514`
- Create: `backend/tests/test_sqlite_migrator_component.py`
- Modify: `backend/tests/test_schema_version_migration.py`
- Modify: `backend/tests/test_legacy_db_compat.py`
- Modify: `backend/tests/test_admin_users.py`
- Modify: `backend/tests/test_community_members_schema.py`
- Modify: `backend/tests/test_canonical_relations.py`
- Modify: `backend/tests/test_mention_bridge.py`
- Modify: `backend/tests/test_ask_jobs.py`

**Interfaces:**

~~~python
SCHEMA_VERSION = 9


class SqliteMigrator:
    def __init__(
        self, database: SqliteDatabase, settings: Settings
    ) -> None:
        self.database = database
        self.settings = settings

    def migrate(self) -> list[int]: ...
    def recover_interrupted_jobs(self) -> None: ...
    def seed(self) -> None: ...

    def initialize(self) -> list[int]:
        applied = self.migrate()
        self.recover_interrupted_jobs()
        self.seed()
        return applied

    @staticmethod
    def add_column_if_missing(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        coldef: str,
    ) -> None: ...
~~~

Keep internal `_migration_1` through `_migration_9` names so the version loop remains:

~~~python
for version in range(current + 1, SCHEMA_VERSION + 1):
    getattr(self, f"_migration_{version}")()
    with self.database.connect() as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    applied.append(version)
~~~

- [ ] **Step 1: Write RED startup-order and old-fixture tests**

~~~python
def test_initialize_orders_migrate_recover_seed(migrator, monkeypatch):
    calls = []
    monkeypatch.setattr(migrator, "migrate", lambda: calls.append("migrate") or [])
    monkeypatch.setattr(
        migrator, "recover_interrupted_jobs", lambda: calls.append("recover")
    )
    monkeypatch.setattr(migrator, "seed", lambda: calls.append("seed"))
    assert migrator.initialize() == []
    assert calls == ["migrate", "recover", "seed"]
~~~

Add tests that a copied frozen v9 DB has no schema rewrite, an unversioned legacy DB reaches v9, and a missing-column fixture is stamped below the migration that introduces that column.

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_sqlite_migrator_component.py \
  tests/test_schema_version_migration.py \
  tests/test_legacy_db_compat.py -q
~~~

Expected: missing `app.repositories.sqlite.migrations`.

- [ ] **Step 3: Move all DDL and startup code verbatim**

Move `_migration_1`–`_migration_9`, `_add_column_if_missing`, recovery and seed without editing SQL strings/order. Re-export `SCHEMA_VERSION` and keep facade wrappers for all existing tests/scripts. Runtime calls `migrator.initialize()` exactly once after stores needed by startup exist and before serving requests.

- [ ] **Step 4: Run schema/startup tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_sqlite_migrator_component.py \
  tests/test_schema_version_migration.py \
  tests/test_legacy_db_compat.py \
  tests/test_auth_migration.py \
  tests/test_admin_seed.py \
  tests/test_admin_users.py \
  tests/test_community_members_schema.py \
  tests/test_canonical_relations.py \
  tests/test_mention_bridge.py \
  tests/test_ask_jobs.py \
  tests/test_sqlite_indexes.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/repositories/sqlite/migrations.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_sqlite_migrator_component.py \
  backend/tests/test_schema_version_migration.py \
  backend/tests/test_legacy_db_compat.py \
  backend/tests/test_admin_users.py \
  backend/tests/test_community_members_schema.py \
  backend/tests/test_canonical_relations.py \
  backend/tests/test_mention_bridge.py \
  backend/tests/test_ask_jobs.py
git commit -m "refactor(repository): extract sqlite migrations and startup recovery"
~~~

- [ ] **Step 6: Run Review Gate 2**

~~~bash
cd backend
python -m pytest \
  tests/test_sqlite_database_component.py \
  tests/test_sqlite_migrator_component.py \
  tests/test_schema_version_migration.py \
  tests/test_legacy_db_compat.py \
  tests/test_request_user_ctx.py \
  tests/test_background_jobs.py \
  tests/test_db_concurrency.py \
  tests/test_sqlite_write_optimization.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

## Review Gate 3 — Identity, Notebook Catalog, Sharing and Read Projections

### Task 7: Compose IdentityStore and QueryStore

**Files:**
- Create: `backend/app/repositories/sqlite/identity_store.py`
- Create: `backend/app/repositories/sqlite/query_store.py`
- Create: `backend/app/core/ask_context.py`
- Create: `backend/app/services/model_provider.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/sqlite_identity.py`
- Modify: `backend/app/api/auth_routes.py`
- Modify: `backend/app/api/routes.py:140-206,1371-1407`
- Create: `backend/tests/test_identity_store_component.py`
- Create: `backend/tests/test_query_store_component.py`
- Create: `backend/tests/test_model_provider_runtime.py`
- Modify: `backend/tests/test_architecture_module_boundaries.py`
- Modify: `backend/tests/test_sources_pagination.py`

**Interfaces:**

`IdentityStore(database, settings, model_config_cache)` implements `IdentityRepository` exactly. `QueryStore` exposes:

~~~python
class QueryStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def list_user_usage(self) -> list[dict[str, Any]]: ...
    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def pending_actions_projection_rows(self, user_id: str) -> dict: ...
    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse: ...
    def load_notebook_scale_facts(
        self, notebook_id: str
    ) -> NotebookScaleFacts: ...
~~~

`RuntimeModelProvider(identity, settings, event_log, ask_context)` implements both `ModelClientProvider` and `ModelErrorSink`. It owns system/per-user chat and rerank client caches, preserves dynamic role fallback and mutable test setters, and records `{stage, model, error}` without changing the current Ask response sink shape. `core/ask_context.py` owns and re-exports the exact `_ASK_MODEL_ERRORS` and `_ASK_EMBED_CACHE` ContextVar objects.

- [ ] **Step 1: Write RED store/delegation tests**

~~~python
def test_identity_and_query_stores_share_runtime_database(repo):
    assert repo._runtime.identity.database is repo._runtime.database
    assert repo._runtime.queries.database is repo._runtime.database


def test_facade_identity_delegates_to_runtime(repo, monkeypatch):
    expected = repo.current_user()
    monkeypatch.setattr(repo._runtime.identity, "current_user", lambda: expected)
    assert repo.current_user() is expected
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_identity_store_component.py \
  tests/test_query_store_component.py \
  tests/test_model_provider_runtime.py \
  tests/test_architecture_module_boundaries.py -q
~~~

Expected: missing store modules and runtime attributes.

- [ ] **Step 3: Move persistence and preserve dynamic user/model behavior**

Move identity/session/model-settings SQL from `sqlite_identity.py`; move admin usage/user-notebooks, notebook analytics, five scale-fact aggregates and `search_notebook` to QueryStore. Move client construction/caching/role fallback/setters and `_note_model_error` to RuntimeModelProvider, and move the two Ask ContextVars without changing object identity. Preserve the exact Notebook→Domain→Source(created_at)→Element(scan order)→knowledge-object scan order, per-entity cap, final 20 cap, payload false-positive filter and empty-query short circuit. Stores/provider receive the same mutable model-config cache object. `current_user()` reads `core.request_context` at call time.

- [ ] **Step 4: Replace inheritance with explicit facade delegates**

Remove `SQLiteIdentityMixin` from SQLiteRepository MRO. Keep the old import symbol in `sqlite_identity.py` as a compatibility marker/re-export, but no production implementation inherits it. Auth routes use `identity_repository()`; admin queries use `admin_query_repository()`.

- [ ] **Step 5: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_identity_store_component.py \
  tests/test_query_store_component.py \
  tests/test_user_session_repo.py \
  tests/test_user_model_settings_store.py \
  tests/test_model_config_resolve.py \
  tests/test_model_provider_runtime.py \
  tests/test_user_llm_client_resolve.py \
  tests/test_admin_users.py \
  tests/test_admin_user_notebooks.py \
  tests/test_pending_actions.py \
  tests/test_sources_pagination.py \
  tests/test_request_user_ctx.py -q
~~~

- [ ] **Step 6: Commit**

~~~bash
git add backend/app/repositories/sqlite/identity_store.py \
  backend/app/repositories/sqlite/query_store.py \
  backend/app/core/ask_context.py \
  backend/app/services/model_provider.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/sqlite_identity.py \
  backend/app/api/auth_routes.py \
  backend/app/api/routes.py \
  backend/tests/test_identity_store_component.py \
  backend/tests/test_query_store_component.py \
  backend/tests/test_model_provider_runtime.py \
  backend/tests/test_architecture_module_boundaries.py \
  backend/tests/test_sources_pagination.py
git commit -m "refactor(repository): compose identity and query stores"
~~~

### Task 8: Extract notebook rows, summary projection and catalog orchestration

**Files:**
- Create: `backend/app/repositories/sqlite/notebook_store.py`
- Create: `backend/app/services/notebook_catalog.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:1516-1588,1775-1960,13786-13841`
- Modify: `backend/app/api/routes.py:235-298,538-546,773-790`
- Create: `backend/tests/test_notebook_store_component.py`
- Create: `backend/tests/test_notebook_summary_query.py`
- Modify: `backend/tests/test_notebook_counts_batched.py`
- Modify: `backend/tests/test_notebook_meta.py`
- Modify: `backend/tests/test_notebook_owner_scope.py`

**Interfaces:**

~~~python
class NotebookStore:
    def create_row(
        self, payload: NotebookCreate, created_by: str
    ) -> str: ...
    def get_row(
        self, notebook_id: str, *, include_copying: bool = False
    ) -> sqlite3.Row: ...
    def update_row(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> None: ...
    def set_tier(
        self, notebook_id: str, tier: Literal["base", "personal"]
    ) -> None: ...
    def delete_row_and_orphan_embeddings(
        self, notebook_id: str
    ) -> list[str]: ...


class NotebookSummaryQuery:
    def get(
        self, notebook_id: str, *, kg_building: bool = False
    ) -> NotebookSummary: ...
    def list_for_user(self, user_id: str) -> list[NotebookSummary]: ...
    def from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> NotebookSummary: ...


class NotebookCatalogService:
    def list_notebook_templates(self) -> list[NotebookTemplate]: ...
    def list_notebooks(self) -> list[NotebookSummary]: ...
    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...
    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...
    def update_notebook(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> NotebookSummary: ...
    def delete_notebook(self, notebook_id: str) -> None: ...
    def mark_notebook_base(self, notebook_id: str) -> None: ...
    def set_notebook_personal(self, notebook_id: str) -> None: ...
    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics: ...
    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse: ...
~~~

- [x] **Step 1: Write RED row, summary and query-count tests**

~~~python
def test_summary_query_keeps_list_kg_building_false(repo):
    notebook = repo.create_notebook(NotebookCreate(name="summary"))
    repo._kg_building.add(notebook.id)
    listed = {item.id: item for item in repo.list_notebooks()}
    assert listed[notebook.id].kg_building is False
    assert repo.get_notebook(notebook.id).kg_building is True
~~~

- [x] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_notebook_store_component.py \
  tests/test_notebook_summary_query.py \
  tests/test_notebook_counts_batched.py -q
~~~

Expected: missing notebook store/catalog modules.

- [x] **Step 3: Move row SQL and cross-table projection separately**

Move CRUD/tier row SQL to NotebookStore. Move counts/base-KG/pending-source aggregation and `_notebook_from_row` to NotebookSummaryQuery. Inject QueryStore into NotebookCatalogService and make `search_notebook` a one-line application delegate; keep the cross-table SQL and ordering in the Task-7 query adapter. Keep DB deletion committed before file deletion and preserve orphan knowledge-embedding cleanup.

- [x] **Step 4: Wire the catalog and typed routes**

Construct one NotebookCatalogService in runtime; facade methods delegate explicitly. Notebook routes use `notebook_catalog_repository()`. Keep endpoint paths, error types and response models unchanged.

- [x] **Step 5: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_notebook_store_component.py \
  tests/test_notebook_summary_query.py \
  tests/test_notebook_counts_batched.py \
  tests/test_notebook_meta.py \
  tests/test_notebook_owner_scope.py \
  tests/test_two_tier_federated.py -q
~~~

- [x] **Step 6: Commit**

~~~bash
git add backend/app/repositories/sqlite/notebook_store.py \
  backend/app/services/notebook_catalog.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/api/routes.py \
  backend/tests/test_notebook_store_component.py \
  backend/tests/test_notebook_summary_query.py \
  backend/tests/test_notebook_counts_batched.py \
  backend/tests/test_notebook_meta.py \
  backend/tests/test_notebook_owner_scope.py
git commit -m "refactor(repository): extract notebook catalog and summaries"
~~~

### Task 9: Extract access, sharing and deep-copy orchestration

**Files:**
- Create: `backend/app/repositories/sqlite/sharing_store.py`
- Create: `backend/app/services/notebook_sharing.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/sqlite_notebook_sharing.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/routes.py:245-248,365-400,702-845,1049-1054`
- Create: `backend/tests/test_sharing_store_component.py`
- Create: `backend/tests/test_notebook_copy_service.py`
- Modify: `backend/tests/test_architecture_module_boundaries.py`
- Modify: `backend/tests/test_notebook_share_copy.py`
- Modify: `backend/tests/test_notebook_share_readonly.py`
- Modify: `backend/tests/test_notebook_copy_stats_memo.py`

**Interfaces:**

~~~python
class SharingStore:
    def set_share_token(self, notebook_id: str, token: str) -> None: ...
    def clear_share(self, notebook_id: str) -> None: ...
    def find_by_token(self, token: str) -> str | None: ...
    def list_shared_by_owner(self, user_id: str) -> list[sqlite3.Row]: ...
    def user_can_access_notebook(
        self, notebook_id: str, user_id: str
    ) -> bool: ...
    def is_member(self, notebook_id: str, user_id: str) -> bool: ...
    def add_member(self, notebook_id: str, user_id: str) -> None: ...
    def remove_member(self, notebook_id: str, user_id: str) -> None: ...
    def kick_all_members(self, notebook_id: str) -> None: ...
    def list_members(self, notebook_id: str) -> list: ...
    def source_owner(self, source_id: str) -> str | None: ...
    def conversation_owner(self, conversation_id: str) -> str | None: ...
    def answer_owner(self, answer_id: str) -> str | None: ...
    def snapshot_copy_rows(
        self, notebook_id: str
    ) -> dict[str, list[dict]]: ...
    def insert_copy_rows(
        self,
        table: str,
        rows: Sequence[dict],
        *,
        chunk_size: int,
    ) -> None: ...
    def compensate_copy(self, notebook_id: str) -> None: ...
    def sweep_stale_copies(
        self, *, created_by: str | None = None
    ) -> int: ...


class NotebookCopyService:
    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: str | None = None,
    ) -> NotebookSummary: ...
~~~

- [x] **Step 1: Write RED transaction, compensation and late-binding tests**

Inject failures after destination sentinel creation and after the first table chunk. Assert only the destination/files are compensated. Patch `sqlite_repository._COPY_CHUNK` and `_new_id` after repository construction and assert the copy operation observes the patched values.

- [x] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_sharing_store_component.py \
  tests/test_notebook_copy_service.py \
  tests/test_notebook_share_copy.py \
  tests/test_notebook_share_readonly.py -q
~~~

Expected: missing sharing store/service.

- [x] **Step 3: Move share/member SQL and keep copy orchestration in a service**

SharingStore owns rows and connection-taking copy primitives. NotebookCopyService owns ID remap, chunked transactions, filesystem copy and compensation. It reads `RepositoryCompatibilitySeams.new_id()` and `copy_chunk_size()` during every operation.

- [x] **Step 4: Replace sharing mixin inheritance and route types**

Remove `SQLiteNotebookSharingMixin` from the facade MRO; preserve the old class/helper imports as compatibility exports. Access guards use `notebook_access_repository()`; sharing routes use `notebook_sharing_repository()`.

- [x] **Step 5: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_sharing_store_component.py \
  tests/test_notebook_copy_service.py \
  tests/test_notebook_share_copy.py \
  tests/test_notebook_share_readonly.py \
  tests/test_notebook_copy_stats_memo.py \
  tests/test_architecture_module_boundaries.py -q
~~~

- [x] **Step 6: Commit**

~~~bash
git add backend/app/repositories/sqlite/sharing_store.py \
  backend/app/services/notebook_sharing.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/sqlite_notebook_sharing.py \
  backend/app/api/deps.py \
  backend/app/api/routes.py \
  backend/tests/test_sharing_store_component.py \
  backend/tests/test_notebook_copy_service.py \
  backend/tests/test_architecture_module_boundaries.py \
  backend/tests/test_notebook_share_copy.py \
  backend/tests/test_notebook_share_readonly.py \
  backend/tests/test_notebook_copy_stats_memo.py
git commit -m "refactor(repository): compose sharing and notebook copy services"
~~~

- [x] **Step 7: Run Review Gate 3**

~~~bash
cd backend
python -m pytest \
  tests/test_auth.py \
  tests/test_auth_deps.py \
  tests/test_request_user_ctx.py \
  tests/test_user_session_repo.py \
  tests/test_user_model_settings_store.py \
  tests/test_admin_users.py \
  tests/test_admin_user_notebooks.py \
  tests/test_notebook_meta.py \
  tests/test_notebook_counts_batched.py \
  tests/test_notebook_owner_scope.py \
  tests/test_notebook_share_copy.py \
  tests/test_notebook_share_readonly.py \
  tests/test_pending_actions.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

Expected: all pass; fixture files remain unchanged.

## Review Gate 4 — Source, Chunk/Embedding Persistence and Ingestion

### Task 10: Extract source, chunk and embedding stores

**Files:**
- Create: `backend/app/repositories/sqlite/source_store.py`
- Create: `backend/app/repositories/sqlite/embedding_store.py`
- Create: `backend/app/repositories/sqlite/chunk_store.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:2188-2367,2871-2902,3050-3419,13843-13961`
- Create: `backend/tests/test_source_store_component.py`
- Create: `backend/tests/test_embedding_store_component.py`
- Create: `backend/tests/test_chunk_store_component.py`
- Modify: `backend/tests/test_sources_page_batched.py`
- Modify: `backend/tests/test_sources_pagination.py`
- Modify: `backend/tests/test_chunk_embed.py`
- Modify: `backend/tests/test_relation_embed.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class SourceElementWrite:
    id: str
    element_type: str
    location_label: str
    text: str
    metadata: Mapping[str, Any]


class SourceStore:
    def list_sources(self, notebook_id: str) -> list[SourceSummary]: ...
    def list_sources_page(
        self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = ""
    ) -> PaginatedSources: ...
    def get_source(self, source_id: str) -> SourceDetail: ...
    def source_elements(self, source_id: str) -> list[SourceElement]: ...
    def insert_source(
        self,
        *,
        source_id: str,
        notebook_id: str,
        title: str,
        source_type: str,
        status: str,
        parse_status: str,
        file_name: str,
        file_path: str,
        file_size: int,
        file_hash: str,
        summary: str,
        doc_type: str,
        source_url: str = "",
    ) -> None: ...
    def set_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: str | None = None,
        error_message: str = "",
    ) -> None: ...
    def replace_elements(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None: ...
    def delete_source_row(
        self, connection: sqlite3.Connection, source_id: str
    ) -> None: ...


class EmbeddingStore:
    def replace_element_vectors(
        self,
        source_id: str,
        notebook_id: str,
        rows: Sequence[tuple[str, Sequence[float]]],
        *,
        created_at: str,
    ) -> None: ...
    def replace_knowledge_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple[str, Sequence[float]]],
        *,
        created_at: str,
    ) -> None: ...
    def replace_relation_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple[str, Sequence[float]]],
        *,
        created_at: str,
    ) -> None: ...
    def replace_chunk_vectors(
        self,
        notebook_id: str,
        rows: Sequence[tuple[str, Sequence[float]]],
        *,
        created_at: str,
    ) -> None: ...


@dataclass(frozen=True)
class ChunkWrite:
    id: str
    text: str
    section_path: str
    element_ids: tuple[str, ...]


class ChunkStore:
    def source_elements_for_chunking(self, source_id: str) -> list[dict]: ...
    def replace_source_chunks(
        self,
        source_id: str,
        notebook_id: str,
        chunks: Sequence[ChunkWrite],
        *,
        created_at: str,
    ) -> None: ...
    def source_chunks(self, source_id: str) -> list[dict]: ...
~~~

- [ ] **Step 1: Write RED store and transaction tests**

~~~python
def test_replace_source_chunks_rolls_back_chunks_and_fts_together(
    chunk_store, source_id, monkeypatch
):
    before = chunk_store.source_chunks(source_id)
    monkeypatch.setattr(
        chunk_store,
        "_insert_fts_rows",
        lambda connection, rows: (_ for _ in ()).throw(RuntimeError("fts")),
    )
    with pytest.raises(RuntimeError, match="fts"):
        chunk_store.replace_source_chunks(
            source_id, "nb", [ChunkWrite("c-new", "x", "", ())], created_at="t"
        )
    assert chunk_store.source_chunks(source_id) == before
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_source_store_component.py \
  tests/test_embedding_store_component.py \
  tests/test_chunk_store_component.py -q
~~~

Expected: missing store modules.

- [ ] **Step 3: Move persistence and preserve mixed vector decoding**

Move source/source-element hydration, source warnings, chunks/FTS and all four embedding tables. Every write continues through the shared database. All reads/writes reuse `encode_vector`/`decode_vector`; tests seed JSON TEXT, bytes and memoryview BLOB rows.

- [ ] **Step 4: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_source_store_component.py \
  tests/test_embedding_store_component.py \
  tests/test_chunk_store_component.py \
  tests/test_sources_page_batched.py \
  tests/test_sources_pagination.py \
  tests/test_chunk_embed.py \
  tests/test_relation_embed.py \
  tests/test_runtime_dim_truncation.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/repositories/sqlite/source_store.py \
  backend/app/repositories/sqlite/embedding_store.py \
  backend/app/repositories/sqlite/chunk_store.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_source_store_component.py \
  backend/tests/test_embedding_store_component.py \
  backend/tests/test_chunk_store_component.py \
  backend/tests/test_sources_page_batched.py \
  backend/tests/test_sources_pagination.py \
  backend/tests/test_chunk_embed.py \
  backend/tests/test_relation_embed.py
git commit -m "refactor(repository): extract source and vector persistence"
~~~

### Task 11: Extract source files, chunking and embedding services

**Files:**
- Create: `backend/app/repositories/source_files.py`
- Create: `backend/app/services/source_embedding.py`
- Create: `backend/app/services/source_chunking.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Create: `backend/tests/test_source_file_store.py`
- Create: `backend/tests/test_source_embedding_service.py`
- Create: `backend/tests/test_source_chunking_service.py`
- Modify: `backend/tests/test_embed_concurrency.py`
- Modify: `backend/tests/test_kg_object_embed_concurrency.py`

**Interfaces:**

~~~python
class SourceFileStore:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir

    def write_upload(
        self,
        notebook_id: str,
        source_id: str,
        file_name: str,
        content: bytes,
    ) -> Path: ...
    def delete(self, file_path: str) -> None: ...
    def read_source_text(
        self,
        file_path: str,
        fallback_elements: Sequence[SourceElement],
    ) -> str: ...


class SourceEmbeddingService:
    def embed_source(self, source_id: str) -> None: ...
    def embed_objects_batch(
        self, notebook_id: str, items: list[dict]
    ) -> None: ...
    def embed_relations_batch(
        self, notebook_id: str, items: list[dict]
    ) -> None: ...
    def embed_chunks_for_source(self, source_id: str) -> None: ...
    def embed_chunks_batch(
        self, notebook_id: str, items: list[dict]
    ) -> None: ...


class SourceChunkingService:
    def build_chunks_for_source(self, source_id: str) -> None: ...
    def chunk_and_embed_source(self, source_id: str) -> None: ...
~~~

- [ ] **Step 1: Write RED service tests**

Test safe file naming/path ownership, missing-file delete no-op, embedding batch isolation, HTTP-client warm-up, runtime dimension truncation and that chunk replacement remains atomic.

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_source_file_store.py \
  tests/test_source_embedding_service.py \
  tests/test_source_chunking_service.py \
  tests/test_embed_concurrency.py -q
~~~

Expected: missing file/embedding/chunking modules.

- [ ] **Step 3: Move methods and inject stores/clients**

Move `_source_raw_text`, `_delete_file`, `_embed_source`, object/relation/chunk batch embedding and chunk construction. Preserve batch sizes, thread names, fail-open item isolation, logging, vector encoding and concurrent extraction behavior.

- [ ] **Step 4: Run tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_source_file_store.py \
  tests/test_source_embedding_service.py \
  tests/test_source_chunking_service.py \
  tests/test_embed_concurrency.py \
  tests/test_kg_object_embed_concurrency.py \
  tests/test_chunk_embed.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/repositories/source_files.py \
  backend/app/services/source_embedding.py \
  backend/app/services/source_chunking.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_source_file_store.py \
  backend/tests/test_source_embedding_service.py \
  backend/tests/test_source_chunking_service.py \
  backend/tests/test_embed_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py
git commit -m "refactor(repository): extract source file and embedding services"
~~~

### Task 12: Extract ingestion orchestration behind fresh compatibility hooks

**Files:**
- Create: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:2233-3037`
- Modify: `backend/app/api/routes.py:291-400`
- Create: `backend/tests/test_source_ingestion_service.py`
- Create: `backend/tests/test_source_ingestion_failure_boundaries.py`
- Modify: `backend/tests/test_pipeline_concurrency.py`
- Modify: `backend/tests/test_parallel_extraction_wiring.py`
- Modify: `backend/tests/test_kg_source_status.py`
- Modify: `backend/tests/test_event_logging.py`
- Modify: `backend/tests/test_url_sources.py`
- Modify: `backend/tests/test_url_sources_api.py`
- Modify: `backend/tests/test_batch_ingest.py`
- Modify: `backend/tests/test_chunk_embed.py`
- Modify: `backend/tests/test_kg_llm_client.py`
- Modify: `backend/tests/test_kg_relink_repository.py`
- Modify: `backend/tests/test_kg_repository.py`
- Modify: `backend/tests/test_p4_kg_shrink.py`
- Modify: `backend/tests/test_resolve_notebook_conflicts.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class SourcePipelineHooks:
    should_extract_kg: Callable[[str], bool]
    extract_source: Callable[[str], None]
    mark_unified_dirty: Callable[[str], None]
    augment_notebook_metadata: Callable[[str, str], None]
    maybe_enqueue_scale_fold: Callable[[str], None]


class SourceIngestionService:
    def import_sources(
        self,
        notebook_id: str,
        payload: SourceImportRequest,
        hooks: SourcePipelineHooks,
    ) -> list[SourceSummary]: ...
    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: SourceScheduler | None,
        hooks: SourcePipelineHooks,
    ) -> AddUrlSourcesResult: ...
    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: SourceScheduler | None,
        hooks: SourcePipelineHooks,
    ) -> list[SourceSummary]: ...
    def process_source(
        self, source_id: str, hooks: SourcePipelineHooks
    ) -> SourceSummary: ...
    def parse_source(
        self, source_id: str, hooks: SourcePipelineHooks
    ) -> SourceSummary: ...
    def delete_source(
        self, source_id: str, hooks: SourcePipelineHooks
    ) -> None: ...
    def should_extract_kg(self, notebook_id: str) -> bool: ...
    def augment_notebook_metadata(
        self, notebook_id: str, pending_source_id: str = ""
    ) -> None: ...
    def parse_url_via_local(
        self, source_id: str, url: str, file_name: str
    ) -> list[SourceElement]: ...
    def run_extraction(self, source_id: str) -> None: ...
~~~

- [ ] **Step 1: Write RED ordering and failure-injection tests**

Add these exact test cases:

~~~text
upload with scheduler commits queued source before callback and does not process inline
upload without scheduler processes inline
parsed source/elements commit before best-effort chunks
background embedding overlaps extraction while extracted waits only for extraction
chunk failure does not abort extraction
embedding failure does not fail pipeline
extraction failure sets failed and persists error_message
delete commits DB cleanup before file delete
fresh hooks preserve post-construction _run_extraction monkeypatch
metadata augmentation/model failure and URL-local parse fallback match master
extra-relation relink ordering and stale-source cleanup match master
pipeline status/event order equals transaction_phases.json
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_source_ingestion_service.py \
  tests/test_source_ingestion_failure_boundaries.py \
  tests/test_pipeline_concurrency.py \
  tests/test_parallel_extraction_wiring.py -q
~~~

Expected: missing `SourceIngestionService`.

- [ ] **Step 3: Move upload/parse/process/delete orchestration**

Move `_should_extract_kg`, `_augment_notebook_meta`, `_parse_url_via_local`, `_run_extraction` and its `_relink_extra_relations` helper with the orchestration rather than leaving business bodies in the facade. The service receives the existing NotebookStore/SourceStore plus fresh temporary KG callbacks; Task 15 replaces those callbacks with KnowledgeLifecycleService/KnowledgeStore dependencies. Migrate all Task-1 test consumers of `_run_extraction`, `_set_source_status`, `_source_raw_text` and `_mark_unified_kg_dirty` to the canonical ingestion/store/mutation components; facade wrappers remain callable but are no longer test patch targets. The facade builds hooks on every call:

~~~python
def _source_pipeline_hooks(self) -> SourcePipelineHooks:
    return SourcePipelineHooks(
        should_extract_kg=self._should_extract_kg,
        extract_source=self._run_extraction,
        mark_unified_dirty=self._mark_unified_kg_dirty,
        augment_notebook_metadata=lambda notebook_id, source_id: (
            self._augment_notebook_meta(
                notebook_id, pending_source_id=source_id
            )
        ),
        maybe_enqueue_scale_fold=self._maybe_enqueue_scale_fold,
    )
~~~

Gate 5 replaces these temporary callbacks with real services. Do not store the callbacks in runtime.

- [ ] **Step 4: Type source routes and run GREEN tests**

~~~bash
cd backend
python -m pytest \
  tests/test_source_ingestion_service.py \
  tests/test_source_ingestion_failure_boundaries.py \
  tests/test_pipeline_concurrency.py \
  tests/test_parallel_extraction_wiring.py \
  tests/test_kg_source_status.py \
  tests/test_event_logging.py \
  tests/test_url_sources.py \
  tests/test_url_sources_api.py \
  tests/test_batch_ingest.py \
  tests/test_chunk_embed.py \
  tests/test_kg_llm_client.py \
  tests/test_kg_relink_repository.py \
  tests/test_kg_repository.py \
  tests/test_p4_kg_shrink.py \
  tests/test_resolve_notebook_conflicts.py \
  tests/test_source_reverse_index.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/source_ingestion.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/api/routes.py \
  backend/tests/test_source_ingestion_service.py \
  backend/tests/test_source_ingestion_failure_boundaries.py \
  backend/tests/test_pipeline_concurrency.py \
  backend/tests/test_parallel_extraction_wiring.py \
  backend/tests/test_kg_source_status.py \
  backend/tests/test_event_logging.py \
  backend/tests/test_url_sources.py \
  backend/tests/test_url_sources_api.py \
  backend/tests/test_batch_ingest.py \
  backend/tests/test_chunk_embed.py \
  backend/tests/test_kg_llm_client.py \
  backend/tests/test_kg_relink_repository.py \
  backend/tests/test_kg_repository.py \
  backend/tests/test_p4_kg_shrink.py \
  backend/tests/test_resolve_notebook_conflicts.py
git commit -m "refactor(repository): compose source ingestion pipeline"
~~~

- [ ] **Step 6: Run Review Gate 4**

~~~bash
cd backend
python -m pytest \
  tests/test_sources_pagination.py \
  tests/test_sources_page_batched.py \
  tests/test_source_url_persist.py \
  tests/test_url_sources.py \
  tests/test_url_sources_api.py \
  tests/test_pipeline_concurrency.py \
  tests/test_parallel_extraction_wiring.py \
  tests/test_embed_concurrency.py \
  tests/test_kg_object_embed_concurrency.py \
  tests/test_chunk_embed.py \
  tests/test_source_reverse_index.py \
  tests/test_knowledge_governance_boundaries.py \
  tests/test_batch_ingest.py \
  tests/test_event_logging.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

Gate 4 fails review if a new application service imports `SQLiteRepository`, contains SQL, calls `_connect/_write`, captures late-bound compatibility constants, changes scheduler timing or changes the frozen transaction/error phase fixtures.

## Review Gate 5 — Knowledge, Governance, Unified KG and Mutation Phases

### Task 13: Extract knowledge, governance and unified-KG persistence stores

**Files:**
- Create: `backend/app/services/knowledge_contracts.py`
- Create: `backend/app/services/schema_registry.py`
- Create: `backend/app/repositories/sqlite/knowledge_store.py`
- Create: `backend/app/repositories/sqlite/governance_store.py`
- Create: `backend/app/repositories/sqlite/unified_kg_store.py`
- Modify: `backend/app/repositories/sqlite/source_store.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:1591-2156,3421-4053,4058-5451,5602-7107`
- Modify: `backend/app/services/kg/search.py`
- Modify: `backend/app/services/communities.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Create: `backend/tests/test_knowledge_store_contract.py`
- Create: `backend/tests/test_schema_registry_service.py`
- Create: `backend/tests/test_repository_module_boundaries.py`

**Interfaces:**

~~~python
USABLE_STATUSES = (
    "approved", "reviewed", "project_specific", "conflict"
)
KNOWLEDGE_STATUSES = (
    "approved", "reviewed", "deprecated", "conflict", "project_specific"
)


class KnowledgeGraphTooLargeError(Exception):
    pass


@dataclass(frozen=True)
class PromotionApproval:
    candidate_id: str
    source_notebook_id: str
    source_object_id: str
    base_notebook_id: str
    base_object_id: str
    created_new_object: bool
~~~

`KnowledgeStore(database, seams)` owns knowledge type/list/graph/schema/provenance/FTS SQL, including the `add_relations` and `relations_for_notebook` compatibility primitives, and these connection-taking writes:

~~~python
def insert_object_chunk(
    connection: sqlite3.Connection, rows: Sequence[tuple]
) -> None: ...
def insert_relation_chunk(
    connection: sqlite3.Connection, rows: Sequence[tuple]
) -> None: ...
def insert_kg_fts_rows(
    connection: sqlite3.Connection, rows: Sequence[tuple]
) -> None: ...
def replace_object_sources(
    connection: sqlite3.Connection,
    object_id: str,
    notebook_id: str,
    evidence_json: str,
) -> None: ...
def get_object_row(
    notebook_id: str, object_id: str
) -> sqlite3.Row | None: ...
~~~

`GovernanceStore(database, seams)` owns review, cluster, merge, conflict, promotion, whitelist and knowledge mutation primitives, including `write_merge_candidate`, `set_merge_decision`, `write_conflict_candidate`, `set_conflict_status`, `get_conflict_candidate`, `apply_conflict_resolution`, `decided_pairs`, `decided_seed_pairs` and `concept_whitelist_terms`:

~~~python
def update_edge_review(
    connection, notebook_id, relation_id, status
) -> None: ...
def delete_clusters(
    connection, notebook_id, object_type
) -> None: ...
def insert_clusters(
    connection, notebook_id, object_type, rows, now
) -> int: ...
def approve_promotion_in_transaction(
    connection, candidate_id, now
) -> PromotionApproval: ...
def update_object_in_transaction(
    connection, notebook_id, object_id, payload, now
) -> sqlite3.Row: ...
def merge_objects_in_transaction(
    connection, notebook_id, source_id, into_id, now
) -> sqlite3.Row: ...
~~~

`UnifiedKgStore(database)` owns unified state, `cluster_map`, cluster input facts, scratch streaming, canonical relations, mention bridge, communities and final rebuild-state upserts. Its rebuild-state update must not alter `kg_mutation_seq`.

`SchemaRegistryService(notebooks, knowledge_store, source_store, model_clients, settings)` owns `effective_schemas`, schema CRUD and LLM-backed `propose_schemas`. Store methods own rows; the service owns notebook validation, bounded content sampling, prompt/validation, duplicate suppression and fail-open behavior.

- [ ] **Step 1: Write RED runtime identity and compatibility-export tests**

~~~python
def test_runtime_owns_three_knowledge_stores(repo):
    runtime = repo._runtime
    assert runtime.knowledge.database is runtime.database
    assert runtime.governance.database is runtime.database
    assert runtime.unified_kg.database is runtime.database


def test_knowledge_contract_exports_are_same_objects():
    from app.services import knowledge_contracts, sqlite_repository
    assert sqlite_repository.USABLE_STATUSES is knowledge_contracts.USABLE_STATUSES
    assert (
        sqlite_repository.KnowledgeGraphTooLargeError
        is knowledge_contracts.KnowledgeGraphTooLargeError
    )
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_knowledge_store_contract.py \
  tests/test_schema_registry_service.py -q
~~~

Expected: missing contracts/stores or runtime attributes.

- [ ] **Step 3: Move SQL first and extract schema orchestration**

Construct all three stores with the same runtime database. Move SQL and row hydration without changing ordering/JSON encoding. Construct SchemaRegistryService and move schema sampling/LLM orchestration out of the facade; characterize configured success, unconfigured no-op, malformed/model-error fail-open, existing-type suppression and exact returned ordering. `kg/search.py` keeps only pure hit merging; `communities.py` consumes UnifiedKgStore instead of `_connect`. Facade methods forward to stores only where no multi-step orchestration is involved.

- [ ] **Step 4: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_knowledge_store_contract.py \
  tests/test_schema_registry_service.py \
  tests/test_repository_module_boundaries.py \
  tests/test_kg_repository.py \
  tests/test_knowledge_pagination.py \
  tests/test_source_reverse_index.py \
  tests/test_sqlite_indexes.py \
  tests/test_kg_search.py \
  tests/test_kg_search_api.py \
  tests/test_rebuild_cache.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/knowledge_contracts.py \
  backend/app/services/schema_registry.py \
  backend/app/repositories/sqlite/knowledge_store.py \
  backend/app/repositories/sqlite/governance_store.py \
  backend/app/repositories/sqlite/unified_kg_store.py \
  backend/app/repositories/sqlite/source_store.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/kg/search.py \
  backend/app/services/communities.py \
  backend/app/repositories/ownership_manifest.py \
  backend/tests/test_knowledge_store_contract.py \
  backend/tests/test_schema_registry_service.py \
  backend/tests/test_repository_module_boundaries.py
git commit -m "refactor(repository): extract sqlite knowledge stores"
~~~

### Task 14: Add KgMutationCoordinator and enforce the frozen phase matrix

**Files:**
- Create: `backend/app/services/kg_mutation.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/repository_runtime.py`
- Test: `backend/tests/fixtures/repository_contract/mutation_phases.json`
- Create: `backend/tests/test_kg_mutation_phase_matrix.py`
- Create: `backend/tests/test_kg_mutation_failure_boundaries.py`

**Interfaces:**

~~~python
class KgMutationCoordinator:
    def __init__(
        self,
        unified_store: UnifiedKgStore,
        unified_cache: MutableMapping[tuple, object],
        vector_cache: VectorCache,
        auto_index_checked: set[str],
        notebook_languages: MutableMapping[str, list[str]],
    ) -> None: ...

    def invalidate_unified_cache(self, notebook_id: str) -> None: ...
    def mark_unified_kg_dirty(self, notebook_id: str) -> None: ...
    def bump_cluster_mutation_seq(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
    ) -> None: ...
~~~

The exact order matrix is:

| Operation | Required order |
|---|---|
| store_kg | object chunks of 1000; relation chunks of 1000; object embed; relation embed; invalidate; dirty |
| relink_notebook_kg | relation transaction; invalidate; dirty; no side effects when zero added |
| set_edge_review | relation transaction; dirty; invalidate |
| write_clusters | replace + cluster-sequence bump in one transaction; invalidate |
| append_clusters | append + cluster-sequence bump in one transaction; invalidate only when rows added |
| confirm/reject merge | candidate transaction; invalidate; dirty |
| review_pending_merges | decision transaction; dirty then invalidate only when decisions exist |
| approve_promotion | candidate/base/provenance transaction; best-effort embed; invalidate; dirty |
| update_knowledge | object transaction; best-effort embed; invalidate; dirty |
| merge_knowledge | evidence/provenance/source-status transaction; dirty; invalidate |
| edge conflict discard | edge transaction; dirty; invalidate; second dirty bump |
| node conflict discard/modify | object transaction; invalidate; dirty; second dirty bump |
| confirm_conflict | apply mutation, then candidate-status transaction; status failure leaves mutation committed |
| unified rebuild | cluster rewrite + cluster seq; no kg_mutation_seq bump |
| deep copy/migration/fixture | never call coordinator |

- [ ] **Step 1: Write RED event-order and failure-injection tests**

~~~python
def test_store_kg_preserves_post_commit_order(repo, monkeypatch):
    runtime = repo._runtime
    events = []
    monkeypatch.setattr(
        runtime.embeddings,
        "embed_objects_batch",
        lambda notebook_id, rows: events.append("embed_objects"),
    )
    monkeypatch.setattr(
        runtime.embeddings,
        "embed_relations_batch",
        lambda notebook_id, rows: events.append("embed_relations"),
    )
    monkeypatch.setattr(
        runtime.kg_mutations,
        "invalidate_unified_cache",
        lambda notebook_id: events.append("invalidate"),
    )
    monkeypatch.setattr(
        runtime.kg_mutations,
        "mark_unified_kg_dirty",
        lambda notebook_id: events.append("dirty"),
    )
    repo.store_kg("nb", None, [], [])
    assert events == [
        "embed_objects", "embed_relations", "invalidate", "dirty"
    ]
~~~

Add failure tests for second object chunk (first 1000 remain, no later phases), cluster insert rollback, promotion rollback, conflict-status failure after mutation commit, and embedding failure that still invalidates/marks dirty.

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_kg_mutation_phase_matrix.py \
  tests/test_kg_mutation_failure_boundaries.py -q
~~~

Expected: runtime is missing `kg_mutations` and the facade has not routed mutation side effects through it.

- [ ] **Step 3: Implement coordinator over existing cache objects**

Runtime passes the existing dictionaries/sets/VectorCache by identity. Do not allocate replacement caches. Route the current facade orchestration through the coordinator before Task 15 moves those methods into `KnowledgeLifecycleService`; document deep-copy/migration/fixture and cluster-rebuild exemptions in executable phase tests. Treat the Task-1 `mutation_phases.json` as read-only input.

- [ ] **Step 4: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_kg_mutation_phase_matrix.py \
  tests/test_kg_mutation_failure_boundaries.py \
  tests/test_scale_index_version_probe.py \
  tests/test_scale_version_probe.py \
  tests/test_vector_cache_invalidation.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/kg_mutation.py \
  backend/app/repositories/ports.py \
  backend/app/services/repository_runtime.py \
  backend/tests/test_kg_mutation_phase_matrix.py \
  backend/tests/test_kg_mutation_failure_boundaries.py
git commit -m "refactor(repository): centralize kg mutation side effects"
~~~

### Task 15: Extract knowledge lifecycle and unified-KG orchestration

**Files:**
- Create: `backend/app/services/knowledge_lifecycle.py`
- Create: `backend/app/services/knowledge_governance.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:1942-1964,2458-2587,3811-4053,4307-4689,4999-5276,5453-7107`
- Create: `backend/tests/test_knowledge_lifecycle_delegation.py`
- Modify: `backend/tests/test_unified_kg_repository.py`
- Modify: `backend/tests/test_rebuild_streaming.py`
- Modify: `backend/tests/test_canonical_relations.py`
- Modify: `backend/tests/test_mention_bridge.py`
- Modify: `backend/tests/test_rebuild_communities.py`
- Modify: `backend/tests/test_kg_building_flag.py`
- Modify: `backend/tests/test_kg_rebuild_relink_api.py`
- Modify: `backend/tests/test_community_reports.py`
- Modify: `backend/tests/test_rebuild_wires_communities.py`
- Modify: `backend/tests/test_resolve_notebook_conflicts.py`
- Modify: `backend/tests/test_rebuild_cache.py`

**Interfaces:**

~~~python
class KnowledgeLifecycleService:
    def delete_notebook_kg(self, notebook_id: str) -> dict: ...
    def build_notebook_kg(
        self,
        notebook_id: str,
        *,
        progress: Callable[[int, int, str, bool], None] | None = None,
    ) -> dict: ...
    def rebuild_notebook_kg(self, notebook_id: str) -> dict: ...
    def store_kg(
        self,
        notebook_id: str,
        source_id: str | None,
        objects: list[dict],
        relations: list[dict],
    ) -> tuple[int, int]: ...
    def relink_notebook_kg(self, notebook_id: str) -> dict: ...
    def incremental_fuse_source(
        self, notebook_id: str, source_id: str
    ) -> None: ...
    def write_clusters(
        self,
        notebook_id: str,
        rows: list[dict],
        object_type: str = "concept",
    ) -> None: ...
    def append_clusters(
        self,
        notebook_id: str,
        rows: list[dict],
        object_type: str = "concept",
    ) -> int: ...
    def unified_kg_status(self, notebook_id: str) -> dict: ...
    def unified_graph(
        self,
        notebook_id: str,
        level: str = "concept",
        limit: int | None = None,
    ) -> dict: ...
    def kg_neighbors(
        self, notebook_id: str, object_id: str, cap: int = 50
    ) -> dict: ...
    def rebuild_unified_kg(
        self,
        notebook_id: str,
        progress: Callable[[str, int, int], None] | None = None,
        force: bool = False,
    ) -> int: ...
    def rebuild_canonical_relations(
        self, notebook_id: str, force: bool = False
    ) -> int: ...
    def rebuild_mention_bridge(
        self, notebook_id: str, force: bool = False
    ) -> int: ...
    def rebuild_communities(
        self,
        notebook_id: str,
        level: int = 0,
        force: bool = False,
    ) -> int: ...
    def list_communities(
        self, notebook_id: str, level: int = 0
    ) -> list[list[str]]: ...
    def summarize_communities(
        self, notebook_id: str, level: int = 0
    ) -> int: ...
    def get_community_reports(
        self, notebook_id: str, level: int = 0
    ) -> list[dict]: ...
~~~

- [ ] **Step 1: Write RED facade-delegation and no-facade-import tests**

~~~python
def test_facade_lifecycle_delegates_to_same_service(repo, monkeypatch):
    sentinel = {"dirty": False}
    monkeypatch.setattr(
        repo._runtime.knowledge_lifecycle,
        "unified_kg_status",
        lambda notebook_id: sentinel,
    )
    assert repo.unified_kg_status("nb-x") is sentinel
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest tests/test_knowledge_lifecycle_delegation.py -q
~~~

Expected: runtime lacks `knowledge_lifecycle`.

- [ ] **Step 3: Move orchestration in four mechanical groups**

Move KG deletion/store/relink/cluster/fusion first. Create a minimal port-based KnowledgeGovernanceService containing `resolve_notebook_conflicts`, then move full-notebook build/rebuild and the `_kg_building` set/lock so build can call that service without a facade callback; Task 16 extends the same instance with the remaining governance methods. Next move unified reads and streamed rebuild, then canonical/mention/community rebuild and community list/LLM-summary/report orchestration. Preserve per-source scheduler concurrency, progress fail-open, dirty/conflict/relink/fold ordering, rebuild coverage across the delete phase, unconfigured-community-summary no-op and per-community model-error isolation. Until Gate 6, inject callable adapters for scale-index load/open/build-viz/auto-index. Replace Gate-4 source hooks with direct KnowledgeLifecycleService/KgMutationCoordinator dependencies. Facade `_kg_building`/lock properties point to the same lifecycle-owned objects. Migrate `test_rebuild_cache.py` to patch the canonical lifecycle/unified store `_stream_seed_reps` seam and the shared late-bound clock, never a facade wrapper.

- [ ] **Step 4: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_knowledge_lifecycle_delegation.py \
  tests/test_kg_repository.py \
  tests/test_unified_kg_repository.py \
  tests/test_rebuild_streaming.py \
  tests/test_canonical_relations.py \
  tests/test_mention_bridge.py \
  tests/test_rebuild_communities.py \
  tests/test_kg_building_flag.py \
  tests/test_kg_rebuild_relink_api.py \
  tests/test_community_reports.py \
  tests/test_rebuild_wires_communities.py \
  tests/test_resolve_notebook_conflicts.py \
  tests/test_incremental_fuse_perf.py \
  tests/test_source_reverse_index.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/knowledge_lifecycle.py \
  backend/app/services/knowledge_governance.py \
  backend/app/services/source_ingestion.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_knowledge_lifecycle_delegation.py \
  backend/tests/test_unified_kg_repository.py \
  backend/tests/test_rebuild_streaming.py \
  backend/tests/test_canonical_relations.py \
  backend/tests/test_mention_bridge.py \
  backend/tests/test_rebuild_communities.py \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_kg_rebuild_relink_api.py \
  backend/tests/test_community_reports.py \
  backend/tests/test_rebuild_wires_communities.py \
  backend/tests/test_resolve_notebook_conflicts.py \
  backend/tests/test_rebuild_cache.py
git commit -m "refactor(repository): extract knowledge lifecycle service"
~~~

### Task 16: Extract governance orchestration and complete Gate 5

**Files:**
- Modify: `backend/app/services/knowledge_governance.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:4058-5451,7110-7647`
- Create: `backend/tests/test_knowledge_governance_delegation.py`
- Modify: `backend/tests/test_knowledge_governance_boundaries.py`
- Modify: `backend/tests/test_trackF_governance_promotion.py`
- Modify: `backend/tests/test_apply_conflict_resolution.py`
- Modify: `backend/tests/test_kg_conflict_candidates.py`
- Modify: `backend/tests/test_resolve_notebook_conflicts.py`
- Modify: `backend/tests/test_edge_review_queue.py`

**Interfaces:**

`KnowledgeGovernanceService` preserves the exact facade signatures for:

~~~text
review_queue / set_edge_review
pending_merges / confirm_merge / reject_merge / review_pending_merges
merge_review_job_status / run_merge_review_job
pending_conflicts / confirm_conflict / reject_conflict / resolve_notebook_conflicts
propose_promotion / list_promotion_queue / approve_promotion / reject_promotion
update_knowledge / find_duplicates / merge_knowledge
concept_whitelist_list / concept_whitelist_add / concept_whitelist_remove
write_merge_candidate / set_merge_decision / write_conflict_candidate
set_conflict_status / get_conflict_candidate / apply_conflict_resolution
decided_pairs / decided_seed_pairs / concept_whitelist_terms
~~~

- [ ] **Step 1: Write RED delegation and phase-order tests**

~~~python
def test_facade_governance_delegates_without_domain_sql(repo, monkeypatch):
    expected = [{"rel_id": "rel-1"}]
    monkeypatch.setattr(
        repo._runtime.knowledge_governance,
        "review_queue",
        lambda notebook_id, limit=200: expected,
    )
    assert repo.review_queue("nb", 9) is expected
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_knowledge_governance_delegation.py \
  tests/test_knowledge_governance_boundaries.py -q
~~~

Expected: the preliminary service lacks the remaining governance methods/delegates.

- [ ] **Step 3: Move governance methods and retain fail-open phases**

Use GovernanceStore transactions and KgMutationCoordinator in the exact matrix order. Do not normalize conflict double-bumps, candidate-status-after-mutation, embedding fail-open or missing-row behavior.

- [ ] **Step 4: Run focused tests and confirm GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_knowledge_governance_delegation.py \
  tests/test_knowledge_governance_boundaries.py \
  tests/test_trackF_governance_promotion.py \
  tests/test_apply_conflict_resolution.py \
  tests/test_kg_conflict_candidates.py \
  tests/test_resolve_notebook_conflicts.py \
  tests/test_edge_review_queue.py \
  tests/test_concept_merge_review.py \
  tests/test_conflict_review.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/knowledge_governance.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_knowledge_governance_delegation.py \
  backend/tests/test_knowledge_governance_boundaries.py \
  backend/tests/test_trackF_governance_promotion.py \
  backend/tests/test_apply_conflict_resolution.py \
  backend/tests/test_kg_conflict_candidates.py \
  backend/tests/test_resolve_notebook_conflicts.py \
  backend/tests/test_edge_review_queue.py
git commit -m "refactor(repository): extract knowledge governance service"
~~~

- [ ] **Step 6: Run Review Gate 5**

~~~bash
cd backend
python -m pytest \
  tests/test_kg_repository.py \
  tests/test_unified_kg_repository.py \
  tests/test_knowledge_governance_boundaries.py \
  tests/test_trackF_governance_promotion.py \
  tests/test_apply_conflict_resolution.py \
  tests/test_resolve_notebook_conflicts.py \
  tests/test_edge_review_queue.py \
  tests/test_source_reverse_index.py \
  tests/test_vector_cache_invalidation.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

## Review Gate 6 — Retrieval Snapshot Cache and Scale/Viz Artifact Runtime

### Task 17: Transfer existing cache objects into RetrievalSnapshotCache

**Files:**
- Create: `backend/app/services/retrieval_snapshot_cache.py`
- Modify: `backend/app/services/vector_cache.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/kg_mutation.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Create: `backend/tests/test_retrieval_snapshot_cache_runtime.py`
- Modify: `backend/tests/test_vector_cache_invalidation.py`

**Interfaces:**

~~~python
class RetrievalSnapshotCache:
    def __init__(
        self,
        vector_cache: VectorCache,
        unified_cache: MutableMapping[tuple, object],
    ) -> None:
        self.vector_cache = vector_cache
        self.unified_cache = unified_cache

    def get(
        self,
        key: str,
        version: Hashable,
        loader: Callable[[], object],
    ) -> object:
        return self.vector_cache.get(key, version, loader)

    def peek(self, key: str, version: Hashable) -> bool:
        return self.vector_cache.peek(key, version)

    def invalidate(self, key: str) -> None:
        self.vector_cache.invalidate(key)

    def invalidate_kg(self, notebook_id: str) -> None: ...
~~~

`invalidate_kg` preserves the existing embedding matrices, kwtok, federated graph, ppr graph, entchunk, elemchunk, edge-centrality, clustermap and copystats key families plus matching unified-cache entries.

- [ ] **Step 1: Write RED object-identity and setter tests**

~~~python
def test_cache_transfer_preserves_object_identity(repo):
    runtime = repo._runtime
    assert repo._vector_cache is runtime.retrieval_snapshots.vector_cache
    assert repo._unified_cache is runtime.retrieval_snapshots.unified_cache
    assert runtime.kg_mutations.vector_cache is repo._vector_cache


def test_facade_vector_cache_setter_updates_all_consumers(repo):
    replacement = VectorCache(max_entries=3)
    repo._vector_cache = replacement
    assert repo._runtime.retrieval_snapshots.vector_cache is replacement
    assert repo._runtime.kg_mutations.vector_cache is replacement
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest tests/test_retrieval_snapshot_cache_runtime.py -q
~~~

Expected: runtime lacks `retrieval_snapshots`.

- [ ] **Step 3: Wrap, do not recreate, current cache objects**

Construct RetrievalSnapshotCache from the objects already created in runtime. Move key-family invalidation from the facade. Implement write-through facade descriptors; no facade-only copies.

- [ ] **Step 4: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_retrieval_snapshot_cache_runtime.py \
  tests/test_vector_cache.py \
  tests/test_vector_cache_invalidation.py \
  tests/test_query_hotpath_cache.py \
  tests/test_incremental_fuse_perf.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/retrieval_snapshot_cache.py \
  backend/app/services/vector_cache.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/kg_mutation.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_retrieval_snapshot_cache_runtime.py \
  backend/tests/test_vector_cache_invalidation.py
git commit -m "refactor(repository): own retrieval snapshots in runtime"
~~~

### Task 18: Extract index projections and filesystem artifact adapters

**Files:**
- Create: `backend/app/repositories/sqlite/index_projection_store.py`
- Create: `backend/app/repositories/filesystem/__init__.py`
- Create: `backend/app/repositories/filesystem/scale_artifact_store.py`
- Create: `backend/app/services/scale_artifact_catalog.py`
- Modify: `backend/app/services/repository_runtime.py`
- Create: `backend/tests/test_scale_artifact_catalog.py`
- Create: `backend/tests/test_scale_artifact_compatibility.py`

**Interfaces:**

~~~python
ScaleGraphEdges = (
    list[tuple[str, str, float]]
    | tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]
)
ScaleBuildArtifacts = Mapping[str, object]


@dataclass(frozen=True)
class ScaleGraphRows:
    node_ids: list[str]
    edges: ScaleGraphEdges
    chunk_ids: list[str]
    kg_node_ids: list[str]
    membership_counts: dict[str, int]


class IndexProjectionStore:
    def version_signal(
        self, notebook_id: str
    ) -> tuple[int, int, tuple[object, ...]]: ...
    def version_facts(self, notebook_id: str) -> list[object]: ...
    def effective_object_count(self, notebook_id: str) -> int: ...
    def total_chunk_count(self, notebook_id: str) -> int: ...
    def source_ids(self, notebook_id: str) -> list[str]: ...
    def delta_chunk_count(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> int: ...
    def graph_rows(
        self,
        notebook_id: str,
        source_ids: Sequence[str] | None,
    ) -> ScaleGraphRows: ...
    def embedding_matrix(
        self,
        notebook_id: str,
        table: str,
        id_column: str,
        object_ids: Sequence[str] | None = None,
    ) -> tuple[list[str], numpy.ndarray]: ...


class ScaleArtifactStore:
    def scale_dir(self, notebook_id: str) -> Path: ...
    def viz_dir(self, notebook_id: str) -> Path: ...
    def read_manifest(self, directory: Path) -> dict | None: ...
    def read_manifest_version(self, directory: Path) -> object | None: ...
    def load_scale(self, notebook_id: str) -> ScaleIndex | None: ...
    def load_viz(self, notebook_id: str) -> VizIndex | None: ...
    def save_viz(
        self,
        notebook_id: str,
        artifacts: Mapping[str, object],
    ) -> dict: ...
    def save_full(
        self,
        notebook_id: str,
        artifacts: ScaleBuildArtifacts,
    ) -> dict: ...
    def prepare_fold_directory(self, notebook_id: str) -> Path: ...
    def swap_fold_directory(
        self, notebook_id: str, temporary: Path
    ) -> None: ...
~~~

- [ ] **Step 1: Write RED old-artifact no-rebuild test**

~~~python
def test_existing_scale_artifact_loads_without_rebuild(
    repo, copied_scale_fixture
):
    before = copied_scale_fixture.manifest_path.stat().st_mtime_ns
    catalog = repo._runtime.scale_catalog
    assert not hasattr(catalog, "builder")
    loaded = repo._scale_index(
        copied_scale_fixture.notebook_id, allow_stale=True
    )
    assert loaded is not None
    assert copied_scale_fixture.manifest_path.stat().st_mtime_ns == before
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_scale_artifact_catalog.py \
  tests/test_scale_artifact_compatibility.py -q
~~~

Expected: missing projection/artifact/catalog components.

- [ ] **Step 3: Move DB reads and filesystem operations without format changes**

IndexProjectionStore owns scale version/graph/vector snapshots. ScaleArtifactStore owns manifest load/save and the current temporary/old/live swap sequence. Catalog applies exact/stale versions and lazy ANN open; it never schedules a rebuild merely because it reads. Runtime exposes these interim objects as `index_projections`, `scale_artifact_store` and `scale_catalog`; Task 20 composes those same objects into `scale_artifacts` without recreating them.

- [ ] **Step 4: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_scale_artifact_catalog.py \
  tests/test_scale_artifact_compatibility.py \
  tests/test_scale_idx_disk_cache.py \
  tests/test_scale_index_version_probe.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/repositories/sqlite/index_projection_store.py \
  backend/app/repositories/filesystem/__init__.py \
  backend/app/repositories/filesystem/scale_artifact_store.py \
  backend/app/services/scale_artifact_catalog.py \
  backend/app/services/repository_runtime.py \
  backend/tests/test_scale_artifact_catalog.py \
  backend/tests/test_scale_artifact_compatibility.py
git commit -m "refactor(repository): extract scale artifact adapters"
~~~

### Task 19: Extract full-build, fold and viz-build orchestration

**Files:**
- Create: `backend/app/services/scale_index_builder.py`
- Modify: `backend/app/services/kg/scale_index.py`
- Modify: `backend/app/services/kg/viz_index.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:8531-9307,9730-9824`
- Create: `backend/tests/test_scale_builder_failure_boundaries.py`
- Modify: `backend/tests/test_ppr_retrieve.py`
- Modify: `backend/tests/test_scale_index_repo.py`
- Modify: `backend/tests/test_runtime_dim_scale_index.py`

**Interfaces:**

~~~python
class ScaleIndexBuilder:
    def build(
        self,
        notebook_id: str,
        on_stage: Callable[[str, int], None] | None = None,
    ) -> dict: ...
    def fold(
        self,
        notebook_id: str,
        assume_locked: bool = False,
    ) -> dict: ...
    def build_viz(self, notebook_id: str) -> dict | None: ...
    def gather_graph(
        self,
        notebook_id: str,
        source_ids: Sequence[str] | None = None,
        synonym_edges: Sequence[tuple[str, str, float]] | None = None,
        as_arrays: bool = False,
    ) -> tuple: ...
~~~

- [ ] **Step 1: Write RED failure and memory-path tests**

~~~python
def test_fold_failure_before_swap_keeps_old_artifact(
    repo, indexed_notebook, monkeypatch
):
    store = repo._runtime.scale_artifact_store
    builder = repo._runtime.scale_builder
    manifest = store.scale_dir(indexed_notebook) / "manifest.json"
    before = manifest.read_bytes()
    monkeypatch.setattr(
        store,
        "swap_fold_directory",
        lambda notebook_id, temporary: (_ for _ in ()).throw(
            RuntimeError("injected swap failure")
        ),
    )
    with pytest.raises(RuntimeError, match="injected swap failure"):
        builder.fold(indexed_notebook)
    assert manifest.read_bytes() == before
~~~

Also assert full build never calls RetrievalSnapshotCache.get, reuses one KG HNSW for synonym KNN and persisted ANN, keeps int32/int32/float32 arrays, preserves stage callback order, treats callback exceptions as fail-open and escalates dimension mismatch to full build.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_scale_builder_failure_boundaries.py \
  tests/test_ppr_retrieve.py::test_build_scale_index_does_not_populate_vector_cache \
  tests/test_scale_index_repo.py::test_build_scale_index_builds_hnsw_once_for_kg_synonym_and_ann -q
~~~

Expected: missing builder/runtime ownership.

- [ ] **Step 3: Move builder code and preserve persistence checkpoints**

Expose the builder temporarily as `runtime.scale_builder` and use `IndexProjectionStore` directly for full matrices. Full build calls `ScaleArtifactStore.save_full`; viz build calls `ScaleArtifactStore.save_viz`, so neither builder writes manifests/files directly. Full build invokes an injected callback over the existing scale-LRU object only after successful save; it never holds a facade reference. Fold failure leaves the previous live directory usable; Task 20 moves building-marker cleanup and that same LRU object into the final runtime layer.

- [ ] **Step 4: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_scale_builder_failure_boundaries.py \
  tests/test_ppr_retrieve.py \
  tests/test_scale_index_repo.py \
  tests/test_runtime_dim_scale_index.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/scale_index_builder.py \
  backend/app/services/kg/scale_index.py \
  backend/app/services/kg/viz_index.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_scale_builder_failure_boundaries.py \
  backend/tests/test_ppr_retrieve.py \
  backend/tests/test_scale_index_repo.py \
  backend/tests/test_runtime_dim_scale_index.py
git commit -m "refactor(repository): extract scale and viz builders"
~~~

### Task 20: Move cache, locks, status and scheduling into ScaleArtifactRuntime

**Files:**
- Create: `backend/app/services/scale_artifact_runtime.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/knowledge_lifecycle.py`
- Modify: `backend/app/services/sqlite_repository.py:8163-8529,9309-10364`
- Create: `backend/tests/test_scale_artifact_runtime.py`
- Modify: `backend/tests/test_scale_index_version_singleflight.py`
- Modify: `backend/tests/test_scale_index_version_probe.py`
- Modify: `backend/tests/test_scale_version_probe.py`
- Modify: `backend/tests/test_scale_idx_disk_cache.py`
- Modify: `backend/tests/test_scale_idx_cache_lru.py`
- Modify: `backend/tests/test_scale_index_repo.py`
- Modify: `backend/tests/test_scale_delta_policy.py`
- Modify: `backend/tests/test_auto_scale_index.py`
- Modify: `backend/tests/test_index_build_consolidation.py`
- Modify: `backend/tests/test_kg_viz_index.py`
- Modify: `backend/tests/test_viz_index_status.py`
- Modify: `backend/tests/test_viz_bounded.py`
- Modify: `backend/tests/test_runtime_dim_scale_index.py`
- Modify: `backend/tests/test_rebuild_communities.py`

**Interfaces:**

`ScaleArtifactRuntime` owns the existing scale/viz LRUs, version memo/locks, cold-load locks, building sets/locks, idle queue, scheduler flag, auto-index once-set and viz-building state. It exposes:

~~~python
def version(notebook_id: str) -> list: ...
def load(
    notebook_id: str, allow_stale: bool = False
) -> ScaleIndex | None: ...
def open_ann(index: ScaleIndex, kind: str) -> object | None: ...
def viz_index(notebook_id: str) -> ScaleIndex | VizIndex | None: ...
def viz_probe(notebook_id: str) -> dict: ...
def build(notebook_id: str, on_stage=None) -> dict: ...
def fold(notebook_id: str, assume_locked: bool = False) -> dict: ...
def build_viz(notebook_id: str) -> dict | None: ...
def status(notebook_id: str) -> dict: ...
def trigger(
    notebook_id: str, when: str = "now", mode: str = "auto"
) -> dict: ...
def cancel(notebook_id: str) -> dict: ...
def maybe_auto_index(notebook_id: str) -> None: ...
def rearm_auto_index(notebook_id: str) -> None: ...
~~~

- [ ] **Step 1: Write RED identity/single-flight/failure tests**

~~~python
def test_facade_scale_runtime_properties_share_identity(repo):
    scale = repo._runtime.scale_artifacts
    assert repo._scale_idx_cache is scale.scale_cache
    assert repo._viz_idx_cache is scale.viz_cache
    assert repo._scale_building is scale.building
    assert repo._scale_idle_queue is scale.idle_queue
    assert repo._auto_index_checked is scale.auto_index_checked
~~~

Add tests for exception-safe version cold path, six concurrent stale loads calling disk once, daemon/viz failure clearing markers, viz probe never building, auto-index short circuit doing zero scale-fact aggregates, LRU reload, exact/stale identity and no lock held around loaders/builders/callbacks/notifications.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest tests/test_scale_artifact_runtime.py -q
~~~

Expected: runtime lacks `scale_artifacts`.

- [ ] **Step 3: Transfer state by identity and replace lifecycle callbacks**

Construct runtime from existing objects; do not duplicate. Replace KnowledgeLifecycleService temporary index callbacks with ScaleArtifactRuntime. Keep explicit facade wrappers and write-through properties. Migrate version/load/build test patches (`_connect`, `_scale_index`, `_index_delta`, `_ensure_scale_scheduler`, `_spawn_viz_build`) to the owning projection store/runtime/builder so the probes still hit real code without a facade backreference.

The wrappers include the exact baseline names/signatures `build_scale_index`, `fold_scale_index_delta`, `build_viz_index`, `scale_index_status`, `index_status`, `trigger_scale_index_rebuild`, `cancel_scale_index` and `maybe_auto_index`; internal runtime method names may be shorter but the manifest owner remains unique.

- [ ] **Step 4: Run GREEN and Gate 6**

~~~bash
cd backend
python -m pytest \
  tests/test_scale_artifact_runtime.py \
  tests/test_scale_index_version_singleflight.py \
  tests/test_scale_index_version_probe.py \
  tests/test_scale_version_probe.py \
  tests/test_scale_idx_disk_cache.py \
  tests/test_scale_idx_cache_lru.py \
  tests/test_scale_index_repo.py \
  tests/test_scale_delta_policy.py \
  tests/test_auto_scale_index.py \
  tests/test_index_build_consolidation.py \
  tests/test_kg_viz_index.py \
  tests/test_viz_index_status.py \
  tests/test_viz_bounded.py \
  tests/test_runtime_dim_scale_index.py \
  tests/test_rebuild_communities.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
cd frontend
npm run build
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/scale_artifact_runtime.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/knowledge_lifecycle.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_scale_artifact_runtime.py \
  backend/tests/test_scale_index_version_singleflight.py \
  backend/tests/test_scale_index_version_probe.py \
  backend/tests/test_scale_version_probe.py \
  backend/tests/test_scale_idx_disk_cache.py \
  backend/tests/test_scale_idx_cache_lru.py \
  backend/tests/test_scale_index_repo.py \
  backend/tests/test_scale_delta_policy.py \
  backend/tests/test_auto_scale_index.py \
  backend/tests/test_index_build_consolidation.py \
  backend/tests/test_kg_viz_index.py \
  backend/tests/test_viz_index_status.py \
  backend/tests/test_viz_bounded.py \
  backend/tests/test_runtime_dim_scale_index.py \
  backend/tests/test_rebuild_communities.py
git commit -m "refactor(repository): own scale artifact runtime"
~~~

## Review Gate 7 — Retrieval and Evidence Context

### Task 21: Reverse RetrievalService dependencies and extract EvidenceContextService

**Files:**
- Create: `backend/app/services/evidence_context.py`
- Create: `backend/app/services/retrieval_candidates.py`
- Create: `backend/app/services/graph_retrieval.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/repositories/sqlite/notebook_store.py`
- Modify: `backend/app/repositories/sqlite/source_store.py`
- Modify: `backend/app/repositories/sqlite/chunk_store.py`
- Modify: `backend/app/repositories/sqlite/embedding_store.py`
- Modify: `backend/app/repositories/sqlite/knowledge_store.py`
- Modify: `backend/app/repositories/sqlite/governance_store.py`
- Modify: `backend/app/repositories/sqlite/unified_kg_store.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/communities.py`
- Modify: `backend/app/services/retrieval_service.py`
- Modify: `backend/app/services/reasoning_retrieval.py`
- Modify: `backend/app/services/sqlite_repository.py:7700-8159,10104-12552,13121-13160`
- Create: `backend/tests/test_retrieval_service_boundary.py`
- Create: `backend/tests/test_evidence_context_service.py`
- Modify: `backend/tests/test_retrieval_service.py`
- Modify: `backend/tests/test_reasoning_retrieval.py`
- Modify: `backend/tests/test_answer_context_budget.py`
- Modify: `backend/tests/test_architecture_hardening.py`
- Modify: `backend/tests/test_ask_vector_matrix.py`
- Modify: `backend/tests/test_bm25_rrf.py`
- Modify: `backend/tests/test_chunk_bruteforce_guard.py`
- Modify: `backend/tests/test_chunk_retrieval_characterization.py`
- Modify: `backend/tests/test_chunk_retrieval_plan.py`
- Modify: `backend/tests/test_graph_k_binding.py`
- Modify: `backend/tests/test_in_batching.py`
- Modify: `backend/tests/test_indexed_only_principle.py`
- Modify: `backend/tests/test_language_policy.py`
- Modify: `backend/tests/test_large_lib_index_required.py`
- Modify: `backend/tests/test_ppr_fallback_guard.py`
- Modify: `backend/tests/test_query_hotpath_cache.py`
- Modify: `backend/tests/test_relation_retrieval.py`
- Modify: `backend/tests/test_relation_scoring_cold_matrix_guard.py`
- Modify: `backend/tests/test_scale_xlayer_bridge_delta.py`

**Interfaces:**

Add to `ports.py`:

~~~python
class NotebookStorePort(Protocol):
    def tier_map(
        self, notebook_ids: Sequence[str]
    ) -> dict[str, str]: ...


class SourceStorePort(Protocol):
    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...
    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...


class KnowledgeStorePort(Protocol):
    def usable_object_rows(
        self,
        notebook_id: str,
        object_ids: Sequence[str],
    ) -> list[dict[str, Any]]: ...


class CommunityQueryPort(Protocol):
    def first_base_notebook_id(
        self, active_notebook_id: str
    ) -> str | None: ...
    def resolve_comparison_peers(
        self,
        base_notebook_id: str,
        focal_name: str,
        question: str,
        *,
        top_k: int,
        candidates: int,
    ) -> tuple[list[str], str]: ...


class EvidenceContextPort(Protocol):
    def chunk_context(
        self,
        chunks: Sequence[RetrievedChunk],
        *,
        notebook_id: str,
        budget_chars: int | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]: ...
    def knowledge_context(
        self,
        notebook_id: str,
        hits: Sequence[RetrievedKnowledge],
        *,
        id_offset: int = 0,
    ) -> tuple[str, dict[str, dict[str, Any]]]: ...
    def parse_anchors(
        self,
        answer: str,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[AnswerAnchor]: ...
    def citations_from(
        self,
        hits: Sequence[RetrievedKnowledge],
        valid_element_ids: set[str],
        label: str,
    ) -> list[Citation]: ...
    def tier_map(
        self, notebook_ids: Sequence[str]
    ) -> dict[str, str]: ...
~~~

Service constructors:

~~~python
class EvidenceContextService:
    def __init__(
        self,
        *,
        notebooks: NotebookStorePort,
        sources: SourceStorePort,
        knowledge: KnowledgeStorePort,
        settings: Settings,
    ) -> None: ...


class CommunityQueryService:
    def __init__(
        self,
        *,
        notebooks: NotebookStorePort,
        unified_kg: UnifiedKgStore,
        event_log: EventLogger,
    ) -> None: ...


class RetrievalService:
    def __init__(
        self,
        *,
        candidates: CandidateRetrievalService,
        graph: GraphRetrievalService,
    ) -> None:
        self.candidates = candidates
        self.graph = graph


class ReasoningRetriever:
    def __init__(
        self,
        *,
        retrieval: RetrievalPort,
        model_clients: ModelClientProvider,
        communities: CommunityQueryPort,
        settings: Settings,
        cancel_event: threading.Event | None = None,
    ) -> None: ...
~~~

- [ ] **Step 1: Write RED boundary and golden tests**

Add:

~~~text
test_retrieval_service_has_no_repository_backreference
test_retrieval_service_does_not_call_facade_private_retrieval
test_retrieval_service_source_has_no_sqlite_repository_import
test_reasoning_retriever_accepts_ports_without_sqlite_repository
test_evidence_context_chunk_golden_matches_master
test_evidence_context_knowledge_golden_matches_master
test_evidence_context_numeric_group_anchors_match_master
test_evidence_context_preserves_tier_and_source_metadata
~~~

- [ ] **Step 2: Run tests and confirm RED**

~~~bash
cd backend
python -m pytest \
  tests/test_retrieval_service_boundary.py \
  tests/test_evidence_context_service.py \
  tests/test_reasoning_retrieval.py -q
~~~

Expected: missing EvidenceContextService; current RetrievalService still has `_repo`; ReasoningRetriever cannot accept narrow fakes.

- [ ] **Step 3: Move evidence composition without moving model synthesis**

Move `_tier_map_for`, `_chunk_answer_context`, `_answer_context`, `_parse_answer_anchors`, `_citations_from`, KG-block truncation and evidence-ID assignment into EvidenceContextService. Preserve `[k_i]` IDs, numeric citation groups only when every member resolves, tier/source metadata, source-level legacy degradation, ordering and budgets. Facade compatibility methods become one-line delegates. Migrate `test_answer_context_budget.py` to patch canonical EvidenceContextService concept/context collaborators instead of `repo._concept_cluster_id`/`repo.node_context`.

- [ ] **Step 4: Move candidate retrieval families**

`retrieval_candidates.py` receives SQLite read stores, RetrievalSnapshotCache, ScaleArtifactRuntime, query embedder/model-error sink and settings. Move element, object, one-hop, relation, chunk, federated assembly, ANN/FTS, `_notebook_langs` bounded language probing/cache and query-embed memoization. Knowledge-existence/base-tier probes become narrow store methods rather than facade helpers. Preserve:

~~~text
chunk baseline is active-notebook-only
knowledge exact-score ties prefer base only in federated_retrieve
relation federation sorts only by score
one-hop neighbors do not gain rejected-edge filtering
ANN hot paths never materialize the full matrix
chunk overlay size guard runs before seed/object/relation retrieval
~~~

Migrate every candidate-path monkeypatch recorded in Task 1—especially `_keyword_chunk_candidates`, `_retrieve_chunks*`, `_mix_retrieve`, `_embed_query`, `_vector_matrix`, `_relations_with_names`, `_rrf_scored`, `_gather_chunks` and `_IN_CHUNK`—to CandidateRetrievalService/store/batching-policy owners. `test_language_policy.py` must still prove the Ask path invokes the keyword candidate once; changing the patch target must not weaken that assertion.

- [ ] **Step 5: Move graph/PPR/follow-chain retrieval**

`graph_retrieval.py` receives stores, caches and scale runtime. Preserve:

~~~text
graph mode tries scale/small-graph PPR before full-graph refusal
rustworkx PPR fallback only materializes small federated graphs
graph BFS source-chunk hydration remains active-notebook scoped
follow_chain remains read-only and keeps every current fail-closed guard
federated graph/version cache keys and invalidation stay byte-compatible
~~~

The public compatibility method `scale_ppr` delegates to this graph/PPR owner with its frozen signature; it must not remain in the facade or ScaleArtifactRuntime as query-time business logic.

Migrate graph-path patches (`_ppr_graph`, `_federated_rx_graph`, `_open_scale_ann`, `_scale_xlayer_bridge_edges`) to GraphRetrievalService/ScaleArtifactRuntime owners.

- [ ] **Step 6: Rewire RetrievalService, ReasoningRetriever and runtime**

Add the port methods above plus consumer-narrow read methods for every moved retrieval SELECT to the owning notebook/source/chunk/embedding/knowledge/governance/unified store; no store gains a facade backreference. `CommunityQueryService` replaces the old repo-accepting helper internals while `communities.py` retains signature-compatible wrappers. RetrievalService stores only `candidates` and `graph`; it never imports/accepts SQLiteRepository. Runtime exposes the exact instances through facade properties. ReasoningRetriever uses ports for retrieval, model clients and communities. The Task-1 monkeypatch-consumer manifest is authoritative: this task fails review if any listed test still patches a moved facade-private retrieval/evidence member.

- [ ] **Step 7: Run functional/performance GREEN gate**

~~~bash
cd backend
python -m pytest \
  tests/test_retrieval_service.py \
  tests/test_retrieval_service_boundary.py \
  tests/test_evidence_context_service.py \
  tests/test_answer_context_budget.py \
  tests/test_architecture_hardening.py \
  tests/test_ask_vector_matrix.py \
  tests/test_bm25_rrf.py \
  tests/test_chunk_bruteforce_guard.py \
  tests/test_chunk_retrieval_characterization.py \
  tests/test_chunk_retrieval_plan.py \
  tests/test_retrieval.py \
  tests/test_relation_ann.py \
  tests/test_relation_retrieval.py \
  tests/test_follow_chain_repository.py \
  tests/test_ppr_retrieve.py \
  tests/test_ppr_fallback_guard.py \
  tests/test_graph_reason.py \
  tests/test_graph_k_binding.py \
  tests/test_in_batching.py \
  tests/test_indexed_only_principle.py \
  tests/test_language_policy.py \
  tests/test_query_hotpath_cache.py \
  tests/test_relation_scoring_cold_matrix_guard.py \
  tests/test_scale_xlayer_bridge_delta.py \
  tests/test_scale_idx_disk_cache.py \
  tests/test_scale_index_version_singleflight.py \
  tests/test_runtime_dim_cache_keys.py \
  tests/test_ask_embed_cache.py \
  tests/test_large_lib_index_required.py \
  tests/test_reasoning_retrieval.py \
  tests/test_reasoning_ppr.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

Expected: goldens/order/query counts unchanged; no full matrix or unbounded graph load; `RetrievalService.__dict__` contains no `_repo`.

- [ ] **Step 8: Commit**

~~~bash
git add backend/app/services/evidence_context.py \
  backend/app/services/retrieval_candidates.py \
  backend/app/services/graph_retrieval.py \
  backend/app/repositories/ports.py \
  backend/app/repositories/sqlite/notebook_store.py \
  backend/app/repositories/sqlite/source_store.py \
  backend/app/repositories/sqlite/chunk_store.py \
  backend/app/repositories/sqlite/embedding_store.py \
  backend/app/repositories/sqlite/knowledge_store.py \
  backend/app/repositories/sqlite/governance_store.py \
  backend/app/repositories/sqlite/unified_kg_store.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/communities.py \
  backend/app/services/retrieval_service.py \
  backend/app/services/reasoning_retrieval.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_retrieval_service_boundary.py \
  backend/tests/test_evidence_context_service.py \
  backend/tests/test_retrieval_service.py \
  backend/tests/test_reasoning_retrieval.py \
  backend/tests/test_answer_context_budget.py \
  backend/tests/test_architecture_hardening.py \
  backend/tests/test_ask_vector_matrix.py \
  backend/tests/test_bm25_rrf.py \
  backend/tests/test_chunk_bruteforce_guard.py \
  backend/tests/test_chunk_retrieval_characterization.py \
  backend/tests/test_chunk_retrieval_plan.py \
  backend/tests/test_graph_k_binding.py \
  backend/tests/test_in_batching.py \
  backend/tests/test_indexed_only_principle.py \
  backend/tests/test_language_policy.py \
  backend/tests/test_large_lib_index_required.py \
  backend/tests/test_ppr_fallback_guard.py \
  backend/tests/test_query_hotpath_cache.py \
  backend/tests/test_relation_retrieval.py \
  backend/tests/test_relation_scoring_cold_matrix_guard.py \
  backend/tests/test_scale_xlayer_bridge_delta.py
git commit -m "refactor(repository): extract retrieval and evidence context services"
~~~

## Review Gate 8 — Ask, Detached Execution and Reports

### Task 22: Extract Ask/answer/conversation/job/trace persistence

**Files:**
- Create: `backend/app/repositories/sqlite/ask_state_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py:13162-13484,13693-13715`
- Create: `backend/tests/test_ask_state_store.py`
- Modify: `backend/tests/test_ask_jobs.py`
- Modify: `backend/tests/test_conversations.py`

**Interfaces:**

~~~python
@dataclass(frozen=True)
class PreparedAskTurn:
    conversation_id: str
    history: str


class AskStateStorePort(Protocol):
    def prepare_turn(
        self,
        notebook_id: str,
        requested_conversation_id: str | None,
        question: str,
        user_id: str,
    ) -> PreparedAskTurn: ...
    def begin_durable_job(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: str,
        user_id: str,
    ) -> tuple[str, str]: ...
    def append_trace(
        self,
        notebook_id: str,
        job_id: str,
        step: dict,
        user_id: str,
    ) -> None: ...
    def save_answer(
        self,
        notebook_id: str,
        conversation_id: str,
        question: str,
        response: AskResponse,
        user_id: str,
    ) -> str: ...
    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        answer_id: str = "",
        error: str = "",
    ) -> str | None: ...
    def cleanup_empty_conversation(self, conversation_id: str) -> None: ...
    def ask_job_status(self, job_id: str) -> dict: ...


class AskStateStore:
    def __init__(self, database: SqliteDatabase, seams: RepositoryCompatibilitySeams) -> None:
        self.database = database
        self.seams = seams
~~~

`begin_durable_job` opens one write transaction, creates/touches the conversation, mutates `payload.conversation_id` at the same point as baseline, inserts the running job and returns `(job_id, conversation_id)`. `finish_job` performs only the terminal job-row transaction and returns its conversation id; cleanup remains a later transaction. Conversation, feedback, job-detail and trace-list methods retain the exact baseline public signatures behind facade adapters.

- [ ] **Step 1: Write RED persistence, fail-open and checkpoint tests**

~~~python
def test_begin_and_answer_and_finish_are_three_commits(
    store, failure_injector
):
    payload = AskRequest(question="q", conversation_id="conv")
    job_id, conversation_id = store.begin_durable_job(
        "nb", payload, "chunk", "u"
    )
    answer_id = store.save_answer(
        "nb", conversation_id, "q", response(), "u"
    )
    failure_injector.fail_next_write()
    with pytest.raises(RuntimeError):
        store.finish_job(job_id, "done", answer_id=answer_id)
    conversation = store.get_conversation(conversation_id)
    assert conversation.turns[-1].answer_id == answer_id
    assert store.ask_job_status(job_id)["status"] == "running"
~~~

Also inject failure on the `ask_jobs` insert for a payload without a conversation id: the new conversation and job both roll back, while the already-mutated `payload.conversation_id` retains the generated id exactly as baseline. Test that existing-conversation touch + job insert are atomic, append-trace persistence failure is surfaced by the raw store while the later coordinator logs/swallows it, and missing report behavior is not part of this store.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_state_store.py \
  tests/test_ask_jobs.py \
  tests/test_conversations.py -q
~~~

Expected: missing AskStateStore.

- [ ] **Step 3: Move SQL and preserve JSON/order**

Move `_save_answer`, conversation CRUD/history, the atomic conversation+job begin transaction, terminal job-row transaction, later empty-conversation cleanup, job status/detail, trace rows and feedback. Stores receive `user_id` explicitly and never read request ContextVar. Preserve answer payload JSON and startup recovery behavior.

- [ ] **Step 4: Add explicit facade adapters and run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_state_store.py \
  tests/test_ask_jobs.py \
  tests/test_conversations.py \
  tests/test_ask_mode_persist.py \
  tests/test_user_isolation.py -q
~~~

- [ ] **Step 5: Commit rollback point A**

~~~bash
git add backend/app/repositories/sqlite/ask_state_store.py \
  backend/app/repositories/ports.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_ask_state_store.py \
  backend/tests/test_ask_jobs.py \
  backend/tests/test_conversations.py
git commit -m "refactor(repository): extract ask state persistence"
~~~

### Task 23: Extract Ask cancellation and detached execution before moving mode engines

**Files:**
- Create: `backend/app/services/ask_execution.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/api/routes.py:583-678`
- Modify: `backend/app/services/sqlite_repository.py:13220-13335`
- Create: `backend/tests/test_ask_execution_coordinator.py`
- Modify: `backend/tests/test_ask_stream_cancel.py`
- Modify: `backend/tests/test_ask_reconnect.py`
- Modify: `backend/tests/test_ask_stage_events.py`

**Interfaces:**

~~~python
class BackgroundJobSubmitter(Protocol):
    def submit(
        self,
        fn: Callable,
        *args,
        name: str | None = None,
        notify_pending: bool = False,
        **kwargs,
    ) -> threading.Thread: ...


class AskCancellationRegistry:
    def register(
        self, key: str, event: threading.Event
    ) -> None: ...
    def get(self, key: str) -> threading.Event | None: ...
    def cancel(self, key: str) -> bool: ...
    def unregister(self, key: str) -> None: ...


class AskExecutionCoordinator:
    def __init__(
        self,
        *,
        ask_state: AskStateStorePort,
        cancellations: AskCancellationRegistry,
        job_submitter: BackgroundJobSubmitter,
        event_log: EventLogger,
    ) -> None: ...
    def start(
        self,
        notebook_id: str,
        payload: AskRequest,
        mode: AskMode,
        *,
        user_id: str,
        runner: Callable[..., AskResponse],
    ) -> queue.Queue[dict[str, Any] | None]: ...
~~~

During this task `ask_runner` is a late-bound facade callback supplied per start; Task 24 replaces it with AskService. The coordinator must not import SQLiteRepository.

- [ ] **Step 1: Write RED event-order/cancel/disconnect tests**

Add:

~~~text
started is emitted before progress
synthetic start is emitted but not inserted into ask_trace_steps
real trace is persisted before delivery; persistence failure logs and still delivers
begin mutates payload.conversation_id in place
answer save occurs before job finish
terminal job commit occurs before cancellation unregister
cancel unregister occurs before empty-conversation cleanup and before final/cancelled/error delivery
disconnect never sets the cancel event
explicit cancel sets the event
cancel observed at the existing final checkpoint produces no answer
cancel arriving after that checkpoint is not asserted atomic
completed answer survives transport close
copied request context reaches detached worker
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_execution_coordinator.py \
  tests/test_ask_stream_cancel.py \
  tests/test_ask_reconnect.py -q
~~~

Expected: missing coordinator; ordering remains embedded in routes.

- [ ] **Step 3: Implement registry/coordinator with the baseline sequence**

~~~text
atomically create/touch conversation + begin durable job, mutating payload in that transaction body
register event
emit started
emit synthetic start without persistence
submit worker through copied-context job helper
for each real trace: try persist, log/swallow failure, emit trace
runner saves answer
finish durable job-row transaction
unregister event
for failed/cancelled: clean up empty conversation in a later transaction
emit final/cancelled/error response
emit sentinel
~~~

The payload mutation occurs inside `begin_durable_job`; the coordinator observes/uses the returned value and must not mutate it a second time. Transport disconnect only ends route queue consumption. Explicit cancel owner-checks the durable job and calls registry.cancel(). No terminal event may be put on the delivery queue while its job remains registered.

- [ ] **Step 4: Keep route helper seams and run GREEN**

Reduce `_stream_ask_events` to coordinator start/queue consumption/disconnect handling; retain its name/signature for tests.

~~~bash
cd backend
python -m pytest \
  tests/test_ask_execution_coordinator.py \
  tests/test_ask_stream_cancel.py \
  tests/test_ask_reconnect.py \
  tests/test_ask_stage_events.py \
  tests/test_background_jobs.py \
  tests/test_request_user_ctx.py -q
~~~

- [ ] **Step 5: Commit rollback point B**

~~~bash
git add backend/app/services/ask_execution.py \
  backend/app/services/repository_runtime.py \
  backend/app/api/routes.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_ask_execution_coordinator.py \
  backend/tests/test_ask_stream_cancel.py \
  backend/tests/test_ask_reconnect.py \
  backend/tests/test_ask_stage_events.py
git commit -m "refactor(repository): extract detached ask execution"
~~~

### Task 24: Move Ask modes and synthesis into AskService

**Files:**
- Create: `backend/app/services/ask_service.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/ask_execution.py`
- Modify: `backend/app/services/ask_modes.py`
- Modify: `backend/app/services/reasoning_retrieval.py`
- Modify: `backend/app/services/sqlite_repository.py:11694-13160`
- Create: `backend/tests/test_ask_service_boundary.py`
- Modify: `backend/tests/test_ask_modes.py`
- Modify: `backend/tests/test_ask_modes_api.py`
- Modify: `backend/tests/test_reasoning_ask.py`

**Interfaces:**

~~~python
class AskService:
    def __init__(
        self,
        *,
        ask_state: AskStateStorePort,
        retrieval: RetrievalPort,
        evidence_context: EvidenceContextPort,
        model_clients: ModelClientProvider,
        model_errors: ModelErrorSink,
        communities: CommunityQueryPort,
        scale_profiles: NotebookScaleProfile,
        settings: Settings,
        event_log: EventLogger,
    ) -> None: ...

    def ask(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        cancel_event: threading.Event | None = None,
        on_trace: Callable[[dict], None] | None = None,
    ) -> AskResponse: ...
    def ask_chunk(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        cancel_event: threading.Event | None = None,
    ) -> AskResponse: ...
    def ask_reasoning(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        on_trace: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AskResponse: ...
    def ask_graph(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        user_id: str,
        seed_ids: list[str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AskResponse: ...
~~~

- [ ] **Step 1: Write RED mode/golden/no-facade tests**

Add tests that non-streaming Ask creates no job, mode IDs/compatibility mappings remain unchanged, each mode and early exit matches Task-1 goldens, per-user model changes are resolved without restart, and AskService source contains no SQLiteRepository import/private DB access.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_service_boundary.py \
  tests/test_ask_modes.py \
  tests/test_ask_repository_golden.py -q
~~~

Expected: missing AskService.

- [ ] **Step 3: Move mode handlers and helpers without altering control flow**

Move chunk/reasoning/graph handlers, follow-up rewrite, refine/retry/mix helpers, unconfigured response, index-required calculation and answer-save call. Retrieval and EvidenceContext remain separate. Keep mode registry IDs `chunk`, `reasoning`, `graph`; persisted `fast`/`global` compatibility maps remain unchanged.

- [ ] **Step 4: Wire coordinator and explicit facade methods**

Runtime builds one AskService; coordinator calls `AskService.ask(..., user_id=...)` instead of a facade callback. Internal mode methods require keyword-only `user_id`; the explicit `ask`, `ask_chunk`, `ask_reasoning`, `ask_graph` facade methods preserve their frozen baseline signatures and adapt `current_user().id` into that keyword. Stores never read the request ContextVar.

- [ ] **Step 5: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_service_boundary.py \
  tests/test_ask_modes.py \
  tests/test_ask_modes_api.py \
  tests/test_ask_mode_persist.py \
  tests/test_ask_jobs.py \
  tests/test_ask_reconnect.py \
  tests/test_ask_stream_cancel.py \
  tests/test_ask_stage_events.py \
  tests/test_reasoning_ask.py \
  tests/test_reasoning_retrieval.py \
  tests/test_reasoning_ppr.py \
  tests/test_ask_requires_model_config.py \
  tests/test_user_llm_client_resolve.py -q
python ../scripts/check_ask_modes_contract.py
~~~

- [ ] **Step 6: Commit rollback point C**

~~~bash
git add backend/app/services/ask_service.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/ask_execution.py \
  backend/app/services/ask_modes.py \
  backend/app/services/reasoning_retrieval.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_ask_service_boundary.py \
  backend/tests/test_ask_modes.py \
  backend/tests/test_ask_modes_api.py \
  backend/tests/test_reasoning_ask.py
git commit -m "refactor(repository): extract ask mode service"
~~~

### Task 25: Extract ReportStore and rewire report execution through ports

**Files:**
- Create: `backend/app/repositories/sqlite/report_store.py`
- Create: `backend/app/services/report_execution.py`
- Modify: `backend/app/repositories/sqlite/source_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/report_engine.py`
- Modify: `backend/app/services/sqlite_repository.py:13487-13592`
- Modify: `backend/app/api/routes.py:900-1045`
- Create: `backend/tests/test_report_store.py`
- Create: `backend/tests/test_report_engine_ports.py`
- Create: `backend/tests/test_report_execution.py`
- Modify: `backend/tests/test_report_engine.py`
- Modify: `backend/tests/test_report_api.py`

**Interfaces:**

~~~python
class ReportSourceQueryPort(Protocol):
    def report_source_rows(
        self, notebook_id: str
    ) -> list[dict[str, str]]: ...


@dataclass(frozen=True)
class ReportEngineDependencies:
    reports: ReportRepository
    retrieval: RetrievalPort
    evidence_context: EvidenceContextPort
    model_clients: ModelClientProvider
    model_errors: ModelErrorSink
    source_query: ReportSourceQueryPort
    communities: CommunityQueryPort
    settings: Settings
    event_log: EventLogger


class ReportEngine:
    def __init__(
        self,
        dependencies: ReportEngineDependencies,
        *,
        user_id: str,
        cancel_event: threading.Event | None = None,
    ) -> None: ...


class ReportEngineFactory(Protocol):
    def __call__(
        self,
        *,
        user_id: str,
        cancel_event: threading.Event | None,
    ) -> ReportEngine: ...


class ReportCancellationRegistry:
    def register(self, key: str, event: threading.Event) -> None: ...
    def cancel(self, key: str) -> bool: ...
    def unregister(self, key: str) -> None: ...


class ReportExecutionCoordinator:
    def __init__(
        self,
        *,
        reports: ReportRepository,
        engine_factory: ReportEngineFactory,
        cancellations: ReportCancellationRegistry,
        job_submitter: BackgroundJobSubmitter,
    ) -> None: ...
~~~

- [ ] **Step 1: Write RED store/port/cancellation tests**

Add:

~~~text
report CRUD payload/order matches master golden
missing update/delete report remains silent no-op
ReportEngine constructs from narrow fakes and has no repo attribute/import
corpus-map query failure remains fail-open
model-error logging failure does not fail report
deep-dive ReasoningRetriever is port-based
report cancel registers before submit and unregisters on success/failure
module register_cancel/cancel_report/unregister_cancel share runtime registry
runtime AskCancellationRegistry and process-global ReportCancellationRegistry are distinct objects
section parallelism/context propagation/order remain unchanged
no report restart recovery is added
~~~

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_report_store.py \
  tests/test_report_engine_ports.py \
  tests/test_report_execution.py -q
~~~

Expected: missing report store/execution; current engine exposes `repo` and private calls.

- [ ] **Step 3: Move report SQL and rewire engine dependencies**

Add `report_source_rows` to SourceStore and replace:

~~~text
repo.federated_retrieve -> dependencies.retrieval.federated_retrieve
repo._ppr_retrieve -> dependencies.retrieval.ppr_retrieve
repo._answer_context -> dependencies.evidence_context.knowledge_context
repo._chunk_answer_context -> dependencies.evidence_context.chunk_context
repo._connect source-title query -> dependencies.source_query.report_source_rows
repo._note_model_error -> dependencies.model_errors.note_model_error
direct model properties -> dependencies.model_clients properties
report CRUD -> dependencies.reports
~~~

- [ ] **Step 4: Move process-global cancellation behind compatibility functions**

One process-global `REPORT_CANCELLATIONS = ReportCancellationRegistry()` remains the owner. `report_engine.register_cancel`, `cancel_report` and `unregister_cancel` are explicit delegates to that same instance, and runtime injects/references it from ReportExecutionCoordinator. Add an identity assertion that `runtime.report_cancellations is REPORT_CANCELLATIONS` and `runtime.ask_cancellations is not REPORT_CANCELLATIONS`. Preserve no-recovery, plan/generate statuses, copied context, section parallelism and cancel checkpoints.

- [ ] **Step 5: Reduce route launch helpers and run GREEN**

Keep `_launch_plan_job`/`_launch_generate_job` helper names for monkeypatch tests, but delegate to the coordinator.

~~~bash
cd backend
python -m pytest \
  tests/test_report_store.py \
  tests/test_report_engine_ports.py \
  tests/test_report_execution.py \
  tests/test_report_engine.py \
  tests/test_report_api.py \
  tests/test_reasoning_retrieval.py \
  tests/test_background_jobs.py \
  tests/test_user_llm_client_resolve.py -q
~~~

- [ ] **Step 6: Commit rollback point D**

~~~bash
git add backend/app/repositories/sqlite/report_store.py \
  backend/app/services/report_execution.py \
  backend/app/repositories/sqlite/source_store.py \
  backend/app/repositories/ports.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/report_engine.py \
  backend/app/services/sqlite_repository.py \
  backend/app/api/routes.py \
  backend/tests/test_report_store.py \
  backend/tests/test_report_engine_ports.py \
  backend/tests/test_report_execution.py \
  backend/tests/test_report_engine.py \
  backend/tests/test_report_api.py
git commit -m "refactor(repository): rewire deep reports through ports"
~~~

- [ ] **Step 7: Run Review Gate 8**

~~~bash
cd backend
python -m pytest \
  tests/test_ask_state_store.py \
  tests/test_ask_execution_coordinator.py \
  tests/test_ask_service_boundary.py \
  tests/test_ask_jobs.py \
  tests/test_ask_reconnect.py \
  tests/test_ask_stream_cancel.py \
  tests/test_ask_stage_events.py \
  tests/test_reasoning_ask.py \
  tests/test_report_store.py \
  tests/test_report_engine_ports.py \
  tests/test_report_execution.py \
  tests/test_report_engine.py \
  tests/test_report_api.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
~~~

## Review Gate 9 — Thin Facade, Callers, Old-DB Verification and PR

### Task 26: Consolidate RepositoryRuntime and the explicit compatibility facade

**Files:**
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/repository.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Create: `backend/tests/test_repository_facade_contract.py`
- Create: `backend/tests/test_repository_runtime_identity.py`
- Create: `backend/tests/test_repository_monkeypatch_owners.py`
- Modify: `backend/tests/test_architecture_module_boundaries.py`
- Modify: `backend/tests/test_architecture_hardening.py`

**Interfaces:**

Final `SQLiteRepository.__init__` resolves root/storage, constructs one RepositoryRuntime and publishes explicit write-through compatibility properties. Every frozen method delegates to exactly one runtime component. The module re-exports every Task-1 import, including:

~~~text
SQLiteRepository / SCHEMA_VERSION / UploadedSourceFile
_now / _new_id / _fast_loads
_REQUEST_USER / set_request_user / reset_request_user
USABLE_STATUSES / KNOWLEDGE_STATUSES / KnowledgeGraphTooLargeError
_COPY_CHUNK / _remap_json_ids
RetrievedKnowledge and other consumer-imported retrieval types
~~~

- [ ] **Step 1: Write RED facade/source/identity tests**

~~~python
def test_facade_matches_frozen_surface_manifest():
    for name, contract in frozen_surface().items():
        assert hasattr(SQLiteRepository, name) or hasattr(sqlite_repository, name)
        if contract["kind"] == "method":
            assert str(inspect.signature(getattr(SQLiteRepository, name))) == (
                contract["signature"]
            )


def test_facade_has_no_getattr_or_sql():
    source = inspect.getsource(SQLiteRepository)
    assert "def __getattr__" not in source
    assert ".execute(" not in source
    assert ".executemany(" not in source
    assert ".executescript(" not in source
~~~

Add identity/write-through assertions for settings, storage_dir, embedder, model/rerank setters, retrieval/evidence services, VectorCache, scale/viz LRUs, write lock, build sets/queues and Ask cancellation registry.

Add an AST assertion that every Task-1 monkeypatch consumer now targets its canonical component unless the manifest marks the member as an intentionally late-bound production compatibility seam (`_now`, `_new_id`, `_COPY_CHUNK`, `_remap_json_ids`); test-only facade-private patch targets are forbidden.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_facade_contract.py \
  tests/test_repository_runtime_identity.py \
  tests/test_repository_monkeypatch_owners.py \
  tests/test_architecture_module_boundaries.py -q
~~~

Expected: facade still contains SQL/business bodies and identity gaps.

- [ ] **Step 3: Replace remaining bodies with explicit delegates**

Do not generate `__getattr__`, a generic dispatch table or Protocol inheritance. Properties must return/set the exact runtime object. Keep private wrappers required by production/scripts/manifest even after their callers move.

- [ ] **Step 4: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_facade_contract.py \
  tests/test_repository_runtime_identity.py \
  tests/test_repository_monkeypatch_owners.py \
  tests/test_architecture_module_boundaries.py \
  tests/test_architecture_hardening.py \
  tests/test_repository_surface_manifest.py -q
~~~

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/repository.py \
  backend/app/repositories/ownership_manifest.py \
  backend/tests/test_repository_facade_contract.py \
  backend/tests/test_repository_runtime_identity.py \
  backend/tests/test_repository_monkeypatch_owners.py \
  backend/tests/test_architecture_module_boundaries.py \
  backend/tests/test_architecture_hardening.py
git commit -m "refactor(repository): consolidate explicit repository facade"
~~~

### Task 27: Migrate production callers and isolate SQLite maintenance

**Files:**
- Create: `backend/app/repositories/sqlite/maintenance.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/services/background_jobs.py`
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `backend/app/eval/inference.py`
- Modify: `backend/app/eval/mrl_truncation.py`
- Modify: `backend/app/eval/retrieval_metrics.py`
- Modify: `backend/app/eval/run_all.py`
- Modify: `backend/app/eval/sa_calibration.py`
- Modify: `backend/app/eval/speed.py`
- Modify: `backend/app/scripts/backfill_relation_embeddings.py`
- Modify: `backend/app/scripts/build_kg.py`
- Modify: `backend/app/scripts/gen_recall_gold.py`
- Modify: `backend/app/scripts/recluster_kg.py`
- Modify: `backend/app/scripts/reembed_kg.py`
- Modify: `scripts/backfill_kg_embeddings.py`
- Modify: `scripts/build_chunks.py`
- Modify: `scripts/compare_kg_dbs.py`
- Modify: `scripts/denoise_reextract_nb.py`
- Modify: `scripts/diag_base_report.py`
- Test: `scripts/diag_slow.py`
- Modify: `scripts/kg_product_smoke.py`
- Modify: `scripts/reextract_notebook.py`
- Modify: `scripts/replay_retrieval.py`
- Modify: `scripts/smoke_backend.py`
- Modify: `scripts/validate_concept_filter.py`
- Modify: `scripts/validate_overmerge_fix.py`
- Create: `backend/tests/test_repository_callers_static.py`
- Modify: `backend/tests/test_sqlite_write_optimization.py`
- Modify: `backend/tests/test_batch_ingest.py`

**Interfaces:**

`SQLiteMaintenanceAdapter` implements `SQLiteMaintenancePort` and receives concrete stores/services/runtime; CLI composition roots may instantiate SQLiteRepository and then request `repo.maintenance`, but portable application ports never include these operations.

- [ ] **Step 1: Write RED static caller/SQL ownership tests**

The AST/static test must fail on:

~~~text
application services importing SQLiteRepository
application services calling _connect/_write
production callers using _retrieve_scored/_ppr_retrieve/_answer_context/_chunk_answer_context
main database SQL outside repositories/sqlite
route/helper code typed as NotebookRepository when a narrow accessor exists
new production consumer of eval_insert_source_for_test
~~~

Allow only API/CLI composition roots and compatibility tests to import the facade. The SQL-call-site audit uses an exact, documented allowlist:

~~~text
backend/app/core/llm_cache.py — independent LLM cache DB
backend/app/eval/db.py — independent evaluation DB
scripts/bench_sqlite_writes.py — synthetic temporary write benchmark, never the product DB
scripts/diag_slow.py — stdlib-only host diagnostic; product DB URI mode=ro, SELECT/PRAGMA only
scripts/generate_repository_contract_fixtures.py — baseline-guarded immutable fixture generator
scripts/verify_repository_snapshot.py — original DB URI mode=ro + sqlite backup only
~~~

The test fails on any new allowlisted file or SQL operation. `diag_slow.py` must remain host-safe/read-only as required by AGENTS.md; the two compatibility tools are allowed only the exact baseline guard/backup operations tested in Tasks 1 and 28.

- [ ] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_callers_static.py \
  tests/test_sqlite_write_optimization.py \
  tests/test_batch_ingest.py -q
~~~

Expected: current private calls/file-specific write audit fail.

- [ ] **Step 3: Migrate each listed caller**

Use domain ports/services for app code; use `SQLiteMaintenanceAdapter` for batch/backfill/mutating CLI work. Add a mode-ro `ReadOnlySQLiteInspector` under the same adapter package for MRL evaluation and arbitrary-path comparison/validation tools, so their SQL also resides under `repositories/sqlite/`. `background_jobs` imports only `core.request_context`. `eval/speed.py` keeps public upload/parse/extract timing. Route paths, CLI arguments/output schemas and FastAPI dependencies stay unchanged. Do not make `diag_slow.py` import application code or open the DB read-write.

- [ ] **Step 4: Expand write audit to every primary SQLite adapter**

Scan `backend/app/repositories/sqlite/*.py`; every runtime write must be under `SqliteDatabase.write()` or an explicit migration/startup allowlist. Separately scan all `backend/app/**/*.py` and `scripts/*.py` SQL/private-facade call sites against the exact exception list above. The old single-file scan is removed only after equivalent coverage exists.

- [ ] **Step 5: Run GREEN**

~~~bash
cd backend
python -m pytest \
  tests/test_repository_callers_static.py \
  tests/test_sqlite_write_optimization.py \
  tests/test_batch_ingest.py \
  tests/test_trackA_eval_connect.py \
  tests/test_background_jobs.py \
  tests/test_request_user_ctx.py \
  tests/test_retrieval_service_boundary.py \
  tests/test_report_engine_ports.py -q
~~~

- [ ] **Step 6: Commit**

~~~bash
git add backend/app/repositories/sqlite/maintenance.py \
  backend/app/api/deps.py \
  backend/app/api/routes.py \
  backend/app/services/background_jobs.py \
  backend/app/services/batch_ingest.py \
  backend/app/eval/inference.py \
  backend/app/eval/mrl_truncation.py \
  backend/app/eval/retrieval_metrics.py \
  backend/app/eval/run_all.py \
  backend/app/eval/sa_calibration.py \
  backend/app/eval/speed.py \
  backend/app/scripts/backfill_relation_embeddings.py \
  backend/app/scripts/build_kg.py \
  backend/app/scripts/gen_recall_gold.py \
  backend/app/scripts/recluster_kg.py \
  backend/app/scripts/reembed_kg.py \
  scripts/backfill_kg_embeddings.py \
  scripts/build_chunks.py \
  scripts/compare_kg_dbs.py \
  scripts/denoise_reextract_nb.py \
  scripts/diag_base_report.py \
  scripts/kg_product_smoke.py \
  scripts/reextract_notebook.py \
  scripts/replay_retrieval.py \
  scripts/smoke_backend.py \
  scripts/validate_concept_filter.py \
  scripts/validate_overmerge_fix.py \
  backend/tests/test_repository_callers_static.py \
  backend/tests/test_sqlite_write_optimization.py \
  backend/tests/test_batch_ingest.py
git commit -m "refactor(repository): migrate callers to repository ports"
~~~

### Task 28: Verify the real old database, synchronize docs and publish one PR

**Files:**
- Create: `scripts/verify_repository_snapshot.py`
- Create: `backend/tests/test_repository_snapshot_verifier.py`
- Modify: `backend/tests/test_architecture_documentation.py`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `fangan_done.md`
- Modify: `docs/superpowers/specs/2026-07-10-architecture-remediation-design.md`
- Modify: `docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md` only if implementation exposed a verified contradiction.

**Snapshot verifier CLI:**

~~~text
python scripts/verify_repository_snapshot.py \
  --database PATH \
  --storage-dir PATH
~~~

It opens the original as SQLite URI `mode=ro` only long enough to call `Connection.backup()`. SQLiteRepository receives only the temporary DB and an empty/copied temporary storage directory under offline settings:

~~~python
Settings(
    database_url=f"sqlite:///{backup_path}",
    storage_dir=str(temporary_storage),
    openai_compat_base_url="",
    openai_compat_api_key="",
    openai_compat_model="",
    reasoning_llm_base_url="",
    reasoning_llm_api_key="",
    reasoning_llm_model="",
    rewrite_llm_base_url="",
    rewrite_llm_api_key="",
    rewrite_llm_model="",
    kg_llm_base_url="",
    kg_llm_api_key="",
    kg_llm_model="",
    embed_provider="",
    embed_model="",
    embed_base_url="",
    embed_api_key="",
    rerank_model="",
    rerank_base_url="",
    rerank_api_key="",
    mineru_mode="off",
    mineru_api_url="",
    mineru_vlm_server_url="",
    mineru_api_token="",
    scale_index_auto_enabled=False,
    event_log_enabled=False,
    llm_log_enabled=False,
    debug_logs_enabled=False,
    auth_optional=True,
)
~~~

- [x] **Step 1: Write RED backup-only/privacy/digest tests**

Add:

~~~text
repository is never constructed with original database path
repository is never constructed with original storage path or a symlink
WAL committed rows are present in backup
schema version/tables/row counts/primary keys/canonical digests are preserved
only recovery/seed/admin-upgrade fields are normalized
stdout never includes seeded username/title/source/prompt/answer/report secrets
original storage path list/size/mtime remain unchanged
hostile configured model/embed/rerank/MinerU environment still creates no client/network call
~~~

- [x] **Step 2: Run RED**

~~~bash
cd backend
python -m pytest tests/test_repository_snapshot_verifier.py -q
~~~

Expected: verifier module absent.

- [x] **Step 3: Implement normalization and representative reads**

Normalize only:

~~~text
user-local admin username/role/password hash/salt/iterations/updated_at
missing built-in user/profile/whitelist/object-schema rows inserted by seed
running merge_review_jobs -> failed with restart error
running ask_jobs -> interrupted with restart error
~~~

Every other existing primary key, row count and canonical row digest must match. Build verifier settings from explicit field values (not ambient `.env` defaults), assert every provider is unconfigured/off, and install a test network/client-factory tripwire. Exercise account resolution, notebooks, sources, knowledge types/list, unified status, conversations/answers, Ask jobs, reports and keyword-only retrieval when populated. Print table names/counts/digests only, never row content.

Success line:

~~~text
repository-snapshot: PASS schema=v9 changed_tables=0
~~~

- [x] **Step 4: Run fixture and real pre-refactor DB verification**

~~~bash
python scripts/verify_repository_snapshot.py \
  --database backend/tests/fixtures/repository_v9/baseline.db \
  --storage-dir backend/tests/fixtures/repository_v9/storage

MAIN_CHECKOUT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
ORIGINAL_DB="$MAIN_CHECKOUT/.local/silicon_notebook.db"
ORIGINAL_STORAGE="$MAIN_CHECKOUT/.local/storage"
test -f "$ORIGINAL_DB"
test -d "$ORIGINAL_STORAGE"
python scripts/verify_repository_snapshot.py \
  --database "$ORIGINAL_DB" \
  --storage-dir "$ORIGINAL_STORAGE"
~~~

Expected: both print PASS; the second command must target the main checkout rather than the isolated worktree, and original DB/storage metadata remain unchanged. Missing original DB/storage is a hard failure, never a skipped compatibility check.

- [x] **Step 5: Synchronize architecture documentation**

Document the final dependency direction, stores/services/runtime ownership, future PostgreSQL extension boundary, v9 compatibility and backup verifier in all synchronized docs. Because current code is the behavior oracle, also correct stale Ask-mode documentation to `chunk` (default), `reasoning`, `graph`, with persisted `fast`/`global` accepted only as aliases to `chunk`; do not preserve the older fast/global product description. Update `fangan_done.md` phase status factually; add documentation tests asserting the earlier spec is superseded only for Repository work and Pydantic/lifespan work remains deferred.

- [x] **Step 6: Run all static, compatibility and full gates**

~~~bash
git diff --check
cd backend
python -m pytest \
  tests/test_repository_facade_contract.py \
  tests/test_repository_callers_static.py \
  tests/test_repository_api_contract.py \
  tests/test_repository_snapshot_verifier.py \
  tests/test_legacy_db_compat.py \
  tests/test_schema_version_migration.py \
  tests/test_rebuild_cache.py \
  tests/test_architecture_documentation.py -q
cd ..
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
cd frontend
npm run build
~~~

Expected: complete green suite; SCHEMA_VERSION remains 9 and schema golden is unchanged.

- [x] **Step 7: Commit final verification/docs**

~~~bash
git add scripts/verify_repository_snapshot.py \
  backend/tests/test_repository_snapshot_verifier.py \
  backend/tests/test_architecture_documentation.py \
  README.md README_zh.md AGENTS.md architecture.md fangan_done.md \
  docs/superpowers/specs/2026-07-10-architecture-remediation-design.md \
  docs/superpowers/specs/2026-07-10-repository-composition-refactor-design.md \
  docs/superpowers/plans/2026-07-10-repository-composition-refactor.md
git commit -m "docs(repository): record composition architecture and compatibility"
~~~

- [ ] **Step 8: Merge latest master and repeat verification**

~~~bash
git fetch origin master
git merge --no-ff origin/master
PYTHON_BIN=/path/to/python3 bash scripts/check.sh
cd frontend
npm run build
~~~

Re-run both snapshot verifier commands after the merge. Any conflict resolution that touches repository/runtime files requires the affected gate subset before the full suite.

- [ ] **Step 9: Request final code review and publish one ready PR**

Use `superpowers:requesting-code-review`, fix findings through `superpowers:receiving-code-review`, then use `superpowers:verification-before-completion`. Create `/tmp/repository-composition-pr.md` with the verified values from the preceding commands and no placeholders. Push the branch and open one ready PR against master:

~~~bash
git push -u origin codex/repository-composition-refactor
gh pr create \
  --base master \
  --head codex/repository-composition-refactor \
  --title "Refactor repository composition boundaries" \
  --body-file /tmp/repository-composition-pr.md
~~~

The PR body must include:

~~~text
zero-behavior-change motivation
component/state ownership
review gates and rollback order
schema v9 and frozen-master fixture results
real old-database backup-only result
retrieval/performance results
Ask stream/cancel/disconnect results
report results
full scripts/check.sh and frontend build results
explicit unchanged route/schema statement
🤖 Generated with [Claude Code](https://claude.com/claude-code)
~~~
