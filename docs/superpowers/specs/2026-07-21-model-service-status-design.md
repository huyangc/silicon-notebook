# Model service status and targeted diagnostics design

## Goal

When a model-backed operation fails, `silicon-notebook` must identify the
affected model clearly, let the user test that exact effective service, and
surface the most recent known model health on the notebook collection page.
Opening the collection must not itself call any model provider.

## Terminology and dynamic model identity

The stable service roles are `llm`, `reasoning_llm`, `rewrite_llm`, `kg_llm`,
`rerank`, and the system-managed `embedding` service. Their Chinese role labels
are front-end vocabulary. A provider model name such as `deepseek-reasoner` is
never a hard-coded display value.

Every displayed model name comes from the backend's resolved effective
configuration at request time. Resolution must mirror runtime behavior:

- a complete user-specific role configuration wins;
- reasoning, rewrite, and KG roles may fall back to the user's primary LLM;
- otherwise the applicable system role or system primary LLM is used;
- rerank uses its resolved user or system configuration;
- embedding is read-only and system-managed in this feature.

Consequently, changing a configured model or changing which fallback applies
automatically changes the name shown in errors, tests, and service status. The
front end must not guess model names from role ids or maintain provider/model
name tables.

## Selected approach

The backend owns a structured, per-user model-service status contract and the
latest result for each effective configuration. The front end reads those
results on normal collection loading, but a read never performs an upstream
probe. Users explicitly initiate an individual or all-model test.

This is preferred over a front-end-only status because it can test system
fallbacks without exposing credentials and survives page refreshes. It is also
preferred over scheduled health checks because it creates no background model
traffic or unrequested cost.

## Backend contract

### Effective service descriptors

A single backend resolver produces the descriptor used by runtime status and
testing. Each descriptor contains:

- stable `service` role;
- resolved `model` name;
- `source` (`user`, `system`, or `none`);
- whether the service is configured;
- whether missing configuration should affect the collection summary;
- an internal configuration fingerprint that is never returned to the browser.

The resolver is the only place allowed to reproduce role-fallback semantics.
It must be covered by parity tests against the runtime model provider so status
cannot claim one model while the operation calls another.

### Status response

An authenticated read endpoint returns all current effective service items.
Each item contains `service`, `model`, `source`, `configured`, `required`,
`status`, `latency_ms`, `checked_at`, `trigger`, and a stable `code`.

`status` is one of:

- `ok`: the current effective configuration most recently passed a test;
- `error`: a manual test or an observed real operation most recently failed;
- `untested`: no result exists for the current effective configuration;
- `unconfigured`: no effective service is configured.

`trigger` distinguishes `manual_test` from `observed_failure`. Raw provider
exceptions, URLs, credentials, and response bodies are never returned. Existing
diagnostic logging remains available for maintainers.

The collection summary counts configured failures and required unconfigured
services as abnormal. Optional unconfigured rerank or embedding services do not
turn the whole application red; their detail rows remain visible as unavailable.

### Persistent latest result

A small SQLite table stores the latest result by user and service, including
the effective-configuration fingerprint. If the current fingerprint differs,
the read endpoint returns `untested` rather than displaying a stale result.
Saving model settings invalidates affected results immediately. System
configuration changes are also detected by fingerprint mismatch after restart.

The fingerprint may include a one-way digest of endpoint, model, source, and
credential material, but neither the fingerprint nor its inputs are exposed via
the API.

### Tests

The existing draft-config test remains available in the model-settings form so
users can test unsaved values. A new current-service test endpoint accepts only
the service role and resolves credentials on the server. It records `ok` or
`error` and returns the sanitized status item.

The all-model test endpoint tests configured effective services concurrently
with bounded timeouts. Identical effective configurations are probed once and
the result is applied to all roles that use that configuration. An optional
unconfigured service is reported without an upstream call.

Embedding testing uses a minimal embedding request; rerank uses its existing
minimal request; LLM roles use the existing bounded JSON ping. Tests must bypass
the LLM response cache so a cached response cannot produce a false healthy
result.

### Observed operation failures

User-facing model errors gain a stable `service` role and retain the actual
resolved `model` name. The backend records these failures against the current
effective configuration before returning the response. A real failure can
therefore replace an older successful test. Normal production calls do not
persist success on every invocation, avoiding database writes on the hot path;
recovery is confirmed by an explicit test.

## Front-end behavior

### Ask error panel

The generic incomplete-answer warning becomes a structured list, deduplicated
by service and effective model. Example shape:

> 推理模型 `deepseek-reasoner` 调用失败，本次回答可能不完整。

The example model name is dynamic data, not product copy. If no model name is
available, the UI identifies the role and says it is not configured instead of
inventing a name.

Each row offers:

- `测试此模型`, which calls the current-service test endpoint;
- `打开模型服务`, which opens the existing settings panel and highlights the
  affected role.

The test button has running, passed, and failed states. The panel shows only
sanitized Chinese copy derived from stable codes. Raw diagnostics continue to
go to the diagnostic logger and never appear in a tooltip.

### Collection service summary

The existing top-bar status combines API and model state:

- `服务正常` when the API is healthy and all required/configured service results
  are healthy;
- `API 正常 · 模型待测试` when no current result exists;
- `API 正常 · 1 个模型异常` (or the corresponding count) when failures exist;
- the existing API connection error when `/health` itself fails.

Clicking the summary opens the model-service panel. Its accessible title/tooltip
lists affected role and dynamic model name rather than only saying “configured”.
Reading this summary never starts an upstream model request.

### Model-service panel

The existing five editable role fieldsets keep their draft-value test buttons.
Each also shows the current saved/effective model status and last checked time.
The panel adds a `测试当前使用的全部模型` action.

Embedding appears as a read-only status row because this feature does not add
per-user embedding configuration. If it fails, the row remains testable and
states that an administrator must inspect the system configuration.

After saving model settings, stale results become `untested`; the panel remains
the place to run a fresh test.

## Data flow

1. Collection load fetches API health, notebooks, and the authenticated status
   snapshot. Only local application data is read.
2. Ask or another model-backed operation resolves a service and model on the
   backend. On failure it emits the existing diagnostic event, records a
   sanitized failed status, and returns `service` plus `model` in the structured
   user-facing error.
3. The Ask panel names the affected model and lets the user test the saved
   effective service directly.
4. A manual test resolves the effective configuration on the backend, performs
   one bounded uncached probe, persists the result, and returns the sanitized
   status.
5. Front-end state is updated immediately; later collection loads read the same
   persisted result without probing the provider.

## Error and privacy rules

- Model role and model name are user-facing; API keys, full provider payloads,
  endpoint internals, IP addresses, and exception strings are diagnostic-only.
- Unknown backend status/error codes use a generic Chinese fallback.
- A failed status request must not hide notebook data; the top bar reports that
  status is unavailable and the rest of the collection remains usable.
- Manual test calls are guarded against duplicate clicks and use bounded server
  timeouts.
- Multiple failing roles that share one effective model are presented without
  redundant provider calls, while the UI still states every affected role.

## Verification

Backend tests cover effective-resolution parity, persistence and fingerprint
invalidation, sanitized schemas, individual tests, deduplicated all-model tests,
embedding/rerank paths, and observed failure recording. Front-end tests cover
dynamic names, multiple failures, unconfigured fallback copy, direct testing,
collection summary states, opening/highlighting the settings panel, all-model
testing, and the rule that collection loading performs no test call.

The full change must pass `scripts/check.sh` and `cd frontend && npm run build`.
Because this changes product behavior and architecture, `README.md`,
`README_zh.md`, and `AGENTS.md` must be updated together. If the capability maps
to an unfinished item in `silicon_notebook_fangan.md`, `fangan_done.md` must be
updated only after the full verification gate passes.

## Non-goals

- No scheduled or automatic provider probing.
- No provider-specific model-name registry in the front end.
- No exposure of raw upstream errors to end users.
- No per-user embedding configuration.
- No change to which model roles or fallbacks runtime operations select.
