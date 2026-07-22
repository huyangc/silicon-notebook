# Deployment and configuration

[Back to README](../README.md) · [中文说明](./deployment-and-configuration_zh.md)

This is the detailed source-checkout deployment and configuration reference. For the short local path, start with the root README; for a packaged offline target, use [packaging/DEPLOY.md](../packaging/DEPLOY.md).

## Deployment

silicon-notebook runs as two processes — a FastAPI backend and a Next.js frontend — over
one repository selected by `DATABASE_URL`. The shipped SQLite default requires **no GPU,
no database server, and no local model server**. PostgreSQL 16 is also a supported direct
backend when an accessible server is provisioned. LLM, embeddings, and rerank stay URL-based; MinerU separately supports remote
HTTP (`MINERU_MODE=http`), an isolated same-host subprocess (`MINERU_MODE=cli`), or the
pypdf fallback (`MINERU_MODE=off`). The pipeline runs offline with deterministic fallbacks
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

- **Embedding dimensions** — `EMBED_DIM` must equal the bound embedding model's output
  dimension. Optional `EMBED_RUNTIME_DIM` (default `0` = off) truncates
  the similarity space to its first N dimensions + re-normalize (MRL) — cuts in-process
  matrix / ANN memory ~`EMBED_DIM/N`× while keeping the native vectors on disk as the
  source of truth. Switching it on/off requires rebuilding scale indexes; see
  [docs/runtime-dim-truncation-runbook.md](./runtime-dim-truncation-runbook.md). Never
  lower `EMBED_DIM` to shrink vectors — that discards every stored vector as wrong-dim.
- **PDF fidelity** (optional) — a MinerU endpoint, see [PDF parsing with MinerU](./operations.md#pdf-parsing-with-mineru);
  leave `MINERU_MODE=off` for the pypdf text fallback.

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

`npm run start` runs `scripts/prod.sh`: `next build` + `next start` for the frontend,
`uvicorn --workers 1` for the backend, both logging to `.local/logs/`. Set `SKIP_BUILD=1`
to reuse an already-built `frontend/.next` (e.g. a prebuilt image). Override
`BACKEND_HOST` / `PORT` / `FRONTEND_PORT` to change bind address/ports. The backend
defaults to `127.0.0.1`; binding it to a non-loopback address requires a non-default
`SILICON_NOTEBOOK_ADMIN_PASSWORD` and fails fast otherwise.

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
back to another database. `SHADOW_DATABASE_URL` is reserved and cannot enable dual-write.

```dotenv
# Shipped default
DATABASE_URL=sqlite:///.local/silicon_notebook.db

# Direct PostgreSQL 16 backend
DATABASE_URL=postgresql://silicon_app:change-me@127.0.0.1:5432/silicon_notebook
```

PostgreSQL must use UTF-8 and have `pg_trgm` installed in `public`. The database owner may
let migration 0001 create it, or a DBA may preinstall it. An extension of that name in
another schema is rejected. PostgreSQL stores vectors as float32 `bytea`; pgvector is not
required. Keep production at one backend worker (`--workers 1`).

Changing the URL never moves existing rows. For a fresh target, stop the service, change
the URL, start, and verify the empty/bootstrap state. For an existing SQLite→PostgreSQL
move, first quiesce all writers, stop the service, make verified backups of both sides,
perform an externally controlled full migration, compare table counts and representative
domain reads, then change the one URL. Detailed cutover and rollback steps are in
[Operations](./operations.md); the packaged decision table is also in
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
`reasoning_agent`, `knowhow_complete`, `kg_extract`, `retrieval_query_embedding`, and
`retrieval_rerank` to those services. Several workloads may share a service;
all of them share that service's one scheduler and one concurrency budget.
`max_concurrency` is the only model-capacity setting. Source-job counts,
window sizes, batch sizes, and local ANN threads do not create another model
gate.

Knowhow row completion uses two interactive chat workloads: `reasoning_agent`
plans and reflects over federated evidence from the active notebook and its
valid mounted reference libraries, then `knowhow_complete` turns that evidence
and the same-table examples into structured suggestions. Bind both to compatible
chat services when this feature is wanted. Leaving either unbound, or a provider
failure in either stage, yields no suggestion; the application never silently
falls back to table-only or fabricates an offline completion.

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

**Retrieval:**

```text
RETRIEVAL_TOP_N         # reasoning/report synthesis evidence-budget floor (default 20)
REASONING_TOP_N_PER_QUERY  # adaptive budget: seats reserved per aspect/sub-query (default 3)
REASONING_TOP_N_CAP        # adaptive budget cap; comparison Qs scale by #aspects (default 36)
```

**Scalable-retrieval index:** notebooks large enough to be non-copyable (the same size
threshold used to gate notebook copy/sharing — bytes or chunk+node count over the
configured limit) get their scale index built or refreshed automatically, no manual
button/CLI step required: on source extraction, on KG rebuild, and as a fallback the first
time a query finds no index. By default the build is queued for a low-traffic off-peak
window rather than run immediately.

```text
SCALE_INDEX_AUTO_ENABLED   # auto-build/refresh the scale index for large notebooks (default true)
SCALE_INDEX_AUTO_WHEN      # "idle"=queue for the off-peak window (default) | "now"=build immediately
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
ANSWER_CONTEXT_BUDGET_CHARS  # answer-context assembly char budget (default 6000)
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
CHUNK_RECALL                 # chunk 大召回数 (default 200; mix 候选池 / MMR 候选)
CHUNK_MMR_K                  # MMR-selected chunks when rerank is off (default 16)
CHUNK_KG_OVERLAY_ENABLED     # chunk×graph mix: add local KG structure + source chunks (default true; rerank path requires `retrieval_rerank` bound)
RERANK_MAX_DOCS              # max docs per rerank request, auto-batched beyond (default 500)
MAX_ENTITY_TOKENS            # mix KG entity-segment token budget (default 6000)
MAX_RELATION_TOKENS          # mix KG relation-segment token budget (default 8000)
MAX_TOTAL_TOKENS             # mix total context token budget (default 30000)
REPORT_MAX_SECTIONS          # deep-report outline: max sections (default 6)
REPORT_SECTION_CHUNK_BUDGET  # deep-report: per-section chunk-context char budget (default 20000)
REPORT_SECTION_MAX_TOKENS    # deep-report: per-section drafting max_tokens (default 8192)
REPORT_ALLOW_PARAMETRIC      # deep-report: allow 【通识】/general-knowledge tier, marked & unverified (default true)
```

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
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
MINERU_RETURN_IMAGES    # retain embedded images from PDF/DOCX/PPTX documents (default on: true; set 0/false to keep text and captions only)
MINERU_MAX_IMAGE_BYTES  # max size per embedded image (default 5MB; larger images are dropped)
MINERU_MAX_IMAGES_PER_SOURCE # max embedded images per source (default 200)
```

**Logging:**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
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

When the required chat workloads are unbound, summaries and answers fall back to deterministic behavior. Source parsing still completes offline, and KG extraction records a completed `no-llm` run without generating synthetic knowledge.
