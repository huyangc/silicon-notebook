# Deployment and configuration

[Back to README](../README.md) · [中文说明](./deployment-and-configuration_zh.md)

This is the detailed source-checkout deployment and configuration reference. For the short local path, start with the root README; for a packaged offline target, use [packaging/DEPLOY.md](../packaging/DEPLOY.md).

## Deployment

silicon-notebook runs as two processes — a FastAPI backend and a Next.js frontend — over
one repository selected by `DATABASE_URL`. The shipped SQLite default requires **no GPU,
no database server, and no local model server**. PostgreSQL 16 is also a supported direct
backend when an accessible server is provisioned. LLM, embeddings, and rerank stay URL-based; MinerU separately supports remote
HTTP (`MINERU_MODE=http`), an isolated same-host subprocess (`MINERU_MODE=cli`), or the
PyMuPDF4LLM fallback (`MINERU_MODE=off`, with pypdf as last resort). The pipeline runs offline with deterministic fallbacks
when no model service or MinerU parser is configured.

### Prerequisites

- **Python ≥ 3.13** — the SQLite write lock's fairness depends on CPython 3.13's
  `PyMutex`-backed `threading.Lock` handoff; older interpreters silently regress to
  writer starvation (see `backend/app/repositories/sqlite/database.py`).
- **Node.js ≥ 20** and npm
- **git**
- A C/C++ toolchain is needed *only as a fallback* — `numpy`, `rustworkx`, and `hnswlib`
  ship prebuilt wheels for common platforms; install Xcode Command Line Tools (macOS) or
  `build-essential` (Debian/Ubuntu) only if pip has to build one from source.

### 1 · Install

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

# Backend — into an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

# Frontend
( cd frontend && npm install )
```

### 2 · Configure

```bash
cp .env.example .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
```

`MODEL_SERVICES_CONFIG` points at the deployment-owned TOML. Edit its `[services]`
entries and `[bindings]`, choose each physical service's `max_concurrency`, and place only
the secrets named by `api_key_env` in `.env`. Delete the path or set it to an empty value
for explicit deterministic offline mode (keyword-only retrieval, no model extraction or
answers). Users cannot supply or override model credentials, endpoints, models, or capacity.

`ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` and
`ANSWER_AUDIT_MAX_FINDINGS` govern the internal post-terminal Ask extension
points. The timeout is cooperative: an in-progress synchronous callback is not
abandoned, while later contributions are skipped after its deadline. Exact
defaults and validation ranges live only in the product/API contract.

`REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` and
`REPORT_AUDIT_MAX_FINDINGS` independently govern Deep Report's post-terminal
auditor/observer points. They use the same cooperative semantics but never
share an Ask budget: a callback already started completes safely, later report
contributions do not start after the deadline, and an oversized finding set is
rejected whole. These hooks run only after a successful durable `done` CAS.
Exact defaults and ranges live only in the product/API contract.

A deployment that registers a parsed-element contributor configures it with
`SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS`,
`SOURCE_ELEMENT_ENRICHER_MAX_PROPOSALS`,
`SOURCE_ELEMENT_ENRICHER_MAX_METADATA_BYTES`, and
`SOURCE_ELEMENT_ENRICHER_MAX_CAPTION_CHARS`. The deadline is point-wide and
cooperative; proposal and byte limits reject an offending contribution as a
whole rather than truncating it. The default registry contains no contributor,
so these settings add no work until one is composed. Exact defaults and ranges
live only in the product/API contract.

A deployment that registers a knowledge candidate projector configures its
point-wide cooperative deadline and aggregate object, relation, and serialized
candidate budgets with `KNOWLEDGE_CANDIDATE_PROJECTOR_TIMEOUT_SECONDS`,
`KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_OBJECTS`,
`KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_RELATIONS`, and
`KNOWLEDGE_CANDIDATE_PROJECTOR_MAX_CANDIDATE_BYTES`. An in-progress synchronous
contribution is not abandoned, but a result returned after the deadline is not
admitted and no later contribution starts. A contribution that exceeds a
remaining budget is rejected atomically rather than truncated. The default registry contains no
projector, so these settings add no schema read, event, database, model,
embedding, or retrieval work until one is composed. Exact defaults and ranges
live only in the product/API contract.

A chat service may optionally set `top_p = 0.95` (or another finite value from `0` through
`1`) when its provider requires a fixed nucleus-sampling value. This service-owned value
overrides every workload's call default and is used by both the outgoing request and the
response-cache key. Omit the field for the historical per-call behavior. `top_p` is rejected
on embedding and rerank services.

The backend watches a non-empty model-services TOML and normally applies a changed file
within about two seconds. The watcher first requires the same changed file signature on two
consecutive observations, parses it, and verifies that the post-read signature still matches
before atomic publication; this prevents an in-place truncate/write save from publishing a
moving snapshot. An empty or comment-only configured TOML is rejected during reload rather
than interpreted as offline mode: clear `MODEL_SERVICES_CONFIG` and restart for that explicit
transition. A valid complete registry becomes the source for new calls, while already-submitted
calls finish on their original service generation. A missing, half-written, or invalid
replacement never clears the live configuration; the backend keeps the last valid registry and
emits a credential-safe diagnostic for a stable invalid file version. An explicit forced reload
skips the two-observation delay but retains the post-read check. Correct and save the file again
to trigger another watcher attempt.
Changes to `.env` secrets alone are not watched; restart the backend, or save the TOML again,
after changing a referenced secret. Removing `MODEL_SERVICES_CONFIG` from `.env` also still
requires a restart because the watched path itself is selected at startup.

- **Embedding dimensions** — `EMBED_DIM` must equal the bound embedding model's output
  dimension. Optional `EMBED_RUNTIME_DIM` (default `0` = off) truncates
  the similarity space to its first N dimensions + re-normalize (MRL) — cuts in-process
  matrix / ANN memory ~`EMBED_DIM/N`× while keeping the native vectors on disk as the
  source of truth. Switching it on/off requires rebuilding scale indexes; see
  [docs/runtime-dim-truncation-runbook.md](./runtime-dim-truncation-runbook.md). Never
  lower `EMBED_DIM` to shrink vectors — that discards every stored vector as wrong-dim.
- **PDF fidelity** (optional) — a MinerU endpoint, see [PDF parsing with MinerU](./operations.md#pdf-parsing-with-mineru);
  leave `MINERU_MODE=off` for the local PyMuPDF4LLM layout/Markdown fallback.
- **Selected-source PPR shadow gate** — `SOURCE_SUBGRAPH_PPR_ENABLED` controls only the
  internal sparse-PPR producer over a frozen selected-source snapshot and defaults to
  `true` for invisible shadow observation. It is independent of `GRAPH_PPR_ENABLED`: switching it off leaves historical
  whole-scope PPR, local graph primitives, direct retrieval, and the protected baseline
  lane unchanged. It becomes a candidate producer only when the separate selected-source
  activation gate below runs in shadow or active mode.
- **Large-source graph companions** — `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED`
  makes a scale rebuild/fold publish a separate source-addressable CSR companion;
  `SOURCE_PARTITIONED_PPR_ENABLED` permits its internal shadow consumer. Both default
  to `true` and are independent rollback controls. Turning on the consumer without a
  matching companion returns a capability-unavailable reason and never falls back to
  whole-graph traversal. `SOURCE_PARTITIONED_PPR_MAX_ITERATIONS` bounds request-time
  sparse passes; partition publication reuses the existing `SOURCE_SUBGRAPH_MAX_*`
  row rails. Ask/Deep Report may consume it only through the shared activation gate; a
  missing/mismatched companion remains a local unavailable reason, never a whole-graph fallback.
- **Selected-source rollout quality gate** — active modes are permitted only from a
  verified, content-free attestation produced by the paired evaluation command. The
  digest is an integrity check, not an authorization signature: keep the input and
  output in a deployment-owned trusted artifact location, and pin the expected corpus
  signature and model contract through `SELECTED_SOURCE_GRAPH_EXPECTED_CORPUS_SIGNATURE`
  and `SELECTED_SOURCE_GRAPH_EXPECTED_MODEL_JSON`. `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE`
  defaults to invisible `shadow`, which never enters public API payloads, traces, streams,
  or UI and is the only mode that may run without an approved attestation.
  `allowlist`, stable-hash `rollout`, and `on` additionally use the configured trusted
  attestation path and fail closed on any mismatch. Ask and Deep Report share this gate.

`.env.example` is the authoritative, fully-commented list of non-service variables and
secret slots; `model-services.example.toml` is the service/binding/capacity template.
[Configuration](#configuration) groups the common ones.

#### Upgrading an old role-based `.env`

Existing deployments can convert the retired `OPENAI_COMPAT_*`, `KG_LLM_*`,
`EMBED_*`, and `RERANK_*` settings into the system-owned configuration:

```bash
# Preview only; reads .env and writes nothing.
python scripts/migrate_legacy_model_env.py --env .env

# After reviewing the service list and inferred capacities, create the TOML and
# rewrite only the managed model assignments in .env.
python scripts/migrate_legacy_model_env.py --env .env --apply
```

The apply step backs up `.env`, tightens the active file and every backup containing
secrets to mode `0600`, keeps secrets in newly named `.env` slots, and writes no
credentials to the TOML or terminal. It preserves legacy role fallbacks and folds
identical endpoints/models/keys into one physical service. Initial capacities are
inferred from the retired `KG_EXTRACT_WORKERS`, `KG_ASK_RESERVE`, and
`EMBED_CONCURRENCY` values; they are migration estimates, so verify them against each
provider's real capacity. Override an estimate with a repeatable option such as
`--max-concurrency general=20 --max-concurrency embedding=4`. An unchanged example
TOML created by setup can be replaced directly. Any other existing output must be
reviewed first and requires `--force`; every replaced file is backed up.

**Remote access — note the browser is on a *different* machine** than the server, so it
cannot use `localhost`/`127.0.0.1` (those resolve to each visitor's own machine).

**Recommended for a single co-located server (same-origin proxy):** the Next.js frontend
proxies `/api/*` to the local backend (`frontend/next.config.mjs`), so browsers only ever
talk to the frontend's origin. Point the frontend at a relative `/api` — then you need **no
CORS config** and **don't expose the backend port**:

```bash
# frontend/.env.local  (NEXT_PUBLIC_* is baked at BUILD time → rebuild after changing)
NEXT_PUBLIC_API_BASE_URL=/api
```

The backend can stay on `127.0.0.1:8000` (the proxy reaches it locally); only the frontend
port needs to be network-reachable. Set `BACKEND_PROXY_TARGET` if the backend isn't on
`127.0.0.1:8000`.

**Alternative — frontend and backend on different hosts (two-origin, direct):** point the
frontend at the backend's reachable URL and allow its origin on the backend:

```bash
# frontend/.env.local  (baked at build time)
NEXT_PUBLIC_API_BASE_URL=http://<backend-host>:8000/api
# backend repo-root .env — comma-separated allowed origins; `*` not allowed (credentials on)
SILICON_NOTEBOOK_CORS_ORIGINS=http://<frontend-host>:3000
```

Then run uvicorn with `--host 0.0.0.0` (or `BACKEND_HOST=0.0.0.0 npm run dev`) so the API is
reachable from other machines.

### 3 · Run

There is no manual schema step — on first boot the backend migrates the selected SQLite
or PostgreSQL datastore, creates the `.local/storage` and `.local/logs` directories, and
seeds only the local user. Always run the backend **without `--reload`**: a reload restart kills in-flight
ingestion background tasks and leaves uploads stuck at `extracting`.

All relative paths (database, storage, logs, `.env`) are **anchored to the repo root in
code**, regardless of which directory a script `cd`s into to launch a process — the
launch directory no longer matters. The backend's first log line prints the resolved
absolute paths (`paths: db=... storage=... log_dir=...`); check it if you're ever unsure
which `.local/` a given launch is actually using. The offline CLI (`scripts/batch_ingest.py`)
and both servers below all resolve to the same repo-root `.local/`.

**The launch scripts require a repo-root `.env`** (`npm run dev` / `npm run start`): if it
is missing they abort with an explicit error instead of silently booting on blank defaults,
and a renamed leftover such as `.env.local` is called out by name — note that Next.js's own
"Environments: .env.local" line only means the *frontend* read it; the backend reads `.env`
only. The backend applies the same check at startup (hard error only when a lookalike file
exists; a plainly missing `.env` logs a warning and boots, so fresh checkouts and containers
injecting real environment variables keep working). Set `ALLOW_NO_ENV_FILE=1` to skip the
check for env-var-only deployments.

```bash
# Development — backend (reload-friendly) and frontend together
npm run dev
```

```bash
# Production — builds the frontend, then serves both (single backend worker)
npm run start

# Stop both services (from any terminal — no need to Ctrl-C the start process)
npm run stop
```

`npm run start` runs `scripts/prod.sh`: it first installs the backend requirements into
`PYTHON_BIN`'s environment with `python -m pip install -r backend/requirements.txt`, then
recreates the locked frontend dependency tree with `npm ci --prefix frontend`. It completes
`next build` in the foreground, then launches `next start` and the single-worker Uvicorn
backend under `nohup`, with stdin detached and both logs written under `.local/logs/`.
The command exits immediately after launching both detached processes; it does not poll backend
readiness or frontend HTTP access. The services therefore survive closing the terminal, and the
operator must verify `/api/ready` and the frontend separately. An interruption before both
children are handed off sends SIGTERM to both launched children together, waits the bounded
`START_CLEANUP_GRACE_SECONDS` (default 10 seconds), SIGKILLs any survivor, and reaps both.
One of `ss`, `lsof`, or `fuser` is required. Occupied target ports fail before dependency
installation even when the current user cannot see the listener PID, so an old listener cannot
hide a bind failure in the newly launched process. Use `npm run stop` for the detached services.
Prebuilt images that already
contain both dependency sets may set `SKIP_INSTALL=1`; with that escape hatch, a missing
`frontend/node_modules/.bin/next` still fails before build rather than silently continuing.

Set `SKIP_BUILD=1` to reuse an already-built `frontend/.next` (e.g. a prebuilt image).
Override `BACKEND_HOST` / `PORT` / `FRONTEND_PORT` to change bind address/ports. The
backend defaults to `127.0.0.1`; binding it to a non-loopback address requires a
non-default `SILICON_NOTEBOOK_ADMIN_PASSWORD` and fails fast otherwise.

For Agent MCP deployments, set `MCP_PUBLIC_URL` to the exact client-reachable `/mcp` URL.
That value configures both MCP transport metadata and the anonymous machine-readable
`GET /api/agent-mcp/onboarding` document linked beside newly issued tokens. Public networks
must also set `MCP_REQUIRE_HTTPS=1`; the onboarding link never carries the bearer token.
Startup rejects userinfo, query strings, fragments, paths other than exactly `/mcp`, and
whitespace/control/backtick characters because this value is rendered into Agent instructions.

The supported production diagnostics target is Ubuntu 24.04 running this normal
`npm run start` flow with its single Uvicorn worker. If that deployment appears hung,
leave it running and capture the incident **while the hang is still present**; see
[Live production incident capture](./operations.md#live-production-incident-capture). Restarting first
destroys the active-request, lock, process, and stack evidence the command is designed
to correlate.

`npm run stop` runs `scripts/stop.sh`, which terminates whatever is listening on the
backend `PORT` and frontend `FRONTEND_PORT` (default `8000` / `3000`) — it sources the
repo-root `.env` for those ports just like start, so if you launched with a custom
`PORT` / `FRONTEND_PORT` set the same value when stopping. It sends `SIGTERM`, waits, then
`SIGKILL`s any survivor, and is a no-op if nothing is running. It locates the listeners
with `ss` (shipped by iproute2 on Ubuntu/Linux), falling back to `lsof` (default on macOS)
and then `fuser` — at least one must be available.

> **One-time migration** — if you previously launched with `npm run dev` (or manually `cd
> backend && uvicorn ...`) on a version before path-anchoring landed, your data may be
> sitting under `backend/.local` instead of the repo-root `.local`. Either merge it in
> (`mv backend/.local/* .local/` from the repo root, checking for conflicts first) or keep
> the old location by pointing at it explicitly with absolute-path env vars
> (`SILICON_NOTEBOOK_STORAGE_DIR=/abs/path/storage`,
> `DATABASE_URL=sqlite:////abs/path/silicon_notebook.db` — note the four slashes for an
> absolute sqlite path) — absolute env values are always respected as-is and never
> re-anchored.

### 3.1 · Select SQLite or PostgreSQL

`DATABASE_URL` is the only active-backend selector. It accepts `sqlite:///...`,
`postgresql://...`, and the normalized legacy alias `postgres://...`; unsupported schemes,
connection failures, migration failures, and warmup failures fail closed without falling
back to another database. `SHADOW_DATABASE_URL` never selects the active backend and setting
it alone starts no synchronization; it is consumed only by the explicit forward-shadow CLI.

```dotenv
# Shipped default
DATABASE_URL=sqlite:///.local/silicon_notebook.db

# Direct PostgreSQL 16 backend
DATABASE_URL=postgresql://silicon_app:change-me@127.0.0.1:5432/silicon_notebook

# Optional one-way shadow target while DATABASE_URL remains SQLite
# SHADOW_DATABASE_URL=postgresql://silicon_shadow:change-me@127.0.0.1:5432/silicon_notebook_shadow
```

PostgreSQL must use UTF-8 and have `pg_trgm` installed in `public`. The database owner may
let migration 0001 create it, or a DBA may preinstall it. An extension of that name in
another schema is rejected. PostgreSQL stores vectors as float32 `bytea`; pgvector is not
required. Keep production at one backend worker (`--workers 1`).

Large PostgreSQL databases should also install `btree_gin` in `public` and the two
notebook-aware lexical indexes. The operator tool can create the extension when run as the
database owner; it inspects by default and changes the database only with `--apply`:

```bash
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py --apply
```

These indexes are intentionally an online operational rollout rather than a startup
migration: building a GIN index over a multi-million-row live table can consume substantial
CPU, I/O, temporary disk, and wall time. The application remains correct without them, but
common lexical terms may scan global trigram matches before filtering by notebook and can
hit the statement timeout. See the monitored rollout and rollback procedure in
[Operations](./operations.md#postgresql-notebook-aware-lexical-indexes).

Changing the URL never moves existing rows. For a fresh target, stop the service, change
the URL, start, and verify the empty/bootstrap state. For an existing SQLite source, the
delivered forward-shadow CLI can build and continuously maintain a PostgreSQL shadow while
SQLite remains active. It requires PostgreSQL 16, a dedicated empty/restorable target,
verified source and target backups, capacity evidence, an owner-private work directory, and
one supervised worker. It never changes `DATABASE_URL`, sends traffic to PostgreSQL, or
replicates PostgreSQL writes back to SQLite. Detailed commands, monitoring, and failure
handling are in [Operations](./operations.md); the packaged checklist is also in
[packaging/DEPLOY.md](../packaging/DEPLOY.md).

### 4 · Verify

```bash
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok","llm_configured":...}
bash scripts/check.sh                        # hermetic smoke + full pytest + frontend test/tsc/build
```

`scripts/check.sh` also runs the contract gates below, each of which can be run on its own
while iterating on the code it guards:

```bash
PYTHONPATH=backend python scripts/check_ask_modes_contract.py            # ask-mode id set
PYTHONPATH=backend python scripts/check_object_type_labels_contract.py   # object_type display names
PYTHONPATH=backend python scripts/check_ui_vocabulary.py                 # user-facing vocabulary
```

The backend writes structured JSONL logs under `.local/logs/` (`requests` / `events` /
`llm`); see [Observability](./operations.md#observability) to follow an upload or diagnose a stuck source.

### 5 · Offline packaging (target has no npm/node)

To deploy onto a machine with **no npm/node**, only a Python package index, and **no root**,
build a self-contained bundle on a host that *does* have Node — and whose **OS/CPU
architecture matches the target** — then ship a single tarball:

```bash
bash scripts/pack.sh          # → dist/silicon_notebook_<version>_<os>-<arch>.tar.gz
```

`pack.sh` builds the frontend as a Next.js **standalone** server, bundles a **portable Node
runtime** (matching the build host's arch) to run it, and prebuilds a **wheelhouse** of every
Python dependency — so compiled packages like `hnswlib` / `scipy` need no compiler on the
target. Because the build host and target share OS/arch, every bundled binary runs as-is.

On the target — no npm/node, no root:

```bash
tar xzf silicon_notebook_<version>_<os>-<arch>.tar.gz
cd    silicon_notebook_<version>_<os>-<arch>
./install.sh    # user-local venv; installs deps offline from wheelhouse
                # (falls back to the pip index for anything missing); writes .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
vi .local/model-services.toml  # services, workload bindings, per-service max_concurrency
vi .env         # MODEL_SERVICES_CONFIG + secrets referenced by api_key_env
./start.sh      # portable-node standalone frontend + venv uvicorn backend
./stop.sh       # stop both
```

Build-host knobs: `NODE_VERSION` / `NODE_DIST_URL` / `NODE_TARBALL` (portable-Node source),
`SKIP_WHEELHOUSE=1` (target installs deps online instead), `PIP_INDEX_URL`, `PACK_PYTHON`.
Target knobs: `PYTHON_BIN`, `PIP_INDEX_URL`, `FRONTEND_HOST` / `FRONTEND_PORT` / `BACKEND_HOST`
/ `PORT`. The build host's Python **minor** version should match the target's, or the prebuilt
wheels won't install (install.sh then falls back to the index). The bundle's `DEPLOY.md` has
target-side details.

## Configuration

All model services are reached over URL endpoints — no local model servers are started.

### System model services, scheduling, and diagnostics

Model endpoints, protocols, models, workload bindings, and service capacity are
owned by the deployment, not by individual users. Copy
`model-services.example.toml` to `.local/model-services.toml`, set
`MODEL_SERVICES_CONFIG=.local/model-services.toml`, and put only the secrets
referenced by each service's `api_key_env` in `.env`. The checked-in example
contains no credentials. An empty `MODEL_SERVICES_CONFIG` explicitly selects
offline/deterministic fallbacks.

Each `[services.<id>]` table defines `display_name`, `kind`, `protocol`,
`base_url`, `model`, `api_key_env`, and `max_concurrency`. The
`[bindings]` table maps stable workload ids such as `ask_answer`,
`reasoning_agent`, `knowhow_complete`, `kg_extract`, `retrieval_query_embedding`,
`retrieval_rerank`, `agent_profile_consolidate` (labeled "库理解整理" in the model
service status page; the background consolidation call behind "AI 对这个库的理解", see
`AGENT_PROFILE_ENABLED` below), and `retrieval_experience_distill` (the deployment-wide,
low-frequency offline call that distills the closed-vocabulary retrieval-tactics library,
see `RETRIEVAL_EXPERIENCE_ENABLED` below — deliberately its own workload rather than
sharing `agent_profile_consolidate`'s, so a deployment can point it at a different model
or disable it independently of notebook-understanding consolidation) to those services. Several workloads may share a service;
all of them share that service's one scheduler and one concurrency budget.
`max_concurrency` is the only model-capacity setting. Source-job counts,
window sizes, batch sizes, and local ANN threads do not create another model
gate.

The optional generated-question index uses background chat workload
`chunk_question_generation` plus the existing `chunk_embedding` workload. Bind both
before running the offline `question-index` phase. Leaving the rollout mode off is the
zero-cost default; deployment rollout semantics and all numeric rails live in the
[Product and API reference](./product-and-api.md#optional-generated-question-recall-supplement).

Knowhow row completion uses two interactive chat workloads: `reasoning_agent`
plans and reflects over federated evidence from the active notebook and its
valid mounted reference libraries, then `knowhow_complete` turns that evidence
and the same-table examples into structured suggestions. Bind both to compatible
chat services when this feature is wanted. Leaving either unbound, or a provider
failure in either stage, yields no suggestion; the application never silently
falls back to table-only or fabricates an offline completion.

Deep Report separates workload shape from planning quality: the checked-in
example binds `report_outline` to the reasoning service, while the long-body
`report_section` and large structured final-editor `report_summary` workloads
use the non-reasoning general service. Deployments may override these bindings,
but the selected provider/model must emit ordinary content within the configured
completion budget rather than consuming it entirely as hidden reasoning.

Scheduling policy is fixed in code:

- at most `max_concurrency` calls run for one physical service, while different
  services have independent slots;
- the total queue is bounded to `10 × max_concurrency`, and one actor may queue
  at most `2 × max_concurrency` items;
- dispatch repeats an 8 interactive : 2 report : 1 background pattern and
  round-robins actors within each lane, so background work progresses under
  sustained interactive traffic;
- queue deadlines are 30 seconds for interactive work, 300 seconds for reports,
  and 1,800 seconds for background work; cancellation is honored before dispatch;
- a fatal provider failure opens the circuit immediately; three consecutive
  transient failures also open it. The cooldown is 30 seconds and admits one
  half-open recovery probe.

The scheduler and breaker are process-local. Production must run exactly one
backend process (`scripts/prod.sh` pins Uvicorn to `--workers 1`); multiple
workers would multiply the configured service concurrency and split queue,
breaker, and health state.

The **模型服务** panel is read-only for ordinary users. It shows sanitized
system service identity, bound workloads, last-known health, active/maximum,
queued work, oldest wait, and breaker state. Reading
`GET /api/model-services/status` never probes an upstream service. Only admins
can explicitly probe one service or all services through
`POST /api/admin/model-services/{service_id}/test` and
`POST /api/admin/model-services/test-all`. Provider endpoints, credentials,
response bodies, and raw exceptions stay in server logs.

Ask/model failures carry the physical service, workload, safe model label, and a
`support_id` when available. Users should send that support id to maintainers;
maintainers can correlate it with server logs and the read-only service panel to
identify the failed model service. Local retrieval/index failures do not mark a
provider unhealthy.

Personal model configuration routes and their editable UI have been removed.
Schema migration v24 irreversibly replaces every historical
`user_profiles.model_settings` value with `{}` and deletes the old per-user
health rows in the same transaction as the version stamp. Back up the database
before upgrading if those historical credentials are needed for an external
record; they are not restored or reused by the application.

Model-call timeout, retry, output-budget, and batching settings remain normal
workload tuning. `EMBED_DIM` must match the bound embedding model. KG source
parallelism remains `KG_JOB_CONCURRENCY`, and adaptive extraction windows use
the `kg_extract` service capacity; neither setting overrides service
`max_concurrency`.

**Core-aware autotune:** local CPU-bound work may scale with the machine:

```text
KG_CLUSTER_ANN_THREADS   # hnswlib concept-clustering threads;
                         # 0 (default) = min(cpu_count, 32)
```

`scripts/dev.sh` and `scripts/prod.sh` source `scripts/autotune.sh` for
local OMP/BLAS threads. This does not change any model-service capacity.

**Database:**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout in ms (default 30000)
DB_WRITE_LOCK_STATS         # enable process-wide SQLite write-lock wait/hold instrumentation (default true)
DB_WRITE_LOCK_WARN_MS       # wait/hold threshold in ms that logs a rate-limited db_write_lock_slow event (default 200)
DB_WRITE_LOCK_FLUSH_SECONDS # interval in seconds for the periodic db_write_lock_stats snapshot, and the per-call-site rate-limit window for db_write_lock_slow (default 60)
SQLITE_CACHE_SIZE_KB    # Per-connection SQLite page cache in KB (negative = KB). Connections are reused per-thread; total memory ≈ threads × |value| (default -16384)
DATABASE_URL            # SQLite path (default .local/silicon_notebook.db)
SILICON_NOTEBOOK_STORAGE_DIR   # uploaded file storage directory (default .local/storage)
```

**Source uploads:**

```text
SOURCE_UPLOAD_MAX_MB    # maximum size in MB for one uploaded source file (default 50)
```

`SOURCE_UPLOAD_MAX_MB` is a whole-number setting from 1 through 1024; one MB is exactly
`1024 × 1024` bytes. The backend derives and enforces that byte limit for every
multipart source file (413 includes the active limit). After login, the browser
reads the parsed byte limit from `GET /api/system/config`, displays it in the
add-source dialog, rejects oversized selections immediately, and rechecks the
staged files before it sends multipart data. Both sides also enforce a fixed
20-file maximum per multipart request so the configured per-file allowance cannot
multiply into an unbounded temporary spool. The backend remains authoritative for
stale tabs and direct API clients. In a same-origin deployment, Next.js's external
rewrite also needs a whole-request transport envelope: its independent default is
only 10 MiB and would otherwise truncate a valid multipart upload before the backend
could enforce `SOURCE_UPLOAD_MAX_MB`. The frontend build derives that envelope from
the same per-file setting, the fixed batch count, and bounded multipart overhead; it
adds no second user-visible size setting. Offline standalone bundles build the
transport envelope against the allowed protocol maximum so a target-machine runtime
`.env` remains free to select any valid `SOURCE_UPLOAD_MAX_MB`.

**Retrieval:**

```text
RETRIEVAL_TOP_N         # reasoning/report synthesis evidence-budget floor (default 20)
REASONING_PER_QUERY_LIMIT # per-query take for compatibility callers without an effort profile
REASONING_TOP_N_PER_QUERY  # adaptive budget: seats reserved per aspect/sub-query (default 3)
REASONING_TOP_N_CAP        # adaptive budget cap; comparison Qs scale by #aspects (default 36)
ASK_RELATED_KNOWLEDGE_LIMIT # related-KG records projected into an Ask response
QUERY_REFINE_MAX_ITEMS / ASK_CONTEXT_RELATION_LIMIT # refined evidence bullets and ranked relationship lines admitted to Ask context
GRAPH_SEED_TOP_N / GRAPH_MAX_DEPTH / GRAPH_MAX_FAN_OUT # graph-walk candidate rails
CHUNK_KG_NODE_SEED_TOP_N / CHUNK_KG_RELATION_SEED_TOP_N / CHUNK_KG_MAX_DEPTH / CHUNK_KG_FAN_OUT # chunk×KG overlay rails
CHUNK_GRAPH_RESERVE        # seats for graph-only chunks already above the relevance floor (default 0; set 1 after evaluation)
EXACT_LOOKUP_ENABLED       # exact-identifier fast path: whole-section fetch for `set_db`-style names (default true)
EXACT_LOOKUP_MAX_IDENTIFIERS       # names probed per question (default 3)
EXACT_LOOKUP_FTS_K                 # exact hits sampled per identifier when ranking sections (default 50)
EXACT_LOOKUP_MAX_SECTIONS          # sections fetched whole per question (default 3)
EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION  # chunks taken per section (default 12)
EXACT_SECTION_RESERVE      # mix-selection seats reserved for those chunks, inside the existing budget (default 4)
```

**Behaviour change — these settings no longer size a deep report's per-section
deep dive.** That deep dive now maps the report's own research-depth level onto
the same five-level budget table Ask's retrieval effort uses, and passes the whole
row down; the numeric contract lives in `docs/product-and-api.md`
(「大纲便签与按节合成」 and the effort table), not here.

* `REASONING_TOP_N_PER_QUERY` / `REASONING_TOP_N_CAP` — no longer read on the
  report path at all. The level's per-aspect seats and cap decide the section's
  final relevance budget.
* `RETRIEVAL_TOP_N` — no longer the report section's evidence floor (the level's
  floor is). It still bounds the top-up retrieval that executes each confirmed
  outline direction.
* `REASONING_MAX_SUBQUERIES` — no longer read on the report path. This one bites
  at the **default** depth 2 too: that level allows 5 first-round sub-queries per
  section where the settings-derived path allowed `REASONING_MAX_SUBQUERIES + 1`
  (6 at the default).

Raising these four therefore no longer widens a report; raise the research depth
instead. Ask's `mix`/`graph` modes and any reasoning run made without an effort
level still read them unchanged.

The same applies to the two **context assembly** budgets a report section used to
size from settings — the level's own numbers replace them:

* `ANSWER_CONTEXT_BUDGET_CHARS` — no longer the report section's KG-context
  budget (the level's `kg_context_chars` is: 4000/6000/8000/12000/16000). It
  still sizes Ask's answer context.
* `REPORT_SECTION_CHUNK_BUDGET` — no longer read when the report supplies a
  research depth (the level's `chunk_context_chars` is:
  12000/30000/50000/80000/120000, and the direct-element sub-budget is derived
  from it as before). It remains the value for callers with no depth.

A report section also admits at most the level's `answer_element_items` direct
source elements (4/6/8/12/16), chosen by retrieval relevance rather than
insertion order.

**Exact-identifier fast path:** when a question names something exactly
lookup-able (`set_db`, `place_opt_design`, `config.yaml`), retrieval first
locates the section that name occurs in and fetches that whole section, so a
command's Arguments and Examples cannot be split off and truncated away.

The gate for this channel is narrower than the identifier extraction used for
ordinary lexical recall, and the difference is a cost decision: a name joined by
`_` or `.` always qualifies, but a purely hyphen-joined word must contain a
digit. `GPT-4` and `v1-2` therefore probe; `state-of-the-art`, `real-time` and
`end-to-end` do not. Those appear in a large share of analytical questions
(every deep-report section question, in practice), and each would buy a real
substring probe — measured at 16 ms / 50 hits on a 20k-chunk library — whose hit
can promote an entire chapter into the answer's evidence budget. They remain in
lexical recall, where an extra OR-ed term is nearly free.

"Fetch the whole section" depends on heading breadcrumbs, which are a property
of the Markdown parsing path. Sources parsed by MinerU (PDF/DOCX) carry no
breadcrumbs; there the channel returns exactly the chunks that matched instead
of a whole section. That is still the behaviour the feature exists for — the
parameter table survives — but it is not section completion, so a library whose
manuals are PDFs benefits less than one whose manuals are Markdown.

The channel itself makes zero model and zero embedding calls; note that in the
mix answering path its chunks do join the existing rerank call, adding at most
`EXACT_LOOKUP_MAX_SECTIONS × EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION` documents to
that one request. Every query is bounded by the settings above, and a question
with no such name issues no additional queries at all. It searches the active
notebook only; mounted reference libraries are deliberately out of scope.

**Bounded KG relation completion:** this post-extraction stage is a deployment
experiment and is strictly disabled by default. It advances persistent, generation-bound
`(source_id,id)` keyset pages and uses only indexed per-candidate relation checks plus
bounded same-source FTS/ANN candidates; it never full-scans the notebook or book.
`shadow` records aggregate proposal/verification statistics but writes nothing. `write`
inserts verified relations as `pending`. Both active modes still require the notebook
allowlist or stable hash rollout gate. Each invocation hydrates only the bounded page's
capped evidence IDs. A pending watermark re-enqueues as another bounded job; after a
process restart, startup schedules current pending source generations again.
Watermarks are mode-specific: changing between active modes atomically publishes
the new recoverable cursor before marking the old pending cursor `stale`; switching
to `off` marks the old cursor stale and schedules no replacement work.

```text
KG_RELATION_COMPLETION_MODE              # off (default) | shadow | write
KG_RELATION_COMPLETION_NOTEBOOK_ALLOWLIST # comma-separated notebook ids; * matches all
KG_RELATION_COMPLETION_ROLLOUT_PERCENT    # stable rollout for non-allowlisted notebooks (default 0)
KG_RELATION_COMPLETION_MAX_OBJECTS        # anchors per keyset page (default 160)
KG_RELATION_COMPLETION_MAX_PAIRS          # issued directed candidate pairs (default 120)
KG_RELATION_COMPLETION_SECTION_QUOTA      # source-section candidate cap (default 24)
KG_RELATION_COMPLETION_BATCH_PAIRS        # candidates per proposer/verifier batch (default 24)
KG_RELATION_COMPLETION_MAX_BATCHES        # maximum model batches per run (default 4)
KG_RELATION_COMPLETION_EXCERPT_CHARS      # maximum characters per candidate excerpt (default 800)
KG_RELATION_COMPLETION_MAX_PAGES_PER_RUN  # bounded keyset pages per invocation (default 4)
KG_RELATION_COMPLETION_NEIGHBOR_TOP_K      # FTS/ANN neighbors per anchor (default 8)
KG_RELATION_COMPLETION_CANDIDATE_OVERFETCH # total candidate-id hydration cap (default 64)
KG_RELATION_COMPLETION_BATCH_CHARS         # serialized candidate characters per batch (default 48000)
```

The stage reuses the existing `kg_extract` proposer and `kg_refine` verifier workload
bindings. A final short transaction rechecks that the source/run generation is still
current and every object/evidence element still belongs to it, then persists the exact
server excerpt seen by the verifier; reparse/delete races therefore insert nothing.
All numeric rails shown above must be positive (batch characters at least 512); invalid
values fail settings validation, and runtime zero rails fail closed without moving a
watermark. Start with an explicit allowlist in `shadow`, inspect
`kg_relation_completion_done` aggregate events, then move selected notebooks to `write`.

**Scalable-retrieval index:** notebooks large enough to be non-copyable (the same size
threshold used to gate notebook copy/sharing — bytes or chunk+node count over the
configured limit) get their scale index built or refreshed automatically, no manual
button/CLI step required: on source extraction, on KG rebuild, and as a fallback the first
time a query finds no index. By default the build is queued for a low-traffic off-peak
window rather than run immediately.

```text
SCALE_INDEX_AUTO_ENABLED   # auto-build/refresh the scale index for large notebooks (default true)
SCALE_INDEX_AUTO_WHEN      # "idle"=queue for the off-peak window (default) | "now"=build immediately
STARTUP_PRELOAD_SCALE_INDEXES # load every published scale index + enabled ANN + safe single-index PPR core before readiness (default true)
SCALE_IDX_CACHE_MAX        # max resident scale indexes; must be >= the published live-index count when preload is enabled (default 8)
```

With startup preload enabled, `/api/ready` remains false during the
`preloading_indexes` phase. A corrupt required artifact, or more live published indexes
than `SCALE_IDX_CACHE_MAX`, keeps startup not-ready instead of passing the cold load to
the first user. Size the cache and RAM for all resident indexes. Temporarily set
`STARTUP_PRELOAD_SCALE_INDEXES=false` only to enter the UI/maintenance flow and rebuild
a damaged index; restore it afterward. `scripts/backend.sh start` waits up to 1,800
seconds by default and prints readiness phase changes; override
`START_TIMEOUT_SECONDS` for unusually slow storage.

The preload boundary covers reusable on-disk artifacts, ANN handles, and each
ScaleIndex's self-only PPR transition/chunk-id core. It deliberately does not eagerly
materialize every cross-notebook mounted combined graph: the current multi-participant
composition copies full node maps and may reconstruct all CSR edges, so doing that for
all mount combinations can multiply a 10M-node graph until startup OOMs. Those combined
graphs remain lazy until they have a bounded/shared representation. The strict guarantee
covers the artifact set published at startup. Runtime build/fold and a newly added
`SCALE_IDX_CACHE_MAX+1` index still use the existing online publication path; size the
cache before that change and restart to re-establish the readiness guarantee.

**Notebook copy vs read-only share — size gate:** sharing a notebook offers a deep
*copy* when it is small enough, otherwise a read-only *join*. "Small enough" (and the
non-copyable threshold above) is the same set of bounds — a notebook must be under ALL
of them to be copyable. A deep copy reads every one of the notebook's tables into memory
to remap ids, so the last bound caps that total independently of the chunk+node count:

```text
NOTEBOOK_COPY_MAX_BYTES          # max total source-file bytes (default 50MB)
NOTEBOOK_COPY_MAX_ROWS           # max chunks + knowledge objects (default 5000)
NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS  # max TOTAL rows a deep copy would materialise across every
                                 # table (relations / embeddings / elements / knowhow included) —
                                 # a defense-in-depth cap so a graph/embedding fan-out far
                                 # exceeding the chunk+node count cannot OOM the copy; over it,
                                 # the notebook is offered as a read-only share (default 200000)
```

**Content-addressed cache (LLM + embedding calls):**

Repeat calls with identical content — same model, same prompt or text — reuse the
previous result instead of hitting the model again; large-scale re-runs (e.g.
re-extracting an already-processed library) are the main beneficiary. Stored in its
own SQLite file, separate from the main database. Health/availability probes always
bypass it, so a cached success can never mask a live model outage.

```text
LLM_CACHE_ENABLED        # content-addressed cache switch (default true)
LLM_CACHE_PATH           # cache DB path (default .local/llm_cache_v2.db)
LLM_CACHE_SIZE_LIMIT     # size cap in bytes; least-recently-used entries evicted first past this (default 2147483648 = 2 GiB)
LLM_CACHE_TTL_DAYS       # max entry age in days before treated as expired (default 90)
```

*Inspecting and clearing the cache (admin only).* The cache is keyed by model
name plus the exact request content, so changing a prompt or switching to a
differently-named model invalidates itself — nothing to do. The one case that
needs a manual step is **the weights behind an unchanged model name being
replaced**: the key does not change, so old answers keep being replayed until
the 90-day TTL expires. Clear that model's entries after such a swap:

```bash
# What is in the cache right now: totals, hit rate, entries per model
curl -H "Authorization: Bearer $TOKEN" http://<host>/api/admin/cache

# Drop one model's entries (do this after replacing a model service)
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"tag": "<model-name>"}' http://<host>/api/admin/cache/evict

# Drop everything (the flag is required — there is no "empty means all")
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"clear_all": true}' http://<host>/api/admin/cache/evict
```

Use `by_tag` from the first call to see which model names are worth clearing.
Losing cache entries is always safe — the next call just goes to the model
again.

**Retrieval / KG enhancements (GraphRAG + ToG-3 borrow, Phase 1+2):**

A mix of opt-in (default off) and on-by-default knobs. On by default: `ANSWER_CONTEXT_*`,
`KG_QUERY_REFINE_ENABLED`, and the KG-quality passes `KG_REFINE` / `KG_GLEANING` /
`KG_CONCEPT_DESC`. Enable other extras **one at a time** and validate with the eval
harness (`backend/app/eval`) — turning RRF + rerank + refinement on together regressed
answer quality in eval.

```text
KG_REFINE_ENABLED            # extraction self-verify: drop hallucinated nodes (default true)
KG_GLEANING_ENABLED          # extra rounds asking the LLM for MISSED nodes (default true)
KG_GLEANING_ROUNDS           # gleaning rounds when enabled (default 1)
KG_CONCEPT_DESC_ENABLED      # LLM-fuse cross-doc concept-cluster descriptions (default true)
KG_COMMUNITY_SUMMARY_ENABLED # LLM community reports during rebuild (community layer; default false)
ANSWER_CONTEXT_BUDGET_CHARS  # answer-context assembly char budget (default 6000; not read by a deep report's sections — see the retrieval behaviour-change note)
ANSWER_CONTEXT_MIN_ITEMS     # keep >= N items regardless of budget (default 3)
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF ranking vs keyword+semantic fusion (default false)
RETRIEVAL_RRF_K              # reciprocal-rank-fusion k (default 60)
KG_QUERY_REFINE_ENABLED      # question-aware evidence refinement before answering (default true)
QUERY_REFINE_MAX_CHARS       # max chars of evidence fed to refinement (default 4000)
GLOBAL_MAX_COMMUNITIES       # accepted for compatibility; the retired `global` mode is a `chunk` alias, so this is currently unused (default 20)
RELATION_RETRIEVAL_ENABLED   # relation-vector retrieval for graph/reasoning seeds (default false, opt-in pending eval)
RELATION_SEED_TOP_N          # top relation/node hits fed as graph seeds when enabled (default 8)
KG_CANONICAL_FOLD_ENABLED    # fold same-canonical fragmented KG nodes at retrieval (default false)
KG_ABOUT_DOWNWEIGHT_ENABLED  # rank-down-weight weak `about` edges in relation retrieval (default false)
KNOWHOW_KG_NODE_RETRIEVAL_ENABLED # projected Knowhow cell objects join reasoning/graph KG-node retrieval (default true; false disables only this direct-node path, not cell-chunk search)
REASONING_ENUM_TOOLS_ENABLED # reasoning Ask's typed collection-enumeration reflect tools, enumerate_elements/enumerate_kg_objects (default true; false disables both tools and the collection map, zero extra queries)
REASONING_OUTLINE_ENABLED    # reasoning Ask's outline scratchpad reflect action, update_outline (default true; only offered at the `exhaustive` effort tier regardless of this flag; false disables the action and section-by-section synthesis, reverting to byte-identical pre-feature behavior); also gates Deep Report's per-section deep-dive at its exhaustive depth tier (depth 16, see REPORT_MAX_SECTIONS below) — same flag, no report-specific toggle
REASONING_OUTLINE_KG_GAP_ENABLED # weak-support KG relation hint fed back into the outline scratchpad after each accepted update_outline (default true; layered on top of REASONING_OUTLINE_ENABLED; false stops the scratchpad from carrying weak-support relation hints, zero extra queries); applies identically inside Deep Report's per-section deep-dive once it reaches the exhaustive depth tier
AGENT_PROFILE_ENABLED        # single gate for "AI 对这个库的理解": plan/reflect injection, the background consolidation trigger, and both API surfaces' visibility (default true; false reverts everywhere to byte-identical pre-feature behavior — no injection, no trace step, no job is ever queued, the API reports enabled=false instead of 404)
AGENT_PROFILE_BASE_TRIGGER   # accumulated source changes before the shared base layer (corpus_shape/key_entities/corpus_gaps) is re-consolidated (default 5)
AGENT_PROFILE_OVERLAY_TRIGGER # completed Ask jobs before one member's private overlay (retrieval_notes/usage_gaps) is re-consolidated; a completed deep report reaches this threshold immediately (default 10)
RETRIEVAL_EXPERIENCE_ENABLED # distillation gate for the deployment-GLOBAL retrieval-strategy experience library (Agentic Memory P2): whether finished asks are ever read and distilled into retrieval_experiences at all (default true — a deployment may distill and observe without ever injecting, see RETRIEVAL_EXPERIENCE_INJECT_ENABLED below)
RETRIEVAL_EXPERIENCE_INJECT_ENABLED # independent injection gate for the same library: whether the distilled block is ever added to the plan/reflect prompt (default **false** — off until the deployment has observed enough distilled entries to judge the effect; false is byte-identical to the feature not existing on the injection side: no read, no block, no trace step)
RETRIEVAL_EXPERIENCE_TRIGGER # accumulated completed asks (deployment-wide, across every notebook and user) before one distillation batch runs (default 40; ge=1)
REASONING_CONSULT_MEMORY_ENABLED # per-scenario kill switch (defense in depth) for the consult_memory reflect action (Agentic Memory P4); the action's actual availability gate is `retrieval_effort` in {deep, thorough, exhaustive} AND RETRIEVAL_EXPERIENCE_INJECT_ENABLED being on — this flag alone flipping true never makes the action appear if the injection flag above is off (default true)
REASONING_MAX_CONSULT_MEMORY # max consult_memory calls per run (default 2; ge=0)
USER_SEARCH_PROFILE_ENABLED  # single gate for the per-user search/answer style preference document (Agentic Memory P3, B-line): background inference, Ask plan/answer injection, and PATCH /me/search-profile's writability all key off it (default true; false reverts everywhere to byte-identical pre-feature behavior on the injection/write side — no inference, no injection, PATCH 409s — but GET /me still returns any existing value already on the row rather than forging search_profile: null)
USER_SEARCH_PROFILE_TRIGGER  # completed asks for that user before the deterministic, zero-LLM answer_language inference job runs again (default 20; ge=1)
CHUNK_RECALL                 # chunk 大召回数 (default 200; mix 候选池 / MMR 候选)
LEXICAL_LANGUAGE_GATE_ENABLED # drop all-CJK lexical terms when the notebook's sampled corpus holds no CJK character (default true; such probes are provably empty yet each costs a real PostgreSQL LATERAL probe — measured 64 terms/29.7s cold vs 3 terms/0.26s warm for the same 26 rows on a 7,026-chunk English corpus, with the ungated form timing out under parallel report sections. Never filters the user's quoted spans or the whole-sentence term, never applies in the Latin direction, and never applies to source-scoped runs, whose lexical arm is their only candidate generator. Set false to restore byte-identical pre-feature behaviour if a notebook's sampled language misjudges it)
POSTGRES_LEXICAL_KNN_ENABLED # GiST `<->` KNN early stop for the KG-name lexical probe on dominant-scale PostgreSQL notebooks (default true; false is the rollback; PostgreSQL-only — the SQLite adapter declares itself incapable and the verdict short-circuits at zero cost). Requires a shape-matched GiST trigram index over the knowledge-object name expression — detected by an exact shape check (single gist_trgm_ops key, the name expression, `COLLATE "C"` collation), so a pre-existing operator-built index counts; without one the legacy statement runs unchanged. Result-wise the on-default changes nothing for deployments without the index; cost-wise each unscoped probe pays one indexed single-row version query for the sizing verdict — a registered, accepted trade (sub-millisecond, next to the lexical LATERALs that follow). Only engages for unscoped runs on notebooks at or above POSTGRES_LEXICAL_KNN_MIN_ROWS. Scores stay `similarity()`; within equal-similarity tie classes the KNN path may pick different members than legacy AND is not run-to-run stable itself (tie membership follows GiST traversal order; measured: 285 rows named exactly "DAC" at similarity 1.0 on a 9.1M-row base) — a registered, accepted trade; deployments needing bit-stable candidate sets set this false. Measured on that base: 7.4s → 123ms per common short term (60×). See Operations for the index DDL and rollout/rollback steps.
POSTGRES_LEXICAL_KNN_MIN_ROWS # minimum notebook size (nodes+chunks) for the KNN route (default 500000). The GiST index has no notebook key, so the KNN scan walks GLOBAL distance order and filters per row — it only pays off when the notebook dominates the table; a small-share notebook would dig through other notebooks' rows and lose its composite-GIN fast path. Set this above the size of your largest NON-dominant notebook (measured deployment: dominant 1.1e7, next largest 2.7e4 — the default separates them cleanly). Below the floor the legacy statement runs byte-identically.
CHUNK_MMR_K                  # MMR-selected chunks when rerank is off (default 16)
CHUNK_KG_OVERLAY_ENABLED     # chunk×graph mix: add local KG structure + source chunks (default true; rerank path requires `retrieval_rerank` bound)
RERANK_MAX_DOCS              # max docs per rerank request, auto-batched beyond (default 500)
MAX_ENTITY_TOKENS            # mix KG entity-segment token budget (default 6000)
MAX_RELATION_TOKENS          # mix KG relation-segment token budget (default 8000)
MAX_TOTAL_TOKENS             # mix total context token budget (default 30000)
REPORT_MAX_SECTIONS          # deep-report outline: max sections (default 6)
REPORT_MAX_SUBQUERIES_PER_SECTION # per-section retrieval-direction contract mirrored by API/UI
REPORT_PROBE_ELEMENT_LIMIT   # direct-element candidates used by planning and direction top-up
REPORT_SCOUT_KG_LIMIT / REPORT_SCOUT_CHUNK_LIMIT / REPORT_SCOUT_MEMORY_LIMIT # corpus-map scout widths
REPORT_SECTION_CHUNK_BUDGET  # deep-report: per-section chunk-context char budget (default 20000; only for callers without a research depth — see the retrieval behaviour-change note)
REPORT_GENERATION_CONCURRENCY # deep-report: whole reports admitted per backend process (default 1; queued reports hold no DB connection)
REPORT_SECTION_CONCURRENCY   # deep-report: section fan-out per admitted report (default 5; also capped by model capacity and the DB-pool reserve)
REPORT_RETRIEVAL_FANOUT      # deep-report: one shared leaf KG/chunk/element/PPR I/O fan-out per planning/generation run (default 8)
REPORT_PROBE_CHANNEL_CONCURRENCY # planning probes: concurrent independent KG/raw-element channels (1..2, default 2)
REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS / REPORT_SUFFICIENCY_MIN_FAMILIES / REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES / REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE # centralized report sufficiency policy; defaults preserve historical judgments, exact rails in product-and-api
REPORT_SECTION_MAX_TOKENS    # deep-report: per-section drafting completion cap (default 65536)
REPORT_SYNTHESIS_MAX_TOKENS  # deep-report: report-wide JSON blueprint completion cap (default 102400)
REPORT_SUMMARY_MAX_TOKENS    # deep-report: final read-only editor completion cap (default 102400)
REPORT_ALLOW_PARAMETRIC      # deep-report: allow 【通识】/general-knowledge tier, marked & unverified (default true)
REPORT_HIGH_RISK_DOWNGRADE_ENABLED # deep-report citation audit may cap a grounded section at overview when its unsupported ratio exceeds the contract threshold (default false; disclosure still runs when false)
REPORT_HIGH_RISK_UNSUPPORTED_RATIO # deep-report high-risk citation-audit threshold; the numeric contract is owned by docs/product-and-api.md
REASONING_MAX_PPR_RETRIEVES / REASONING_MAX_EXACT_LOOKUPS / REASONING_MAX_FOLLOW_CHAIN_ACTIONS / REASONING_COMMUNITY_PEERS_CAP_FACTOR / REASONING_MAX_OUTLINE_UPDATES # centralized reasoning action/expansion rails; defaults preserve historical behavior, exact rails in product-and-api
```

The three `REPORT_*_MAX_TOKENS` values are completion ceilings, not total-context
declarations or reserved output. The bound provider/model owns prompt+completion
compatibility; verify that it accepts each configured ceiling together with the
largest prompt that workload receives. Lower these values when a provider has a
smaller output or total-context limit.

**Behavior change (PR-5, no new flag):** each report section's deep-dive retrieval budget now follows the report's own `depth` value (1/2/4/8/16, clamped API-side to `[1, 16]`) mapped onto the same named effort tiers reasoning Ask uses (`overview`/`standard`/`deep`/`thorough`/`exhaustive`) rather than always running at the `standard` budget. Low depths therefore retrieve with a smaller budget than before this change and high depths with a larger one — this is an intentional alignment fix (same tier name, same budget in both Ask and Deep Report), not a regression. Reaching depth 16 (`exhaustive`) additionally activates the outline scratchpad and KG weak-support gap feedback described above inside that section's deep-dive only; see `docs/product-and-api.md`'s "Deep Report outline co-evolution" section for the full contract.

**Two-tier KB & graph reasoning (Wave 1+2):** these have no `.env` toggles today.
A notebook's `tier` (`base` | `personal`, default `personal`) is data on the notebook
row, set via the repository's `mark_notebook_base()`; publishing a notebook to `base`
does not make it globally shared — every other notebook must explicitly mount it as a
reference library (persisted in `notebook_bases`, managed through `GET`/`PUT
/api/notebooks/{id}/bases`, discovered via `GET /api/notebooks/{id}/mountable`)
before it joins that notebook's retrieval participant set. Tier-aware federation
leaves retrieval scores unchanged: relevance is the primary ordering key, with `base`
used only as the secondary key for an exact score tie among mounted participants. The
base-wins contradiction rule is a separate answer-synthesis policy that remains active
for evidence from a mounted base notebook. The opt-in
graph-reasoning Ask mode (`mode="graph"`) bounds its multi-hop traversal with fixed
defaults `max_depth=3` and `max_fan_out=8` (read via `getattr` on settings, so a future
`GRAPH_MAX_DEPTH` / `GRAPH_MAX_FAN_OUT` env override would slot in without code changes).
Edge-trust scoring, the curator review queue, and personal→base promotion are likewise
behavior, not env-gated.

**User accounts:**

```text
SILICON_NOTEBOOK_ADMIN_PASSWORD   # admin login password (local default "admin"; production/non-loopback
                                  # startup requires a non-default value)
SILICON_NOTEBOOK_AUTH_OPTIONAL    # true = no-token requests act as admin (local/testing only);
                                  # false (default) = login required for all requests
AUTH_SESSION_TOUCH_INTERVAL_SECONDS # sliding-session DB write interval (default 300)
```

**MinerU (PDF parsing):**

```text
MINERU_MODE             # off (default) | http | cli
MINERU_API_URL          # remote mineru-api endpoint (http mode)
MINERU_BACKEND          # pipeline | vlm-auto-engine | vlm-http-client | vlm-sglang-client
MINERU_VLM_SERVER_URL   # standalone VLM inference server URL
MINERU_PARSE_METHOD     # auto | txt | ocr
MINERU_LANG             # e.g. en, ch
MINERU_MODEL_SOURCE     # huggingface | modelscope
MINERU_TIMEOUT_SECONDS  # MinerU call timeout
MINERU_MAX_RETRIES      # extra transient HTTP attempts, 0..5 (default 2 = at most 3 total attempts)
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
MINERU_RETURN_IMAGES    # retain embedded images from PDF/DOCX/PPTX/XLSX documents (default on: true; set 0/false to keep text and captions only)
MINERU_MAX_IMAGE_BYTES  # max size per embedded image (default 5MB; larger images are dropped)
MINERU_MAX_IMAGES_PER_SOURCE # max embedded images per source (default 200)
```

Parser routing is declared by one backend registry and projected through the authenticated
system-configuration response. It always prefers a configured self-hosted MinerU path,
then permits public cloud only when no self-hosted path is configured, and retains the
built-in parser as the format-specific fallback. The browser receives only capability,
execution-boundary, availability, and fixed reason enums—never endpoints or credentials.

**Generated-question rollout (optional retrieval supplement):**

```text
GENERATED_QUESTION_INDEX_MODE
GENERATED_QUESTION_QUESTIONS_PER_CHUNK
GENERATED_QUESTION_TRIGGER_HITS
GENERATED_QUESTION_RECALL
GENERATED_QUESTION_MAX_SCAN_ROWS
```

Keep the mode `off` unless an operator is deliberately building/evaluating this index.
Use `shadow` for counts-only A/B before `on`; see the product contract for exact defaults
and bounds and Operations for the offline command.

`MINERU_MAX_RETRIES` is shared by the self-hosted `MINERU_MODE=http` adapter and
mineru.net cloud requests, including URL submission/poll/result download and signed
file upload. Retries use bounded exponential delays (1 second, then 2 seconds with the
default) and apply only to transient network/timeout errors, HTTP 408/425/429/5xx, and
empty or non-JSON responses. Explicit 4xx responses and terminal parsing/business
failures are not retried; `MINERU_MODE=cli` remains a single local subprocess attempt.
After the adapter reaches a terminal failure (or returns no usable elements), source
ingestion falls back to local PyMuPDF4LLM. For URL sources the backend downloads the
validated public PDF first. A successful fallback remains `extracted`, exposes only a
safe `parse_quality_warning` to clients, and can be reparsed later; pypdf is used only
if PyMuPDF4LLM itself is unavailable or errors.

`MINERU_RETURN_IMAGES` / `MINERU_MAX_IMAGE_BYTES` / `MINERU_MAX_IMAGES_PER_SOURCE` also
govern `data:image/...;base64,...` embedded images in Markdown sources — these three
settings are the single guardrail for persisting any source's images, not only ones
parsed by MinerU.

**Logging:**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
MODEL_JSON_REPAIR_MODE  # off | shadow | on (default on)
EVENT_LOG_ENABLED / EVENT_LOG_DIR
SLOW_REQUEST_MS         # requests slower than this (ms) are flagged SLOW (default 3000)
SILICON_NOTEBOOK_CORS_ORIGINS
```

`.env.example` is the authoritative, complete list of non-service variables and secret
slots; `model-services.example.toml` is the service/binding/capacity template. The groups
above highlight the common settings. A dedicated reasoning model is selected by binding
`reasoning_agent` to a separate service in TOML, while its guardrails remain
`REASONING_MAX_STEPS`, `REASONING_MAX_SUBQUERIES`, `REASONING_TIMEOUT_SECONDS`, and `REASONING_MAX_RETRIES`;
retrieval/grounding tuning (`PROC_MIN`, `EVIDENCE_TAU_LOW`,
`EVIDENCE_TAU_HIGH`), the opt-in debug log viewer (`DEBUG_LOGS_ENABLED`), and runtime
identity (`SILICON_NOTEBOOK_ENV`, `SILICON_NOTEBOOK_SINGLE_USER_EMAIL`,
`SILICON_NOTEBOOK_SINGLE_USER_NAME`).

`MODEL_JSON_REPAIR_MODE` applies only to `reasoning_agent` and `ask_answer`.
`off` keeps strict rejection, `shadow` records whether a response would be safely
repairable but still rejects it, and `on` accepts conservative repairs (the default).
It does not complete truncated output or relax schema/type/prose safety checks. Repair
events are content-free and correlate through the model call's safe `support_id`.

The same-origin `/api/*` rewrite has a finite proxy idle timeout. Ask therefore sends a
content-free blank NDJSON heartbeat every 5 seconds and returns anti-buffering headers;
do not configure an ingress to buffer `application/x-ndjson`. This addresses idle
timeouts. If a CDN/load balancer enforces an absolute request-duration ceiling, raise
that deployment setting above the longest supported Ask run or use the durable job to
reopen the completed conversation after a disconnect.

When the required chat workloads are unbound, summaries and answers fall back to deterministic behavior. Source parsing still completes offline, and KG extraction records a completed `no-llm` run without generating synthetic knowledge.
