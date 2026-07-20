# KG Extraction Task Circuit Breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop only the current notebook's KG task when its model service stays unavailable, preserve completed source graphs, persist an accurate terminal status, and let the user continue unfinished sources.

**Architecture:** A schema-v20 `kg_build_jobs` store persists one active job per notebook. A task-local run control and KG-client wrapper own bounded KG retries and open a circuit on classified model failures; the lifecycle coordinator cancels and drains only that job's queued work. Existing notebook and index-status projections expose the latest job to a shared frontend status helper.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, SQLite (`sqlite3`), OpenAI Python SDK, `concurrent.futures`, React 19, Next.js 15, TypeScript, Node test runner.

## Global Constraints

- The circuit scope is one `kg_build_jobs.id`; it must never be keyed only by endpoint, model, user, or process.
- Keep source-level commits that finished before the circuit opened; never store a partial graph for the interrupted source.
- A failed rebuild resumes through the incremental endpoint and must not delete the completed subset again.
- `KG_LLM_TIMEOUT_SECONDS` defaults to `60`; `KG_LLM_MAX_RETRIES` defaults to `2` and validates in `0..3`.
- HTTP connection errors, timeouts, 429, and 5xx are transient; 401/403 and incompatible model/request failures are immediate task failures.
- Malformed model JSON, empty extraction output, and evidence-grounding misses remain soft window outcomes.
- Preserve `kg_building`, `kg_ready`, `kg_pending_sources`, existing POST response fields, synchronous repository/CLI entry points, request-context propagation, and the two global KG executor caps.
- API output exposes reviewed `user_message`; raw provider exceptions remain in existing logs and must not reach the frontend.
- Schema work is `_migration_20` with `SCHEMA_VERSION = 20`; do not edit migrations 1–19 or the frozen v9 baseline database.
- Product behavior, setup, architecture, and constraints must be synchronized across `README.md`, `README_zh.md`, and `AGENTS.md`; update `fangan_done.md` only after verification.
- User-facing backend changes must ship with their frontend surface in the same change.
- Do not rename Ask mode protocol ids or display names.
- Do not mark the feature complete until `scripts/check.sh` and `cd frontend && npm run build` both pass.

---

## File Map

**Create**

- `backend/app/services/kg/run_control.py` — task-local circuit, error mapping, retry wrapper, and model probe.
- `backend/app/repositories/sqlite/kg_build_job_store.py` — all `kg_build_jobs` SQL and row projection.
- `backend/tests/test_kg_run_control.py` — task-local retry/circuit isolation tests.
- `backend/tests/test_kg_build_job_store.py` — schema, state transition, stale-writer, and restart recovery tests.
- `backend/tests/test_kg_build_circuit_breaker.py` — multi-source lifecycle and preservation tests.
- `frontend/app/kg-build-status.ts` — pure status/resume/toast/presentation logic.
- `frontend/app/kg-build-status.test.mjs` — frontend state-contract tests.

**Modify**

- `backend/app/core/config.py` — dedicated KG timeout/retry settings.
- `backend/app/core/llm.py` — precise transient/status/response-format classification.
- `backend/app/repositories/sqlite/migrations.py` — schema v20 and every-boot recovery.
- `backend/app/repositories/ports.py` — job and extraction keyword interfaces.
- `backend/app/services/repository_runtime.py` — compose and inject the job store.
- `backend/app/models/schemas.py` — `KgBuildJobStatus` and `NotebookSummary.kg_build`.
- `backend/app/services/notebook_catalog.py` — hydrate the durable latest job.
- `backend/app/services/scale_artifact_runtime.py` — include the same job in `/index-status`.
- `backend/app/services/kg/extract.py` — never swallow a typed task abort.
- `backend/app/services/kg_ingest.py` — cancel and drain window futures on task abort.
- `backend/app/services/source_ingestion.py` — accept an explicit KG client and preserve source extraction failure state.
- `backend/app/services/knowledge_lifecycle.py` — prepare, probe, run, stop, drain, finish, and resume jobs.
- `backend/app/services/sqlite_repository.py` — explicit compatibility delegates.
- `backend/app/api/routes.py` — synchronously register jobs, handle duplicates/submission failure, return job ids.
- `backend/tests/test_llm_client.py` — HTTP status retry/fallback characterization.
- `backend/tests/kg/test_extract.py` — glean/refine task-abort propagation.
- `backend/tests/test_kg_ingest.py` — window cancellation/drain behavior.
- `backend/tests/test_kg_building_flag.py` — durable building semantics and compatibility set identity.
- `backend/tests/test_kg_rebuild_relink_api.py` — KG-role guard, job id, duplicate 409, and submission-failure terminal state.
- `backend/tests/test_schema_version_migration.py` — interrupted KG-job recovery.
- `backend/tests/test_legacy_db_compat.py` — schema v20 pin and schema contract.
- `frontend/app/workspace-model.ts` — shared KG job API/view types.
- `frontend/app/in-progress-resume.ts` — delegate KG resume/finish semantics to the job helper.
- `frontend/app/in-progress-resume.test.mjs` — durable job resume compatibility.
- `frontend/app/page.tsx` — start/poll/toast/inline status and retry UI.
- `.env.example`, `README.md`, `README_zh.md`, `AGENTS.md`, `fangan_done.md` — configuration, behavior, schema, and completion tracking.
- `backend/tests/fixtures/schema_contract.txt` — mechanically regenerated schema contract.
- `backend/tests/fixtures/repository_contract/api_contract.json` and `backend/tests/fixtures/repository_v9/expected_snapshot.json` — mechanically regenerated living contracts.

---

### Task 1: Classify OpenAI-compatible failures and bound KG call settings

**Files:**

- Modify: `backend/app/core/config.py`
- Modify: `backend/app/core/llm.py`
- Modify: `backend/tests/test_llm_client.py`

**Interfaces:**

- Produces: `is_transient_llm_error(exc: Exception) -> bool`
- Produces: `llm_status_code(exc: Exception) -> int | None`
- Produces: `is_response_format_rejection(exc: Exception) -> bool`
- Produces: `Settings.kg_llm_timeout_seconds: int`
- Produces: `Settings.kg_llm_max_retries: int`

- [ ] **Step 1: Write failing configuration and retry-classification tests**

Append tests that build real `Settings(_env_file=None)` and fake OpenAI calls:

```python
def test_kg_llm_limits_have_bounded_defaults(monkeypatch):
    monkeypatch.delenv("KG_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("KG_LLM_MAX_RETRIES", raising=False)
    settings = Settings(_env_file=None)
    assert settings.kg_llm_timeout_seconds == 60
    assert settings.kg_llm_max_retries == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_status_uses_bounded_retry(monkeypatch, status):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "1")
    err = _api_status_error(status, "upstream unavailable")
    create = _FakeCreate([err, _Resp()])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == '{"ok":1}'
    assert len(create.calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404])
def test_permanent_http_status_does_not_retry_or_plain_fallback(monkeypatch, status):
    monkeypatch.setenv("OPENAI_COMPAT_MAX_RETRIES", "3")
    create = _FakeCreate([_api_status_error(status, "denied")])
    client = _make(monkeypatch, create)
    with pytest.raises(APIStatusError):
        client.chat_json([{"role": "user", "content": "hi"}], "{}")
    assert len(create.calls) == 1


def test_only_explicit_response_format_rejection_falls_back(monkeypatch):
    rejected = _api_status_error(400, "response_format json_object is unsupported")
    create = _FakeCreate([rejected, _Resp()])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == '{"ok":1}'
    assert len(create.calls) == 2
    assert "response_format" not in create.calls[1]
```

Define `_api_status_error()` in the test with an `httpx.Response` attached so
`status_code` is realistic.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_llm_client.py::test_kg_llm_limits_have_bounded_defaults \
  backend/tests/test_llm_client.py::test_transient_http_status_uses_bounded_retry \
  backend/tests/test_llm_client.py::test_permanent_http_status_does_not_retry_or_plain_fallback \
  backend/tests/test_llm_client.py::test_only_explicit_response_format_rejection_falls_back
```

Expected: failures for missing settings and current generic exception fallback.

- [ ] **Step 3: Add the dedicated settings and classification helpers**

Add to `Settings` next to the existing OpenAI and KG extraction settings:

```python
kg_llm_timeout_seconds: int = Field(
    60, gt=0, validation_alias="KG_LLM_TIMEOUT_SECONDS"
)
kg_llm_max_retries: int = Field(
    2, ge=0, le=3, validation_alias="KG_LLM_MAX_RETRIES"
)
```

In `llm.py`, import `APIStatusError` and add:

```python
def llm_status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    return int(value) if isinstance(value, int) else None


def is_transient_llm_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    status = llm_status_code(exc)
    return status == 429 or (status is not None and 500 <= status <= 599)


def is_response_format_rejection(exc: Exception) -> bool:
    status = llm_status_code(exc)
    text = str(exc).lower()
    return status in (400, 422) and (
        "response_format" in text
        or "json_object" in text
        or "json mode" in text
    )
```

Restructure the JSON-mode inner `except` so transient and permanent
`APIStatusError` instances propagate; use the plain-mode fallback only for
`is_response_format_rejection(exc)` and the existing explicit non-HTTP provider
compatibility exception. Make the outer retry loop call
`is_transient_llm_error(exc)` and preserve jittered backoff and cancellation.

- [ ] **Step 4: Run all LLM-client tests and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q backend/tests/test_llm_client.py
```

Expected: all tests pass; exact call counts remain bounded.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/app/core/llm.py backend/tests/test_llm_client.py
git commit -m "fix: classify bounded KG model failures"
```

---

### Task 2: Implement the task-local KG run control and client wrapper

**Files:**

- Create: `backend/app/services/kg/run_control.py`
- Create: `backend/tests/test_kg_run_control.py`

**Interfaces:**

- Consumes: Task 1 classification helpers and `Settings.kg_llm_*`.
- Produces: `KgBuildFailure(code: str, user_message: str)`
- Produces: `KgBuildAborted(failure: KgBuildFailure)`
- Produces: `KgExtractionRunControl(job_id: str)`
- Produces: `TaskScopedKgClient(delegate, settings, control)`
- Produces: `probe_kg_model(client: TaskScopedKgClient) -> None`

- [ ] **Step 1: Write failing run-control tests**

Cover first-failure ownership, bounded wrapper attempts, cancellation during
backoff, permanent failure, malformed JSON softness, and run isolation:

```python
def test_transient_exhaustion_opens_only_its_run(monkeypatch):
    a = KgExtractionRunControl("job-a")
    b = KgExtractionRunControl("job-b")
    delegate = _SequenceClient([_connection_error(), _connection_error(), _connection_error()])
    client = TaskScopedKgClient(delegate, _settings(retries=2), a)

    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")

    assert raised.value.failure.code == "model_unavailable"
    assert delegate.calls == 3
    assert a.aborted is True
    assert b.aborted is False


def test_success_before_limit_does_not_open_circuit():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_timeout_error(), '{"ok":true}'])
    client = TaskScopedKgClient(delegate, _settings(retries=2), control)
    assert client.chat_json([{"role": "user", "content": "x"}], "{}") == '{"ok":true}'
    assert control.aborted is False


def test_auth_failure_is_immediate():
    control = KgExtractionRunControl("job-a")
    delegate = _SequenceClient([_status_error(401)])
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    with pytest.raises(KgBuildAborted) as raised:
        client.chat_json([{"role": "user", "content": "x"}], "{}")
    assert raised.value.failure.code == "model_auth_failed"
    assert delegate.calls == 1


def test_abort_wakes_retry_backoff(monkeypatch):
    control = KgExtractionRunControl("job-a")
    entered = threading.Event()
    delegate = _BlockingFailureClient(entered)
    client = TaskScopedKgClient(delegate, _settings(retries=3), control)
    future = ThreadPoolExecutor(max_workers=1).submit(
        client.chat_json, [{"role": "user", "content": "x"}], "{}"
    )
    assert entered.wait(1)
    control.abort(KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE))
    with pytest.raises(KgBuildAborted):
        future.result(timeout=1)
    assert delegate.calls == 1
```

- [ ] **Step 2: Run the new test file and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q backend/tests/test_kg_run_control.py
```

Expected: import failure because `run_control.py` does not exist.

- [ ] **Step 3: Implement the control, wrapper, and probe**

Create the module with stable constants and these concrete shapes:

```python
@dataclass(frozen=True)
class KgBuildFailure:
    code: str
    user_message: str


class KgBuildAborted(RuntimeError):
    def __init__(self, failure: KgBuildFailure):
        super().__init__(failure.user_message)
        self.failure = failure


class KgExtractionRunControl:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._failure: KgBuildFailure | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def failure(self) -> KgBuildFailure | None:
        with self._lock:
            return self._failure

    def abort(self, failure: KgBuildFailure) -> KgBuildFailure:
        with self._lock:
            if self._failure is None:
                self._failure = failure
                self._event.set()
            return self._failure

    def raise_if_aborted(self) -> None:
        failure = self.failure
        if failure is not None:
            raise KgBuildAborted(failure)

    def wait_backoff(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            self.raise_if_aborted()
```

`TaskScopedKgClient.chat_json()` must call the delegate with
`timeout=settings.kg_llm_timeout_seconds` and `max_retries=0`, own exactly
`1 + settings.kg_llm_max_retries` attempts, add jittered exponential backoff,
and map:

```python
def _failure_for(exc: Exception) -> KgBuildFailure | None:
    status = llm_status_code(exc)
    if is_transient_llm_error(exc):
        return KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
    if status in (401, 403):
        return KgBuildFailure("model_auth_failed", MODEL_AUTH_FAILED_MESSAGE)
    if status is not None:
        return KgBuildFailure("model_request_rejected", MODEL_REQUEST_REJECTED_MESSAGE)
    return None
```

Unclassified exceptions re-raise without opening the circuit, preserving soft
extraction handling. Proxy `configured`, `model`, and `settings`. Implement the
probe as one small JSON request through the wrapper:

```python
def probe_kg_model(client: TaskScopedKgClient) -> None:
    client.chat_json(
        [{"role": "user", "content": 'Return {"ok":true} and nothing else.'}],
        '{"ok":true}',
        max_tokens=16,
    )
```

- [ ] **Step 4: Run run-control and LLM tests and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_run_control.py backend/tests/test_llm_client.py
```

Expected: all pass without real sleeps or network calls.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/run_control.py backend/tests/test_kg_run_control.py
git commit -m "feat: add task-scoped KG model circuit"
```

---

### Task 3: Add schema-v20 durable KG build jobs

**Files:**

- Modify: `backend/app/repositories/sqlite/migrations.py`
- Create: `backend/app/repositories/sqlite/kg_build_job_store.py`
- Create: `backend/tests/test_kg_build_job_store.py`
- Modify: `backend/tests/test_schema_version_migration.py`
- Modify: `backend/tests/test_legacy_db_compat.py`

**Interfaces:**

- Produces: `KgBuildJobStore.create_job(notebook_id, created_by, mode, total_sources) -> dict`
- Produces: `KgBuildJobStore.get(job_id) -> dict`
- Produces: `KgBuildJobStore.latest(notebook_id) -> dict | None`
- Produces: `KgBuildJobStore.set_stage(job_id, stage, *, error_code="", error_message="") -> bool`
- Produces: `KgBuildJobStore.record_source_result(job_id, *, succeeded: bool) -> bool`
- Produces: `KgBuildJobStore.finish(job_id, status, *, error_code="", error_message="") -> bool`
- Produces: `KgBuildJobStore.fail_submission(job_id) -> bool`

- [ ] **Step 1: Write failing migration and store tests**

Test the complete state machine and stale-writer guard:

```python
def test_create_job_is_single_flight(store, notebook):
    first = store.create_job(notebook.id, "user-local", "incremental", 8)
    assert first["status"] == "running"
    with pytest.raises(KgBuildAlreadyRunning):
        store.create_job(notebook.id, "user-local", "rebuild", 8)


def test_progress_and_terminal_update_require_running_job(store, notebook):
    job = store.create_job(notebook.id, "user-local", "incremental", 3)
    assert store.record_source_result(job["id"], succeeded=True) is True
    assert store.record_source_result(job["id"], succeeded=False) is True
    assert store.finish(job["id"], "succeeded") is True
    assert store.record_source_result(job["id"], succeeded=True) is False
    saved = store.get(job["id"])
    assert (saved["completed_sources"], saved["failed_sources"]) == (1, 1)


def test_restart_marks_running_kg_job_failed(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path/'db.sqlite'}")
    repo = SQLiteRepository(settings)
    notebook = repo.create_notebook(NotebookCreate(name="Restart recovery"))
    job = repo._runtime.kg_build_jobs.create_job(
        notebook.id, repo.current_user().id, "incremental", 4
    )
    restarted = SQLiteRepository(settings)
    recovered = restarted._runtime.kg_build_jobs.get(job["id"])
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "worker_interrupted"
```

Update the schema-version pin to `20`.

- [ ] **Step 2: Run focused persistence tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_build_job_store.py \
  backend/tests/test_schema_version_migration.py \
  backend/tests/test_legacy_db_compat.py -k "schema_version or kg_job or contract"
```

Expected: missing table/store and schema-version failures.

- [ ] **Step 3: Add migration 20 and every-boot recovery**

Set `SCHEMA_VERSION = 20` and append only `_migration_20()`:

```sql
CREATE TABLE IF NOT EXISTS kg_build_jobs (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  created_by TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  stage TEXT NOT NULL DEFAULT 'probing',
  total_sources INTEGER NOT NULL DEFAULT 0,
  completed_sources INTEGER NOT NULL DEFAULT 0,
  failed_sources INTEGER NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  error_message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_build_jobs_one_running
  ON kg_build_jobs(notebook_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_kg_build_jobs_nb_created
  ON kg_build_jobs(notebook_id, created_at DESC, id DESC);
```

Extend `_recover_interrupted_jobs()` with the reviewed
`worker_interrupted` code/message and terminal timestamps. Keep recovery outside
the migration gate.

- [ ] **Step 4: Implement the focused store**

Use `SqliteDatabase.write()` for mutations and `connect()` for reads. Define
`KgBuildAlreadyRunning(RuntimeError)` and translate only the partial-unique
`sqlite3.IntegrityError` caused by a running row. Every stage/progress/finish
update includes `WHERE id=? AND status='running'`; return `rowcount == 1`.
`finish()` accepts only `succeeded` or `failed` and sets
`stage='finished'`, `finished_at`, and `updated_at`.

The row projection returns integer counts and maps store `error_message` to
`user_message` only at the API-model boundary, not inside SQL.

- [ ] **Step 5: Regenerate the schema contract and verify GREEN**

Run:

```bash
UPDATE_SCHEMA_GOLDEN=1 PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_legacy_db_compat.py::test_fresh_schema_matches_committed_contract
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_build_job_store.py \
  backend/tests/test_schema_version_migration.py \
  backend/tests/test_legacy_db_compat.py
```

Expected: schema and recovery tests pass; the frozen v9 database is untouched.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/app/repositories/sqlite/migrations.py \
  backend/app/repositories/sqlite/kg_build_job_store.py \
  backend/tests/test_kg_build_job_store.py \
  backend/tests/test_schema_version_migration.py \
  backend/tests/test_legacy_db_compat.py \
  backend/tests/fixtures/schema_contract.txt
git commit -m "feat: persist KG build job state"
```

---

### Task 4: Project the latest job through notebook and index status

**Files:**

- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/notebook_catalog.py`
- Modify: `backend/app/services/scale_artifact_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/tests/test_kg_building_flag.py`
- Modify: `backend/tests/test_index_build_consolidation.py`

**Interfaces:**

- Consumes: `KgBuildJobStore.latest(notebook_id)`.
- Produces: `KgBuildJobStatus`.
- Produces: `NotebookSummary.kg_build: KgBuildJobStatus | None`.
- Produces: `index_status()["kg"]["job"]`.

- [ ] **Step 1: Write failing projection tests**

Add tests proving a durable running job drives `kg_building`, a terminal job
does not, and both projections match:

```python
def test_get_notebook_hydrates_latest_durable_kg_job(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    job = repo._runtime.kg_build_jobs.create_job(nb.id, "user-local", "incremental", 5)
    summary = repo.get_notebook(nb.id)
    assert summary.kg_building is True
    assert summary.kg_build is not None
    assert summary.kg_build.job_id == job["id"]
    assert summary.kg_build.stage == "probing"


def test_index_status_reuses_notebook_job_projection(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    job = repo._runtime.kg_build_jobs.create_job(nb.id, "user-local", "incremental", 5)
    status = repo.index_status(nb.id)
    assert status["kg"]["job"]["job_id"] == job["id"]
    assert status["kg"]["building"] is True
```

Keep the existing in-memory set identity test. Change its expectation to
`summary.kg_building is (durable running OR compatibility-set membership)`.

- [ ] **Step 2: Run focused projection tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_index_build_consolidation.py
```

Expected: missing runtime store/model/projection fields.

- [ ] **Step 3: Add the API model and runtime wiring**

Add:

```python
class KgBuildJobStatus(BaseModel):
    job_id: str
    mode: Literal["incremental", "rebuild"]
    status: Literal["running", "succeeded", "failed"]
    stage: Literal["probing", "extracting", "stopping", "finished"]
    total_sources: int = 0
    completed_sources: int = 0
    failed_sources: int = 0
    error_code: str = ""
    user_message: str = ""
    updated_at: str = ""
```

Add `kg_build: Optional[KgBuildJobStatus] = None` to `NotebookSummary`.

Construct `KgBuildJobStore` before `NotebookSummaryQuery` in
`RepositoryRuntime`, inject it into the query and `KnowledgeLifecycleService`,
and expose it as `runtime.kg_build_jobs`.

- [ ] **Step 4: Hydrate and reuse the projection**

`NotebookSummaryQuery.get()` loads the latest row on the same read connection
and maps it through one helper:

```python
def kg_build_status(row) -> KgBuildJobStatus | None:
    if row is None:
        return None
    return KgBuildJobStatus(
        job_id=row["id"],
        mode=row["mode"],
        status=row["status"],
        stage=row["stage"],
        total_sources=int(row["total_sources"]),
        completed_sources=int(row["completed_sources"]),
        failed_sources=int(row["failed_sources"]),
        error_code=row["error_code"],
        user_message=row["error_message"],
        updated_at=row["updated_at"],
    )
```

Set `kg_building = compatibility_flag or (
summary.kg_build is not None and summary.kg_build.status == "running"
)`. Leave collection-list hydration unchanged to avoid an added per-notebook
query; opening a workspace already calls the single-notebook endpoint.

`ScaleArtifactRuntime.index_status()` adds
`"job": notebook.kg_build.model_dump(mode="json") if notebook.kg_build else None`
under `kg`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_index_build_consolidation.py \
  backend/tests/test_notebook_summary_query.py
```

Expected: all pass, including list-path no-extra-status behavior.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/app/models/schemas.py \
  backend/app/repositories/ports.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/notebook_catalog.py \
  backend/app/services/scale_artifact_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_index_build_consolidation.py
git commit -m "feat: expose durable KG build status"
```

---

### Task 5: Propagate task aborts through KG windows and source extraction

**Files:**

- Modify: `backend/app/services/kg/extract.py`
- Modify: `backend/app/services/kg_ingest.py`
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/tests/kg/test_extract.py`
- Modify: `backend/tests/test_kg_ingest.py`
- Modify: `backend/tests/test_kg_repository.py`

**Interfaces:**

- Consumes: `KgBuildAborted`.
- Changes: `SourceIngestionService.run_extraction(source_id, *, kg_client=None)`.
- Changes: facade `_run_extraction(source_id, *, kg_client=None)`.
- Guarantees: a typed abort is never converted to empty nodes or a failed-window warning.

- [ ] **Step 1: Write failing task-abort propagation tests**

Add a fake client that raises `KgBuildAborted` and assert all optional passes
propagate it:

```python
def test_extract_window_does_not_swallow_task_abort(elements):
    failure = KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
    with pytest.raises(KgBuildAborted):
        extract_window(_RaiseClient(KgBuildAborted(failure)), elements, "s", "academic")


def test_extract_graph_cancels_and_drains_sibling_windows(monkeypatch):
    release = threading.Event()
    client = _AbortOneBlockOneClient(release)
    started = []
    with pytest.raises(KgBuildAborted):
        kg_ingest.extract_graph(client, _multi_window_text(), "doc.md", "academic", n=40)
    assert client.running == 0
    assert client.calls < client.total_windows
```

Add a repository test that passes an explicit fake `kg_client` and verifies the
default resolver is not used.

- [ ] **Step 2: Run focused extraction tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/kg/test_extract.py \
  backend/tests/test_kg_ingest.py \
  backend/tests/test_kg_repository.py -k "abort or explicit_kg_client"
```

Expected: typed abort is swallowed and the new keyword is rejected.

- [ ] **Step 3: Preserve typed aborts in every KG LLM pass**

In `extract.py`, add `except KgBuildAborted: raise` before generic catches in:

- the primary extraction call;
- `refine_nodes()`;
- `_glean_nodes()`; and
- the refine wrapper inside `extract_window()`.

Do not change malformed JSON or other generic soft-failure behavior.

- [ ] **Step 4: Cancel and drain window futures**

Change `extract_graph()` to observe completion order and handle a typed abort:

```python
futs = [...]
try:
    for fut in cf.as_completed(futs):
        try:
            ns, es = fut.result()
        except KgBuildAborted:
            for sibling in futs:
                sibling.cancel()
            cf.wait(futs)
            raise
        except Exception:
            failed += 1
        else:
            nodes += ns
            edges += es
finally:
    # no executor shutdown: the process-global pool remains owned by scheduler
    pass
```

The wait is required: a failed job must not become retryable while an old
window is still running.

- [ ] **Step 5: Add the explicit client seam**

Change source extraction to:

```python
def run_extraction(self, source_id: str, *, kg_client=None) -> None:
    ...
    kg_llm_client = kg_client if kg_client is not None else self.kg_llm()
```

Thread the keyword through the facade and runtime callback types. Existing
callers that pass only `source_id` remain byte-compatible.

- [ ] **Step 6: Run extraction suites and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/kg/test_extract.py \
  backend/tests/test_kg_ingest.py \
  backend/tests/test_kg_repository.py \
  backend/tests/test_kg_llm_client.py
```

Expected: all pass; existing soft failed-window warnings remain intact.

- [ ] **Step 7: Commit**

```bash
git add \
  backend/app/services/kg/extract.py \
  backend/app/services/kg_ingest.py \
  backend/app/services/source_ingestion.py \
  backend/app/services/sqlite_repository.py \
  backend/app/services/repository_runtime.py \
  backend/app/repositories/ports.py \
  backend/tests/kg/test_extract.py \
  backend/tests/test_kg_ingest.py \
  backend/tests/test_kg_repository.py
git commit -m "feat: stop KG windows on task abort"
```

---

### Task 6: Orchestrate probe, circuit stop, preservation, progress, and continuation

**Files:**

- Modify: `backend/app/services/knowledge_lifecycle.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/repositories/ports.py`
- Create: `backend/tests/test_kg_build_circuit_breaker.py`
- Modify: `backend/tests/test_kg_building_flag.py`
- Modify: `backend/tests/test_kg_repository.py`

**Interfaces:**

- Produces: `prepare_notebook_kg_job(notebook_id, mode) -> dict`.
- Produces: `fail_notebook_kg_job_submission(job_id) -> bool`.
- Changes: `build_notebook_kg(notebook_id, *, progress=None, job_id=None) -> dict`.
- Changes: `rebuild_notebook_kg(notebook_id, *, job_id=None) -> dict`.
- Internal: `_run_notebook_kg_job(notebook_id, job_id, mode, progress=None)`.

- [ ] **Step 1: Write failing multi-source circuit tests**

Create hermetic tests with two notebooks and controlled fake clients:

```python
def test_model_outage_preserves_completed_source_and_stops_remaining(repo):
    nb, source_ids = _seed_three_parsed_sources(repo)
    client = _FirstSourceThenUnavailableClient()
    repo._runtime.models._kg_llm_client = client
    job = repo.prepare_notebook_kg_job(nb.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.build_notebook_kg(nb.id, job_id=job["id"])

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "model_unavailable"
    assert saved["completed_sources"] == 1
    assert repo.get_notebook(nb.id).kg_pending_sources == 2
    assert _source_statuses(repo, source_ids).count("extracting") == 0
    assert _kg_source_ids(repo, nb.id) == {source_ids[0]}


def test_one_notebook_circuit_does_not_abort_another(repo):
    run_a = KgExtractionRunControl("a")
    run_b = KgExtractionRunControl("b")
    run_a.abort(KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE))
    run_b.raise_if_aborted()


def test_failed_rebuild_continues_without_second_delete(repo, monkeypatch):
    nb, _ = _seed_three_parsed_sources(repo)
    rebuild = repo.prepare_notebook_kg_job(nb.id, "rebuild")
    _run_until_one_source_then_fail(repo, rebuild)
    delete_calls = _spy_delete(monkeypatch, repo)
    continuation = repo.prepare_notebook_kg_job(nb.id, "incremental")
    _run_with_success_client(repo, continuation)
    assert delete_calls == []
```

Also test that the model probe runs before rebuild deletion and that duplicate
jobs cannot enter the executor.

- [ ] **Step 2: Run lifecycle circuit tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_build_circuit_breaker.py \
  backend/tests/test_kg_building_flag.py
```

Expected: missing preparation/job orchestration and current source-failure
isolation leaves sources at `extracting`.

- [ ] **Step 3: Add job preparation and target counting**

`prepare_notebook_kg_job()`:

1. validates `mode in {"incremental", "rebuild"}`;
2. calls `get_notebook`;
3. verifies the resolved KG client is configured;
4. reads `source_build_rows()` and `sources_with_elements()`;
5. counts parsed incremental targets, or all parsed non-Knowhow sources for
   rebuild;
6. creates the durable job with the current authenticated user id; and
7. adds the notebook to the compatibility `kg_building` set immediately.

Inject a `current_user_id: Callable[[], str]` seam into lifecycle rather than
reading request ContextVars in the store.

- [ ] **Step 4: Refactor build/rebuild into one internal job runner**

Implement this order:

```python
control = KgExtractionRunControl(job_id)
controlled_client = TaskScopedKgClient(self.kg_llm_client, self.settings, control)
try:
    probe_kg_model(controlled_client)
    if mode == "rebuild":
        self.delete_notebook_kg(notebook_id)
    targets = self._kg_targets(notebook_id)
    self.kg_build_jobs.set_stage(job_id, "extracting")
    result = self._extract_targets(
        notebook_id, targets, job_id, control, controlled_client, progress
    )
    self._run_success_side_effects(notebook_id, result)
    self.kg_build_jobs.finish(job_id, "succeeded")
    return {**result, "job_id": job_id}
except KgBuildAborted as exc:
    self.kg_build_jobs.set_stage(
        job_id, "stopping",
        error_code=exc.failure.code,
        error_message=exc.failure.user_message,
    )
    self._cancel_and_drain_source_futures(...)
    self.kg_build_jobs.finish(
        job_id, "failed",
        error_code=exc.failure.code,
        error_message=exc.failure.user_message,
    )
    raise
except Exception:
    self.kg_build_jobs.finish(
        job_id, "failed",
        error_code="internal_error",
        error_message=INTERNAL_ERROR_MESSAGE,
    )
    raise
finally:
    self.kg_building.discard(notebook_id)
```

For every source callable:

- check the control before changing status;
- set `extracting`;
- call `_run_extraction(source_id, kg_client=controlled_client)`;
- on success set `extracted`;
- on `KgBuildAborted`, restore `parsed` with no raw error and re-raise;
- on another exception, restore `parsed`, log it, and return `False`.

On the first source future raising `KgBuildAborted`, cancel all pending source
futures and `concurrent.futures.wait()` for the full set before terminal job
state. Update progress after each non-cancelled source result.

- [ ] **Step 5: Preserve synchronous compatibility**

If public build/rebuild receives no `job_id`, call preparation and synchronously
own the created job. Rebuild must probe before `delete_notebook_kg()`. Return
the existing `built`, `failed`, `skipped`, and `skipped_no_elements` fields plus
`job_id`; keep the existing optional CLI progress callback.

- [ ] **Step 6: Run lifecycle and source tests and verify GREEN**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_build_circuit_breaker.py \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_kg_repository.py \
  backend/tests/test_batch_ingest.py \
  backend/tests/test_kg_job_user_context.py
```

Expected: preservation, isolation, progress, context propagation, and existing
CLI behavior pass.

- [ ] **Step 7: Commit**

```bash
git add \
  backend/app/services/knowledge_lifecycle.py \
  backend/app/services/repository_runtime.py \
  backend/app/services/sqlite_repository.py \
  backend/app/repositories/ports.py \
  backend/tests/test_kg_build_circuit_breaker.py \
  backend/tests/test_kg_building_flag.py \
  backend/tests/test_kg_repository.py
git commit -m "feat: abort unavailable KG build tasks"
```

---

### Task 7: Register jobs synchronously in the KG API

**Files:**

- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_kg_rebuild_relink_api.py`
- Modify: `backend/tests/test_repository_api_contract.py`

**Interfaces:**

- Consumes: lifecycle preparation and submission-failure methods.
- Produces: POST body `{status, notebook_id, job_id}`.
- Produces: duplicate active job HTTP 409.

- [ ] **Step 1: Write failing endpoint tests**

Cover the KG-role readiness bug and job registration timing:

```python
def test_build_uses_resolved_kg_role_not_primary(client, repo, monkeypatch):
    monkeypatch.setattr(type(repo), "llm_client", property(lambda _self: _Client(False)))
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda _self: _Client(True)))
    response = client.post(f"/api/notebooks/{notebook_id}/kg/build")
    assert response.status_code == 200
    assert response.json()["job_id"].startswith("kgj-")


def test_duplicate_running_build_returns_409(client):
    first = client.post(f"/api/notebooks/{notebook_id}/kg/build")
    second = client.post(f"/api/notebooks/{notebook_id}/kg/build")
    assert first.status_code == 200
    assert second.status_code == 409


def test_submission_failure_marks_job_failed(client, monkeypatch):
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    response = client.post(f"/api/notebooks/{notebook_id}/kg/build")
    assert response.status_code == 500
    latest = repo._runtime.kg_build_jobs.latest(notebook_id)
    assert latest["status"] == "failed"
    assert latest["error_code"] == "job_submission_failed"
```

Mock the background submit for deterministic endpoint tests rather than racing a
daemon thread.

- [ ] **Step 2: Run endpoint tests and verify RED**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q backend/tests/test_kg_rebuild_relink_api.py
```

Expected: response lacks `job_id`, guard checks the primary client, and duplicate
requests are accepted.

- [ ] **Step 3: Change build and rebuild routes**

For each route:

1. resolve `repo.kg_llm_client`;
2. validate notebook/access as today;
3. call `prepare_notebook_kg_job(notebook_id, mode)`;
4. translate `KgBuildAlreadyRunning` to 409 using the repository's trusted
   user-error response helper;
5. submit `repo.build_notebook_kg(..., job_id=job["id"])` or rebuild equivalent;
6. if submit raises, call `fail_notebook_kg_job_submission(job["id"])` and
   re-raise; and
7. return existing fields plus `job_id`.

Do not put model probes or source work on the request thread.

- [ ] **Step 4: Run endpoint and API contract tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_kg_rebuild_relink_api.py \
  backend/tests/test_repository_api_contract.py
```

Expected: behavioral tests pass; API contract test reports an intentional
fixture delta until Task 9 regeneration.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/tests/test_kg_rebuild_relink_api.py
git commit -m "feat: return durable KG build jobs"
```

---

### Task 8: Render durable KG build states and continuation controls

**Files:**

- Create: `frontend/app/kg-build-status.ts`
- Create: `frontend/app/kg-build-status.test.mjs`
- Modify: `frontend/app/workspace-model.ts`
- Modify: `frontend/app/in-progress-resume.ts`
- Modify: `frontend/app/in-progress-resume.test.mjs`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/globals.css`

**Interfaces:**

- Produces: `KgBuildJobStatus` TypeScript type.
- Produces: `kgBuildPresentation(job, pendingSources, ready)`.
- Produces: `shouldResumeKgBuild(job)`.
- Produces: `kgBuildTerminalToast(job)`.
- Produces: `isTrackedKgTerminal(trackedJobId, job)`.

- [ ] **Step 1: Write failing pure-logic tests**

Create tests for every state and stale job guard:

```javascript
test("probing/extracting/stopping labels are explicit", () => {
  assert.equal(kgBuildPresentation(job("running", "probing"), 80, false).label, "正在连接模型服务…");
  assert.equal(
    kgBuildPresentation(job("running", "extracting", {completed_sources: 12, total_sources: 80}), 68, true).label,
    "正在分析 12/80",
  );
  assert.equal(
    kgBuildPresentation(job("running", "stopping"), 68, true).label,
    "模型服务异常，正在停止本次分析…",
  );
});

test("model failure preserves progress and exposes continuation", () => {
  const view = kgBuildPresentation(
    job("failed", "finished", {
      completed_sources: 12,
      total_sources: 80,
      error_code: "model_unavailable",
      user_message: "模型服务暂时不可用，本次分析已停止；已完成内容已保留，请在服务恢复后继续分析未完成内容。",
    }),
    68,
    true,
  );
  assert.equal(view.label, "分析已中断 · 已完成 12/80");
  assert.equal(view.actionLabel, "继续分析未完成内容");
});

test("old terminal response cannot finish a newer tracked job", () => {
  assert.equal(isTrackedKgTerminal("new-job", job("failed", "finished", {job_id: "old-job"})), false);
  assert.equal(isTrackedKgTerminal("new-job", job("succeeded", "finished", {job_id: "new-job"})), true);
});
```

- [ ] **Step 2: Run frontend logic tests and verify RED**

Run:

```bash
cd frontend && node --test app/kg-build-status.test.mjs app/in-progress-resume.test.mjs
```

Expected: missing helper module/type behavior.

- [ ] **Step 3: Implement shared types and pure presentation logic**

Add the exact API type:

```typescript
export type KgBuildJobStatus = {
  job_id: string;
  mode: "incremental" | "rebuild";
  status: "running" | "succeeded" | "failed";
  stage: "probing" | "extracting" | "stopping" | "finished";
  total_sources: number;
  completed_sources: number;
  failed_sources: number;
  error_code: string;
  user_message: string;
  updated_at: string;
};
```

Add `kg_build?: KgBuildJobStatus | null` to `NotebookSummary` and
`job: KgBuildJobStatus | null` under `IndexStatus.kg`.

`kgBuildPresentation()` returns `{label, detail, tone, actionLabel}` and contains
all new Chinese copy in one testable registry. `in-progress-resume.ts` delegates
to `job?.status === "running"` while retaining a legacy `kg_building` fallback
for old servers/persisted responses.

- [ ] **Step 4: Integrate start, tracking, poll, and terminal toasts**

In `page.tsx`:

- change `buildKg`/`rebuildKg` return type to include `job_id`;
- add `trackedKgJobId` state;
- on POST success, store the returned id and immediately refresh the notebook;
- polling continues while the latest job is running;
- terminal handling requires a matching tracked job id;
- success with `failed_sources=0` emits the success toast;
- success with failures emits the warning toast;
- failed emits the interruption toast; and
- clearing the local busy flag never implies success.

Do not pass `user_message` through `toUserMessage`; it is already the
backend-reviewed API field. Do not read any raw `.error` or `.error_message`
field in the new UI.

- [ ] **Step 5: Render the persistent status in both user entry points**

Use the same `kgBuildPresentation()` result:

1. in the source/workspace KG action area, render a compact inline status bar
   under the build button; and
2. in the “索引与构建” KG card, render the same label/detail/tone.

For failed or warning completion with pending sources and write access, render
`继续分析未完成内容` and call `startKgBuild`. Preserve the separately confirmed
`全部重新分析` action.

Add CSS classes for neutral-running, warning-stopping, error-terminal, progress
text, and the retry action using existing design tokens.

- [ ] **Step 6: Run frontend tests, typecheck, and build**

Run:

```bash
cd frontend && npm test
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: all frontend tests pass, TypeScript is clean, production build
succeeds.

- [ ] **Step 7: Commit**

```bash
git add \
  frontend/app/kg-build-status.ts \
  frontend/app/kg-build-status.test.mjs \
  frontend/app/workspace-model.ts \
  frontend/app/in-progress-resume.ts \
  frontend/app/in-progress-resume.test.mjs \
  frontend/app/page.tsx \
  frontend/app/globals.css
git commit -m "feat: show interrupted KG build status"
```

---

### Task 9: Synchronize documentation and living contracts, then run full verification

**Files:**

- Modify: `.env.example`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `fangan_done.md`
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`
- Modify: `backend/tests/fixtures/repository_v9/expected_snapshot.json`
- Modify: `backend/tests/fixtures/repository_v9/manifest.json`

**Interfaces:**

- Consumes: all implemented backend/frontend contracts.
- Produces: schema-v20 documentation and verified completion record.

- [ ] **Step 1: Update setup and behavior documentation**

Document, in both READMEs and `AGENTS.md`:

- `KG_LLM_TIMEOUT_SECONDS=60`;
- `KG_LLM_MAX_RETRIES=2` with allowed `0..3`;
- initial KG model probe;
- task-scoped connection/timeout/429/5xx circuit;
- immediate auth/request failure;
- durable `kg_build_jobs` status and startup interruption recovery;
- source-level preservation and incremental continuation;
- source/workspace and index-panel status behavior; and
- current schema version 20 and v9 upgrade path through migrations 10–20.

Update `.env.example` adjacent to `KG_LLM_*`:

```dotenv
# KG extraction calls use their own bounded timeout/retry policy. A task opens
# its notebook-local circuit after these attempts are exhausted.
KG_LLM_TIMEOUT_SECONDS=60
KG_LLM_MAX_RETRIES=2
```

Correct the already-stale schema statements in all three required documents
from 15 to 20; describe migrations 16–19 factually before adding migration 20.

- [ ] **Step 2: Regenerate living contracts mechanically**

Run:

```bash
PYTHONPATH=backend python3 scripts/generate_repository_contract_fixtures.py
```

Review the diff. It may update:

- API schema/serialization for `kg_build`;
- v9 expected projection and manifest hash.

It must not modify:

- `backend/tests/fixtures/repository_v9/baseline.db`;
- `backend/tests/fixtures/repository_contract/facade_surface.json`; or
- unrelated Ask response goldens without a demonstrated serialization cause.

- [ ] **Step 3: Run targeted contract and compatibility tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -q \
  backend/tests/test_repository_api_contract.py \
  backend/tests/test_repository_phase_contracts.py \
  backend/tests/test_legacy_db_compat.py \
  backend/tests/test_schema_version_migration.py
```

Expected: all pass with schema 20 and the frozen v9 baseline replay.

- [ ] **Step 4: Run the complete offline gate**

Run:

```bash
scripts/check.sh
```

Expected: exit 0; no network model configuration inherited; backend, smoke,
frontend tests, typecheck, and production build all pass.

- [ ] **Step 5: Run the explicit frontend production build**

Run:

```bash
cd frontend && npm run build
```

Expected: exit 0.

- [ ] **Step 6: Update the factual completion tracker only after green gates**

Add a verified entry to `fangan_done.md` under KG extraction/pipeline
reliability. State:

- the exact failure classes;
- task-local scope;
- source-level preservation;
- durable UI status and manual continuation;
- schema v20;
- deterministic/offline tests use fakes and do not call a real model; and
- both required gates passed.

Remove any directly contradictory “current boundary / unfinished” item if one
exists; do not claim global endpoint circuit breaking or automatic resume.

- [ ] **Step 7: Review the final diff and commit**

Run:

```bash
git diff --check
git status --short
```

Confirm the frozen v9 database is not modified and no unrelated user changes
are staged. Then:

```bash
git add \
  .env.example README.md README_zh.md AGENTS.md fangan_done.md \
  backend/tests/fixtures/repository_contract/api_contract.json \
  backend/tests/fixtures/repository_v9/expected_snapshot.json \
  backend/tests/fixtures/repository_v9/manifest.json
git commit -m "docs: document KG task circuit breaker"
```

---

## Final Review Checklist

- [ ] A model outage stops queued calls for only the current job.
- [ ] Calls already in an HTTP attempt drain within the configured timeout and do not retry again after abort.
- [ ] Other notebooks retain independent run controls and executor work.
- [ ] One source is either fully stored or left pending; no partial source graph is committed.
- [ ] Completed source graphs survive interruption and continuation.
- [ ] Rebuild probe precedes destructive deletion.
- [ ] Failed rebuild continuation is incremental.
- [ ] Durable job status survives refresh and recovers correctly after process restart.
- [ ] The frontend never treats `building=false` alone as success.
- [ ] Persistent user copy comes only from `user_message`; raw exceptions remain in logs.
- [ ] `SCHEMA_VERSION` and all required docs say 20.
- [ ] Frozen v9 baseline database is unchanged.
- [ ] `scripts/check.sh` passes.
- [ ] `cd frontend && npm run build` passes.
