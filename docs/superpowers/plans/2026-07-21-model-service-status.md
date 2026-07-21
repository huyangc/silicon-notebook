# Model Service Status and Targeted Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Identify the actual effective model behind a user-visible failure, let the user test that saved service directly, and show persisted last-known model health on the collection page without probing providers on page load.

**Architecture:** Extend the existing model-config resolver so runtime and health views share one effective configuration. Persist sanitized per-user results in SQLite, expose authenticated read/test endpoints through a focused `ModelStatusService`, and carry stable service roles plus dynamic model names through `AskResponse.model_errors`. The front end keeps provider names as backend data, centralizes model-role vocabulary and status summaries, and wires the existing settings modal to Ask and collection status actions.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, stdlib `sqlite3`, pytest, TypeScript, React 19, Next.js 15, Node test runner, Vitest/Testing Library.

## Global Constraints

- A provider model name such as `deepseek-reasoner` is dynamic backend data and must never be hard-coded in `frontend/app` product copy.
- Collection/status reads must perform no upstream model call; only explicit test actions may probe providers.
- Never return API keys, provider payloads, endpoints, IP addresses, response bodies, or raw exception strings in model-status responses or tooltips.
- Keep existing draft-value `POST /api/me/model-settings/test` behavior intact.
- `embedding` is system-managed and read-only in this feature.
- Optional unconfigured rerank/embedding services do not mark the collection unhealthy.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together.
- Do not update `fangan_done.md`: no matching unfinished feature exists in `silicon_notebook_fangan.md`.
- Final verification requires both `scripts/check.sh` and `cd frontend && npm run build`.

---

### Task 1: One effective model-configuration resolver

**Files:**
- Modify: `backend/app/services/model_config.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`
- Modify: `backend/app/services/model_provider.py`
- Test: `backend/tests/test_user_llm_client_resolve.py`
- Test: `backend/tests/test_model_status_resolution.py`

**Interfaces:**
- Produces: `MODEL_SERVICE_ROLES`, `STATUS_SERVICE_ROLES`, `system_model_settings(settings)`, `resolve_effective_config(model_settings, role, policy, system_settings=None)`, `ResolvedModelConfig.kind`, `ResolvedModelConfig.configured`, and `model_config_fingerprint(config)`.
- Consumes: existing `Settings`, per-user `user_profiles.model_settings`, and the existing user-first/variant-fallback/policy rules.

- [ ] **Step 1: Write failing parity tests for dynamic names and fallbacks**

Create `backend/tests/test_model_status_resolution.py` with direct tests equivalent to:

```python
from app.core.config import Settings
from app.services.model_config import (
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)


def _settings(**overrides):
    values = {
        "openai_compat_base_url": "https://system.example/v1",
        "openai_compat_api_key": "system-secret",
        "openai_compat_model": "system-primary",
        "reasoning_llm_base_url": "https://reason.example/v1",
        "reasoning_llm_api_key": "reason-secret",
        "reasoning_llm_model": "reason-live",
        "rewrite_llm_model": "rewrite-live",
        "rerank_base_url": "https://rerank.example/v1",
        "rerank_api_key": "rerank-secret",
        "rerank_model": "rerank-live",
        "embed_provider": "dashscope",
        "embed_base_url": "https://embed.example/v1",
        "embed_api_key": "embed-secret",
        "embed_model": "embed-live",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_dedicated_reasoning_model_name_is_resolved_from_settings():
    settings = _settings(reasoning_llm_model="configured-at-runtime")
    config = resolve_effective_config(
        {}, "reasoning_llm", "fallback", system_model_settings(settings)
    )
    assert config.model == "configured-at-runtime"
    assert config.source == "system"
    assert config.kind == "llm"


def test_user_reasoning_role_falls_back_to_dynamic_user_primary():
    user = {
        "llm": {
            "base_url": "https://user.example/v1",
            "api_key": "user-secret",
            "model": "user-primary-live",
        }
    }
    config = resolve_effective_config(
        user, "reasoning_llm", "fallback", system_model_settings(_settings())
    )
    assert (config.model, config.source) == ("user-primary-live", "user")


def test_embedding_descriptor_is_system_managed():
    config = resolve_effective_config(
        {}, "embedding", "fallback", system_model_settings(_settings())
    )
    assert config.model == "embed-live"
    assert config.kind == "embedding"
    assert config.configured is True


def test_fingerprint_changes_when_credential_or_model_changes():
    settings = system_model_settings(_settings())
    first = resolve_effective_config({}, "llm", "fallback", settings)
    second = first.__class__(
        base_url=first.base_url,
        api_key="rotated-secret",
        model=first.model,
        source=first.source,
        kind=first.kind,
    )
    assert model_config_fingerprint(first) != model_config_fingerprint(second)
```

Extend `backend/tests/test_user_llm_client_resolve.py` to assert that the model returned by `identity.resolve_model_config(user, role).model` equals the model on the runtime client for primary, dedicated reasoning, and user-primary fallback cases.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
cd backend && pytest -q tests/test_model_status_resolution.py tests/test_user_llm_client_resolve.py
```

Expected: collection fails because `system_model_settings`, `kind`, `configured`, and `model_config_fingerprint` do not exist, or parity assertions show that system configs currently resolve to empty strings.

- [ ] **Step 3: Implement the shared resolver**

In `backend/app/services/model_config.py`, retain existing call compatibility and add these concrete shapes:

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

MODEL_SERVICE_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")
STATUS_SERVICE_ROLES = (*MODEL_SERVICE_ROLES, "embedding")
LLM_VARIANTS = ("reasoning_llm", "rewrite_llm", "kg_llm")


@dataclass(frozen=True)
class ResolvedModelConfig:
    base_url: str
    api_key: str
    model: str
    source: str
    kind: str = "llm"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def system_model_settings(settings) -> dict[str, dict[str, str]]:
    primary = {
        "base_url": settings.openai_compat_base_url,
        "api_key": settings.openai_compat_api_key,
        "model": settings.openai_compat_model,
        "source": "system",
        "kind": "llm",
    }
    reasoning = (
        {
            "base_url": settings.reasoning_llm_base_url,
            "api_key": settings.reasoning_llm_api_key,
            "model": settings.reasoning_llm_model,
            "source": "system",
            "kind": "llm",
        }
        if settings.reasoning_llm_configured else primary
    )
    rewrite = (
        {
            "base_url": settings.rewrite_llm_base_url or settings.openai_compat_base_url,
            "api_key": settings.rewrite_llm_api_key or settings.openai_compat_api_key,
            "model": settings.rewrite_llm_model,
            "source": "system",
            "kind": "llm",
        }
        if settings.rewrite_llm_configured else primary
    )
    kg = (
        {
            "base_url": settings.kg_llm_base_url,
            "api_key": settings.kg_llm_api_key,
            "model": settings.kg_llm_model,
            "source": "system",
            "kind": "llm",
        }
        if settings.kg_llm_configured else primary
    )
    return {
        "llm": primary,
        "reasoning_llm": reasoning,
        "rewrite_llm": rewrite,
        "kg_llm": kg,
        "rerank": {
            "base_url": settings.rerank_base_url,
            "api_key": settings.rerank_api_key,
            "model": settings.rerank_model,
            "source": "system",
            "kind": "rerank",
        },
        "embedding": {
            "base_url": settings.embed_base_url,
            "api_key": settings.embed_api_key,
            "model": settings.embed_model,
            "source": "system",
            "kind": "embedding",
        },
    }


def resolve_effective_config(
    model_settings: dict,
    role: str,
    policy: str,
    system_settings: Mapping[str, Mapping[str, str]] | None = None,
) -> ResolvedModelConfig:
    if role == "embedding":
        svc = dict((system_settings or {}).get(role) or {})
        return ResolvedModelConfig(
            svc.get("base_url", ""), svc.get("api_key", ""),
            svc.get("model", ""), svc.get("source", "system"), "embedding",
        )
    svc = (model_settings or {}).get(role) or {}
    if _full(svc):
        kind = "rerank" if role == "rerank" else "llm"
        return ResolvedModelConfig(
            svc["base_url"], svc["api_key"], svc["model"], "user", kind
        )
    if role in LLM_VARIANTS:
        primary = (model_settings or {}).get("llm") or {}
        if _full(primary):
            return ResolvedModelConfig(
                primary["base_url"], primary["api_key"],
                primary["model"], "user", "llm",
            )
    if policy == "required":
        return ResolvedModelConfig("", "", "", "none", "rerank" if role == "rerank" else "llm")
    system = dict((system_settings or {}).get(role) or {})
    return ResolvedModelConfig(
        system.get("base_url", ""), system.get("api_key", ""),
        system.get("model", ""), system.get("source", "system"),
        system.get("kind", "rerank" if role == "rerank" else "llm"),
    )


def model_config_fingerprint(config: ResolvedModelConfig) -> str:
    material = "\0".join(
        (config.kind, config.source, config.base_url, config.model, config.api_key)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
```

Keep `_full()` and `ModelNotConfiguredError` intact. Update `IdentityStore.resolve_model_config()` to pass `system_model_settings(self.settings)`. Keep `RuntimeModelProvider`'s current cached clients and fallback behavior, but make parity tests prove that the shared resolved descriptor reports the same model; do not introduce extra provider instances on status reads.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/app/services/model_config.py backend/app/repositories/sqlite/identity_store.py backend/app/services/model_provider.py backend/tests/test_model_status_resolution.py backend/tests/test_user_llm_client_resolve.py
git commit -m "refactor: unify effective model resolution"
```

---

### Task 2: Persistent per-user last-known model status

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/repositories/sqlite/identity_store.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/tests/test_sqlite_migrator_component.py`
- Modify: `backend/tests/test_legacy_db_compat.py`
- Modify: `backend/tests/fixtures/schema_contract.txt`
- Create: `backend/tests/test_model_status_store.py`

**Interfaces:**
- Produces: migration 23 table `model_service_status`; `IdentityStore.get_model_service_statuses(user_id)`, `record_model_service_status(user_id, service, config_fingerprint, status, latency_ms, code, trigger, checked_at)`, and `clear_model_service_statuses(user_id, services=None)`.
- Consumes: user ids, stable service roles, internal fingerprints, and sanitized result fields only.

- [ ] **Step 1: Write failing migration and store tests**

Create `backend/tests/test_model_status_store.py`:

```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/status.db",
        storage_dir=str(tmp_path / "storage"),
    ))


def test_migration_23_creates_cascading_latest_status_table(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(model_service_status)"
        )}
        assert columns == {
            "user_id", "service", "config_fingerprint", "status",
            "latency_ms", "code", "trigger", "checked_at",
        }


def test_status_store_upserts_and_clears_by_service(tmp_path):
    store = _repo(tmp_path)._runtime.identity
    store.record_model_service_status(
        "user-local", "llm", "fp-1", "error", 121,
        "upstream_error", "manual_test", "2030-01-01T00:00:00+00:00",
    )
    store.record_model_service_status(
        "user-local", "llm", "fp-1", "ok", 44,
        "", "manual_test", "2030-01-01T00:01:00+00:00",
    )
    assert store.get_model_service_statuses("user-local")["llm"] == {
        "config_fingerprint": "fp-1",
        "status": "ok",
        "latency_ms": 44,
        "code": "",
        "trigger": "manual_test",
        "checked_at": "2030-01-01T00:01:00+00:00",
    }
    store.clear_model_service_statuses("user-local", ["llm"])
    assert store.get_model_service_statuses("user-local") == {}
```

Update the schema version assertions from 22 to 23 and add a deployed-v22-upgrade assertion.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd backend && pytest -q tests/test_model_status_store.py tests/test_sqlite_migrator_component.py tests/test_legacy_db_compat.py
```

Expected: missing table/method failures and schema-version/golden mismatches.

- [ ] **Step 3: Add migration 23 and store methods**

Set `SCHEMA_VERSION = 23` and add:

```python
def _migration_23(self) -> None:
    with self._connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS model_service_status (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              service TEXT NOT NULL,
              config_fingerprint TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
              latency_ms INTEGER NOT NULL DEFAULT 0,
              code TEXT NOT NULL DEFAULT '',
              trigger TEXT NOT NULL CHECK (trigger IN ('manual_test', 'observed_failure')),
              checked_at TEXT NOT NULL,
              PRIMARY KEY (user_id, service)
            );
            CREATE INDEX IF NOT EXISTS idx_model_service_status_user_checked
              ON model_service_status(user_id, checked_at DESC);
            """
        )
```

Add the exact IdentityStore methods exercised above, using `database.connect()` for reads and `database.write()` for writes. `record_model_service_status` must use `INSERT ... ON CONFLICT(user_id, service) DO UPDATE`. `clear_model_service_statuses` must issue either one user-wide delete or an `IN (...)` delete with explicit placeholders; an empty service list is a no-op.

Add matching method signatures to `IdentityRepository` in `backend/app/repositories/ports.py`.

- [ ] **Step 4: Regenerate and inspect the intentional schema golden**

Run:

```bash
cd backend && UPDATE_SCHEMA_GOLDEN=1 pytest -q tests/test_legacy_db_compat.py -k fresh_schema_matches_committed_contract
git diff -- tests/fixtures/schema_contract.txt
```

Expected diff: only the new table/index and the version-driven schema additions described above.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/app/repositories/sqlite/migrations.py backend/app/repositories/sqlite/identity_store.py backend/app/repositories/ports.py backend/tests/test_model_status_store.py backend/tests/test_sqlite_migrator_component.py backend/tests/test_legacy_db_compat.py backend/tests/fixtures/schema_contract.txt
git commit -m "feat: persist latest model service status"
```

---

### Task 3: Sanitized status reads and explicit provider tests

**Files:**
- Create: `backend/app/services/model_status.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/api/routes.py`
- Create: `backend/tests/test_model_status_service.py`
- Modify: `backend/tests/test_model_settings_api.py`

**Interfaces:**
- Produces: `ModelServiceStatusItem`, `ModelServicesStatus`, `ModelStatusService.snapshot(user)`, `test_one(user, service)`, and `test_all(user)`.
- Produces endpoints: `GET /api/me/model-services/status`, `POST /api/me/model-services/{service}/test`, `POST /api/me/model-services/test-all`.
- Consumes: the resolver and IdentityStore methods from Tasks 1–2.

- [ ] **Step 1: Write failing service tests**

Create tests with fake probes so no network is used:

```python
def test_snapshot_never_probes_and_invalidates_fingerprint(identity, user, settings):
    calls = []
    service = ModelStatusService(identity, settings, probe=lambda cfg: calls.append(cfg))
    snapshot = service.snapshot(user)
    assert calls == []
    assert next(x for x in snapshot.services if x.service == "llm").status == "untested"


def test_test_one_returns_dynamic_model_and_sanitized_failure(identity, user, settings):
    def fail(_config):
        raise RuntimeError("provider 10.0.0.8 rejected secret payload")
    service = ModelStatusService(identity, settings, probe=fail)
    item = service.test_one(user, "reasoning_llm")
    assert item.model == settings.reasoning_llm_model
    assert item.status == "error"
    assert item.code == "upstream_error"
    assert "10.0.0.8" not in item.model_dump_json()
    assert "secret" not in item.model_dump_json()


def test_test_all_deduplicates_roles_sharing_one_effective_llm(identity, user, settings):
    calls = []
    service = ModelStatusService(
        identity, settings,
        probe=lambda config: calls.append((config.kind, config.model)),
    )
    result = service.test_all(user)
    expected_unique = {
        (item.config.kind, item.config.model)
        for item in service.descriptors(user)
        if item.config.configured
    }
    assert set(calls) == expected_unique
    assert {item.service for item in result.services} >= {
        "llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank", "embedding"
    }
```

The test fixture must use dynamic model values supplied through `Settings`; it must not assert a product constant.

- [ ] **Step 2: Run service tests and verify RED**

```bash
cd backend && pytest -q tests/test_model_status_service.py tests/test_model_settings_api.py
```

Expected: missing schemas, service, and routes.

- [ ] **Step 3: Add the sanitized response schemas**

Add to `backend/app/models/schemas.py`:

```python
class ModelServiceStatusItem(BaseModel):
    service: str
    model: str = ""
    source: str = "none"
    kind: str = "llm"
    configured: bool = False
    required: bool = False
    status: str = "unconfigured"
    latency_ms: int = 0
    checked_at: str = ""
    trigger: str = ""
    code: str = ""


class ModelServicesStatus(BaseModel):
    services: List[ModelServiceStatusItem] = Field(default_factory=list)
```

Do not add an exception/detail field.

- [ ] **Step 4: Implement `ModelStatusService`**

Implement a focused service whose public signatures are
`__init__(identity, settings, probe=None)`,
`descriptors(user) -> list[ServiceDescriptor]`,
`snapshot(user) -> ModelServicesStatus`,
`test_one(user, service: str) -> ModelServiceStatusItem`,
`test_all(user) -> ModelServicesStatus`, and
`record_observed_failure(user, service: str) -> None`.

`snapshot()` only combines descriptors with stored rows. It maps fingerprint mismatches to `untested` and missing effective config to `unconfigured`. `required` is true only for `llm`; configured optional services still participate in “needs test”.

The default probe must:

```python
def _probe(self, config: ResolvedModelConfig) -> None:
    if config.kind == "llm":
        OpenAICompatibleClient(
            self.settings,
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
        ).chat_json(
            [{"role": "user", "content": "ping"}],
            "{}", timeout=10, max_retries=0, bypass_cache=True,
        )
        return
    if config.kind == "rerank":
        RerankClient(
            self.settings,
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
        )._rerank_batch("ping", ["a", "b"])
        return
    if config.kind == "embedding":
        make_embedder(self.settings).embed_query("ping")
        return
    raise ValueError("unsupported model service kind")
```

Time each call with `time.perf_counter()`. Catch all provider exceptions, log the raw diagnostic through the existing event logger/caller logger, persist only `code="upstream_error"`, and return no raw text. `test_all()` groups configured descriptors by `model_config_fingerprint(config)`, runs at most four worker threads, and fans one result out to all roles sharing that fingerprint. Unconfigured roles perform no probe.

- [ ] **Step 5: Add authenticated routes while preserving the draft test**

Add route functions using `identity_repository()` and `get_settings()`:

```python
def _model_status_service() -> ModelStatusService:
    return ModelStatusService(identity_repository(), get_settings())


@router.get("/me/model-services/status", response_model=ModelServicesStatus)
def get_model_services_status(user: UserProfile = Depends(get_current_user)):
    return _model_status_service().snapshot(user)


@router.post("/me/model-services/test-all", response_model=ModelServicesStatus)
def test_all_model_services(user: UserProfile = Depends(get_current_user)):
    return _model_status_service().test_all(user)


@router.post("/me/model-services/{service}/test", response_model=ModelServiceStatusItem)
def test_current_model_service(service: str, user: UserProfile = Depends(get_current_user)):
    if service not in STATUS_SERVICE_ROLES:
        raise HTTPException(status_code=404, detail="unknown model service")
    return _model_status_service().test_one(user, service)
```

Declare `/test-all` before the dynamic `{service}` route. In `put_model_settings`, clear statuses for the changed role and every variant that may inherit from `llm`; this makes the next snapshot return `untested`.

- [ ] **Step 6: Add API contract tests and verify GREEN**

Extend `backend/tests/test_model_settings_api.py` to assert authentication, all six dynamic rows, no secrets/raw errors, no probe on GET, successful/failed current tests, invalid service 404, and status invalidation after PUT. Run the Step 2 command and expect all tests to pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add backend/app/services/model_status.py backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_model_status_service.py backend/tests/test_model_settings_api.py
git commit -m "feat: expose explicit model service health tests"
```

---

### Task 4: Attach dynamic service identity to real Ask failures

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/services/model_provider.py`
- Modify: `backend/app/services/ask_service.py`
- Modify: `backend/app/services/retrieval_candidates.py`
- Modify: `backend/app/services/graph_retrieval.py`
- Modify: `backend/tests/test_model_errors.py`
- Modify: `backend/tests/test_reasoning_empty_answer.py`

**Interfaces:**
- Produces: `ModelError.service`; `RuntimeModelProvider.note_model_error(stage, model, error, service="")`.
- Consumes: `ModelStatusService.record_observed_failure` and the request-local Ask error sink.

- [ ] **Step 1: Write failing Ask model-error tests**

Extend `backend/tests/test_model_errors.py`:

```python
def test_answer_failure_names_dynamic_primary_service_and_persists_error(repo):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    raising = _RaisingLLM()
    raising.model = "runtime-primary-name"
    repo.llm_client = raising
    nb = _seed_chunks(repo)
    response = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    error = next(item for item in response.model_errors if item.stage == "answer")
    assert (error.service, error.model) == ("llm", "runtime-primary-name")
    stored = repo._runtime.identity.get_model_service_statuses("user-local")
    assert stored["llm"]["status"] == "error"
    assert stored["llm"]["trigger"] == "observed_failure"


def test_reasoning_answer_failure_is_reasoning_service(arepo):
    stub = _StubLLM(answers=["{}"])
    stub.model = "runtime-reasoning-name"
    arepo._reasoning_llm_client = stub
    nb = _seed(arepo)
    response = arepo.ask(
        nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning")
    )
    error = next(item for item in response.model_errors if item.stage == "answer")
    assert (error.service, error.model) == (
        "reasoning_llm", "runtime-reasoning-name"
    )


def test_embedding_failure_is_embedding_service(repo, monkeypatch):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    repo.settings.embed_provider = "dashscope"
    repo.settings.embed_base_url = "http://fake"
    repo.settings.embed_api_key = "k"
    repo.settings.embed_model = "runtime-embedding-name"
    monkeypatch.setattr(
        repo.embedder,
        "embed_query",
        lambda _query: (_ for _ in ()).throw(RuntimeError("embed boom")),
    )
    repo.llm_client = _AnswerLLM()
    nb = _seed_chunks(repo)
    response = repo.ask_chunk(
        nb.id, AskRequest(question="cascode", mode="chunk")
    )
    error = next(item for item in response.model_errors if item.stage == "embed")
    assert (error.service, error.model) == (
        "embedding", "runtime-embedding-name"
    )
```

Place the primary and embedding tests in `test_model_errors.py`. Place the reasoning test in `test_reasoning_empty_answer.py`, where `_StubLLM`, `_seed`, and `arepo` already exist. Do not introduce network mocks.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd backend && pytest -q tests/test_model_errors.py tests/test_reasoning_empty_answer.py
```

Expected: `ModelError` has no `service`, and no observed status is persisted.

- [ ] **Step 3: Carry explicit service roles through model errors**

Change the schema to:

```python
class ModelError(BaseModel):
    service: str = "llm"
    stage: str
    model: str = ""
    message: str
```

Change `_answer_with_retry(self, synth, model_label, service="llm")` and pass `service="reasoning_llm"` only in `ask_reasoning`; chunk and graph answer synthesis remain `llm` because those existing branches call `llm_client`. Pass `service="rerank"` at rerank callbacks and `service="embedding"` from embedding/ANN failure sites. For legacy three-argument callers, `note_model_error` must map stage prefixes conservatively:

```python
def _service_for_stage(stage: str) -> str:
    value = (stage or "").lower()
    if "rerank" in value:
        return "rerank"
    if "embed" in value or "ann" in value:
        return "embedding"
    if "rewrite" in value:
        return "rewrite_llm"
    if value.startswith("kg_"):
        return "kg_llm"
    return "llm"
```

In `RuntimeModelProvider.note_model_error`, retain raw event logging. Only when the Ask sink exists, append `{service, stage, model, message}` and call `record_observed_failure` inside a best-effort `try/except` so status persistence can never break the answer path. Resolve the model name from the actual client at each Ask call (`getattr(client, "model", "")`), not from a front-end or provider-name registry.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Also run:

```bash
cd backend && pytest -q tests/test_ask_repository_golden.py tests/test_ask_requires_model_config.py
```

If repository goldens serialize `model_errors`, regenerate only the affected expected entries after inspecting that the sole contract change is the added stable `service` field.

- [ ] **Step 5: Commit Task 4**

```bash
git add backend/app/models/schemas.py backend/app/services/model_provider.py backend/app/services/ask_service.py backend/app/services/retrieval_candidates.py backend/app/services/graph_retrieval.py backend/tests/test_model_errors.py backend/tests/test_reasoning_empty_answer.py backend/tests/fixtures/repository_v9/expected_snapshot.json backend/tests/fixtures/repository_contract/ask_responses.json
git commit -m "feat: identify effective models in ask failures"
```

Only add fixture files that actually changed.

---

### Task 5: Front-end model-status client and pure presentation rules

**Files:**
- Modify: `frontend/app/model-settings.ts`
- Modify: `frontend/app/workspace-model.ts`
- Modify: `frontend/app/vocabulary.ts`
- Modify: `frontend/app/vocabulary.test.mjs`
- Modify: `frontend/app/model-settings.test.mjs`
- Create: `frontend/app/model-service-status.test.mjs`

**Interfaces:**
- Produces: `STATUS_MODEL_ROLES`, `MODEL_ROLE_LABELS`, `ModelServiceStatusItem`, `ModelServicesStatus`, fetch/test functions, `summarizeModelServices`, `modelFailureText`, and `mergeModelServiceStatus`.
- Consumes: the Task 3 API and Task 4 `ModelError.service/model` fields.

- [ ] **Step 1: Write failing pure-function and client tests**

Add tests equivalent to:

```javascript
test("model failure text uses the backend model name dynamically", () => {
  assert.equal(
    modelFailureText({ service: "reasoning_llm", model: "runtime-name-A" }),
    "推理模型 runtime-name-A 调用失败，本次回答可能不完整。",
  );
  assert.equal(
    modelFailureText({ service: "reasoning_llm", model: "runtime-name-B" }),
    "推理模型 runtime-name-B 调用失败，本次回答可能不完整。",
  );
});

test("collection summary does not treat optional unconfigured models as broken", () => {
  assert.deepEqual(
    summarizeModelServices([
      status("llm", "ok"),
      status("rerank", "unconfigured", { configured: false, required: false }),
      status("embedding", "unconfigured", { configured: false, required: false }),
    ]),
    { text: "服务正常", tone: "ok", abnormal: [] },
  );
});

test("configured untested and failed services have distinct summaries", () => {
  assert.equal(summarizeModelServices([status("llm", "untested")]).text,
    "API 正常 · 模型待测试");
  assert.equal(summarizeModelServices([status("llm", "error")]).text,
    "API 正常 · 1 个模型异常");
});

```

The two-input behavior test above is the automated guard that model names come
from response data. Do not add a front-end test that reads production source;
the repository's test-governance contract forbids direct production-source
layout scans. Add fetch tests that assert GET status has no request body and no
test endpoint is called, while individual/all tests use POST and authenticated
headers.

- [ ] **Step 2: Run tests and verify RED**

```bash
cd frontend && node --test app/model-settings.test.mjs app/model-service-status.test.mjs app/vocabulary.test.mjs
```

Expected: missing types/functions and the old role labels remain local to `page.tsx`.

- [ ] **Step 3: Implement types, vocabulary, clients, and pure reducers**

In `model-settings.ts`, export:

```typescript
export const MODEL_ROLES = ["llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"] as const;
export const STATUS_MODEL_ROLES = [...MODEL_ROLES, "embedding"] as const;
export type ModelRole = (typeof MODEL_ROLES)[number];
export type StatusModelRole = (typeof STATUS_MODEL_ROLES)[number];

export const MODEL_ROLE_LABELS: Record<StatusModelRole, string> = {
  llm: "主模型",
  reasoning_llm: "推理模型",
  rewrite_llm: "改写模型",
  kg_llm: "构图模型",
  rerank: "重排模型",
  embedding: "嵌入模型",
};

export type ModelServiceStatusItem = {
  service: StatusModelRole;
  model: string;
  source: "user" | "system" | "none";
  kind: "llm" | "rerank" | "embedding";
  configured: boolean;
  required: boolean;
  status: "ok" | "error" | "untested" | "unconfigured";
  latency_ms: number;
  checked_at: string;
  trigger: "manual_test" | "observed_failure" | "";
  code: string;
};

export type ModelServicesStatus = { services: ModelServiceStatusItem[] };
```

Add `fetchModelServiceStatus()`, `testCurrentModelService(service)`, and `testAllCurrentModelServices()` using the same safe HTTP-error path as existing model settings.

Implement `summarizeModelServices` with precedence: failed or required-unconfigured → bad/count; configured-untested → warn/pending; otherwise ok. Implement `modelFailureText` from `MODEL_ROLE_LABELS[service]` plus the runtime `model`; when model is blank return `${role}尚未配置，本次回答可能不完整。`. Implement `mergeModelServiceStatus` as an immutable replacement by stable service role.

Update `AskResponse.model_errors` in `workspace-model.ts` to include `service: StatusModelRole`. Add stable code labels to `vocabulary.ts`; keep raw diagnostics out of copy.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected Node tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add frontend/app/model-settings.ts frontend/app/workspace-model.ts frontend/app/vocabulary.ts frontend/app/vocabulary.test.mjs frontend/app/model-settings.test.mjs frontend/app/model-service-status.test.mjs
git commit -m "feat: model service status client and summaries"
```

---

### Task 6: Ask diagnostic actions, collection summary, and settings status UI

**Files:**
- Modify: `frontend/app/answer-panel.tsx`
- Modify: `frontend/app/page.tsx`
- Create: `frontend/app/model-service-panel.tsx`
- Modify: `frontend/app/globals.css`
- Create: `frontend/app/model-error-panel.component.test.tsx`
- Create: `frontend/app/model-service-panel.component.test.tsx`
- Modify: `frontend/app/answer-memory.component.test.tsx`

**Interfaces:**
- Consumes: Task 5 client and pure rules.
- Produces: per-error `测试此模型`/`打开模型服务`, clickable collection status, saved/effective status rows, read-only embedding row, and all-model test action.

- [ ] **Step 1: Write failing component tests for dynamic Ask failures**

Render `AnswerView` with two model errors and assert:

```tsx
expect(screen.getByText("推理模型 runtime-reasoner 调用失败，本次回答可能不完整。"))
  .toBeInTheDocument();
expect(screen.getByText("嵌入模型 runtime-embed 调用失败，本次回答可能不完整。"))
  .toBeInTheDocument();
await user.click(screen.getAllByRole("button", { name: "测试此模型" })[0]);
expect(onTestModel).toHaveBeenCalledWith("reasoning_llm");
await user.click(screen.getAllByRole("button", { name: "打开模型服务" })[1]);
expect(onOpenModelSettings).toHaveBeenCalledWith("embedding");
```

Also assert duplicate `(service, model)` errors render once, raw `message` text is absent from DOM/title, and a test button transitions through `测试中…` to `正常 42ms` or the stable failure label.

- [ ] **Step 2: Run the Ask component test and verify RED**

```bash
cd frontend && npx vitest run app/model-error-panel.component.test.tsx app/answer-memory.component.test.tsx
```

Expected: the generic banner is still rendered and AnswerView lacks callbacks.

- [ ] **Step 3: Implement the structured Ask error panel**

Add optional AnswerView props so existing isolated consumers stay source-compatible:

```typescript
onTestModel?: (service: StatusModelRole) => Promise<ModelServiceStatusItem>;
onOpenModelSettings?: (service: StatusModelRole) => void;
```

Extract an internal `ModelErrorPanel` in `answer-panel.tsx` that deduplicates errors, logs every raw `message` through `logDiagnostic`, renders `modelFailureText(error)`, guards duplicate tests per service, and calls the two props. Do not put raw messages in `title` or visible nodes.

Pass callbacks from `page.tsx`. Because the new props are optional, keep `answer-memory.component.test.tsx` unchanged and run it as a compatibility regression.

- [ ] **Step 4: Write failing collection/settings integration component tests**

Create focused exported `ModelServicePanel` and `ModelServiceSummaryButton` components in `frontend/app/model-service-panel.tsx` rather than attempting to render all of `page.tsx`. The summary button receives already-computed text/tone/title and an `onOpen` callback; it performs no fetch. Assert:

- statuses display dynamic backend model names and last-checked time;
- embedding inputs do not exist and its admin guidance is visible;
- `测试当前使用的全部模型` calls the all-test callback once and locks while running;
- an affected role passed at open receives a highlight class/accessible marker;
- the collection summary is a button that opens the panel;
- initial status rendering makes no POST/test call.

- [ ] **Step 5: Run the panel tests and verify RED**

```bash
cd frontend && npx vitest run app/model-service-panel.component.test.tsx
```

Expected: component/controls are missing.

- [ ] **Step 6: Implement page state and UI wiring**

In `page.tsx` add:

```typescript
const [modelStatus, setModelStatus] = useState<ModelServicesStatus | null>(null);
const [modelStatusUnavailable, setModelStatusUnavailable] = useState(false);
const [highlightedModelRole, setHighlightedModelRole] = useState<StatusModelRole | null>(null);
```

Change collection loading so `/health` and `/notebooks` remain required, while `fetchModelServiceStatus()` is best-effort and never blocks notebook rendering. It must call only the GET status endpoint. Derive status text/tone from `summarizeModelServices`; if status GET fails, show `API 正常 · 模型状态未知`.

Make the top-bar status an accessible button. `openModelPanel(role?)` fetches settings and the local status snapshot, records the highlight, and scrolls/focuses the role only after the modal mounts. `runCurrentModelTest` and `runAllCurrentModelTests` merge returned rows into state immediately. After `saveModelPanel`, refetch the status snapshot so invalidated roles display `待测试`.

Extract the existing inline modal into `frontend/app/model-service-panel.tsx`; keep `page.tsx` as the workspace orchestrator. The component receives forms/status/callback props and performs no fetch itself.

- [ ] **Step 7: Add scoped styling**

In `globals.css`, add button-reset styles for the clickable `.status`, `.model-service-status-row`, status tones, error-list actions, highlighted role, all-test action, and read-only embedding guidance. Preserve the existing compact header at desktop widths and ensure buttons wrap on narrow viewports.

- [ ] **Step 8: Run focused tests, TypeScript, and verify GREEN**

```bash
cd frontend && npx vitest run app/model-error-panel.component.test.tsx app/model-service-panel.component.test.tsx app/answer-memory.component.test.tsx
npm run lint
```

Expected: component tests and TypeScript pass without warnings.

- [ ] **Step 9: Commit Task 6**

```bash
git add frontend/app/answer-panel.tsx frontend/app/page.tsx frontend/app/model-service-panel.tsx frontend/app/globals.css frontend/app/model-error-panel.component.test.tsx frontend/app/model-service-panel.component.test.tsx frontend/app/answer-memory.component.test.tsx
git commit -m "feat: show and test failing models in the ui"
```

`model-service-panel.tsx` is required by Step 4 and must be included in this commit.

---

### Task 7: Documentation, contract sync, and complete verification

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Test: all repository checks

**Interfaces:**
- Documents: dynamic effective model identity, manual/persisted testing, collection behavior, embedding read-only status, and privacy/no-auto-probe rules.

- [ ] **Step 1: Update all three documentation contracts together**

Add matching English/Chinese README sections stating:

- Ask errors identify the affected role and backend-resolved current model;
- model names are never front-end constants;
- the collection reads last-known state and never auto-probes providers;
- users can test one or all current effective services;
- embedding status is system-managed/read-only;
- raw upstream diagnostics remain logs-only.

Add the same durable constraints to the Model Services/Product Flow area of `AGENTS.md`.

- [ ] **Step 2: Run documentation/source guards**

```bash
rg -n "deepseek-reasoner" frontend/app
git diff --check
```

Expected: no production source match for the example model name; `git diff --check` emits no output. Test fixtures may use arbitrary runtime names such as `runtime-reasoner`.

- [ ] **Step 3: Run focused backend and frontend suites**

```bash
cd backend && pytest -q tests/test_model_status_resolution.py tests/test_model_status_store.py tests/test_model_status_service.py tests/test_model_settings_api.py tests/test_model_errors.py tests/test_reasoning_empty_answer.py
cd frontend && node --test app/model-settings.test.mjs app/model-service-status.test.mjs app/vocabulary.test.mjs
cd frontend && npx vitest run app/model-error-panel.component.test.tsx app/model-service-panel.component.test.tsx app/answer-memory.component.test.tsx
```

Expected: all selected tests pass.

- [ ] **Step 4: Run the repository full gate**

```bash
scripts/check.sh
```

Expected: exit code 0 with backend, frontend, contract, smoke, and static-boundary checks green.

- [ ] **Step 5: Run the production frontend build**

```bash
cd frontend && npm run build
```

Expected: Next.js production build exits 0.

- [ ] **Step 6: Inspect final scope and status**

```bash
git status --short
git diff --stat HEAD~7..HEAD
```

Expected: only model-service status/diagnostic implementation, tests, schema contract, and the three synchronized docs are changed. Confirm no API keys, raw provider errors, generated `.next` artifacts, local databases, or logs are staged.

- [ ] **Step 7: Commit Task 7**

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: document model service health diagnostics"
```

Commit documentation only at this step. Any legitimate test/fixture correction discovered by the full gate must first be handled as a separate RED/GREEN correction in its owning task, then the full gate must be rerun before this documentation commit.
