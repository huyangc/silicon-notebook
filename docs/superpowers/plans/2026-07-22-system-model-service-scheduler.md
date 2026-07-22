# System-Managed Model Services and Unified Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every user-editable model setting and route every product-runtime chat, embedding, and rerank provider call through one system-owned, per-service scheduler whose capacity is the configured model-service parallelism.

**Architecture:** A strict TOML registry defines named physical services and binds stable workload ids to exactly one compatible service. `RuntimeModelProvider` creates workload-bound scheduled adapters over private raw protocol clients; one `ServiceScheduler` per service applies derived queue bounds, weighted priority, per-user fairness, cancellation, support ids, and a shared circuit breaker. SQLite persists only sanitized system-level health observations, while the browser exposes a read-only service-status panel and administrator-only probes.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, `tomllib`, `contextvars`, `threading`, `concurrent.futures`, SQLite, pytest/pytest-xdist, TypeScript, React 19, Next.js 15, Vitest, Node test runner.

## Global Constraints

- Read and follow `docs/superpowers/specs/2026-07-22-system-model-service-scheduler-design.md` before implementation.
- Work on a dedicated `codex/system-model-service-scheduler` branch/worktree. Preserve unrelated PostgreSQL design/plan changes in the current workspace.
- Use TDD for every behavior change: add the focused failing test, run it and confirm the intended failure, implement minimally, rerun, then commit.
- The only per-service scheduler-capacity setting is `max_concurrency`. Queue limits, priority weights, wait deadlines, breaker thresholds, cooldown, and half-open behavior are fixed code policy.
- For service concurrency `N`, active calls are capped at `N`, queued calls at `10N`, and one actor's total queued calls for that service at `2N`.
- Priority weights are exactly `8 interactive : 2 report : 1 background`; wait deadlines are exactly 30, 300, and 1,800 seconds respectively.
- Three consecutive transient failures open a breaker; authentication/model/protocol fatal failures open it immediately; cooldown is 30 seconds; half-open admits exactly one probe.
- A service id matches `[a-z][a-z0-9_]{0,63}`. One workload binds to one service. Different named services never share capacity, even when their URLs point to the same host.
- Keep deterministic/offline behavior when `MODEL_SERVICES_CONFIG` is empty. Never silently fall back from one configured workload binding to another service.
- API keys remain environment variables referenced by `api_key_env`; never persist, return, or log them. Do not return URLs, raw provider bodies, prompts, source text, embeddings, or raw exceptions from status APIs.
- Keep exactly one backend process. Multiple Uvicorn workers/replicas are unsupported because the scheduler is process-local.
- Keep business orchestration concurrency only where it does not authorize provider parallelism. Remove `KG_EXTRACT_WORKERS`, `EMBED_CONCURRENCY`, batch `--llm-conc`/`--embed-conc`, `LimitedJsonChatClient`, and the old global embedding executor as capacity authorities.
- Never wait on a model queue while holding a SQLite write transaction. Provider submission belongs before or after the write unit.
- Full-stack parity is mandatory: removed backend configuration capabilities and the read-only status replacement ship with the frontend changes in the same branch.
- Synchronize `README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`, `.env.example`, fixtures, migration manifests, and `fangan_done.md` before completion.
- Completion requires `scripts/check.sh` and `cd frontend && npm run build` to pass offline.

---

## Target file map

### System configuration and workload protocol

- Create `backend/app/services/model_registry.py`: immutable service definitions, exact workload registry, TOML loading, secret resolution, fingerprinting, and strict validation.
- Modify `backend/app/core/config.py`: add and repository-anchor `MODEL_SERVICES_CONFIG`; retain timeout/token/batch/domain settings but retire endpoint/capacity runtime reads.
- Create `model-services.example.toml`: checked-in deployment template without credentials.
- Create `backend/tests/test_model_registry.py`: config, binding, duplicate-service, secret, and legacy-env migration-error coverage.

### Scheduling runtime

- Create `backend/app/services/model_work.py`: priorities, current work context, support ids, typed scheduling/provider errors, and safe metadata.
- Create `backend/app/services/model_circuit_breaker.py`: deterministic breaker state machine and upstream failure classifier.
- Replace `backend/app/services/model_concurrency.py` with `backend/app/services/model_scheduler.py`: bounded weighted/fair queues, dispatcher, executor, cancellation, snapshots, shutdown, and registry.
- Create `backend/tests/test_model_circuit_breaker.py` and `backend/tests/test_model_scheduler.py`.
- Retire `backend/tests/test_model_concurrency.py` after its still-valid peak/shutdown assertions move to the new scheduler tests.

### Scheduled clients and product call sites

- Refactor `backend/app/services/model_provider.py`: system-only raw clients plus `chat(workload)`, `embedding(workload)`, `rerank(workload)`, `parallelism(workload)`, direct service probes, and safe failure reporting.
- Modify `backend/app/services/repository_runtime.py`, `backend/app/services/sqlite_repository.py`, `backend/app/api/deps.py`, `backend/app/main.py`, and `backend/app/services/startup_warmup.py`: own one scheduler registry for the process, propagate it through runtime composition, and shut it down once.
- Modify every chat caller in `backend/app/services/ask_service.py`, `reasoning_retrieval.py`, `graph_retrieval.py`, `report_engine.py`, `source_ingestion.py`, `concept_merge_review.py`, `schema_registry.py`, `knowledge_lifecycle.py`, `knowledge_governance.py`, `query_rewrite.py`, `kg/extract.py`, `kg/conflict_review.py`, `kg/graph_reason.py`, `kg/run_control.py`, `knowhow/api.py`, and `memory_service.py`.
- Modify embedding/rerank callers in `backend/app/services/source_embedding.py`, `retrieval_candidates.py`, `retrieval_service.py`, `memory_service.py`, `memory_retrieval.py`, `knowledge_query.py`, `knowhow/projection.py`, `rerank_client.py`, and their runtime wiring.

### System health, migration, API, and frontend

- Create `backend/app/repositories/sqlite/model_status_store.py`; remove model configuration/status responsibilities from `IdentityStore` and repository identity ports.
- Migrate SQLite to v24, scrub stored user credentials, and create `system_model_service_status`.
- Refactor `backend/app/services/model_status.py`, `backend/app/models/model_services.py`, `backend/app/models/ask.py`, `backend/app/core/model_safety.py`, `backend/app/api/system_routes.py`, and `backend/app/api/admin_routes.py`.
- Create `frontend/app/model-services.ts`; delete `frontend/app/model-settings.ts`; refactor the panel, orchestration, Ask error panel, page state, vocabulary, styles, and tests.

---

### Task 1: Introduce the strict system service registry and workload catalog

**Files:**
- Create: `backend/app/services/model_registry.py`
- Modify: `backend/app/core/config.py`
- Create: `backend/tests/test_model_registry.py`
- Create: `model-services.example.toml`

**Interfaces:**

```python
ModelKind = Literal["chat", "embedding", "rerank"]
ModelPriorityName = Literal["interactive", "report", "background"]

@dataclass(frozen=True)
class WorkloadSpec:
    id: str
    kind: ModelKind
    default_priority: ModelPriorityName
    display_label: str

@dataclass(frozen=True)
class ModelServiceDefinition:
    id: str
    display_name: str
    kind: ModelKind
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)
    max_concurrency: int
    fingerprint: str

class SystemModelServiceRegistry:
    @classmethod
    def load(cls, settings: Settings,
             environ: Mapping[str, str] | None = None
             ) -> "SystemModelServiceRegistry": ...
    def service(self, service_id: str) -> ModelServiceDefinition: ...
    def service_for(self, workload_id: str) -> ModelServiceDefinition | None: ...
    def workload(self, workload_id: str) -> WorkloadSpec: ...
    def workloads_for(self, service_id: str) -> tuple[WorkloadSpec, ...]: ...

def workload_map(*, chat: Mapping[str, ModelPriorityName],
                 embedding: Mapping[str, ModelPriorityName],
                 rerank: Mapping[str, ModelPriorityName]
                 ) -> Mapping[str, WorkloadSpec]: ...
```

- [ ] **Step 1: Write failing registry tests**

Cover an empty path, the checked-in example, secret resolution, relative-path anchoring, unknown TOML keys, invalid ids/kinds/protocols, non-positive parallelism, missing secrets, unknown/mismatched bindings, duplicate physical definitions, and fingerprints changing when URL/model/key/concurrency changes. Cover legacy endpoint detection in both real environment variables and the active Pydantic `.env` file, including an explicit empty process variable overriding a stale file value; failure text contains only variable names, never values.

```python
def test_registry_binds_one_physical_service_to_many_workloads(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text("""
[services.general]
display_name = "通用模型"
kind = "chat"
protocol = "openai"
base_url = "https://llm.example/v1"
model = "general-model"
api_key_env = "GENERAL_KEY"
max_concurrency = 2

[bindings]
ask_answer = "general"
source_summary = "general"
""", encoding="utf-8")
    registry = SystemModelServiceRegistry.load(
        Settings(_env_file=None, model_services_config=str(path)),
        {"GENERAL_KEY": "secret"},
    )
    assert registry.service_for("ask_answer") is registry.service_for("source_summary")
    assert registry.service("general").max_concurrency == 2
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 backend/tests/test_model_registry.py
```

Expected: FAIL because `SystemModelServiceRegistry` and `MODEL_SERVICES_CONFIG` do not exist.

- [ ] **Step 3: Implement the exact workload catalog**

Define every approved id as data, not scattered conditionals:

```python
WORKLOADS = workload_map(
    chat={
        "ask_answer": "interactive", "reasoning_agent": "interactive",
        "query_rewrite": "interactive", "evidence_refine": "interactive",
        "graph_chain_verify": "interactive", "report_outline": "report",
        "report_sufficiency": "report", "report_section": "report",
        "report_summary": "report", "source_summary": "background",
        "notebook_metadata": "background", "paper_metadata": "background",
        "kg_extract": "background", "kg_refine": "background",
        "kg_glean": "background", "kg_merge_review": "background",
        "kg_concept_description": "background",
        "kg_community_summary": "background",
        "kg_conflict_review": "background", "schema_induction": "interactive",
        "memory_preview": "interactive", "knowhow_optimize": "interactive",
        "knowhow_reformat": "interactive",
    },
    embedding={
        "retrieval_query_embedding": "interactive",
        "source_element_embedding": "background", "chunk_embedding": "background",
        "knowledge_object_embedding": "background", "relation_embedding": "background",
        "memory_embedding": "interactive", "knowhow_embedding": "background",
    },
    rerank={"retrieval_rerank": "interactive"},
)
```

`workload_map` must also assign the Chinese display labels fixed in the design. Tests compare the exact id set, kind, default priority, and non-empty label.

- [ ] **Step 4: Implement fail-closed TOML parsing and legacy-env detection**

Use `tomllib.load`, `dotenv_values`, explicit allowed-key sets, immutable mappings, `safe_model_label`, and SHA-256 fingerprints. If the path is empty, return an empty registry only after checking the effective legacy environment. Merge the active `Settings.model_config["env_file"]` values with the supplied/process environment using normal environment-over-file precedence; an explicit empty process value suppresses the file value. Use that same merged map to resolve each configured `api_key_env`. If no path is set but a legacy endpoint variable is effectively non-empty, raise a credential-safe migration error naming only the variable. Never retain or log a parsed legacy credential value.

- [ ] **Step 5: Add the credential-free template and rerun tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_registry.py backend/tests/test_env_preflight.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/model_registry.py backend/app/core/config.py \
  backend/tests/test_model_registry.py model-services.example.toml
git commit -m "feat: load system model service registry"
```

### Task 2: Build the bounded fair scheduler and service circuit breaker

**Files:**
- Create: `backend/app/services/model_work.py`
- Create: `backend/app/services/model_circuit_breaker.py`
- Create: `backend/app/services/model_scheduler.py`
- Create: `backend/tests/test_model_circuit_breaker.py`
- Create: `backend/tests/test_model_scheduler.py`

**Interfaces:**

```python
class ModelPriority(StrEnum):
    INTERACTIVE = "interactive"
    REPORT = "report"
    BACKGROUND = "background"

class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...

@dataclass(frozen=True)
class ModelWorkContext:
    actor_id: str
    workload_id: str
    priority: ModelPriority
    parent_id: str
    support_id: str
    deadline_at: float
    cancel_event: CancellationSignal | None

@contextmanager
def model_work_scope(*, priority: ModelPriority,
                     parent_id: str = "") -> Iterator[None]: ...

class ModelSchedulingError(Exception): ...
class ModelQueueFull(ModelSchedulingError): ...
class ModelQueueTimeout(ModelSchedulingError): ...
class ModelServiceUnavailable(ModelSchedulingError): ...
class ModelProviderError(Exception): ...
class MalformedModelResponse(ModelProviderError): ...

@dataclass(frozen=True)
class ProviderObservation:
    service_id: str
    config_fingerprint: str
    status: Literal["ok", "error"]
    code: str
    trigger: Literal["manual_test", "observed_failure", "recovery_probe"]
    support_id: str
    latency_ms: int
    occurred_at: str

@dataclass(frozen=True)
class SchedulerSnapshot:
    active: int
    maximum: int
    queued: int
    oldest_wait_ms: int
    breaker_state: Literal["closed", "open", "half_open"]
    busy: bool

class ServiceScheduler:
    def submit(self, *, context: ModelWorkContext,
               invoke: Callable[[], T]) -> Future[T]: ...
    def snapshot(self) -> SchedulerSnapshot: ...
    def shutdown(self, *, wait: bool = True) -> None: ...
```

- [ ] **Step 1: Write deterministic breaker tests using an injected clock**

Assert transient classification for connection/timeout/429/5xx, fatal classification for 401/403 and explicit unknown-model/protocol errors, no breaker effect for queue/cancellation/local failures, open after three consecutive transient failures, reset on success, 30-second cooldown, exactly one half-open permit, and reopen/close outcomes. A malformed/empty success payload follows the transient threshold rather than immediate opening.

Constructing a new breaker (the process-restart case) must always start closed even when the persistent health row is error.

- [ ] **Step 2: Write scheduler tests before implementation**

Use events/barriers rather than timing sleeps. Cover peak `N`, independent services, shared service across workloads, `10N` total queue, `2N` actor queue, `8:2:1` dispatch, per-lane actor round robin, deadline expiry, pre-start cancellation, in-flight cancellation, breaker drain/fail-fast, retry/backoff inside one slot, submit/shutdown races, and exactly-once future completion.

```python
def test_same_service_workloads_share_one_peak():
    scheduler = ServiceScheduler("general", maximum=2)
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def invoke():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(2)
        finally:
            with lock:
                active -= 1

    def work_context(workload: str, actor: str) -> ModelWorkContext:
        return ModelWorkContext(
            actor_id=actor, workload_id=workload,
            priority=ModelPriority.INTERACTIVE, parent_id="test",
            support_id=f"mdl-{actor}", deadline_at=time.monotonic() + 30,
            cancel_event=None,
        )

    futures = [
        scheduler.submit(
            context=work_context(workload, actor=f"u{i}"), invoke=invoke,
        )
        for i, workload in enumerate(["ask_answer", "source_summary", "kg_extract"])
    ]
    assert two_started.wait(2)
    assert scheduler.snapshot().active == 2
    assert peak == 2
    release.set()
    assert [future.result(timeout=2) for future in futures] == [None, None, None]
    scheduler.shutdown()
```

- [ ] **Step 3: Run focused tests and confirm expected failures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_circuit_breaker.py backend/tests/test_model_scheduler.py
```

Expected: FAIL because the new scheduling modules are absent.

- [ ] **Step 4: Implement work context and typed safe errors**

Generate `mdl-` support ids with cryptographic URL-safe randomness. Actor defaults to `request_user_id()` or literal `system`. Fixed deadline constants are 30/300/1,800 seconds. An outer scope may override only priority/parent id; it never overrides service or capacity.

- [ ] **Step 5: Implement breaker transitions and classification**

Keep transitions under one lock. The breaker grants a closed/half-open admission token and receives one success/failure completion. Opening returns queued futures to the scheduler for failure outside the breaker lock.

- [ ] **Step 6: Implement weighted per-user dispatch**

Use three actor-to-deque lanes and this exact pattern:

```python
_DISPATCH_PATTERN = (
    *(ModelPriority.INTERACTIVE for _ in range(8)),
    *(ModelPriority.REPORT for _ in range(2)),
    ModelPriority.BACKGROUND,
)
```

Increment queue counters only after admission; decrement on cancellation, timeout, breaker drain, or dispatch. Never wait for queue capacity and never run provider code while holding queue/breaker locks.

- [ ] **Step 7: Run scheduler tests five times**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_circuit_breaker.py backend/tests/test_model_scheduler.py
```

Run the command five times. Expected: every run passes with no leaked dispatcher/executor thread.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/model_work.py \
  backend/app/services/model_circuit_breaker.py backend/app/services/model_scheduler.py \
  backend/tests/test_model_circuit_breaker.py backend/tests/test_model_scheduler.py
git commit -m "feat: add fair per-service model scheduler"
```

### Task 3: Create scheduled protocol adapters and own their lifecycle

**Files:**
- Modify: `backend/app/services/model_provider.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/startup_warmup.py`
- Modify: `backend/app/core/llm.py`
- Modify: `backend/app/core/llm_logging.py`
- Modify: `backend/app/services/embedding.py`
- Modify: `backend/app/services/rerank_client.py`
- Replace: `backend/tests/test_model_provider_runtime.py`
- Create: `backend/tests/test_scheduled_model_clients.py`
- Create: `backend/tests/model_testkit.py`
- Modify: `backend/tests/test_event_logging.py`
- Modify: `backend/tests/test_trackA_cache_backend.py`

**Interfaces:**

```python
class RuntimeModelProvider:
    def chat(self, workload_id: str) -> JsonChatClientPort: ...
    def embedding(self, workload_id: str) -> Embedder: ...
    def rerank(self, workload_id: str) -> RerankClientPort: ...
    def configured(self, workload_id: str) -> bool: ...
    def parallelism(self, workload_id: str) -> int: ...
    def probe(self, service_id: str, *, actor_id: str,
              allow_half_open: bool) -> ProviderObservation: ...
    def scheduler_snapshot(self, service_id: str) -> SchedulerSnapshot: ...
    def close(self) -> None: ...
```

- [ ] **Step 1: Write failing adapter and lifecycle tests**

Assert kind mismatches fail before traffic, unbound workloads return deterministic unconfigured adapters and `parallelism(workload) == 1`, bound workloads return their service's `max_concurrency`, workloads sharing a service share one scheduler/raw client, safe support metadata survives upstream failure, retries remain in one slot, rerank splits never hide parallel HTTP calls, shutdown rejects new work while draining active work, and startup/shutdown own the registry exactly once.

Assert startup rejects known multi-process launch settings (`WEB_CONCURRENCY` or `UVICORN_WORKERS` greater than one) with a credential-safe explanation that the scheduler is process-local. An unset value or the literal `1` remains valid.

Assert scheduler events include support id, workload/service ids, safe labels, actor/parent ids, queue/execution latency, retry outcome, and breaker transition, while excluding URL/key/prompt/response/vector/raw exception data. The opt-in `LLMInteractionLogger` keeps its existing bounded content policy and gains the same support id.

- [ ] **Step 2: Run tests and confirm failure against the per-user provider**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_scheduled_model_clients.py backend/tests/test_model_provider_runtime.py \
  backend/tests/test_startup_warmup.py backend/tests/test_llm_client.py \
  backend/tests/test_rerank_client.py backend/tests/test_trackA_cache_backend.py
```

- [ ] **Step 3: Construct one private raw client per named service**

Use registry URL/key/model and existing timeout/retry/token/cache policies. Add `max_connections` to `OpenAICompatibleClient` and derive it from the service capacity. Do not read user settings or create per-user client dictionaries.

Apply the same connection-pool derivation to embedding and rerank transports: their HTTP connection ceiling is the service `max_concurrency`, with no ask reserve or protocol-specific concurrency override. The scheduler, not pool blocking, remains the visible admission boundary.

```python
def _raw_chat(self, service: ModelServiceDefinition) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        self.settings, base_url=service.base_url, api_key=service.api_key,
        model=service.model, max_connections=service.max_concurrency,
    )
```

- [ ] **Step 4: Implement workload-bound wrappers**

Every wrapper call creates a fresh `ModelWorkContext`, inherits outer priority, takes chat cancellation from `cancel_event`, and schedules the whole raw retry/backoff call. Typed errors expose only service id/display name, workload id/label, safe model, stable code, and support id.

`parallelism(workload)` is only a producer-pool planning hint: it returns the bound service capacity or `1` for an unbound/offline workload. The service scheduler remains the sole enforcement authority.

`ScheduledJsonChatClient` verifies that the raw return is a non-empty JSON-object string before marking provider success; it returns the original string after validation. Empty content or malformed top-level JSON raises a transient `MalformedModelResponse`. Do not let the raw client silently turn empty content into `{}` before this classification.

Move rerank split orchestration out of raw `RerankClient`: each `_rerank_batch` is separately scheduled, then ordering is merged locally.

- [ ] **Step 5: Wire runtime ownership and shutdown**

`RepositoryRuntime` constructs the system provider. `SQLiteRepository.close()` delegates to it. Add `shutdown_repository_if_initialized()` in `api/deps.py`, checking `repository.cache_info().currsize` before calling `repository().close()`, and call it in `main._lifespan`'s `finally`.

Validate the known worker-count environment variables before constructing the runtime. `scripts/prod.sh` is pinned to one Uvicorn worker in Task 9; the startup validation protects alternate launch paths.

- [ ] **Step 6: Preserve test injection explicitly**

Production `SQLiteRepository` always constructs the system provider. Its constructor accepts an optional `model_provider` test dependency, passed into `RepositoryRuntime` before any service is composed. Create `backend/tests/model_testkit.py` with a `RecordingModelProvider` that maps exact workload ids to fake chat/embedding/rerank delegates and records calls; focused and later integration tests construct the repository with this provider.

Raw protocol tests inject delegate factories through `RuntimeModelProvider` itself. Remove mutable facade client setters, user-client caches, and role-based client resolution rather than preserving them as hidden compatibility paths.

- [ ] **Step 7: Run focused tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_scheduled_model_clients.py backend/tests/test_model_provider_runtime.py \
  backend/tests/test_startup_warmup.py backend/tests/test_llm_client.py \
  backend/tests/test_rerank_client.py backend/tests/test_trackA_cache_backend.py
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/model_provider.py backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py backend/app/api/deps.py backend/app/main.py \
  backend/app/services/startup_warmup.py backend/app/core/llm.py \
  backend/app/core/llm_logging.py backend/app/services/embedding.py \
  backend/app/services/rerank_client.py \
  backend/tests/test_model_provider_runtime.py backend/tests/test_scheduled_model_clients.py \
  backend/tests/model_testkit.py \
  backend/tests/test_startup_warmup.py backend/tests/test_llm_client.py \
  backend/tests/test_rerank_client.py backend/tests/test_event_logging.py \
  backend/tests/test_trackA_cache_backend.py
git commit -m "refactor: route model protocols through system provider"
```

### Task 4: Migrate every chat workload and support diagnostic to explicit scheduling

**Files:**
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/ask_service.py`
- Modify: `backend/app/services/reasoning_retrieval.py`
- Modify: `backend/app/services/graph_retrieval.py`
- Modify: `backend/app/services/report_engine.py`
- Modify: `backend/app/services/report_execution.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/concept_merge_review.py`
- Modify: `backend/app/services/schema_registry.py`
- Modify: `backend/app/services/knowledge_lifecycle.py`
- Modify: `backend/app/services/knowledge_governance.py`
- Modify: `backend/app/services/query_rewrite.py`
- Modify: `backend/app/services/kg/extract.py`
- Modify: `backend/app/services/kg/conflict_review.py`
- Modify: `backend/app/services/kg/graph_reason.py`
- Modify: `backend/app/services/kg/run_control.py`
- Modify: `backend/app/services/knowhow/api.py`
- Modify: `backend/app/services/memory_service.py`
- Modify: `backend/app/api/kg_routes.py`
- Modify: `backend/app/api/report_routes.py`
- Modify: `backend/app/api/source_routes.py`
- Modify: `backend/app/api/memory_routes.py`
- Modify: `backend/app/core/model_safety.py`
- Modify: `backend/app/models/ask.py`
- Modify: focused chat-path tests named below
- Modify: `backend/tests/test_memory_mcp.py`
- Modify: `backend/tests/test_followup_retrieval_grounding.py`

**Interfaces:**

```python
class ModelClientProviderPort(Protocol):
    def chat(self, workload_id: str) -> JsonChatClientPort: ...
    def configured(self, workload_id: str) -> bool: ...
    def parallelism(self, workload_id: str) -> int: ...
    def note_model_error(self, stage: str, error: Exception,
                         *, workload_id: str) -> None: ...

class ModelError(BaseModel):
    service_id: str
    service_name: str
    workload_id: str
    workload_label: str
    stage: str
    model: str
    message: str
    support_id: str
```

- [ ] **Step 1: Add failing workload-attribution tests**

Extend each domain's existing tests with a recording provider and assert the exact workload id. Add replay tests proving old persisted `ModelError` rows with `service`/`stage` deserialize safely while new failures include sanitized service/workload/model labels and support id.

Exercise MCP `ask_notebook` with the same recording provider and assert it reaches the normal Ask workload adapters; MCP is not an alternate raw-client or capacity path.

Also inject `ModelQueueFull`, `ModelQueueTimeout`, and `ModelServiceUnavailable`: Ask returns safe busy/unavailable metadata without saving a fabricated final answer; Deep Report, KG jobs, source enrichment, Memory preview, and Knowhow actions retain their existing observable failed/paused/retryable or deterministic-fallback semantics. No queue wait may occur inside a traced SQLite write transaction.

Use this exact mapping:

| Operations | Workload id |
|---|---|
| Ask final synthesis in every mode | `ask_answer` |
| Ask/reasoning planning and reflection | `reasoning_agent` |
| Follow-up, subquery, and report-fallback rewrites | `query_rewrite` |
| Ask evidence refinement and PPR candidate filtering | `evidence_refine` |
| Graph adversarial chain verification | `graph_chain_verify` |
| Report outline/STORM | `report_outline` |
| Report sufficiency judge | `report_sufficiency` |
| Report deep-dive section | `report_section` |
| Report executive summary | `report_summary` |
| Source summary | `source_summary` |
| Notebook name/description inference | `notebook_metadata` |
| Paper metadata | `paper_metadata` |
| KG first extraction/refine/glean | `kg_extract` / `kg_refine` / `kg_glean` |
| Merge review | `kg_merge_review` |
| Concept description/community summary/conflict review | `kg_concept_description` / `kg_community_summary` / `kg_conflict_review` |
| Object schema induction | `schema_induction` |
| Memory preview | `memory_preview` |
| Knowhow wording optimization/reformat | `knowhow_optimize` / `knowhow_reformat` |

- [ ] **Step 2: Run chat integration tests and confirm role-property failures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_ask_redesign.py backend/tests/test_reasoning_ask.py \
  backend/tests/test_model_errors.py backend/tests/test_report_engine.py \
  backend/tests/test_report_execution.py backend/tests/test_source_ingestion_failure_boundaries.py \
  backend/tests/test_paper_meta_service.py backend/tests/test_merge_review_job.py \
  backend/tests/test_schema_registry_service.py backend/tests/test_conflict_review.py \
  backend/tests/test_knowhow_optimize.py backend/tests/test_knowhow_reformat.py \
  backend/tests/test_memory_preview.py backend/tests/test_kg_run_control.py \
  backend/tests/test_memory_mcp.py backend/tests/test_followup_retrieval_grounding.py
```

Expected: FAIL until callers request explicit workloads.

- [ ] **Step 3: Replace role properties with workload-bound clients**

Resolve a client before spawning raw worker threads, but resolve by workload:

```python
answer_client = self.model_clients.chat("ask_answer")
reasoning_client = self.model_clients.chat("reasoning_agent")
kg_extract_client = self.model_clients.chat("kg_extract")
```

Helpers such as `expand_query` continue to accept an already-bound client; their caller owns the workload. Replace configuration checks with `provider.configured(workload_id)` or `client.configured`. New failures must not infer physical service identity from stage text.

Replace route-level readiness checks in KG, report, source, and Memory APIs with their exact workload binding. Routes must not consult retired repository properties such as `repo.llm_client`, `repo.kg_llm_client`, or `repo.reasoning_llm_client`.

- [ ] **Step 4: Establish report priority inheritance**

Wrap the detached report execution once so its reasoning/rewrite/embedding/rerank calls inherit report priority:

```python
with model_work_scope(priority=ModelPriority.REPORT, parent_id=report_id):
    engine.run(notebook_id, report_id, question, history, depth, auto_generate)
```

Ask uses interactive defaults; source/KG work uses background defaults. Preserve every existing `contextvars.copy_context()` boundary.

- [ ] **Step 5: Replace Ask error metadata with safe service/support fields**

`RuntimeModelProvider.note_model_error` extracts metadata only from typed scheduler/provider exceptions. Legacy local errors get empty service/support fields and `upstream_error`; Pydantic `before` validators map persisted old shapes to safe compatibility values without inventing a current service id.

- [ ] **Step 6: Run chat integration tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_ask_redesign.py backend/tests/test_reasoning_ask.py \
  backend/tests/test_model_errors.py backend/tests/test_report_engine.py \
  backend/tests/test_report_execution.py backend/tests/test_source_ingestion_failure_boundaries.py \
  backend/tests/test_paper_meta_service.py backend/tests/test_merge_review_job.py \
  backend/tests/test_schema_registry_service.py backend/tests/test_conflict_review.py \
  backend/tests/test_knowhow_optimize.py backend/tests/test_knowhow_reformat.py \
  backend/tests/test_memory_preview.py backend/tests/test_kg_run_control.py \
  backend/tests/test_memory_mcp.py backend/tests/test_followup_retrieval_grounding.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/repositories/ports.py backend/app/core/model_safety.py \
  backend/app/models/ask.py backend/app/services/ask_service.py \
  backend/app/services/reasoning_retrieval.py backend/app/services/graph_retrieval.py \
  backend/app/services/report_engine.py backend/app/services/report_execution.py \
  backend/app/services/source_ingestion.py backend/app/services/concept_merge_review.py \
  backend/app/services/schema_registry.py backend/app/services/knowledge_lifecycle.py \
  backend/app/services/knowledge_governance.py backend/app/services/query_rewrite.py \
  backend/app/services/kg/extract.py backend/app/services/kg/conflict_review.py \
  backend/app/services/kg/graph_reason.py backend/app/services/kg/run_control.py \
  backend/app/services/knowhow/api.py backend/app/services/memory_service.py \
  backend/app/api/kg_routes.py backend/app/api/report_routes.py \
  backend/app/api/source_routes.py backend/app/api/memory_routes.py
git add backend/tests/test_ask_redesign.py backend/tests/test_reasoning_ask.py \
  backend/tests/test_model_errors.py backend/tests/test_report_engine.py \
  backend/tests/test_report_execution.py backend/tests/test_source_ingestion_failure_boundaries.py \
  backend/tests/test_paper_meta_service.py backend/tests/test_merge_review_job.py \
  backend/tests/test_schema_registry_service.py backend/tests/test_conflict_review.py \
  backend/tests/test_knowhow_optimize.py backend/tests/test_knowhow_reformat.py \
  backend/tests/test_memory_preview.py backend/tests/test_kg_run_control.py \
  backend/tests/test_ask_service_boundary.py backend/tests/test_memory_mcp.py \
  backend/tests/test_followup_retrieval_grounding.py
git commit -m "refactor: bind chat calls to model workloads"
```

Verify `git diff --cached --name-only` contains only the listed chat-workload files before committing.

### Task 5: Migrate embedding/rerank traffic and remove independent capacity knobs

**Files:**
- Modify: `backend/app/services/source_embedding.py`
- Modify: `backend/app/services/retrieval_candidates.py`
- Modify: `backend/app/services/retrieval_service.py`
- Modify: `backend/app/services/memory_service.py`
- Modify: `backend/app/services/memory_retrieval.py`
- Modify: `backend/app/services/knowledge_query.py`
- Modify: `backend/app/services/knowhow/projection.py`
- Modify: `backend/app/services/rerank_client.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/kg/scheduler.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/batch_ingest.py`
- Modify: `scripts/batch_ingest.py`
- Modify: `scripts/backfill_kg_embeddings.py`
- Modify: `scripts/backfill_knowhow_md.py`
- Modify: `scripts/smoke_backend.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/tests/test_env_aliases.py`
- Delete: `backend/app/services/model_config.py`
- Delete: `backend/app/services/model_concurrency.py`
- Delete/replace: `backend/tests/test_model_concurrency.py`
- Delete: `backend/tests/test_kg_llm_client.py`
- Delete: `backend/tests/test_user_rerank_resolve.py`
- Modify: focused embedding/rerank/KG/batch tests named below
- Modify: `backend/tests/test_backfill_knowhow_md.py`

**Interfaces:**

```python
embedder = model_clients.embedding("retrieval_query_embedding")
reranker = model_clients.rerank("retrieval_rerank")
workers = model_clients.parallelism("source_element_embedding")
```

- [ ] **Step 1: Add failing peak and retired-config tests**

Prove every embedding method uses its exact workload, all bulk batches share the bound service peak, Ask query embedding and Memory recall are interactive, and rerank batches each consume a slot. Add Settings/CLI tests proving `KG_EXTRACT_WORKERS`, `EMBED_CONCURRENCY`, `KG_ASK_RESERVE`, `--llm-conc`, and `--embed-conc` no longer control or appear in product help.

Use this exact mapping:

| Operation | Workload id |
|---|---|
| Retrieval, graph, knowledge search, Memory recall query vectors | `retrieval_query_embedding` |
| Source elements | `source_element_embedding` |
| Source chunks | `chunk_embedding` |
| Knowhow chunks | `knowhow_embedding` |
| KG objects | `knowledge_object_embedding` |
| KG relations | `relation_embedding` |
| Memory persistence | `memory_embedding` |
| Retrieval document ordering | `retrieval_rerank` |

- [ ] **Step 2: Run focused tests and confirm failures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_embed_concurrency.py backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_source_embedding_service.py backend/tests/test_embedding.py \
  backend/tests/test_rerank_client.py backend/tests/test_memory_retrieval.py \
  backend/tests/test_memory_service.py backend/tests/test_knowhow_projection.py \
  backend/tests/test_batch_ingest.py backend/tests/test_kg_scheduler.py \
  backend/tests/test_parallel_extraction_wiring.py backend/tests/test_backfill_knowhow_md.py \
  backend/tests/test_env_aliases.py
```

- [ ] **Step 3: Make source embedding choose workload per method**

Change its collaborator from `Callable[[], Embedder]` to `Callable[[str], Embedder]`. Each method resolves the exact scheduled adapter. The producer pool uses `min(number_of_batches, provider.parallelism(workload))`, deriving throughput from the same service capacity rather than a second setting.

- [ ] **Step 4: Schedule query, Memory, Knowhow, and rerank calls**

Inject distinct workload-bound embedders into retrieval and Memory services. Keep `FakeEmbedder` only for an unbound/offline workload. Remove raw rerank split concurrency; `ScheduledRerankClient` schedules each raw batch independently and merges locally.

- [ ] **Step 5: Derive KG window planning from the configured model**

Replace `settings.kg_extract_workers` in KG scheduler/window planning with `provider.parallelism("kg_extract")`. `kg_job_concurrency` remains document/business orchestration and cannot bypass the model scheduler.

- [ ] **Step 6: Remove legacy gates and provider-capacity overrides**

Delete `activate_model_concurrency`, `current_model_concurrency`, `ConcurrencyGate`, `BoundedEmbeddingExecutor`, `LimitedJsonChatClient`, batch `_batch_concurrency_scope`, and provider-cap CLI flags. Remove the three retired capacity fields. Batch `--workers` remains for non-provider orchestration.

Move the still-valid raw rerank normalization/configuration assertions from `test_user_rerank_resolve.py` into `test_rerank_client.py`, then delete the per-user resolution test file.

Remove legacy endpoint fields/properties from `Settings`: `OPENAI_COMPAT_BASE_URL/API_KEY/MODEL`, `REASONING_LLM_BASE_URL/API_KEY/MODEL`, `REWRITE_LLM_BASE_URL/API_KEY/MODEL`, `KG_LLM_BASE_URL/API_KEY/MODEL`, `EMBED_PROVIDER/BASE_URL/API_KEY/MODEL`, and `RERANK_BASE_URL/API_KEY/MODEL/API_STYLE`. Keep timeout, retry, output-token, embedding-dimension/batch, rerank-document-limit, feature, and domain settings. Raw protocol tests supply explicit service values.

Move the remaining rerank protocol normalizer into `rerank_client.py` or `model_registry.py`, replace fingerprint access with typed scheduled exceptions, and delete `model_config.py`; no compatibility module may retain user/system fallback resolution.

Keep the non-model alias coverage in `test_env_aliases.py`, remove its retired KG/rewrite endpoint assertions, and delete the old KG-role fallback test file; extraction workload routing is covered in Task 4.

Make the Knowhow Markdown backfill's `--use-llm` path request `knowhow_reformat` from the system provider rather than resolving the notebook owner's model. Update the offline smoke settings to use `model_services_config=""`; do not pass retired endpoint fields to `Settings`.

- [ ] **Step 7: Run focused tests and CLI help**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_embed_concurrency.py backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_source_embedding_service.py backend/tests/test_embedding.py \
  backend/tests/test_rerank_client.py backend/tests/test_memory_retrieval.py \
  backend/tests/test_memory_service.py backend/tests/test_knowhow_projection.py \
  backend/tests/test_batch_ingest.py backend/tests/test_kg_scheduler.py \
  backend/tests/test_parallel_extraction_wiring.py backend/tests/test_backfill_knowhow_md.py \
  backend/tests/test_env_aliases.py
PYTHONPATH=backend ${PYTHON_BIN:-python3} scripts/batch_ingest.py --help
```

Expected: tests PASS; help contains `--workers` but not `--llm-conc` or `--embed-conc`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/source_embedding.py \
  backend/app/services/retrieval_candidates.py backend/app/services/retrieval_service.py \
  backend/app/services/memory_service.py backend/app/services/memory_retrieval.py \
  backend/app/services/knowledge_query.py backend/app/services/knowhow/projection.py \
  backend/app/services/rerank_client.py backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py backend/app/services/kg/scheduler.py \
  backend/app/services/source_ingestion.py backend/app/services/batch_ingest.py \
  scripts/batch_ingest.py scripts/backfill_kg_embeddings.py \
  scripts/backfill_knowhow_md.py scripts/smoke_backend.py backend/app/core/config.py
git add backend/tests/test_embed_concurrency.py \
  backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_source_embedding_service.py backend/tests/test_embedding.py \
  backend/tests/test_rerank_client.py \
  backend/tests/test_memory_retrieval.py backend/tests/test_memory_service.py \
  backend/tests/test_knowhow_projection.py backend/tests/test_batch_ingest.py \
  backend/tests/test_kg_scheduler.py backend/tests/test_parallel_extraction_wiring.py \
  backend/tests/test_backfill_knowhow_md.py backend/tests/test_env_aliases.py
git rm backend/app/services/model_concurrency.py backend/tests/test_model_concurrency.py
git rm backend/tests/test_kg_llm_client.py
git rm backend/tests/test_user_rerank_resolve.py
git rm backend/app/services/model_config.py
git commit -m "refactor: schedule embedding and rerank workloads"
```

Verify `git diff --cached --name-only` contains only the listed embedding/rerank files before committing.

### Task 6: Migrate to system-level health persistence and scrub user credentials

**Files:**
- Create: `backend/app/repositories/sqlite/model_status_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Refactor: `backend/app/services/model_status.py`
- Replace: `backend/app/models/model_services.py`
- Modify: `backend/tests/test_model_status_store.py`
- Modify: `backend/tests/test_model_status_service.py`
- Modify: `backend/tests/test_schema_version_migration.py`
- Modify: `backend/tests/test_legacy_db_compat.py`
- Modify: `backend/tests/test_auth_migration.py`
- Delete: `backend/tests/test_user_model_settings_store.py`
- Delete: `backend/tests/test_model_config_resolve.py`
- Delete: `backend/tests/test_user_llm_client_resolve.py`
- Delete: `backend/tests/test_reasoning_llm_config.py`
- Delete: `backend/tests/test_model_status_resolution.py`
- Modify: `backend/tests/test_ask_requires_model_config.py`
- Modify: `backend/tests/test_identity_store_component.py`
- Modify: `backend/tests/test_merge_dbs.py`
- Modify: `scripts/merge_dbs.py`
- Modify: `backend/tests/fixtures/schema_contract.txt`
- Modify: `backend/tests/fixtures/repository_v9/expected_snapshot.json`
- Modify: `backend/tests/fixtures/repository_v9/manifest.json`
- Modify: `backend/tests/test_repository_v9_fixture.py`
- Modify: `backend/tests/test_repository_snapshot_verifier.py`

**Interfaces:**

```python
class ModelStatusStorePort(Protocol):
    def get_all(self) -> dict[str, dict[str, object]]: ...
    def record(self, *, service_id: str, config_fingerprint: str,
               status: Literal["ok", "error"], latency_ms: int,
               code: str, trigger: str, support_id: str,
               checked_at: str) -> None: ...

class ModelStatusService:
    def snapshot(self) -> ModelServicesStatus: ...
    def test_one(self, service_id: str, *, actor_id: str) -> ModelServiceStatusItem: ...
    def test_all(self, *, actor_id: str) -> ModelServicesStatus: ...
    def record_provider_observation(self, observation: ProviderObservation) -> None: ...
```

- [ ] **Step 1: Write failing schema-v24 migration tests**

Seed multiple users with credential-looking settings and old status rows. After migration, assert every `user_profiles.model_settings` is `{}`, the old table is empty, and the new table matches:

```sql
CREATE TABLE system_model_service_status (
  service_id TEXT PRIMARY KEY,
  config_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
  latency_ms INTEGER NOT NULL DEFAULT 0,
  code TEXT NOT NULL DEFAULT '',
  trigger TEXT NOT NULL CHECK (
    trigger IN ('manual_test', 'observed_failure', 'recovery_probe')
  ),
  support_id TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL
);
```

- [ ] **Step 2: Write failing status-store/service tests**

Assert status is keyed only by service/fingerprint, stale fingerprints present as untested, ordering is monotonic, reads never probe, `busy/circuit_open/half_open` are live overlays only, and a provider failure updates the shared service regardless of workload/actor.

- [ ] **Step 3: Run tests and confirm per-user persistence fails**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_status_store.py backend/tests/test_model_status_service.py \
  backend/tests/test_schema_version_migration.py backend/tests/test_legacy_db_compat.py \
  backend/tests/test_auth_migration.py backend/tests/test_ask_requires_model_config.py \
  backend/tests/test_identity_store_component.py backend/tests/test_merge_dbs.py
```

- [ ] **Step 4: Implement migration 24 as an irreversible scrub**

Set `SCHEMA_VERSION = 24`. In the migration transaction, clear stored settings, delete old status rows, and create the system table. Retain the empty legacy column/table for upgrade simplicity; runtime code must not read or write them.

- [ ] **Step 5: Split status persistence out of identity**

Remove user model settings/resolution/status methods from `IdentityStore`, `IdentityRepository`, and facade compatibility properties. Add the focused system status store with offset-aware UTC validation and the existing timestamp/trigger tie-break semantics.

Delete tests whose sole contract is per-user model selection. Rewrite `test_ask_requires_model_config.py` to exercise an unbound system `ask_answer` workload, update identity component tests to assert no model methods, and make DB merge preserve only the primary database's system health rows (never import a secondary deployment's health state).

- [ ] **Step 6: Merge persisted observations with live state**

`snapshot()` iterates configured services, reads all stored rows once, and merges scheduler snapshots. Effective precedence is `half_open`, `circuit_open`, `busy`, persisted `error`, persisted `ok`, `untested`. Busy/circuit/half-open are never persisted.

- [ ] **Step 7: Connect observations without affecting results**

Provider success/failure/recovery observations carry service fingerprint and support id. Persistence failures are logged and swallowed after the result future is resolved; they never replace a model result/error.

Emit observations for provider failures and for a success that recovers a prior transient/open/half-open state; do not write SQLite on every ordinary successful call. Wire one runtime-owned `ModelStatusService(registry, provider, status_store)` and pass its observer to the provider/schedulers.

- [ ] **Step 8: Regenerate and review schema fixtures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} scripts/generate_repository_contract_fixtures.py
git diff -- backend/tests/fixtures/schema_contract.txt \
  backend/tests/fixtures/repository_v9/expected_snapshot.json \
  backend/tests/fixtures/repository_v9/manifest.json
```

- [ ] **Step 9: Run tests and commit**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_status_store.py backend/tests/test_model_status_service.py \
  backend/tests/test_schema_version_migration.py backend/tests/test_legacy_db_compat.py \
  backend/tests/test_auth_migration.py backend/tests/test_ask_requires_model_config.py \
  backend/tests/test_identity_store_component.py backend/tests/test_merge_dbs.py \
  backend/tests/test_repository_v9_fixture.py \
  backend/tests/test_repository_snapshot_verifier.py
git add backend/app/repositories/sqlite/model_status_store.py \
  backend/app/repositories/ports.py backend/app/repositories/sqlite/identity_store.py \
  backend/app/repositories/sqlite/migrations.py backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py backend/app/services/model_status.py \
  backend/app/models/model_services.py backend/tests/test_model_status_store.py \
  backend/tests/test_model_status_service.py backend/tests/test_schema_version_migration.py \
  backend/tests/test_legacy_db_compat.py backend/tests/test_auth_migration.py \
  backend/tests/test_ask_requires_model_config.py \
  backend/tests/test_identity_store_component.py backend/tests/test_merge_dbs.py \
  backend/tests/test_repository_v9_fixture.py \
  backend/tests/test_repository_snapshot_verifier.py scripts/merge_dbs.py \
  backend/tests/fixtures/schema_contract.txt \
  backend/tests/fixtures/repository_v9/expected_snapshot.json \
  backend/tests/fixtures/repository_v9/manifest.json
git rm backend/tests/test_user_model_settings_store.py \
  backend/tests/test_model_config_resolve.py backend/tests/test_user_llm_client_resolve.py \
  backend/tests/test_reasoning_llm_config.py backend/tests/test_model_status_resolution.py
git commit -m "feat: persist system model service health"
```

### Task 7: Replace personal settings APIs with sanitized status and administrator probes

**Files:**
- Modify: `backend/app/api/system_routes.py`
- Modify: `backend/app/api/admin_routes.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/models/schemas.py`
- Delete: `backend/tests/test_model_settings_api.py`
- Create: `backend/tests/test_system_model_services_api.py`
- Modify: `backend/tests/test_model_errors.py`
- Modify: `backend/tests/test_model_domain_boundaries.py`
- Modify: `backend/tests/test_route_domain_boundaries.py`

**API contract:**

```text
GET  /api/model-services/status
POST /api/admin/model-services/{service_id}/test
POST /api/admin/model-services/test-all
```

```python
class ModelWorkloadView(BaseModel):
    id: str
    label: str

class ModelServiceStatusItem(BaseModel):
    service_id: str
    display_name: str
    kind: Literal["chat", "embedding", "rerank"]
    model: str
    workloads: list[ModelWorkloadView]
    status: Literal[
        "untested", "ok", "busy", "error", "circuit_open", "half_open"
    ]
    active: int
    maximum: int
    queued: int
    oldest_wait_ms: int
    latency_ms: int
    checked_at: str
    trigger: str
    code: str
    support_id: str

class ModelServicesStatus(BaseModel):
    services: list[ModelServiceStatusItem]
```

- [ ] **Step 1: Replace settings-route tests with the new API contract**

Assert authentication on GET, identical sanitized GET output for admin and ordinary users, ordinary-user 403 for probes, admin single/all success, unknown service 404, probes consuming scheduler slots, queue-full probes returning safe busy status, half-open single probe, status reads causing zero provider traffic, and no URL/key/raw diagnostics in responses.

Assert OpenAPI contains none of:

```text
/api/me/model-settings
/api/me/model-settings/test
/api/me/model-services/status
/api/me/model-services/{service}/test
/api/me/model-services/test-all
```

Update `/api/health` tests so any configuration summary is derived from the system registry (for example whether `ask_answer` and `retrieval_query_embedding` are bound), never from retired Settings endpoint fields.

- [ ] **Step 2: Run API tests and confirm expected failures**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_system_model_services_api.py \
  backend/tests/test_model_settings_api.py \
  backend/tests/test_model_domain_boundaries.py \
  backend/tests/test_route_domain_boundaries.py
```

Expected: FAIL because personal routes still exist and probes are not admin-only.

- [ ] **Step 3: Remove personal settings and old per-user status routes**

Delete `_MODEL_ROLES`, `_mask_key`, settings GET/PUT/draft-test handlers, their schemas, and old status handlers. Do not leave deprecated 200/410 routes; they must disappear from OpenAPI.

Change `/api/health` to read registry binding availability without probing. Keep it a local health/readiness response and expose no service id, model, URL, credential, queue, or provider diagnostic.

- [ ] **Step 4: Add system read and admin probe handlers**

`GET` calls `ModelStatusService.snapshot()` only. Admin handlers require `user.role == "admin"`, pass `user.id` as fairness actor, and return typed sanitized data. Probes bypass caches but obey scheduler capacity and breaker state.

```python
@router.post(
    "/admin/model-services/{service_id}/test",
    response_model=ModelServiceStatusItem,
)
def test_system_model_service(
    service_id: str,
    user: UserProfile = Depends(get_current_user),
) -> ModelServiceStatusItem:
    if user.role != "admin":
        raise user_error(403, "仅管理员可测试模型服务")
    return model_status_service().test_one(service_id, actor_id=user.id)
```

- [ ] **Step 5: Run API/model-error tests**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_system_model_services_api.py backend/tests/test_model_errors.py \
  backend/tests/test_model_domain_boundaries.py backend/tests/test_route_domain_boundaries.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/system_routes.py backend/app/api/admin_routes.py \
  backend/app/api/deps.py backend/app/models/schemas.py \
  backend/tests/test_system_model_services_api.py backend/tests/test_model_errors.py \
  backend/tests/test_model_domain_boundaries.py backend/tests/test_route_domain_boundaries.py
git rm backend/tests/test_model_settings_api.py
git commit -m "feat: expose system model service status"
```

### Task 8: Replace the editable model page with a read-only status panel

**Files:**
- Create: `frontend/app/model-services.ts`
- Delete: `frontend/app/model-settings.ts`
- Modify: `frontend/app/model-service-panel.tsx`
- Modify: `frontend/app/model-service-orchestration.ts`
- Modify: `frontend/app/answer-panel.tsx`
- Modify: `frontend/app/workspace-model.ts`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/vocabulary.ts`
- Modify: `frontend/app/globals.css`
- Delete: `frontend/app/model-settings.test.mjs`
- Modify: `frontend/app/model-service-status.test.mjs`
- Modify: `frontend/app/model-service-orchestration.test.mjs`
- Modify: `frontend/app/model-service-panel.component.test.tsx`
- Modify: `frontend/app/model-error-panel.component.test.tsx`
- Modify: `frontend/app/errors-guard.test.mjs`
- Modify: `frontend/app/architecture-boundaries.test.mjs`

**Interfaces:**

```typescript
export type ModelServiceStatusItem = {
  service_id: string;
  display_name: string;
  kind: "chat" | "embedding" | "rerank";
  model: string;
  workloads: Array<{ id: string; label: string }>;
  status: "untested" | "ok" | "busy" | "error" | "circuit_open" | "half_open";
  active: number;
  maximum: number;
  queued: number;
  oldest_wait_ms: number;
  latency_ms: number;
  checked_at: string;
  trigger: string;
  code: string;
  support_id: string;
};

export type ModelServicesStatus = {
  services: ModelServiceStatusItem[];
};

export async function fetchModelServiceStatus(): Promise<ModelServicesStatus>;
export async function testSystemModelService(
  serviceId: string,
): Promise<ModelServiceStatusItem>;
export async function testAllSystemModelServices(): Promise<ModelServicesStatus>;
```

- [ ] **Step 1: Rewrite frontend tests before production code**

Assert dynamic service ids, safe labels, `active / maximum`, queued count, busy/circuit/half-open copy, copyable support id, highlighted failing service, focus trap/return focus, collection GET without provider probe, admin-only test buttons, no inputs/save/draft actions, and `查看模型状态` in Ask errors.

Add source-contract assertions forbidding:

```text
/me/model-settings
/me/model-settings/test
fetchModelSettings
saveModelSettings
testModelService
baseUrlDirty
keyDirty
测试未保存设置
编辑个人设置
```

- [ ] **Step 2: Run focused frontend tests and confirm failures**

```bash
cd frontend && node --test \
  app/model-service-status.test.mjs \
  app/model-service-orchestration.test.mjs \
  app/architecture-boundaries.test.mjs
cd frontend && npx vitest run \
  app/model-service-panel.component.test.tsx \
  app/model-error-panel.component.test.tsx
```

Expected: FAIL because the panel remains editable and uses old endpoints/fixed roles.

- [ ] **Step 3: Create the status-only client/model module**

Move summary/merge/failure helpers to `model-services.ts`, update endpoints, and validate untrusted response scalars. Friendly copy uses sanitized backend labels; missing labels fall back to `模型服务` and do not display raw ids as names.

- [ ] **Step 4: Rewrite `ModelServicePanel` as read-only**

Render one service card per backend row: status, check time/latency, workloads, capacity, queue, safe failure category, and support id. `isAdmin` controls only `测试服务`/`测试全部`. Preserve focus trap, Escape/backdrop close, highlighted-service focus, narrow-screen body scrolling, and return focus.

- [ ] **Step 5: Simplify page orchestration**

Delete form/draft/save states, refs, handlers, and coordinator branches. `openModelPanel(serviceId?)` opens immediately and refreshes status only. Derive `isAdmin` from `currentUser.role`; merge probe results by `service_id`.

```tsx
{modelPanelOpen && (
  <ModelServicePanel
    status={modelStatus}
    highlightedServiceId={highlightedModelServiceId}
    isAdmin={currentUser.role === "admin"}
    onTestOne={runSystemModelTest}
    onTestAll={runAllSystemModelTests}
    onClose={closeModelPanel}
    returnFocusTo={modelPanelReturnFocusRef.current}
  />
)}
```

- [ ] **Step 6: Update Ask errors and CSS**

Change `打开模型服务` to `查看模型状态`; focus `error.service_id`. Remove Base URL/model/key/draft/save styles while keeping status/focus/modal styles. Show the `支持编号：` label followed by the returned `support_id`, with a copy action when non-empty.

- [ ] **Step 7: Run frontend tests, lint, and build**

```bash
cd frontend && npm test
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add frontend/app/model-services.ts frontend/app/model-service-panel.tsx \
  frontend/app/model-service-orchestration.ts frontend/app/answer-panel.tsx \
  frontend/app/workspace-model.ts frontend/app/page.tsx frontend/app/vocabulary.ts \
  frontend/app/globals.css frontend/app/model-service-status.test.mjs \
  frontend/app/model-service-orchestration.test.mjs \
  frontend/app/model-service-panel.component.test.tsx \
  frontend/app/model-error-panel.component.test.tsx \
  frontend/app/errors-guard.test.mjs frontend/app/architecture-boundaries.test.mjs
git rm frontend/app/model-settings.ts frontend/app/model-settings.test.mjs
git commit -m "feat: make model service status read only"
```

### Task 9: Enforce architecture, update operations/docs, and verify the release

**Files:**
- Modify: `backend/tests/test_architecture_hardening.py`
- Modify: `backend/tests/test_architecture_module_boundaries.py`
- Modify: `backend/tests/test_test_architecture_policy.py`
- Modify: `backend/tests/test_repository_protocol_coverage.py`
- Modify: `backend/tests/test_repository_ports.py`
- Modify: `backend/tests/test_repository_facade_contract.py`
- Modify: `backend/tests/test_repository_runtime_identity.py`
- Modify: `backend/tests/test_repository_snapshot_verifier.py`
- Modify: `backend/tests/test_runtime_dim_bypasses.py`
- Modify: `backend/tests/test_strip_think_json.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Create: `backend/app/services/kg/json_utils.py`
- Modify: `backend/app/services/kg/client.py`
- Modify: `backend/app/services/kg/extract.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/eval/inference.py`
- Modify: `backend/app/eval/sa_calibration.py`
- Modify: `backend/app/eval/speed.py`
- Modify: `backend/app/eval/report.py`
- Modify: `backend/app/scripts/gen_recall_gold.py`
- Modify: `scripts/kg_product_smoke.py`
- Modify: `scripts/verify_repository_snapshot.py`
- Modify: `scripts/generate_repository_contract_fixtures.py`
- Modify: `backend/tests/fixtures/repository_contract/caller_boundaries.json`
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`
- Modify: `backend/tests/fixtures/repository_contract/facade_surface.json`
- Modify: `scripts/check.sh`
- Modify: `scripts/prod.sh`
- Modify: `scripts/pack.sh`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `fangan_done.md`
- Modify: `scripts/README.md`

- [ ] **Step 1: Add failing semantic architecture guards**

Allow raw transport construction/calls only in:

```text
backend/app/core/llm.py
backend/app/services/embedding_dashscope.py
backend/app/services/rerank_client.py
backend/app/services/model_provider.py
```

Enumerate `backend/app/services/kg/client.py` and its offline evaluation/gold callers separately; it must not be imported by product runtime. Move shared `safe_json` parsing into `backend/app/services/kg/json_utils.py`, then switch `source_ingestion.py`, `kg/extract.py`, and its parser test to the transport-free module.

Reject runtime uses of `OpenAICompatibleClient(`, `DashscopeEmbedder(`, `RerankClient(`, `.embeddings.create(`, `_rerank_batch(`, and unbound raw `.chat_json(`. Reject user model-setting symbols and retired concurrency gates. `model_status.py` is not a raw-transport boundary: manual tests call `RuntimeModelProvider.probe()` and therefore use the same scheduler.

- [ ] **Step 2: Run architecture tests and remove each discovered bypass**

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_architecture_hardening.py \
  backend/tests/test_architecture_module_boundaries.py \
  backend/tests/test_test_architecture_policy.py \
  backend/tests/test_repository_protocol_coverage.py \
  backend/tests/test_repository_ports.py
```

Expected before cleanup: FAIL naming remaining bypass paths. Move product calls behind the provider; allowlist only files not imported by product runtime.

Adapt offline callers deliberately: `eval/inference.py` and `gen_recall_gold.py` use an explicit `EVAL_JUDGE_`/`GOLDGEN_` client from the offline-only KG client; `eval/sa_calibration.py`, `eval/speed.py`, and `scripts/kg_product_smoke.py` request the relevant registered workload from the runtime provider. Remove the retired `KG_EXTRACT_WORKERS` advice from `eval/report.py`.

Migrate every remaining repository-fixture client assignment to `backend/tests/model_testkit.py`; do not restore production setters for test convenience. This scan must be empty except for explicit negative assertions proving the attributes are absent:

```bash
rg -n "(repo|repository|r)\.(llm_client|reasoning_llm_client|rewrite_llm_client|kg_llm_client|rerank_client|embedder)\s*=" \
  backend/tests
```

Rerun the five architecture tests from this step after the migration. Expected: PASS and no product-runtime raw client construction or generic repository-client assignment remains.

- [ ] **Step 3: Update scripts and repository fixtures**

Set `MODEL_SERVICES_CONFIG=""` in `scripts/check.sh`. Remove legacy model endpoint/capacity overrides from check/prod/batch scripts. Pin `scripts/prod.sh` to one Uvicorn worker. Package `model-services.example.toml`, never `.local/model-services.toml` or secrets. Update the repository snapshot verifier's expected scrubbed `model_settings` value and regenerate fixtures:

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} scripts/generate_repository_contract_fixtures.py
git diff -- backend/tests/fixtures/repository_contract
```

Run the repository fixture and offline-tool compatibility tests after regeneration:

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_repository_facade_contract.py \
  backend/tests/test_repository_runtime_identity.py \
  backend/tests/test_repository_snapshot_verifier.py \
  backend/tests/test_runtime_dim_bypasses.py backend/tests/test_strip_think_json.py
```

- [ ] **Step 4: Synchronize deployment/product documentation**

Both READMEs and `AGENTS.md` must document deployment TOML plus `.env` secrets; only `max_concurrency` as service capacity; one-process constraint; fixed scheduling/fairness/queue/breaker behavior; read-only status/admin probes; support-id escalation; removed personal routes/UI; irreversible v24 scrub; and empty-config offline fallback. Update `architecture.md` ownership/flow, `.env.example`, `scripts/README.md`, and factual `fangan_done.md` entries.

- [ ] **Step 5: Run placeholder and retired-symbol scans**

```bash
rg -n "TBD|FIXME|implement later|fill in details" \
  backend/app frontend/app README.md README_zh.md AGENTS.md architecture.md
rg -n "USER_MODEL_CONFIG_POLICY|/me/model-settings|fetchModelSettings|saveModelSettings|LimitedJsonChatClient|activate_model_concurrency|KG_EXTRACT_WORKERS|EMBED_CONCURRENCY|KG_ASK_RESERVE|--llm-conc|--embed-conc" \
  backend/app frontend/app scripts README.md README_zh.md AGENTS.md architecture.md .env.example
```

Expected: no product-runtime/documentation matches. Historical specs and migration tests may match only when explicitly testing legacy removal/scrubbing.

- [ ] **Step 6: Run complete offline verification**

```bash
scripts/check.sh
cd frontend && npm run build
```

Expected: both exit 0.

- [ ] **Step 7: Run bounded concurrency acceptance stress**

Use fake chat/embedding/rerank delegates with 20 caller threads, two workloads sharing an `N=3` service, and a separate `N=2` service. Assert peaks at 3 and 2, queues drain, every future resolves, and background starts under sustained interactive load.

```bash
PYTHONPATH=backend ${PYTHON_BIN:-python3} -m pytest -q -n0 \
  backend/tests/test_model_scheduler.py -k "stress or shared_peak or starvation"
```

- [ ] **Step 8: Review final scope and commit**

```bash
git status --short
git diff --check
git diff --stat
git add -u backend/tests
git add backend/tests/test_architecture_hardening.py \
  backend/tests/test_architecture_module_boundaries.py \
  backend/tests/test_test_architecture_policy.py \
  backend/tests/test_repository_protocol_coverage.py \
  backend/tests/test_repository_ports.py backend/tests/test_repository_facade_contract.py \
  backend/tests/test_repository_runtime_identity.py \
  backend/tests/test_repository_snapshot_verifier.py \
  backend/tests/test_runtime_dim_bypasses.py backend/tests/test_strip_think_json.py \
  backend/app/repositories/ownership_manifest.py backend/app/services/kg/json_utils.py \
  backend/app/services/kg/client.py backend/app/services/kg/extract.py \
  backend/app/services/source_ingestion.py backend/app/eval/inference.py \
  backend/app/eval/sa_calibration.py backend/app/eval/speed.py backend/app/eval/report.py \
  backend/app/scripts/gen_recall_gold.py scripts/kg_product_smoke.py \
  scripts/verify_repository_snapshot.py scripts/generate_repository_contract_fixtures.py \
  backend/tests/fixtures/repository_contract/caller_boundaries.json \
  backend/tests/fixtures/repository_contract/api_contract.json \
  backend/tests/fixtures/repository_contract/facade_surface.json \
  scripts/check.sh scripts/prod.sh scripts/pack.sh .env.example \
  model-services.example.toml README.md README_zh.md AGENTS.md architecture.md \
  fangan_done.md scripts/README.md
git diff --cached --name-only
git commit -m "chore: enforce system model scheduling boundaries"
```

Before committing, confirm staged paths contain only this feature and exclude unrelated PostgreSQL documents.

---

## Final acceptance checklist

- [ ] No authenticated user can read, write, or draft-test model credentials through API or UI.
- [ ] Migration v24 clears all `user_profiles.model_settings` values and old per-user status rows.
- [ ] Every physical service has one process-wide scheduler whose peak never exceeds `max_concurrency`.
- [ ] Every product-runtime chat, embedding, and rerank call has a registered workload id and passes architecture guards.
- [ ] Shared-service workloads share capacity; different service ids remain independent.
- [ ] Weighted priority, per-user round robin, `10N`/`2N` bounds, deadlines, cancellation, breaker drain, and half-open single-flight have deterministic tests.
- [ ] Users see only sanitized labels, aggregate capacity/queue state, safe failure codes, and support ids.
- [ ] Only administrators can run probes, and probes consume normal scheduler slots.
- [ ] Documentation, config examples, fixtures, scripts, and `fangan_done.md` agree with code.
- [ ] `scripts/check.sh` and `cd frontend && npm run build` pass offline.
