# KG Extraction Task Circuit Breaker Design

**Date:** 2026-07-20

**Status:** Approved in conversation; awaiting written-spec review

## 1. Problem

`silicon-notebook` already bounds retries for an individual OpenAI-compatible
connection or timeout failure. That bound is not sufficient for a large KG
build:

- one notebook build submits many source jobs;
- every source submits many extraction windows;
- every window independently consumes its retry budget;
- `extract_graph()` catches failed window futures and continues through the
  remaining queue;
- `build_notebook_kg()` isolates source failures and continues through the
  remaining sources; and
- the frontend treats `kg_building=false` as successful completion because the
  backend exposes no durable notebook-build terminal status.

If the KG model service stays unavailable, the system can therefore keep
starting and retrying queued calls for a long time. When the queue finally
drains, the UI can incorrectly report success even though the model service
prevented the build from completing.

## 2. Goals

1. Stop one notebook's current KG build after the KG model service is confirmed
   unavailable.
2. Do not stop or reject KG builds belonging to other notebooks or users.
3. Preserve sources that completed successfully before the failure.
4. Do not persist a partially extracted source.
5. Allow the user to continue later by processing only sources that still have
   no KG.
6. Expose durable running, stopping, success, warning, and failure information
   after refresh or backend restart.
7. Keep retries bounded and observable without turning a large pending queue
   into a retry storm.
8. Keep current synchronous repository and CLI entry points compatible.

## 3. Non-goals

- A model-endpoint-wide or process-wide circuit breaker.
- Automatic resumption when the model service recovers.
- A user-operated cancel button for KG builds.
- Transactional rollback of source graphs that completed before the outage.
- Changing Ask, Deep Report, embedding, MinerU, paper-metadata, unified-KG
  clustering, or scale-index retry policies.
- Persisting window-level progress or model output.

## 4. Selected Approach

Use a task-scoped circuit breaker plus a durable `kg_build_jobs` record.

Each incremental build or full rebuild gets:

- one job row that owns user-visible status and source-level progress;
- one in-memory `KgExtractionRunControl` shared by that job's source and window
  workers; and
- one task-scoped KG-client wrapper that checks the run control around every KG
  extraction LLM call.

The control object is passed explicitly. It is not a process-global flag and is
not keyed only by model endpoint, so a failure cannot spill into another
notebook's task.

### 4.1 Alternatives rejected

**Failure-ratio-only stop.** Waiting for a source or window failure ratio allows
too many queued calls to start during a total outage and makes the stop point
depend on corpus shape.

**Endpoint-global circuit breaker.** This protects a shared model aggressively
but violates the required isolation: another notebook's task must not be
stopped by this task.

## 5. Durable Job Model

Schema version 20 adds `kg_build_jobs`:

| Column | Meaning |
| --- | --- |
| `id` | Opaque job id |
| `notebook_id` | Owning notebook; delete cascades |
| `created_by` | Authenticated user who started the job |
| `mode` | `incremental` or `rebuild` |
| `status` | `running`, `succeeded`, or `failed` |
| `stage` | `probing`, `extracting`, `stopping`, or `finished` |
| `total_sources` | Target sources selected at job start |
| `completed_sources` | Sources fully extracted and stored |
| `failed_sources` | Non-fatal source failures encountered before terminal state |
| `error_code` | Stable machine-readable terminal error code |
| `error_message` | Stable user-facing Chinese terminal message |
| `created_at` | Start timestamp |
| `updated_at` | Last state/progress change |
| `finished_at` | Terminal timestamp, empty while running |

A partial unique index permits at most one `status='running'` row per notebook.
A latest-job index supports notebook status hydration without scanning job
history. Latest-job ordering uses `(created_at DESC, id DESC)` so same-timestamp
jobs are deterministic.

The SQLite store owns all job SQL. `KnowledgeLifecycleService` owns
orchestration. The repository facade exposes only explicit compatibility
delegates and the consumer-specific port is updated.

### 5.1 Startup recovery

Every boot, outside the schema-version gate, changes leftover `running` jobs to:

- `status='failed'`;
- `stage='finished'`;
- `error_code='worker_interrupted'`;
- `error_message='服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。'`;
- `finished_at` and `updated_at` set to the recovery time.

This recovery never resumes a job automatically and never deletes completed KG.

## 6. Start and Single-flight Flow

The HTTP route resolves the authenticated user's **KG-role** client, not merely
the primary LLM client. It rejects an unconfigured KG role with the existing
409 behavior.

Before submitting background work, the request path:

1. verifies notebook access;
2. selects the incremental targets, or the rebuild target count;
3. inserts the durable running job synchronously;
4. rejects a duplicate running job with HTTP 409; and
5. submits the worker with the job id and propagated request context.

If background submission raises synchronously, the same job is marked failed
with `job_submission_failed` before the exception is returned. This prevents a
durable false-running state.

Existing synchronous `build_notebook_kg()` and `rebuild_notebook_kg()` callers
remain supported. When no pre-created job id is supplied, the compatibility
wrapper creates and owns a job itself, runs it synchronously, and returns the
existing result fields plus `job_id`.

For `rebuild`, the job is registered before deletion. A failed rebuild can later
be continued with the normal incremental action; that continuation does not
delete the successfully rebuilt subset again.

## 7. Model Probe and Failure Classification

### 7.1 Initial probe

While `stage='probing'`, the worker sends one small JSON-mode chat request
through the resolved KG-role client. The probe uses the same base URL, API key,
model, timeout, response-format fallback rules, retry policy, and cancellation
checks as extraction, with a small output token limit.

No source jobs or window jobs are submitted before the probe succeeds. A probe
failure therefore prevents an already-unavailable service from receiving a
corpus-sized burst.

After success, the job moves to `stage='extracting'` and schedules source work.

### 7.2 Dedicated KG call limits

KG extraction calls use:

- `KG_LLM_TIMEOUT_SECONDS`, default `60`;
- `KG_LLM_MAX_RETRIES`, default `2`, constrained to `0..3`;
- jittered exponential backoff using the existing 30-second cap.

The total attempts for a call are `1 + KG_LLM_MAX_RETRIES`. These per-call
values override `OPENAI_COMPAT_TIMEOUT_SECONDS` and
`OPENAI_COMPAT_MAX_RETRIES` only for KG extraction and its probe. This prevents
an accidentally large global retry setting from recreating the large-build
request storm.

### 7.3 Error classes

The OpenAI-compatible boundary classifies failures before the extraction layer
decides whether to trip the task:

| Failure | Retry | Task result after exhaustion |
| --- | --- | --- |
| Connection error | Bounded | `model_unavailable` |
| Timeout | Bounded | `model_unavailable` |
| HTTP 429 | Bounded | `model_unavailable` |
| HTTP 5xx | Bounded | `model_unavailable` |
| HTTP 401/403 | None | `model_auth_failed` |
| Missing model / incompatible endpoint or request | None | `model_request_rejected` |
| Explicit unsupported `response_format` | One plain-mode fallback, then classify result | Depends on fallback result |
| Malformed/empty model JSON | None at transport layer | Soft window failure |
| Evidence grounding yields no objects | None | Valid empty window |
| Internal non-model source/window error | None | Isolated source/window failure |

The generic JSON-mode fallback must no longer treat every HTTP exception as
proof that `response_format` is unsupported. It falls back only when the error
actually indicates that parameter or JSON mode is unsupported.

Underlying exception details continue to go to the existing event and LLM
logs. API responses expose only the stable code and message.

## 8. Task-scoped Circuit and Concurrency

`KgExtractionRunControl` contains:

- the job id;
- a `threading.Event`;
- a lock protecting first-failure ownership;
- the first fatal error code and safe message; and
- `raise_if_aborted()` and idempotent `abort()` operations.

The task-scoped KG-client wrapper:

1. checks `raise_if_aborted()` before an attempt;
2. delegates with the dedicated KG timeout and retry limit;
3. checks the control before every retry and during retry backoff;
4. aborts the control on a fatal classified KG model error; and
5. re-raises a typed task-abort exception that extraction code must not swallow.

Main extraction, gleaning, and refinement calls all use this wrapper. Their
existing best-effort handling remains for malformed content and non-fatal
quality failures, but a typed task abort always propagates.

Source and window schedulers keep the existing process-global capacity limits,
but every submitted callable checks the task control before starting. On the
first task abort:

- pending window futures for this task are cancelled;
- pending source futures for this task are cancelled;
- running workers skip every later retry or later LLM pass;
- the job moves to `stage='stopping'`; and
- the coordinator waits for already-running calls to leave their current HTTP
  attempt before marking the job terminal.

Waiting for running workers prevents a new retry job from overlapping stale
workers from the failed job. The HTTP timeout bounds that drain. Other tasks in
the shared pools continue normally.

## 9. Source-level Consistency

A source is the persistence boundary:

- `begin_extraction_run()` may clear that source's old extraction state;
- a graph is stored only after all required windows for that source finish
  without a task abort;
- if the task aborts, no partial graph from that source is stored;
- its `extraction_runs` row is marked failed with the stable task error code in
  its diagnostic message; and
- its visible source status is restored to the parsed/waiting-for-analysis
  state, never left at `extracting`.

Sources committed before the breaker opened remain intact and increment
`completed_sources`. Non-fatal source errors increment `failed_sources` and do
not trip the task. If the model remains healthy, the coordinator finishes the
other sources and records `succeeded` with warnings.

An incremental retry uses the established `source_build_rows()` rule: sources
with KG are skipped and parsed sources without KG are targets. The retry
therefore preserves completed work and naturally resumes the remainder.

## 10. Terminal States and Messages

Stable task-level error codes and user messages are:

| Code | Message |
| --- | --- |
| `model_unavailable` | `模型服务暂时不可用，本次分析已停止；已完成内容已保留，请在服务恢复后继续分析未完成内容。` |
| `model_auth_failed` | `模型服务认证失败，本次分析已停止；请检查 API Key 或访问权限后重试。` |
| `model_request_rejected` | `模型服务拒绝了知识抽取请求；请检查模型名称、地址和兼容性设置后重试。` |
| `worker_interrupted` | `服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。` |
| `job_submission_failed` | `知识图谱分析任务未能启动，请稍后重试。` |
| `internal_error` | `知识图谱分析意外中断；已完成内容已保留，可继续分析未完成内容。` |

Normal completion uses `status='succeeded'`. If `failed_sources > 0`, the UI
renders completion with warnings and offers the same incremental retry action.
Model and orchestration failures use `status='failed'`.

## 11. API Projection

`NotebookSummary` adds an optional nested `kg_build` object:

```json
{
  "job_id": "kgj-...",
  "mode": "incremental",
  "status": "failed",
  "stage": "finished",
  "total_sources": 80,
  "completed_sources": 12,
  "failed_sources": 0,
  "error_code": "model_unavailable",
  "error_message": "模型服务暂时不可用，本次分析已停止；已完成内容已保留，请在服务恢复后继续分析未完成内容。",
  "updated_at": "2026-07-20T12:00:00+08:00"
}
```

For compatibility:

- `kg_building` remains present and is true exactly when the durable latest job
  is `running`;
- `kg_ready` and `kg_pending_sources` retain their current meanings; and
- notebooks with no job history return `kg_build=null` and
  `kg_building=false`.

`GET /notebooks/{id}/index-status` returns the same nested job information under
`kg.job` while preserving `kg.ready`, `kg.building`, and
`kg.pending_sources`.

The build and rebuild POST responses retain `status` and `notebook_id` and add
`job_id`. No separate polling endpoint is needed.

## 12. Frontend Behavior

The source/workspace KG action area, the “索引与构建” panel, and toast
completion logic all consume the same job projection.

### 12.1 Labels

| Job state | Primary status |
| --- | --- |
| `running/probing` | `正在连接模型服务…` |
| `running/extracting` | `正在分析 12/80` |
| `running/stopping` | `模型服务异常，正在停止本次分析…` |
| `succeeded`, no failures | `知识图谱分析完成` |
| `succeeded`, failures > 0 | `分析完成，2 篇来源需要重试` |
| `failed` | `分析已中断 · 已完成 12/80` |

For a failed job, the stable error message appears inline rather than only in a
transient toast. When pending sources remain and the user has write access, the
inline action is `继续分析未完成内容`. It invokes the existing incremental build
endpoint.

The full rebuild action remains separately available behind its existing
destructive confirmation. A failed rebuild's primary recovery action is the
non-destructive incremental continuation.

### 12.2 Polling and refresh

Frontend completion detection no longer means “`kg_building` became false,
therefore success.” It compares the latest job id and terminal status:

- `succeeded` produces a success or warning toast;
- `failed` produces an interruption toast and leaves the persistent inline
  message visible; and
- a changed job id prevents an old poll response from completing a newer job.

The existing six-second polling cadence remains. Page refresh and notebook
switch hydrate the durable job and resume polling only for `status='running'`.

Source cards use the restored parsed state plus the existing `kg_extracted`
projection, so aborted sources show as waiting for analysis, not indefinitely
extracting.

## 13. Observability

The existing event logger receives:

- `kg_build_started`;
- `kg_build_progress`;
- `kg_build_circuit_opened`;
- `kg_build_stopping`;
- `kg_build_succeeded`; and
- `kg_build_failed`.

Events include job id, notebook id, mode, counts, stage, safe error code, and
latency. Raw provider errors stay in backend logs and are clipped using the
existing model-error conventions. API keys, bearer tokens, prompt text, and
source contents are never added to these job events.

## 14. Testing

Implementation follows test-driven development.

### 14.1 LLM boundary tests

- connection, timeout, 429, and 5xx use exactly the configured bounded attempts;
- 401/403 and incompatible requests do not retry;
- only a real response-format rejection gets the one plain-mode fallback;
- a task abort prevents a later retry during backoff; and
- global retry settings cannot override the bounded KG-specific limit.

### 14.2 Circuit and scheduler tests

- the initial probe fails before any source/window submission;
- the first exhausted fatal KG call opens the task circuit once;
- pending futures for that task never call the model;
- running futures stop before their next retry or optional extraction pass;
- another notebook's run control and futures remain unaffected; and
- the coordinator waits for running workers before allowing a new job.

### 14.3 Repository and API tests

- job creation is durable and duplicate running jobs return 409;
- source-level progress is monotonic and stale job ids cannot update a newer job;
- one completed source remains queryable after a later model failure;
- an interrupted source stores no partial graph and returns to parsed/pending;
- incremental continuation skips completed sources;
- a failed rebuild continues incrementally without a second delete;
- startup recovery marks leftover running jobs failed;
- `NotebookSummary` and index status expose the same job;
- POST responses contain `job_id`; and
- the v9 fixture upgrades through schema v20.

### 14.4 Frontend tests

Pure status helpers cover probing, extracting, stopping, success, warning, and
failed labels; resume predicates; terminal toast selection; stale job-id
responses; and retry-action visibility. Component/source scans pin the user
copy and prevent a return to the old “not building means success” rule.

Final verification must include:

```bash
scripts/check.sh
cd frontend && npm run build
```

No feature is marked done until both commands pass.

## 15. Documentation and Tracking

The implementation change updates together:

- `README.md`;
- `README_zh.md`;
- `AGENTS.md`;
- `.env.example`; and
- `fangan_done.md` under the relevant KG reliability/status entry.

The documentation records schema version 20, KG-specific timeout/retry
configuration, task-scoped failure semantics, durable status fields, retained
partial completion, manual continuation, and deterministic offline/test
behavior.
