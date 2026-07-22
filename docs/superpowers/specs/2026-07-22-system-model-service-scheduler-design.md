# System-managed model services and unified scheduler design

## Goal

`silicon-notebook` will remove every user-editable model configuration and make
model access a system-managed deployment concern. Maintainers define the
available remote model services, the maximum parallelism supported by each
service, and which product workloads use each service. Every chat, embedding,
and rerank provider call must pass through one process-wide scheduling layer so
the configured service parallelism is never exceeded.

The scheduler must keep interactive work responsive, prevent one user from
monopolizing a service, allow long-running background work to make progress,
bound memory during overload, stop sending traffic to an unhealthy service,
and give users a safe support identifier that lets maintainers identify the
failing service and call stage.

This design supersedes the per-user model-resolution and editable model-service
UI described in the 2026-07-21 model-service status/settings designs. Those
documents remain historical records; this document defines the new target
behavior.

## Confirmed product decisions

- Model configuration is system-managed and loaded from deployment files and
  environment variables at process startup. Changes require a restart.
- A named service describes one model endpoint and owns one independent
  parallelism budget. Different endpoints are independent even if they run on
  the same machine; there is no cross-service capacity group in this version.
- A workload binds to exactly one service. There is no automatic load balancing,
  failover, or silent fallback to a different model.
- Multiple workloads may bind to the same service. Their requests then share
  the same queue, circuit breaker, and parallelism budget.
- Chat LLM, embedding, and rerank traffic all use the scheduler. They retain
  protocol-specific clients behind a shared scheduling contract.
- Interactive work has priority over reports, and reports have priority over
  background ingestion/KG work. Fixed weighted scheduling prevents starvation.
- Users are round-robined within a priority class. Queue capacity is bounded and
  derived from service parallelism; maintainers do not configure queue tuning.
- Services use a unified circuit breaker with automatic half-open recovery.
- Ordinary users may inspect sanitized service status and support identifiers.
  Administrators may run health checks. No browser user may edit configuration.
- Existing per-user model settings and credentials are erased by migration.
- The implementation is process-local and therefore requires one backend
  process. Distributed scheduling is a future concern.

## Mental model

For a service configured with `max_concurrency = 20`, all workloads bound to
that service share twenty execution slots:

```text
Ask -----------+
Memory preview +--> general service scheduler --> at most 20 provider calls
Knowhow -------+
KG extraction -+

Reasoning Ask -+
Deep Report ---+--> reasoning service scheduler -> at most 4 provider calls

Embedding --------> embedding scheduler --------> at most 8 provider calls
```

The twenty-first general-service call waits in the general-service queue. It
does not use a separate Ask, KG, or report limit. A local feature thread pool
may still orchestrate work, but it cannot bypass or multiply the provider
parallelism configured for the target service.

## System configuration

### Files and secrets

`MODEL_SERVICES_CONFIG` points to a TOML file owned by the deployment. The
recommended local path is `.local/model-services.toml`, which is gitignored.
The TOML file contains service metadata and workload bindings, but API keys are
referenced by environment-variable name rather than embedded in the file.

Example:

```toml
[services.general]
display_name = "通用模型"
kind = "chat"
protocol = "openai"
base_url = "https://llm.example.com/v1"
model = "general-model"
api_key_env = "GENERAL_LLM_API_KEY"
max_concurrency = 20

[services.reasoning]
display_name = "推理模型"
kind = "chat"
protocol = "openai"
base_url = "https://reasoning.example.com/v1"
model = "reasoning-model"
api_key_env = "REASONING_LLM_API_KEY"
max_concurrency = 4

[services.embedding]
display_name = "向量模型"
kind = "embedding"
protocol = "dashscope"
base_url = "https://embedding.example.com"
model = "embedding-model"
api_key_env = "EMBEDDING_API_KEY"
max_concurrency = 8

[services.rerank]
display_name = "重排模型"
kind = "rerank"
protocol = "openai"
base_url = "https://rerank.example.com/v1"
model = "rerank-model"
api_key_env = "RERANK_API_KEY"
max_concurrency = 8

[bindings]
ask_answer = "general"
memory_preview = "general"
knowhow_optimize = "general"
source_summary = "general"
kg_extract = "general"
reasoning_agent = "reasoning"
report_outline = "reasoning"
report_section = "reasoning"
report_summary = "reasoning"
retrieval_query_embedding = "embedding"
chunk_embedding = "embedding"
retrieval_rerank = "rerank"
```

The corresponding `.env` contains only the path and secrets:

```text
MODEL_SERVICES_CONFIG=.local/model-services.toml
GENERAL_LLM_API_KEY=...
REASONING_LLM_API_KEY=...
EMBEDDING_API_KEY=...
RERANK_API_KEY=...
```

`max_concurrency` is the only scheduler-capacity setting exposed per service.
Queue size, per-user admission, priority weights, wait deadlines, breaker
thresholds, and recovery behavior are system policy, not deployment knobs.

### Service identity and validation

A service id is a stable lowercase identifier matching
`[a-z][a-z0-9_]{0,63}`. `display_name` is sanitized with the existing safe-model
label policy before it can reach the browser. The internal configuration
fingerprint covers service id, kind, protocol, normalized URL, model, resolved
credential material, and parallelism, but is never returned to a client.

Startup validates the complete registry before constructing the repository:

- `kind` is one of `chat`, `embedding`, or `rerank`;
- the protocol is supported for that kind;
- URL, model, `api_key_env`, resolved key, and positive parallelism are present;
- every binding names a known service of the required kind;
- every workload id is known to the backend workload registry;
- duplicate physical definitions with identical kind/protocol/URL/model/key are
  rejected and must be replaced by one shared service id;
- no service or binding key is silently ignored.

An empty `MODEL_SERVICES_CONFIG` keeps the supported deterministic/offline
mode. When a config file exists, an omitted optional workload remains
unconfigured and follows that workload's existing deterministic or explicit
unavailable behavior. It never falls back to another binding. Invalid files or
invalid references fail startup with a credential-safe, actionable message.

If legacy `OPENAI_COMPAT_*`, `REASONING_LLM_*`, `REWRITE_LLM_*`, `KG_LLM_*`,
`EMBED_*`, or `RERANK_*` endpoint settings are present without
`MODEL_SERVICES_CONFIG`, startup reports a migration error instead of guessing
service identities or capacity. Non-model settings such as request timeouts,
token budgets, feature flags, and extraction window sizes remain in their
existing configuration domains; they are not scheduler-capacity knobs.

## Workload registry and bindings

Every provider call carries a stable workload id. The configuration binds each
workload id to one compatible service. The initial registry is:

### Chat workloads

- `ask_answer`
- `reasoning_agent`
- `query_rewrite`
- `evidence_refine`
- `graph_chain_verify`
- `report_outline`
- `report_sufficiency`
- `report_section`
- `report_summary`
- `source_summary`
- `notebook_metadata`
- `paper_metadata`
- `kg_extract`
- `kg_refine`
- `kg_glean`
- `kg_merge_review`
- `kg_concept_description`
- `kg_community_summary`
- `kg_conflict_review`
- `schema_induction`
- `memory_preview`
- `knowhow_optimize`
- `knowhow_reformat`

### Embedding workloads

- `retrieval_query_embedding`
- `source_element_embedding`
- `chunk_embedding`
- `knowledge_object_embedding`
- `relation_embedding`
- `memory_embedding`
- `knowhow_embedding`

### Rerank workloads

- `retrieval_rerank`

Health checks target a service directly and therefore do not require a workload
binding. Adding a provider call without first adding a workload id, compatible
kind, default priority class, binding validation, and tests is an architecture
violation. Nested embedding, rerank, and helper calls inherit the explicitly
established top-level priority when one exists; the default applies only when a
caller has not already established a work context.

The detailed mapping from every current call site to these ids is part of the
implementation plan and static architecture tests. Workload ids are backend
protocol, not user-facing labels.

## Runtime architecture

### Components

`SystemModelServiceRegistry` is an immutable process-owned registry containing
validated service definitions and workload bindings. It replaces per-user
effective-configuration resolution.

`ModelSchedulerRegistry` owns one `ServiceScheduler` for every configured
service. It is created during application startup and shut down during
lifespan teardown. Each scheduler owns:

- one bounded logical queue;
- per-priority, per-user deques and round-robin cursors;
- one dispatcher thread;
- one lazily populated executor capped at `max_concurrency`;
- active and queued counters;
- a service-local circuit breaker;
- completion, rejection, cancellation, and timing metrics.

`SystemModelProvider` resolves a workload id to a scheduled chat, embedding, or
rerank adapter. It never reads the current user's model settings and never
constructs a user-specific client. Raw protocol clients are private delegates
owned by the service runtime.

`ModelWorkContext` carries request metadata through synchronous handlers,
copied-context background jobs, KG windows, report section pools, and MCP:

- authenticated user id, or the reserved `system` actor;
- business workload id;
- priority class;
- request/job id;
- cancellation event;
- enqueue deadline;
- support id.

Existing `contextvars.copy_context()` boundaries remain required. The service
binding is system-owned, while the propagated user id is used only for
fairness, ownership-aware logging, cancellation, and support diagnosis.

### Scheduled invocation

An invocation contains metadata plus an in-memory callable and result future.
Prompts, source text, embeddings, and results are not persisted by the
scheduler. A caller:

1. resolves its workload binding;
2. constructs or inherits `ModelWorkContext`;
3. submits to the target `ServiceScheduler`;
4. waits or asynchronously awaits the returned future;
5. receives the delegate result or a typed scheduling/provider exception.

The dispatcher starts work only while `active < max_concurrency`. A slot is
held from the first provider attempt until the scheduled invocation returns or
raises, including internal retry and retry backoff. This is conservative but
ensures retries cannot multiply provider pressure.

Local KG, report, source, knowhow, and embedding executors remain free to
orchestrate independent business work, but the model scheduler is the sole
authority over remote provider parallelism. `KG_EXTRACT_WORKERS`, the batch
`LimitedJsonChatClient` gate, and the global embedding executor cease to be
parallelism authorities and are removed or redefined only for non-provider
orchestration where still needed.

## Scheduling policy

### Priority classes

The fixed priority classes are:

1. `interactive`: Ask, Memory preview, Knowhow suggestions, and other requests
   whose user is waiting directly;
2. `report`: Deep Report planning, reasoning, drafting, and summary work;
3. `background`: source enrichment, KG extraction/review, bulk embedding,
   indexing projections, and other detached maintenance.

An embedding or rerank call inherits the priority of the operation that caused
it. Query embedding and reranking for Ask are interactive; bulk source
embedding is background.

The dispatcher uses the fixed service pattern `8 interactive : 2 report : 1
background`. Empty lanes are skipped, so unused capacity is never reserved.
After a lane consumes its current weight, the dispatcher advances even when
that lane remains non-empty. Background work therefore cannot be starved by a
continuous interactive load.

### Per-user fairness

Within each priority class, users are selected round-robin and one task is
taken per selected user before the cursor advances. A single user's large KG
or report cannot monopolize all starts in a lane. Jobs submitted without an
authenticated human use the reserved `system` actor.

### Derived bounds

For service parallelism `N`:

- active provider calls are capped at `N`;
- total queued tasks are capped at `10N`;
- one user may queue at most `2N` tasks in total for that service.

The user bound is checked across all three priority lanes. It prevents one
account from filling the shared queue, while the service-wide `10N` limit stays
absolute. Queue accounting excludes active tasks and cancelled tombstones.

Wait deadlines are fixed product policy:

- interactive: 30 seconds;
- report: 300 seconds;
- background: 1,800 seconds.

These values are not deployment configuration. A task that cannot start before
its deadline completes with `ModelQueueTimeout`. A full queue completes new
admission with `ModelQueueFull`. Interactive API callers receive a safe busy
response and support id. Detached jobs translate the typed exception into
their existing observable failed, paused, or retryable business state.

Cancellation before execution marks the future cancelled and leaves a lazy
tombstone for the dispatcher to discard without consuming a slot. Cancellation
after execution starts propagates the existing cancellation event to the raw
client. Queue waiting never occurs while holding a SQLite write transaction.

## Circuit breaker

Each service owns one breaker shared by all bound workloads. Only failures
caused by a real upstream provider call affect it. Queue saturation, queue
timeout, user cancellation, local validation, parsing, persistence, FTS, ANN,
or index failures do not.

Fixed breaker policy:

- a successful provider response resets the consecutive transient-failure
  counter;
- connection failures, provider timeouts, HTTP 429, and HTTP 5xx count as
  transient failures;
- three consecutive transient failures open the breaker;
- HTTP 401/403, an explicit provider error that identifies a rejected or
  unknown model, and a confirmed endpoint/protocol capability mismatch open it
  immediately;
- the normal cooldown is 30 seconds;
- after cooldown, exactly one invocation may enter half-open state;
- half-open success closes the breaker; failure reopens it;
- a manual administrator health check may request the single controlled
  half-open probe but never bypasses `max_concurrency`.

When a breaker opens, queued invocations complete with
`ModelServiceUnavailable` rather than remaining indefinitely. New invocations
fail fast until half-open admission is available. Business layers retain their
current domain-specific terminal/retry semantics.

Breaker state is process runtime state and starts closed after restart. The
last sanitized health result remains persistent for user visibility, but it
does not silently keep a newly started process's breaker open.

A one-off malformed or empty success payload is treated as a transient provider
failure, not a confirmed protocol mismatch. It therefore follows the normal
three-consecutive-failure threshold. Immediate opening requires a classified
upstream response or validation result that conclusively applies to the whole
named service.

## Diagnostics, support ids, and status

Every scheduled invocation receives an opaque `support_id` such as
`mdl-<random-id>`. Failures returned to the browser contain:

- the safe workload/role label;
- safe service `display_name`;
- safe model label when available;
- stable error code;
- support id;
- retry guidance appropriate to busy, unavailable, or local failure.

The event/log correlation record contains support id, workload id, service id,
safe service/model identity, requesting user id, parent request/job id, queue
latency, execution latency, sanitized upstream status class, retry outcome, and
breaker transition. API keys, complete URLs, prompts, source text, embedding
vectors, provider response bodies, and raw exception strings never enter the
status API or ordinary scheduler event. Existing opt-in LLM interaction logs
may continue their separately documented bounded prompt/response logging and
must add the same support id for correlation.

The persisted latest provider-health status becomes system-scoped, keyed by
service id and configuration fingerprint. A configuration change invalidates
the prior result after restart. Reads never probe a provider. Runtime scheduler
snapshots are merged into the response to expose only aggregate `active`,
`maximum`, `queued`, oldest-wait, and breaker status; no per-user queue
identities are returned.

The API's effective health state distinguishes:

- `untested`;
- `ok`;
- `busy` (healthy but currently saturated or rejecting admission);
- `error` / `circuit_open`;
- `half_open`.

Only the latest provider check/observation (`untested`, `ok`, or `error`) is
persisted. `busy`, `circuit_open`, and `half_open` are live scheduler overlays;
they disappear or change as the in-process queue and breaker recover.

Manual success, observed provider failure, and recovery probe results use the
existing monotonic timestamp/fingerprint protections adapted to system service
identity.

## API and front-end behavior

### Removed capability

The personal model-configuration surface is removed completely:

- no Base URL, model, or API-key fields;
- no save action;
- no draft-value test;
- no personal source/fallback labels;
- no `GET/PUT /api/me/model-settings`;
- no draft `POST /api/me/model-settings/test`;
- no runtime read, write, cache, or resolution of user model settings.

The removed routes disappear from the OpenAPI contract rather than pretending
to accept ignored settings.

### System status surface

The collection retains a service summary, but it is system-scoped. Opening it
shows a read-only `模型服务状态` panel with:

- service display name and safe model name;
- product workloads bound to the service;
- latest sanitized health state and check time;
- last check latency;
- live `active / maximum` and aggregate queued count;
- circuit/half-open state;
- safe recent failure category and copyable support id when present.

All authenticated users may read this panel. Administrators additionally see
`测试服务` and `测试全部` actions. Tests target named services, pass through the
normal scheduler, consume a normal execution slot, bypass response caches, and
may perform the one controlled half-open probe. Configuration remains
deployment-only.

Ask and other error panels replace `打开模型服务` with `查看模型状态`, open the
read-only panel, and focus the failing service. User reports include the service
display name and support id so maintainers can correlate the precise service,
workload, and failure in logs.

Proposed system endpoints are:

- `GET /api/model-services/status` for authenticated users;
- `POST /api/admin/model-services/{service_id}/test` for administrators;
- `POST /api/admin/model-services/test-all` for administrators.

They return sanitized service data only. Status reads are local and never
perform provider traffic.

## Persistence and migration

The next schema migration performs an intentional credential scrub:

- update every `user_profiles.model_settings` value to `{}`;
- delete all rows from the old per-user model-status table;
- retain the empty legacy `model_settings` column for SQLite upgrade simplicity,
  but make runtime reads and writes an architecture violation;
- create the system-scoped latest-status table keyed by service id and
  configuration fingerprint.

No user credential backup is created by the application. Normal operator
database backups remain outside this migration's scope. The release notes must
call out the irreversible credential scrub.

The front end removes personal settings state, dirty-field orchestration,
draft-test state, save requests, and their component tests. It replaces the
editable panel with the read-only status panel in the same full-stack change.

`README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`, `.env.example`, API
fixtures, migration manifests, and `fangan_done.md` must be synchronized with
the implemented behavior. Historical design documents are not rewritten.

## Single-process boundary

The scheduler is process-local. The supported deployment for this version is
exactly one backend process, matching `scripts/prod.sh --workers 1`. Running
multiple Uvicorn workers or multiple backend replicas would multiply every
service's configured concurrency and is therefore unsupported, not an
optimization option. Documentation and startup diagnostics must state this
constraint explicitly.

A future multi-process deployment may replace `ModelSchedulerRegistry` with a
Redis-backed or dedicated scheduling service behind the same scheduled-client
ports. Distributed scheduling, leader election, and durable provider-call
queues are non-goals here.

## Failure semantics

- Missing optional bindings preserve each feature's documented deterministic or
  explicit unavailable behavior; there is no cross-model fallback.
- Queue-full and queue-timeout errors mean `busy`, not provider failure, and do
  not open the breaker.
- A provider failure updates the named service's shared status regardless of
  which workload observed it.
- A failure in status persistence or scheduler observability is fail-open for
  the model invocation and logged; it cannot corrupt the result future.
- Raw model errors remain logs-only. Browser errors use stable codes and safe
  labels.
- A scheduler shutdown stops admission, cancels queued futures, and waits for
  active invocations according to the application lifespan shutdown budget.
- Process death may lose queued provider calls. Durable Ask/KG/Report/source
  state owns user-visible recovery or interruption semantics; the scheduler
  does not persist prompts to recreate them.

## Verification and architecture guards

### Scheduler tests

- peak delegate concurrency never exceeds service `max_concurrency`;
- workloads sharing one service share one cap;
- different services do not consume each other's slots;
- fixed priority weights favor interactive work without starving background;
- users round-robin within each priority;
- total and per-user queue bounds derive exactly from parallelism;
- queued cancellation never calls the delegate;
- retry and backoff remain inside one slot;
- queue timeout/full are classified as busy and do not affect the breaker;
- transient thresholds, immediate fatal opening, half-open single-flight, and
  recovery are deterministic under a fake clock;
- shutdown and submit/shutdown races resolve every future exactly once.

### Integration tests

- Ask, reasoning, graph verification, reports, KG extraction/refinement,
  Memory, Knowhow, schema induction, paper metadata, all embedding paths,
  rerank, MCP, and administrator probes use scheduled clients;
- request-user context reaches detached work for fairness and log ownership;
- a service shared by unrelated features observes one combined peak;
- support ids correlate model errors without exposing confidential inputs;
- system health persistence is fingerprint-safe and occurrence-ordered;
- legacy database migration scrubs all stored user model settings;
- removed personal settings routes are absent from OpenAPI;
- ordinary users cannot run probes or edit configuration;
- administrator probes consume scheduler slots and obey the circuit contract;
- the read-only front end contains no configuration fields or save/draft-test
  requests and can focus a failing service from an error panel.

### Static guards

Semantic source scans permit raw chat/embedding/rerank transport calls only in
the protocol-client and scheduler execution boundaries. Application services,
routes, scripts that use the repository runtime, and MCP may not construct raw
clients or call raw provider methods. Offline evaluation/gold-generation tools
that intentionally own isolated clients must be explicitly allowlisted and do
not weaken product-runtime guards.

### Completion gate

The full change is complete only when:

- `scripts/check.sh` passes;
- `cd frontend && npm run build` passes;
- an offline concurrency stress test proves every service peak is bounded;
- README/README_zh/AGENTS/architecture/config/migration/fangan documentation is
  synchronized;
- the frontend and backend ship together with no half-feature;
- the verified migration output shows that stored user model credentials are
  cleared.

## Non-goals

- No user-editable or per-user model configuration.
- No runtime configuration editing, hot reload, or database-stored system keys.
- No multi-service load balancing or automatic model failover.
- No capacity group shared by different service definitions.
- No durable provider-call queue or prompt persistence.
- No multi-process or multi-host global scheduling in this version.
- No exposure of URLs, credentials, raw provider diagnostics, prompts, or other
  users' queue identities through status APIs.
