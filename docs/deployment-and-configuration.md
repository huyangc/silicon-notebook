# Deployment and configuration

[Back to README](../README.md) · [中文说明](./deployment-and-configuration_zh.md)

This is the detailed source-checkout deployment and configuration reference. For the short local path, start with the root README; for a packaged offline target, use [packaging/DEPLOY.md](../packaging/DEPLOY.md).

## Deployment

silicon-notebook runs as two processes — a FastAPI backend and a Next.js frontend — over
one repository selected by `DATABASE_URL`. PostgreSQL 16 is the default deployment choice;
both the current development and production environments use PostgreSQL. Provision an
accessible database and set `DATABASE_URL` explicitly. SQLite is kept long-term as a supported
alternative without a database server (product decision of 2026-10-19); its support scope is
offline, single-machine and test use, see section 3.1. Neither backend requires a GPU or local model server.
LLM, embeddings, and rerank stay URL-based; MinerU separately supports remote
HTTP (`MINERU_MODE=http`), an isolated same-host subprocess (`MINERU_MODE=cli`), or the
PyMuPDF4LLM fallback (`MINERU_MODE=off`, with pypdf as last resort). The pipeline runs offline with deterministic fallbacks
when no model service or MinerU parser is configured.

### Prerequisites

- **macOS 14+ on Apple Silicon, or Linux x86_64/aarch64 with glibc 2.28+.** Several
  locked packages publish no wheel below these floors: onnxruntime 1.31.0 has only
  `macosx_14_0_arm64` and `manylinux_2_28` wheels and no source distribution, and
  igraph, PyMuPDF and pymupdf-layout also start at `manylinux_2_28`. Intel Macs are
  not supported (no onnxruntime wheel at all; cryptography would also have to build
  from source), nor is Windows; on Windows run it inside WSL.
- **Python ≥ 3.13** — the SQLite write lock's fairness depends on CPython 3.13's
  `PyMutex`-backed `threading.Lock` handoff; older interpreters silently regress to
  writer starvation (see `backend/app/repositories/sqlite/database.py`).
- **Node.js 26** and npm — the one supported major, pinned in `.nvmrc`; `frontend/.npmrc`'s
  `engine-strict` makes `npm ci`/`npm install` refuse any other major
- **git**
- An accessible **PostgreSQL 16** database for the default deployment; extension prerequisites are listed in section 3.1.
- A **C/C++ toolchain** — `hnswlib` 0.8.0 publishes only a source distribution, so pip
  always builds it: install Xcode Command Line Tools (macOS) or `build-essential`
  (Debian/Ubuntu). On the supported platforms above, every other locked package
  installs from a prebuilt wheel.

### 1 · Install

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

# Backend — into an isolated Python environment
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
# hash-locked runtime dependencies (docs/development.md, "Python dependencies")
python -m pip install --require-hashes -r backend/requirements.txt

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

`ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` governs the internal
post-terminal Ask extension point. The timeout is cooperative: an in-progress
synchronous callback is not abandoned, while later contributions are skipped
after its deadline. Exact defaults and validation ranges live only in the
product/API contract.

`REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` independently governs Deep
Report's post-terminal observer point. It uses the same cooperative semantics
but never shares an Ask budget: a callback already started completes safely and
later report contributions do not start after the deadline. This hook runs only
after a successful durable `done` CAS. Exact defaults and ranges live only in
the product/API contract.

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

`.env.example` is a curated starter for common deployment choices and secret slots;
`model-services.example.toml` is the service/binding/capacity template. Advanced
overrides remain supported without appearing in the starter. See
[Configuration](#configuration) for their ownership and use.

#### Upgrading an old role-based `.env`

Existing deployments can convert the retired `OPENAI_COMPAT_*`, `KG_LLM_*`,
`EMBED_*`, and `RERANK_*` settings into the system-owned configuration:

```bash
# Preview only; reads .env and writes nothing.
python scripts/migrate_legacy_model_env.py --env .env --context-window general=131072 --max-output-tokens general=32768

# After reviewing the service list and inferred capacities, create the TOML and
# rewrite only the managed model assignments in .env.
python scripts/migrate_legacy_model_env.py --env .env --context-window general=131072 --max-output-tokens general=32768 --apply
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

**Serving the app at `http://<IP>:3000` is a supported deployment, but it is not a browser Secure Context:**
browsers expose `crypto.randomUUID`, `navigator.clipboard`, Service Workers and similar APIs only under HTTPS
or on `localhost`. Product code carries a fallback for each (`newClientRequestId()` for ids, `copyTextSafely()`
for copying; see `docs/development.md`), so features keep working; under plain HTTP copying uses the legacy
hidden-textarea path, which an individual browser policy may still refuse. For a long-lived multi-user
installation, put TLS in front (terminating HTTPS at a reverse proxy is enough; neither the frontend nor the
backend needs a configuration change).

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

Only the backend's startup and the explicit `bash scripts/cli.sh database migrate
--confirm-service-stopped` (stopped service, never seeds) migrate the schema. Every other command-line
tool — `batch_ingest.py` and the other maintenance, backfill, eval and diagnostic commands — is a
schema guest: it never migrates and never seeds (so it never rewrites the admin password), and it
refuses a database not at this build's schema, including one the backend has never created, with exit
status 3 and a message naming the reason and the remedy. Start the backend once before the first
offline import, and after an upgrade start it once (or run `database migrate`) before offline
maintenance. Details: [Operations](./operations.md#schema-ownership-only-the-service-and-database-migrate-migrate).

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

`npm run start` runs `scripts/prod.sh`: it first installs the hash-locked backend dependencies
into `PYTHON_BIN`'s environment with
`python -m pip install --require-hashes -r backend/requirements.txt`, which moves every
package to the lock's exact version and refuses any file whose hash differs, then
recreates the locked frontend dependency tree with `npm ci --prefix frontend`. It completes
`next build` in the foreground, then launches `next start` and the single-worker Uvicorn
backend under `nohup`, with stdin detached and both logs written under `.local/logs/`.
The command exits immediately after launching both detached processes; it does not poll backend
readiness or frontend HTTP access. The services therefore survive closing the terminal, and the
operator must verify `/api/ready` and the frontend separately. An interruption before both
children are handed off sends SIGTERM to both launched children together, waits the bounded
`START_CLEANUP_GRACE_SECONDS` (default 10 seconds), SIGKILLs any survivor, and reaps both.
A further interruption during that wait SIGKILLs the survivors at once, and the companion
services are still stopped (see [operations](./operations.md#plugin-companion-services)).
One of `ss`, `lsof`, or `fuser` is required. Occupied target ports fail before dependency
installation even when the current user cannot see the listener PID, so an old listener cannot
hide a bind failure in the newly launched process. Use `npm run stop` for the detached services.
Prebuilt images that already
contain both dependency sets may set `SKIP_INSTALL=1`; with that escape hatch, a missing
`frontend/node_modules/.bin/next` still fails before build rather than silently continuing.

The backend install can leave a dependency conflict behind without stopping the start. When
moving packages to the lock's versions breaks a requirement of something else installed in
that environment (typically a deployment plugin that wants a newer version of a shared
package), pip prints `ERROR: pip's dependency resolver does not currently take into account
all the packages that are installed…` with the `… requires …, but you have … which is
incompatible` lines, still exits 0, and `npm run start` carries on. Check an environment with
`"$PYTHON_BIN" -m pip check` (exit 1 and one line per broken requirement when there is a
conflict); fix the plugin's requirement or the lock. Until then, `SKIP_INSTALL=1 npm run
start` starts without reinstalling anything, so the environment keeps the versions it has.

Before launching the backend, `npm run start` regenerates `release-manifest.json` at the repo
root with the same `scripts/build_release_manifest.py` that `pack.sh` uses, so a
`git pull` + `npm run start` deployment gets the in-app "system updated" notice. It needs the
checkout to be a **full (non-shallow) clone** of `master`; the version string is HEAD's commit
date plus short sha. Unlike packing, a failure here (shallow clone, a note with an invalid front matter or a bad name,
no git, or a checkout that is not a repository root) only warns and deletes any old manifest —
the services still start, just without update notices.

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

For a co-located Nginx → Next.js → FastAPI deployment, Next.js also rewrites the public
`/mcp` endpoint directly to `${BACKEND_PROXY_TARGET}/mcp/`. Set
`MCP_PUBLIC_URL=https://notebook.example.com/mcp` (no trailing slash) and
`MCP_REQUIRE_HTTPS=1`, keep `BACKEND_HOST=127.0.0.1` and `PORT=8000`, and use
`NEXT_PUBLIC_API_BASE_URL=/api`, `BACKEND_PROXY_TARGET=http://127.0.0.1:8000`, and
`FRONTEND_PORT=3000` in the root `.env`. Nginx forwards all paths to
`http://127.0.0.1:3000` with `Host`, `Authorization`, and `X-Forwarded-Proto` preserved;
disable response buffering and caching for streamed responses. Only Nginx's 80/443 ports
need public ingress; block public ingress to 3000/8000. Next.js's proxy idle timeout is
300 seconds, and the Nginx read timeout should be at least as generous. This is an idle
timeout, not a total tool-call deadline. Rebuild the frontend after changing the rewrite
target or public API base. Clients use the exact published `/mcp` URL; the rewrite adds
the backend-only trailing slash without a client redirect. A trusted-intranet HTTP-only
deployment can instead use `MCP_PUBLIC_URL=http://notebook.example.com/mcp` and
`MCP_REQUIRE_HTTPS=0`; this relaxes Host/Origin checks and transmits tokens in cleartext.

An Agent's `ask` / `get_ask` can hand an answer out as an expiring anonymous link with `delivery="link"` (see
MCP `ask` in [Product and API](./product-and-api.md)). Enabling it requires `PUBLIC_WEB_BASE_URL`: the public
address of the **frontend** as readers' browsers reach it, so a link is `${PUBLIC_WEB_BASE_URL}/c/<token>`. The
server does **not** derive it from request headers (`Host`, `X-Forwarded-*`): those come from the client or an
intermediary and can be forged, so a derived link could point at another site, and the entry an MCP request
used need not be the one readers can open. It accepts `http` (the production frontend is often
`http://<IP>:3000`) and `https`, may carry a sub-path (such as `https://example.com/notebook`), has trailing
slashes trimmed, and fails startup if it carries userinfo, a query, a fragment, whitespace or control characters.
**When unset**, `delivery="link"` returns `unavailable` and nothing else changes. Three optional settings:
`MCP_ANSWER_LINK_TTL_HOURS` (default 168, range 1-720; the default and the cap for `link_ttl_hours`, where a larger
request is refused rather than clamped), `MCP_ANSWER_LINK_GRACE_HOURS` (default 168, range 0-720; how long an
expired link is kept: readers see 410 "expired" meanwhile, then the sweep that runs on issuing deletes it and the
link becomes 404), and `MCP_ANSWER_PREVIEW_MAX_CHARS` (default 300, range 40-2000; the plain-text preview cap).
Each token also needs "allow expiring answer links" switched on separately on the Agent access page.

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
`SILICON_NOTEBOOK_SYNC_ENV` names this deployment (e.g. `prod-shanghai`) for the separate,
unrelated cross-environment notebook sync CLI (`scripts/cli.sh sync export/import/status`,
see [Operations](./operations.md#cross-environment-notebook-sync) and
[the sync design doc](./incremental-sync-design.md)); it only supplies `export`'s default
`source_env` and is otherwise inert. `SILICON_NOTEBOOK_SYNC_KNOWHOW_HISTORY` (default `false`)
is read by the same CLI's `export` only: when true, packages also carry the Knowhow edit
history (`knowhow_milestones`/`knowhow_changes`); when false they carry the tables' current state
only and the target keeps whatever history it already has. Turning it on for a target that has
been receiving packages without it needs one `sync export --full` to that target to bring the
history across in full (see the design doc §11).

```dotenv
# Default deployment for development and production; replace connection settings
DATABASE_URL=postgresql://silicon_app:change-me@127.0.0.1:5432/silicon_notebook

# Optional SQLite backend; replace the PostgreSQL URL above
# DATABASE_URL=sqlite:///.local/silicon_notebook.db

# Optional one-way shadow target while DATABASE_URL remains SQLite
# SHADOW_DATABASE_URL=postgresql://silicon_shadow:change-me@127.0.0.1:5432/silicon_notebook_shadow
```

The deployment default is distinct from the unset-setting fallback: if `DATABASE_URL` is
omitted entirely, `Settings` still uses `sqlite:///.local/silicon_notebook.db`. Development
and production explicitly configure PostgreSQL; `.env.example` follows that deployment choice.

SQLite's support scope is offline use (a local or packaged deployment without a database
server), single-machine use (one host, one service process) and tests. It offers the same
features as PostgreSQL, but admits one writer at a time and has no advisory-style
cross-process coordination locks (only the `BEGIN IMMEDIATE` write fence). Multi-user production deployments and large notebooks should use PostgreSQL: the
offline scale-index build (`scripts/cli.sh scale`) supports PostgreSQL only, so a SQLite
deployment builds inside the service process, and the hot-path/retrieval index builders and
PostgreSQL hot-path diagnostics target PostgreSQL only. The per-capability differences
between the two backends are listed in
[the architecture document](../architecture.md#222-双后端长期并存与能力对等矩阵).

PostgreSQL must use UTF-8 and have `pg_trgm` installed in `public`. The database owner may
let migration 0001 create it, or a DBA may preinstall it. An extension of that name in
another schema is rejected. Since schema v42, `btree_gin` in `public` is a prerequisite on
the same terms: migration 0042 creates it when the migration role can (a trusted extension
still needs CREATE on the database), and otherwise fails with an explicit
preinstall-then-retry instruction — a DBA-preinstalled `public.btree_gin` satisfies it
exactly like pg_trgm. PostgreSQL stores vectors as float32 `bytea`; pgvector is not
required. Keep production at one backend worker (`--workers 1`).

Large PostgreSQL databases should also install the two
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

An already-populated PostgreSQL database should also get the accumulated hot-path fix indexes (batch 1's six groups / eight indexes, plus batch 2's payload-search GIN and checkup-H5 partial index, plus batch 3's one keyset-covering index, plus batch 4's three source-search GIN trigram indexes, plus batch 5's three delete-jobization FK/keyset indexes, plus batch 6's three generation-aware cluster replacements (batch 3 W2 PR-1), plus batch 7's paper-metadata covering index; batch 2's GIN ran about 1.5x the knowledge_objects table segment on a synthetic low-entropy benchmark (real payloads have richer trigrams and may run larger; measure after building) and is a registered write-amplification debt, reversible via DROP INDEX CONCURRENTLY)
across six query-family groups (`concept_clusters(notebook_id, canonical_id)`, its
`lower(canonical_name)` companion, three reverse-FK covers on
`extraction_runs`/`knowledge_source_fact_elements`/`memory_items`,
`knowledge_relations(notebook_id, source_object_id, target_object_id, edge_type)`,
`chunks(source_id, ordinal)`, and a partial `sources(notebook_id, source_type)` index) plus
batch 3's `concept_clusters(notebook_id, canonical_id, member_object_id)` keyset-covering
index and batch 4's three source-search trigram indexes
(`sources(notebook_id, lower(title), lower(file_name))` partial on the visible source types,
`source_authors(notebook_id, lower(name))`, `source_paper_meta(notebook_id, lower(paper_title))`),
built online, same inspect/apply shape as the retrieval-index tool above:

```bash
PYTHONPATH=backend python scripts/build_hotpath_indexes.py
PYTHONPATH=backend python scripts/build_hotpath_indexes.py --apply
```

Batch 1's eight are plain btree (one partial, one expression) indexes, so those builds are
fast even on large tables; batch 2's payload index IS a notebook-scoped
composite partial GIN over the full jsonb-as-text (`notebook_id` leads via btree_gin,
`WHERE status != 'deprecated'` — the same shape as the operational
`idx_knowledge_objects_nb_name_trgm`, so a term concentrated in other notebooks never
builds a global bitmap; `--apply` installs the btree_gin extension on demand) and
is minutes-scale on a large `knowledge_objects` table — schedule the window accordingly.
Batch 3's keyset-covering index is a plain (non-partial) btree, same seconds-scale build
profile as batch 1's, with none of the GIN-specific concerns above. Batch 4's three are
notebook-scoped composite GIN trigram indexes over short text columns (title, file name,
author name, paper title) rather than a whole jsonb payload, so they measured roughly 1.0x,
0.3x and 1.2x their own tables' heaps on a benchmark corpus — tens of megabytes at the
scale of a 49k-source notebook, not the double-digit gigabytes batch 2's payload GIN
implies, and minutes rather than tens of minutes to build. Each is independently
reversible; see `0048_source_search_trgm_indexes.sql`'s header for the per-index
`DROP INDEX CONCURRENTLY` list and for the measured, registered trade-off on short (<3
character) needles. After building them on a freshly loaded database, `VACUUM` the three
tables (or wait for autovacuum) before judging any EXPLAIN: an unmerged GIN fastupdate
pending list inflates every GIN cost estimate roughly tenfold and makes PostgreSQL reject
its own index — see [Operations](./operations.md#postgresql-notebook-aware-lexical-indexes). In
every case `CREATE INDEX CONCURRENTLY` still takes a full table scan per index and should
run outside peak hours on a busy database.
Migrations `0039_hotpath_batch1_indexes.sql`, `0042_hotpath_batch2_search_indexes.sql`,
`0043_concept_cluster_keyset_index.sql`, and `0048_source_search_trgm_indexes.sql` use plain
`CREATE INDEX IF NOT EXISTS` (a
migration runs inside a transaction, where `CONCURRENTLY` cannot run) and becomes a no-op
ledger entry once this script has built every index; on a fresh database with no existing
traffic, the migration alone is sufficient and running the script first is optional.
Migrations 0042 and 0043 additionally validate any pre-existing same-named index before creating:
an INVALID residue row or a wrong-shape name collision fails the migration loudly (with
the exact online remediation in the error) instead of being silently skipped by
`IF NOT EXISTS` while the ledger records success. If
`--apply` reports an `INVALID` index (a prior `CONCURRENTLY` build that failed partway
through), the tool prints the exact `DROP INDEX CONCURRENTLY <name>;` to run before
retrying — it never drops that index on its own. Batch 1's indexes and the batch-2 GIN are pure read-path additions with no query or
service-code change. The batch-2 partial index ships together with one query-text change
(`maintenance.py`'s eligibility predicate moved from a bound parameter to the identical
inlined literal — same semantics, hardened against generic-plan cache states); dropping the
index alone is still safe, since the literal form works with or without it. If write latency
on a table regresses more than 20% after building an index, `DROP INDEX CONCURRENTLY <name>`
removes it with no other effect.

Known, registered write-amplification debt from this batch (not addressed here — dropping a
live index is a deliberate, separate operator call): the pre-existing `idx_chunks_source`
(migration 0003) is now fully covered by the new `idx_chunks_source_ordinal` above and can be
retired with `DROP INDEX CONCURRENTLY idx_chunks_source` once production has verified the new
index is stable. `knowledge_relations` separately already carries three same-leading-prefix
indexes on the `source_object_id` side (`idx_knowledge_relations_nb_source`,
`idx_knowledge_relations_nb_source_id`, and now `idx_knowledge_relations_nb_source_target_edge`)
— a pre-existing overlap this batch does not introduce and does not change. Batch 3 registers
a second case: the pre-existing `idx_clusters_nb_canonical` (migration 0039) became a strict
prefix of `idx_clusters_nb_canonical_member` — that debt is now COLLECTED by batch 6 (below),
which drops it outright.

**Batch 6 (W2 generational cluster swap, migration 0051) changes the operator flow in two
ways.** First, `scripts/build_hotpath_indexes.py --apply` now executes an idempotent
prerequisite `ALTER TABLE concept_clusters ADD COLUMN IF NOT EXISTS generation ...` before
building its three entries (the columns land with migration 0051, but the builder-first
production order needs them earlier; the ALTER is metadata-only on PG 11+ and schema-
qualified). Second, after the builder reports the three new indexes ready
(`uq_clusters_nb_type_member_generation`, `idx_clusters_nb_canonical_member_gen`,
`idx_clusters_nb_created_gen`), retire the FIVE superseded indexes online BEFORE migrating:

```bash
psql "$DATABASE_URL" -c 'DROP INDEX CONCURRENTLY uq_clusters_notebook_type_member;' \
  -c 'DROP INDEX CONCURRENTLY idx_clusters_nb_canonical_member;' \
  -c 'DROP INDEX CONCURRENTLY idx_clusters_nb_created;' \
  -c 'DROP INDEX CONCURRENTLY idx_clusters_nb;' \
  -c 'DROP INDEX CONCURRENTLY idx_clusters_nb_canonical;'
```

(The two bare-prefix indexes are retired with the rework because, measured on live plan
probes, a narrower prefix hijacks the generation-predicated readers into Index Scan + heap
Filter — the regression the INCLUDE columns exist to prevent.) Migration 0051 then validates
shapes and is a pure ledger no-op. Skipping the builder-first flow still works but pays the
in-transaction index builds/drops on `concept_clusters` during the migration window.

**Batch 7 (migration 0084, SQLite v103)** adds one covering btree,
`idx_source_paper_meta_source_nb` on `source_paper_meta(source_id) INCLUDE (notebook_id,
is_paper)`. The paper-metadata readers count a metadata row as its source's only when the
row's own `notebook_id` is the source's notebook; this index lets the missing-metadata
probe (the backfill queue and the 「补全论文信息」 button) and the analytics paper-metadata
counts check that without visiting the heap. Same builder-first order as the earlier
batches: `scripts/build_hotpath_indexes.py --apply` builds it with `CREATE INDEX
CONCURRENTLY` (seconds-scale: one row per source), then migrate (0084 validates the shape
and is a ledger no-op). Nothing is retired. Rollback: `DROP INDEX CONCURRENTLY
idx_source_paper_meta_source_nb;` (the readers then probe the heap). SQLite builds the same
name over `(source_id, notebook_id, is_paper)` in v103, and its readers name it with
`INDEXED BY`.

If you intend to build scale indexes offline or off-host
(`scripts/build_scale_index.py`, an independent process that runs *beside* the live
service), two PostgreSQL-side prerequisites apply. First, its per-notebook mutual
exclusion is a **session-level** `pg_try_advisory_lock`, so the deployment must use
**session pooling or direct connections** — PgBouncer transaction pooling can move later
statements to another backend and silently destroys lock ownership. Second, these claims
are **non-pooled** connections: budget `max_connections` for the application pool ceiling
plus the service's concurrent-build lock sessions plus one per concurrently running CLI
invocation. Full procedure, the two-machine pin list and recovery steps are in
[Operations](./operations.md#offline--off-host-scale-builds-scriptsbuild_scale_indexpy).

A database that already holds global Ask history needs no manual step after upgrading to SQLite v98 /
PostgreSQL 0078: once the service is ready it attributes the global Ask jobs that finished before the
upgrade in one background pass (the startup log reports how many are pending). Until it has caught up the
notebook analytics panel counts those jobs through the slower payload predicate, with the same result.
`PYTHONPATH=backend python scripts/backfill_global_ask_attribution.py` (`maintain global-attribution` in the
unified CLI) re-runs the pass by hand; like every maintenance command it never migrates or seeds and
refuses (exit status 3) a database that is not at this build's schema. No configuration is involved. Details:
[Operations](./operations.md#global-ask-attribution-backfill-scriptsbackfill_global_ask_attributionpy).

Upgrading to SQLite v101 / PostgreSQL 0082 needs no manual step either: once the service is ready it judges,
in one light background pass, which already-chunked sources' chunks are navigation pages (tables of contents,
lists of figures or tables) and flags them (the startup log reports how many sources are pending); new chunks
are judged as they are built. `NAVIGATION_DETECTION_ENABLED` (default `true`) is the write-side switch;
`RETRIEVAL_NAVIGATION_FILTER_ENABLED` (default `true`) is the read-side one (`false` stops retrieval from
dropping flagged chunks, with no write).
`PYTHONPATH=backend python scripts/backfill_navigation_chunks.py --all-notebooks --dry-run` (`maintain
navigation` in the unified CLI) previews the judgement; re-runs and `--clear` are described in
[Operations](./operations.md#navigation-page-marker-backfill-scriptsbackfill_navigation_chunkspy).

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

`pack.sh` also writes `release-manifest.json` at the bundle root (via
`scripts/build_release_manifest.py`): the build's mainline ordinal plus every
`release-notes/*.md`, which drives the in-app "system updated" notice. It needs the **full
(non-shallow) git history** — a shallow clone, a note with a missing or invalid front matter or an invalid note file name
fails the pack. Ordinals assume master's first-parent lineage, so **build production
packages from `master`**; a branch build may skip notes. Without git (or when the source tree is not itself a repository root) the pack warns
loudly, writes no manifest, and that bundle shows no update notices.

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

Build-host knobs: `NODE_VERSION` / `NODE_DIST_URL` / `NODE_TARBALL` (portable-Node source;
`NODE_VERSION` defaults to the build host's `node -v`, and `pack.sh` refuses to pack unless both the
build host's Node and `NODE_VERSION` are the major pinned in `.nvmrc`, currently 26 — there is no override),
`SKIP_WHEELHOUSE=1` (target installs deps online instead), `PIP_INDEX_URL`, `PACK_PYTHON`.
Target knobs: `PYTHON_BIN`, `PIP_INDEX_URL`, `FRONTEND_HOST` / `FRONTEND_PORT` / `BACKEND_HOST`
/ `PORT`. The build host's Python **minor** version should match the target's, or the prebuilt
wheels won't install (install.sh then falls back to the index). The bundle's `DEPLOY.md` has
target-side details.

## Configuration

All model services are reached over URL endpoints — no local model servers are started.

### Which settings belong in `.env`

Copy [`.env.example`](../.env.example) as a starting point, then keep the assignments
your deployment needs. It deliberately does not enumerate every `Settings` field.
The backend's accepted environment names, executable defaults, and validation are
defined in [`Settings`](../backend/app/core/config.py); this reference owns deployment
meaning, and the [Product and API reference](./product-and-api.md) owns exact product
limits. Frontend and standalone-script variables are documented by their consumers.

| Category | Where to configure it |
| --- | --- |
| Common deployment choices | The starter includes service/database addresses, credentials, model dimension and output budgets, main concurrency and memory budgets, uploads, logging/retention, and optional external-service entry points. |
| Advanced tuning, recovery, and extensions | Add only the overrides needed for an evaluated change or a documented operation. Retrieval/PPR/MMR, action budgets, experimental rollout, plugin bounds, spreadsheet detail, and maintenance intervals belong in their sections below or the linked operating procedure. |
| Fixed internal rules | Protocol and structural constants stay in code, with their public contract in the product reference; they are not deployment environment variables. |
| Inactive compatibility settings | Accepted legacy names with no production consumer are listed below for migration clarity and are omitted from the starter. |

Omitting a supported setting from `.env.example` does not remove its environment
override or change its default. Leaving an advanced assignment out of a local `.env`
lets it follow the current release's defaults; copying every default pins old tuning
across upgrades. Changes to the starter do not rewrite an existing `.env`. For an
empty value followed by a comment, use `KEY="" # explanation`, or put the comment on
its own line, so dotenv does not interpret the comment as the value.

**Compatibility and initial identity.** `EMBED_PERSIST_CHUNK`,
`ANSWER_CONTEXT_MIN_ITEMS`, `PROC_MIN`, and `GLOBAL_MAX_COMMUNITIES` are still accepted
by `Settings` for compatibility but have no production consumer. Changing them has no
runtime effect; do not use them for tuning. In particular, the retired `global` mode
is a `chunk` alias and does not use a community-count budget.

`SILICON_NOTEBOOK_SINGLE_USER_EMAIL` and `SILICON_NOTEBOOK_SINGLE_USER_NAME` only
supply the initial `user-local` row. SQLite inserts with `INSERT OR IGNORE` and
PostgreSQL with `ON CONFLICT DO NOTHING`; changing these variables does not update an
existing user's email or display name. They remain supported for initialization but
are omitted from the common starter. `SILICON_NOTEBOOK_ENV` still selects the runtime
environment; administrator authentication is configured separately.

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
`max_concurrency` is the only model-concurrency setting. Source-job counts,
window sizes, batch sizes, and local ANN threads do not create another model
gate.

Each chat service additionally requires positive integer `context_window_tokens`
and `max_output_tokens`; output must be smaller than the total window. Optional
`max_input_tokens` is positive and cannot exceed that window. These are the
actual endpoint's token limits, not inferred model-name defaults; fill them in
for existing files before upgrading. Non-chat services reject chat capacity
fields. Missing/invalid capacity refuses startup independently of
`MODEL_BINDINGS_STRICT`; invalid hot reload retains the last valid registry.
Only administrators see these declarations in the **模型服务** panel.

Advanced `MODEL_CONTEXT_SAFETY_RATIO` controls the fraction of the context window
reserved as a safety margin, validated as `0 <= ratio < 1`. Its default is owned
by the [product/API context rails](./product-and-api.md#model-context-capacity-and-report-compression).
It does not belong in the starter `.env`; override only after checking the
endpoint's actual counting behavior. One validated ratio follows the pinned
service snapshot through provider preparation and the final transport check.

The common chat boundary counts the complete final request using conservative
UTF-8 byte estimation and reserves a safety margin; it caps requested output by
service capacity. No tokenizer is guessed or downloaded. Token counting is
explicitly an estimate because providers can add hidden templates. Reports
automatically fit/compress over-capacity material using task-wide
`REPORT_CONTEXT_EXTRA_CALLS_MAX`, `REPORT_CONTEXT_COMPRESSION_MAX_LEVELS` and
`REPORT_CONTEXT_COMPRESS_MAX_TOKENS`; these advanced cost/quality settings belong
here rather than the starter `.env`. Exact defaults and failure/receipt contracts
are owned by [the product/API reference](./product-and-api.md#model-context-capacity-and-report-compression).

Bind the new `report_context_compress` chat workload (「报告材料整理」) as well;
its default thinking policy is disabled. It uses the report queue priority and
may share an existing service, with no independent service concurrency budget.

**The binding table is checked at startup: one missing workload or one unknown
id and the backend refuses to start.** Whenever `MODEL_SERVICES_CONFIG` is
non-empty, `[bindings]` must cover every workload (all three kinds — chat,
embedding, rerank) and must not name an id that is not a workload (a
misspelling, or one retired by an upgrade such as `graph_chain_verify` or
`kg_community_summary`). Either
one and the process exits non-zero, and the backend log — which is also stderr,
since `prod.sh`/`backend.sh` append it to the same file — carries one readable
line stating why, with no traceback:

```
model-bindings: 缺少绑定的工作负载：库理解整理（agent_profile_consolidate），检索打法总结（retrieval_experience_distill）；已退役、升级后请删除的绑定：graph_chain_verify（[bindings]/[thinking]）；未知、疑似拼写错误的绑定：ask_anwser（[bindings]）。请在 MODEL_SERVICES_CONFIG 指向的文件的 [bindings]/[thinking] 补齐/删除后重启；确需临时放行设 MODEL_BINDINGS_STRICT=false（仅告警）。
```

Every segment is listed in full (sorted by id, workloads with their Chinese
label), so one edit fixes everything instead of one restart per mistake. Stale
ids are split into "retired, delete the line" and "unknown, probably a typo"
because those call for opposite actions, and each id names the table it was
found in — an id that exists only in `[thinking]` is not findable in
`[bindings]`. The message carries ids, labels and table names only — never a
secret or an endpoint.

A retired id that your `model-services.toml` still carries is refused like any
other — for example `kg_community_summary` (the community-report summarizer,
retired as dead code because it was never called at runtime), which files
generated from the template before its retirement bind in both tables. Delete
its lines from `[bindings]` and `[thinking]` before upgrading (the refusal names
the table each one is in). Rolling back to a version older than that retirement
needs the opposite edit: that version still lists the workload, so add
`kg_community_summary = "<id of a chat service>"` back to `[bindings]` first, or
its startup check refuses the file for the missing binding. Rolling back to a
version that still ignored the leftover lines needs no edit.

**The refusal line never prints the configuration file's path** — it says
"MODEL_SERVICES_CONFIG 指向的文件", so look the TOML up through that environment
variable. The backend runs as `uvicorn … >>"$BACKEND_LOG" 2>&1`, which makes
stderr the log file, so a failed startup prints this one line instead of raising
a traceback. For the same reason a stale id read out of the file is echoed only
when it is shaped like a workload id (`^[a-z0-9_]{1,64}$`); anything else
collapses into a counted `<无法显示的键名>×N` with its table — a TOML key holding
a path or a newline would otherwise write a private path into the log, or forge
a whole extra line. Workload ids and their Chinese labels are a closed in-code
vocabulary and are always named. Other model-configuration errors (unreadable
file, missing secret, invalid service fields) stop the process the same way but
print only the fixed sentence `模型服务配置无效（MODEL_SERVICES_CONFIG 指向的文件解析失败）`,
with no exception text at all; which line is wrong is read from the file itself.

The check lives in registry loading, so **hot reload runs
through the same gate**: an edit that deletes a binding line is rejected, the
previous registry is kept, and the service never carries on with half a binding
table (that diagnostic goes to the backend log only; the `model_config_reload`
event carries just `code=invalid_configuration`).

**Feature switches grant no exemption.** A workload must be bound even when its
switch (`AGENT_PROFILE_ENABLED` and friends) is off: switches change at runtime
while the binding table is read once at startup, so leaving it unbound today
means turning the switch on tomorrow silently downgrades instead of working.
That is the literal reading of the ruling this check implements, and the reason
the escape hatch is a single deployment-wide flag rather than a per-workload
waiver.

Why the rule exists: an unbound workload simply resolves to "no service" in the
registry and the whole chain fails soft — no error anywhere, just a feature that
silently does nothing (an unbound `agent_profile_consolidate`, for instance,
makes every background consolidation run behind "AI 对这个库的理解" settle as
`failed:模型未配置，无法整理`). A `[bindings]` file is normally generated once
and then carried across versions, so every **new** workload starts life unbound
in every existing deployment, and the silent downgrade lands on end users.
**After an upgrade, regenerate `.local/model-services.toml` or add bindings for
the newly introduced workloads by hand.** `scripts/migrate_legacy_model_env.py`
walks every workload when generating the file (it can only bind what the legacy
`.env` actually configured, and warns on the spot when the result is
incomplete); adding the missing `[bindings]` lines by hand works just as well.

`MODEL_BINDINGS_STRICT=false` downgrades that same diagnostic from a refusal to
a startup warning, as a temporary pass during an upgrade. It is not the product
default and it does not change the fact that an unbound workload is silently
unavailable. With the gate off, the backend emits one WARNING line before READY
naming every workload nothing is bound to — all three kinds, the same scope as
the refusal it replaces (Chinese label plus workload id) — and one more naming
the unknown/retired ids it ignored; on top of that,
`agent_profile_consolidate` and `retrieval_experience_distill` each get their
own line ("特性已开但模型未绑定") when their feature flag
(`AGENT_PROFILE_ENABLED`, `RETRIEVAL_EXPERIENCE_ENABLED`) is on.

An empty `MODEL_SERVICES_CONFIG` is a different thing entirely: that is the
supported offline/deterministic runtime and the strict gate has no opinion about
it (otherwise every development environment and `scripts/check.sh` would fail to
start). It gets one WARNING line before READY saying that everything needing a
model will answer deterministically.

The cards of the model-service status panel are organised by **physical
service**, so an unbound workload appears on no card. With the gate off the
panel therefore adds one block above the card list (administrators only):
「未绑定」 lists every workload with no binding (Chinese label), marking the two
feature-gated ones 「（功能已开启）」 when their flag is on, and 「已忽略的绑定」
lists the unknown/retired ids that were ignored. The block is **the same
computation** as the startup warnings (`model_registry.binding_report`; the
warnings only render it as log lines), read from the live registry each time the
panel loads, so a hot reload that fills in the bindings clears it without a
restart. It is not rendered when there is no gap; the offline mode says only
that no model service is available, exactly like its startup line (the panel
shows 「暂无模型服务」), and never reports every workload as unbound; under the
default strict gate a process that started has no gap, so the block never
appears. The startup failure message remains the only report in strict mode.

Auto mode (the simplified interface) always uses step-by-step reasoning
(`reasoning`) at standard effort, going through the same `/ask/intent` intent
preview (reusing the `reasoning_agent` workload) that the advanced interface
uses when `reasoning` is selected; it introduces no additional model-service
binding. When that workload is unbound or the call fails, question
understanding fails open to an empty contract, the question is treated as
clear, and the run still executes as `reasoning`; there is no longer an
ordinary Q&A fallback.

The optional `[thinking]` table controls thinking per **chat workload**, using
`enabled`, `disabled`, or `provider_default`. The policy is workload-scoped
rather than service-scoped because one physical chat service may carry both
reasoning and mechanical structured-output calls. The resolved policy is the
only switch: the transport sends `enabled` or `disabled` as `thinking.type`
through the OpenAI SDK's `extra_body`, while `provider_default` sends no
override. Neither the provider nor transport layer inspects the configured
model name.

The checked-in example spells out every current default. `ask_answer`,
`reasoning_agent`, `report_outline`, `report_sufficiency`, `schema_induction`,
`agent_profile_consolidate`, and `retrieval_experience_distill` are enabled. They are one-shot or bounded
planning/judgement/synthesis calls whose result directly affects the user or a
durable retrieval policy. Every other current chat workload is disabled. In
particular, all KG extraction/governance/description passes, chunk-question
generation, metadata/summary extraction, query/evidence rewriting, report
section drafting/final audit, Memory preview, and Knowhow formatting/completion
stay non-thinking. Those paths are mechanical, already receive an upstream
plan, are human-reviewed, or multiply over windows/chunks/sections; hidden
reasoning therefore has a much worse quality-per-token ratio. An omitted entry
uses the same built-in default, while `provider_default` deliberately sends no
override.

For every bound chat service, an explicit mode is sent via the OpenAI-compatible
`extra_body={"thinking":{"type":"enabled|disabled"}}`; no `reasoning_effort` is
sent. The `model` value remains an opaque endpoint routing identifier and never
controls whether thinking is applied.
Unknown workloads, non-chat workloads, and invalid values make configuration
validation fail rather than being ignored. The resolved mode is frozen with the
physical route when a call is submitted, so TOML hot reload affects new calls
without changing queued ones. An explicit mode is part of the LLM response-cache
identity; `provider_default` requests retain their historical cache keys.
Chat health checks are outside the workload policy: they always send
`thinking_mode="disabled"` regardless of model name, bypass the response cache,
and cannot be overridden through `[thinking]`.

The optional generated-question index uses background chat workload
`chunk_question_generation` plus the existing `chunk_embedding` workload. Bind both
before running the offline `question-index` phase. Leaving the rollout mode off is the
zero-cost default; deployment rollout semantics and all numeric rails live in the
[Product and API reference](./product-and-api.md#optional-generated-question-recall-supplement).

Deployment Ask engines use the interactive chat workload `plugin_engine`. The checked-in
example binds it to `general` and disables provider thinking because the plugin owns its
prompting loop. Its completion output budget deliberately inherits the bound model
client's ordinary answer cap. The advanced `ASK_PLUGIN_ENGINE_*` settings bound
retrieval calls, evidence and prompt size, model calls, and trace shape; add an override
to `.env` only when the deployed engine needs one. Exact defaults and valid ranges live in the
[Product and API reference](./product-and-api.md#deployment-ask-engines-askengine).

Deployment indexing pipelines do not add a separate model workload in PR-1. A plugin may
contribute notebook-scoped chunk/index strategies through `indexing.pipeline`; parser
routing remains automatic. The browser's notebook settings now show the current pipeline
read-only to pure readers, allow owners and group content-managers to switch it with an
explicit full-rebuild confirmation, and still keep mounted reference-library management
owner-only. The pending/missing/unavailable rails and sanitized API surface are specified
in the [Product and API reference](./product-and-api.md#deployment-indexing-pipelines-indexingpipeline).
Operators may tune `INDEXING_PIPELINE_MAX_PROPOSALS_PER_SOURCE`,
`INDEXING_PIPELINE_MAX_TEXT_CHARS`, `INDEXING_PIPELINE_MAX_ELEMENT_REFS`,
`INDEXING_PIPELINE_REBUILD_MAX_PROPOSALS`, and
`INDEXING_PIPELINE_REBUILD_MAX_TEXT_CHARS`. The exact defaults/ranges stay in that
product reference. A switch reuses the durable KG rebuild job row even when no KG model
is bound. Rebuild work is persisted into an unpublished notebook stage; model and embedding
I/O stay outside the final transaction, and only an exact job/generation/source-snapshot CAS
publishes all visible-source chunks and optional KG products together. Failure, cancellation,
startup recovery, or a late worker discards only that stage and leaves the live generation
unchanged. With no KG model, the same publisher explicitly preserves the live KG while
publishing the core chunk generation (and a full scale generation when eligible).

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
  half-open recovery probe. A malformed model response (empty body, bad
  JSON, truncation) never counts toward the breaker — it is model behavior,
  not provider availability, and already has its own per-call retry and
  run-level degrade path. It is still recorded as a health observation, but
  one that does not change availability: the panel keeps the service's
  status at `ok` (the provider demonstrably answered) and carries the
  diagnostic in the `code` (`malformed_response`) and `trigger`
  (`observed_failure`) columns. It never arms a recovery probe, and it never
  overwrites a standing `error` row that no success has cleared yet.

The scheduler and breaker are process-local. Production must run exactly one
backend process (`scripts/prod.sh` pins Uvicorn to `--workers 1`); multiple
workers would multiply the configured service concurrency and split queue,
breaker, and health state.

The **模型服务** panel is read-only for ordinary users. It shows sanitized
system service identity, bound workloads, last-known health, active/maximum,
queued work, oldest wait, and breaker state. A malformed model reply shows up
there as an observation only (`ok` + `malformed_response`), never as a change
in availability. Reading
`GET /api/model-services/status` never probes an upstream service. Besides the
per-service `services[]`, the response carries three binding-gap fields, filled
for admins only and always empty for everyone else: `unbound_workloads[]` (each
`{id, label, feature_enabled}`, in workload-catalogue order; `feature_enabled`
is true when the workload's own feature flag is on), `stale_binding_ids[]` (the
ignored unknown/retired ids, sanitised exactly like the startup log: only keys
shaped like a workload id are echoed), and `stale_binding_hidden_count` (how
many other keys could not be shown, the log's `<无法显示的键名>×N`). The
`POST /api/admin/model-services/test-all` response carries the same three
fields; a single-service test response is one service row and does not. Only admins
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

`OPENAI_COMPAT_MAX_TOKENS` supplies the default completion cap for generation calls
without a workload override; zero removes this global override while the declared
service output cap still applies. `ANSWER_MAX_TOKENS` caps final answers and `KG_EXTRACT_MAX_TOKENS`
caps extraction output. A non-positive value for either omits that workload override
and falls back to the global cap. `REASONING_MAX_TOKENS` caps planning and reflection;
`REPORT_SECTION_MAX_TOKENS`, `REPORT_SYNTHESIS_MAX_TOKENS`, and
`REPORT_SUMMARY_MAX_TOKENS` cap section drafting, the report-wide evidence blueprint,
and final editing respectively. These reasoning/report caps must be positive. For
every workload, input plus requested completion must fit the bound model's context
window; model service concurrency is independent of these budgets.

Advanced retry controls are `OPENAI_COMPAT_MAX_RETRIES` for ordinary chat and
`KG_LLM_MAX_RETRIES` for KG calls, alongside their respective
`OPENAI_COMPAT_TIMEOUT_SECONDS` and `KG_LLM_TIMEOUT_SECONDS` deadlines.
`EMBED_RATE_LIMIT_RETRIES` and `EMBED_RATE_LIMIT_BASE_DELAY` bound embedding
rate-limit retries and their exponential backoff. Raising retries increases potential
latency as well as resilience; adjust them for the endpoint's measured behavior.

Extraction tuning uses `KG_WINDOW_TARGET_CHARS` (zero selects adaptive sizing, positive
values select a fixed target), `KG_WINDOW_MIN_CHARS`, `KG_WINDOW_MAX_CHARS`, and
`KG_WINDOW_OVERLAP_CHARS`. `KG_WINDOW_WARN_THRESHOLD` only reports a large window count;
it never truncates the document. `KG_RELINK_ENABLED` controls deterministic isolated-node
relinking, `KG_INCREMENTAL_FUSION_ENABLED` controls incremental graph fusion, and
`KG_CONFLICT_RESOLUTION_ENABLED` enables model conflict adjudication. Merge pre-review
uses separate confidence thresholds, `KG_MERGE_CONFIRM_THRESHOLD` for confirming a
merge and `KG_MERGE_SEPARATE_THRESHOLD` for keeping candidates separate. These quality
and cost choices belong in evaluated advanced overrides, not every deployment's `.env`.

**Core-aware autotune:** local CPU-bound work may scale with the machine:

```text
KG_CLUSTER_ANN_THREADS   # hnswlib concept-clustering threads;
                         # 0 (default) = min(cpu_count, 32)
```

`scripts/dev.sh` and `scripts/prod.sh` source `scripts/autotune.sh` for
local OMP/BLAS threads. This does not change any model-service capacity.
Set the launch-script variable `AUTOTUNE=0` to disable that automatic behavior.

**Database:**

PostgreSQL pool sizing uses `POSTGRES_POOL_MIN_SIZE` / `POSTGRES_POOL_MAX_SIZE`,
with `POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS` bounding the wait for a connection.
`POSTGRES_STATEMENT_TIMEOUT_SECONDS` and `POSTGRES_LOCK_TIMEOUT_SECONDS` bound one
statement and database-lock waits respectively. The whole-table operations of offline
maintenance, the two whole-table streaming reads of the knowledge-graph rebuild (the
relation graph and the per-source object scan), and each page of the rebuild's
cluster-map write (one transaction per 20,000 members of an object type) raise the
statement timeout to at least 600 seconds inside their own transaction (raise-only: a
larger configured value is kept, `0` stays disabled, and it reverts when the transaction
ends); every other statement stays bound by this value. Tune these advanced PostgreSQL-only
settings together with database resources and concurrent workloads; the migration-only
role of `SHADOW_DATABASE_URL` is described in backend selection above.

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout in ms (default 30000)
DB_WRITE_LOCK_STATS         # enable process-wide SQLite write-lock wait/hold instrumentation (default true)
DB_WRITE_LOCK_WARN_MS       # wait/hold threshold in ms that logs a rate-limited db_write_lock_slow event (default 200)
DB_WRITE_LOCK_FLUSH_SECONDS # interval in seconds for the periodic db_write_lock_stats snapshot, and the per-call-site rate-limit window for db_write_lock_slow (default 60)
SQLITE_CACHE_SIZE_KB    # Per-connection SQLite page cache in KB (negative = KB). Connections are reused per-thread; total memory ≈ threads × |value| (default -16384)
DATABASE_URL            # Active database URL; PostgreSQL for development/production; unset fallback: sqlite:///.local/silicon_notebook.db
SILICON_NOTEBOOK_STORAGE_DIR   # uploaded file storage directory (default .local/storage)
SILICON_NOTEBOOK_SYNC_ENV      # this deployment's name in cross-environment notebook sync (default empty; see Operations)
SILICON_NOTEBOOK_SYNC_KNOWHOW_HISTORY # sync export also carries the Knowhow edit history (default false; export side only)
```

The synchronous Ask-cancellation endpoint may cross more than one database
transaction and can also wait on process-local or backend-specific connection
locks. Because no deployment setting provides an enforceable whole-request
deadline, the browser keeps one cancellation request in flight until the server
answers instead of applying a guessed client timeout.

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

**URL source import:**

```text
URL_IMPORT_TRUSTED_PROXY_HOSTS  # comma-separated trusted plugin-proxy origins exempt from the URL-import SSRF public-address check (default empty = no exemption)
```

The URL-import chain refuses outbound URLs that resolve to private, loopback or
reserved addresses. A deployment plugin proxy co-deployed with this service (for
example a signed PDF-download proxy on `http://127.0.0.1:8100`) naturally lives on
such an address; list its origin here to let URL imports reach it. Every entry
must carry its `http://` or `https://` scheme — a bare `host:port` entry is
silently ignored. Matching is by exact origin (`scheme://host:port`, lowercased,
default ports normalized — a different port is a different origin), and a match
skips only the public-address check in the import probe and the parse-time
download; scheme/credential/port shape checks still apply. The list comes from
this deployment configuration only — request input can never alter it. The
probe checks the initial URL against this deployment list for both browser and
plugin-port imports; an origin match exempts redirects only while each target
remains on that same normalized origin. Cross-origin redirects must pass the
public-address check, even if their destination is also listed. The parse-time
download applies the list to every matching
URL source, reparse included. A matching proxy may return `text/markdown` as a
snapshot: it is imported as `.md` through the builtin Markdown parser without
MinerU, including from the ordinary UI import button. Other origins remain
direct-PDF-only. Only list origins you trust to that extent.

Reasoning Ask's optional gap consultation uses `gap_consult_query` to select
described external sources and prepare per-source queries, then uses
`external_evidence_answer` for a separate unverified supplement when snippets
are available. The example binds both to `general` with thinking disabled.
An unbound or failed query workload calls no external contributor; an unbound
supplement workload leaves the drafted answer intact. Both phases, source
discovery, and contributor calls share `ASK_GAP_CONSULT_TIMEOUT_SECONDS`
(default 4 seconds, `0 < x ≤ 30`), rather than receiving separate budgets.

**Retrieval:**

Advanced ranking controls include `PPR_DAMPING`, `PPR_TOL`, `PPR_TOP_CHUNKS`, and
`PPR_VARIANT_EDGE_WEIGHT` for graph propagation, convergence, selection, and alias-edge
weighting; `PPR_EMB_SYNONYM_ENABLED`, `PPR_EMB_SYNONYM_THRESHOLD`,
`PPR_EMB_SYNONYM_TOPK`, and `PPR_EMB_SYNONYM_MAX_ENTITIES` control synthetic synonym
edges and their construction cost. `CHUNK_MMR_LAMBDA` balances chunk relevance and
diversity, while `EVIDENCE_TAU_LOW` / `EVIDENCE_TAU_HIGH` govern grounding thresholds.
Keep these out of the common starter and evaluate quality and cost before overriding
them; the active retrieval route determines which settings apply.
`KG_ISOLATED_RANK_PENALTY` lowers an isolated node's ranking score without changing its
relevance value; `QUERY_REWRITE_ENABLED` controls pre-answer query rewriting/expansion.

Global Ask uses `GLOBAL_ASK_CANDIDATE_LIMIT` for merged cross-notebook evidence and
`GLOBAL_ASK_HISTORY_TURNS` for admitted completed conversational turns.
`GLOBAL_ASK_KEYWORD_ARM_ENABLED` (default `true`) runs the bilingual keyword recall supplement once per
participant in global Ask; `false` is the rollback that closes it for global runs (see the product reference).
`GLOBAL_ASK_EXACT_ARM_ENABLED` (default `true`) does the same for the exact-identifier lookup arm (still subject
to `EXACT_LOOKUP_ENABLED`); `false` closes that arm for global runs.
`GLOBAL_ASK_ELEMENT_ARM_ENABLED` (default `true`) does the same for `reasoning`'s raw-element search arm (the
`search_elements` action and the empty-first-round top-up), capped in total at the single-notebook element limit;
`false` closes it for global runs and drops `search_elements` from their action catalog again.
`GLOBAL_ASK_MAX_NOTEBOOKS`, `GLOBAL_ASK_MAX_CONCURRENT`, `GLOBAL_ASK_RETRIEVAL_CONCURRENCY`,
`GLOBAL_ASK_RETRIEVAL_TIMEOUT_SECONDS`,
`GLOBAL_ASK_NOTEBOOK_TIMEOUT_SECONDS` and `GLOBAL_ASK_SHUTDOWN_TIMEOUT_SECONDS` control explicit
scope admission, process capacity, cross-notebook retrieval parallelism and retrieval/shutdown budgets.
`GLOBAL_ASK_RETRIEVAL_CONCURRENCY` sizes the ONE process-level executor shared by every global run, and
`GLOBAL_ASK_RETRIEVAL_TIMEOUT_SECONDS` budgets ONE federated retrieval call rather than a whole job, so a
`reasoning` job spends it once per retrieval round.
`GLOBAL_ASK_MAX_NOTEBOOKS` is capped at 8 by the product contract and will not validate above it.
**Upgrade note:** a deployment that previously set it above 8 (the old default was 32) now fails settings
validation and the backend will not start. Remove the variable or lower it to 8 or less before upgrading.
Size the PostgreSQL pool with BOTH global Ask knobs included: each job thread holds a connection for its
own authority and progress work, and the single process-wide retrieval pool holds one more per busy slot,
so the startup warning counts `GLOBAL_ASK_MAX_CONCURRENT + GLOBAL_ASK_RETRIEVAL_CONCURRENCY`. The chunk
arm's own fan-out is counted as well (`CHUNK_FANOUT_MAX_WORKERS`): each of its worker slots issues a
database query of its own, and a budget that leaves it out under-reports a deployment that the ordinary
in-notebook chunk arm alone can exhaust.
`GLOBAL_ASK_MIN_RELEVANCE` and
`GLOBAL_ASK_RELATIVE_RELEVANCE` filter weak candidates before reserving peer evidence; tune them against
representative questions. Libraries without semantic recall use bounded lexical fallback.
`CHUNK_FEDERATION_ENABLED=0` does not switch global Ask off or back to a single-library short circuit: a
global run degrades to a one-leg federation that searches the first resolved library only.
Model context shares
`CHUNK_ANSWER_BUDGET_CHARS`; no separate model endpoint is needed (`query_rewrite`
and `ask_answer` use the existing model-service configuration). Exact defaults and
rails are owned by [Global Ask](./product-and-api.md#global-ask).

General-Q&A document introductions reuse `CHUNK_ANSWER_BUDGET_CHARS` for evidence
context. `DOCUMENT_OVERVIEW_MAX_ELEMENTS` (default 64, minimum 2) bounds the number
of original parsed elements sampled for one document, including the final position.
This is a deployment cost/coverage budget, not a guarantee of complete chapter
coverage. Library introductions reuse the existing request enumeration rails.
This same knob now also bounds reasoning Ask's `read_document` action's
per-document element sampling pool (see `REASONING_MAX_DOCUMENT_READS` below) —
the two lanes share one deployment cost ceiling rather than each getting its own.

```text
RETRIEVAL_TOP_N         # reasoning/report synthesis evidence-budget floor (default 20)
REASONING_PER_QUERY_LIMIT # per-query take for compatibility callers without an effort profile
REASONING_TOP_N_PER_QUERY  # adaptive budget: seats reserved per aspect/sub-query (default 3)
REASONING_TOP_N_CAP        # adaptive budget cap; comparison Qs scale by #aspects (default 36)
ASK_RELATED_KNOWLEDGE_LIMIT # related-KG records projected into an Ask response
QUERY_REFINE_MAX_ITEMS / ASK_CONTEXT_RELATION_LIMIT # refined evidence bullets and ranked relationship lines admitted to Ask context
CHUNK_KG_NODE_SEED_TOP_N / CHUNK_KG_RELATION_SEED_TOP_N / CHUNK_KG_MAX_DEPTH / CHUNK_KG_FAN_OUT # chunk×KG overlay rails
CHUNK_GRAPH_RESERVE        # seats for graph-only chunks already above the relevance floor (default 0; set 1 after evaluation)
EXACT_LOOKUP_ENABLED       # exact-identifier fast path: whole-section fetch for `set_db`-style names (default true)
EXACT_LOOKUP_MAX_IDENTIFIERS       # names probed per question (default 3)
EXACT_LOOKUP_FTS_K                 # exact hits sampled per identifier when ranking sections (default 50)
EXACT_LOOKUP_MAX_SECTIONS          # sections fetched whole per question (default 3)
EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION  # chunks taken per section (default 12)
EXACT_SECTION_RESERVE      # mix-selection seats reserved for those chunks, inside the existing budget (default 4)
REASONING_EXACT_RESERVE    # reasoning's own synthesis-assembly prefix seats for exact-channel chunks, reordering only, no budget growth (default 4, 0=off)
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
instead. Ask's `mix` mode and any reasoning run made without an effort level
still read them unchanged.

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
SCALE_IDX_CACHE_MAX_LARGE  # max resident indexes whose estimated ANN matrices exceed SCALE_IDX_LARGE_BYTES; preload also requires this to cover every published large index (default 2)
SCALE_IDX_LARGE_BYTES      # large-index classification threshold shared by runtime LRU and startup preload (default 8GiB)
SEARCH_CONCURRENCY_LIMIT   # process-wide cap on concurrently executing notebook full-text searches, shared by the HTTP /search route and the MCP search tool (default 4, matching the frontend's own per-collection fan-out). Waiters queue on the event loop (no thread, no DB connection held) with no timeout — results are delayed, never narrowed. It is a deployment cost budget: lower it on hosts with a small POSTGRES_POOL_MAX_SIZE, raise it only with pool headroom to match.
SCALE_BUILD_CONCURRENCY    # process-wide cap on concurrently running scale index build/fold operations (default 2). Each build used to run on its own unbounded daemon thread, so the off-peak scheduler could start the whole idle queue's threads at once and spike memory/CPU on the host; a build beyond the cap blocks on this limit before it starts (its own run time once started is unaffected).
SCALE_BUILD_FAILURE_BACKOFF_SECONDS     # minimum delay before an *automatic* retry (scheduler/post-publish follow-up — never an explicit user "rebuild now" click) of a failed scale build/fold for the same notebook (default 60). Exponential: doubles on each consecutive failure.
SCALE_BUILD_FAILURE_BACKOFF_MAX_SECONDS # ceiling on that exponential backoff (default 1800), so a persistently failing notebook's retries space out instead of growing without bound while still never retrying back-to-back and burning a concurrency slot on a build that will fail again immediately.
```

**Notebook delete jobs** (batch 3·W1 PR-3 Phase A — see the design doc for the full six-phase job: `docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-design_zh.md`). `DELETE /api/notebooks/{id}` commits a single-row tombstone (`notebooks.status='deleting'`) and returns 202 immediately; the actual database and disk cleanup runs afterward in this dedicated background pool.

```text
NOTEBOOK_DELETE_CONCURRENCY               # process-wide cap on concurrently running notebook-delete jobs, in their own pool separate from BACKGROUND_MAINTENANCE_CONCURRENCY (LLM-scale rebuilds) and BACKGROUND_LIGHT_JOB_CONCURRENCY (second-scale single-table work) — a delete is neither: it is long-running, low-CPU, high-I/O, so it gets its own budget rather than starving or being starved by either sibling pool (default 1; 1 or 2)
NOTEBOOK_DELETE_SWEEP_SECONDS             # interval for the delete-job sweep's two drivers: requeuing a stale active job row (a worker died mid-phase, or a KG-rebuild-quiesce wait timed out) and recreating a missing job row for a notebook still marked 'deleting' (default 300, same order of magnitude as the checkup H4/H5 cache TTL)
NOTEBOOK_DELETE_QUIESCE_TIMEOUT_SECONDS   # total time a delete job waits for any knowledge-graph build/rebuild/relink already running against that notebook to stop on its own before giving up and handing the job back to the sweep for a later retry — it is NEVER forced through while a rebuild is still writing (default 1800, sized to cover one worst-case LLM extraction batch plus margin)
NOTEBOOK_DELETE_FINALIZE_TIMEOUT_SECONDS  # PostgreSQL-only, optional per-transaction statement_timeout override for the delete job's single atomic finalize step (fence + archive + drop the four directly-fenced tables + DELETE FROM notebooks). Default 0 = unset, falls back to the pool's own POSTGRES_STATEMENT_TIMEOUT_SECONDS. This is a TIGHTENING knob, not a relaxing one: the finalize transaction normally completes in single-digit seconds, far under the pool's own timeout, so any nonzero value here makes failure surface faster, never slower. Startup validation rejects a nonzero value outside `0 < value <= min(120, POSTGRES_STATEMENT_TIMEOUT_SECONDS)` — same cross-field validator family as POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS above.
KG_GRAPH_DRAIN_PAGE_ROWS                  # row budget for delete_notebook_kg's pre-reset drain (batch-3-W1 T-5a): one value serves as both the per-batch page size (one bounded DELETE per write transaction) and the per-table residue threshold deliberately left for the final single atomic reset. Sized against the deployment's statement timeout — the default 2000 sits comfortably under a 180s POSTGRES_STATEMENT_TIMEOUT_SECONDS; lower-resource installations can shrink it to shorten each batch's write lock / statement at the cost of more batches and a longer partially-drained window (default 2000; 50-20000, the ceiling stays under SQLite's default SQLITE_MAX_VARIABLE_NUMBER because drain pages bind selected ids as SQL parameters)
KG_DERIVED_BUILD_TTL_SECONDS              # crash-fallback TTL for the generational rebuild's in-flight claim (batch-3-W2). NOT the normal release channel: failure paths release instantly via the rebuild's finally-CAS; the TTL only covers kill -9/power loss where even finally never ran. Numeric guardrail (floor 1800 enforced at startup): must sit well above this deployment's worst-case full recluster wall clock — the production 484GB library runs 30-60 minutes, the 4h default leaves headroom; setting it too low preempts a still-running rebuild as a corpse and both writers' flip double-CAS void each other, spinning the notebook. A live claim heartbeats at stage boundaries and LLM chunk callbacks (codex #671 R13), so claimed_at keeps advancing and the TTL only ever reaps true corpses; a failed heartbeat degrades to plain fixed-TTL semantics. Do not lower without a measured faster rebuild wall clock (default 14400; >=1800)
KG_CATCHUP_SKEW_SECONDS                   # clock-skew allowance for the single-pass catch-up after a generation flip: replays retired-generation fusion rows with created_at >= (flip anchor - this allowance). The anchor uses the DB server clock; the allowance covers application-clock skew on row timestamps and long-transaction visibility drift. Directional guardrail: raising it only replays a few more rows (idempotent placement is harmless); lowering it is what risks dropped rows — do not lower unless every writer's clock is proven far tighter than the default (default 300; >=0)
INDEXING_PIPELINE_SWITCH_MAX_OBJECTS      # batch-3-W3 (decision D3): notebooks whose active-object count exceeds this are locked out of switching to a CUSTOM indexing pipeline (builtin revert exempt). Numeric guardrail: sized at the WR-2 failure scale (the whole-notebook publish transaction cannot complete at millions of objects), default 200k; deliberately NOT the copy threshold (NOTEBOOK_COPY_MAX_ROWS=5000, three orders too small and would lock ordinary notebooks). Counting uses the seq-gated count_active_objects memo (default 200000; >=1000)
KG_GENERATION_REAP_PAGE_ROWS              # rows per stale-generation reap page (shared by the rebuild pre-reclaim and the communities pre-publish reap; startup recovery uses its own constant plus a global page budget). Same family and bounds as KG_GRAPH_DRAIN_PAGE_ROWS: one bounded DELETE per page, one write transaction each, write lock released between pages (default 5000; 50-20000)
KG_FINDINGS_MAX_CANDIDATE_PAIRS           # cap on the candidate pairs GENERATED by the KG-analysis findings list's duplicate-import rule (F2) prefix-filtering join, repeats included (two sources generate one pair per shared prefix concept) — a cost budget, not a judgment threshold (the thresholds are named code constants written into the ledger with the rules version). The candidate count grows with the number of sources and cannot be extrapolated to production scale (to be measured by the read-only rehearsal). Generation measured at about 25 bytes of transient memory per generated pair, about 125 MB at the default cap; verification runs in element-budgeted batches whose peak does not grow with the candidate count. When the cap is hit only the candidates within it are verified, the ledger records candidates_truncated and candidate_pair_generations, the off-topic rule's duplicate-partner exclusion is marked incomplete too, and the two builds are not compared — never silent. Raising it only trades more transient memory and time (default 5000000; 1-100000000)
KG_MERGE_SQUASH_CANDIDATES_MAX            # cap on the pending-merge candidates of the "same name, different spelling" tier (merged knowledge objects whose seeds differ only in punctuation-made spaces; one star per group) written by one full merge — a cost cap, not a judgment threshold (the criterion is equal seeds once the spaces go). Hitting it keeps the member-richest groups and emits kg_merge_squash_candidates_capped (total / kept) — never silent. 0 = this tier proposes nothing (default 1000; 0-100000)
GRAPH_FETCH_PAGE_ROWS                     # row budget per page for the offline scale build's read-side pagination (batch-3-W4, codex #676): the graph-side keyset scans (graph_rows' objects/relations/chunks/clusters legs, active_object_graph_rows, id_element_rows, notebook_object_evidence_rows_paged) and the embedding-vector pagination (embedding_pages) all share this one value — this used to be a 10_000-row literal baked into the production code. Bounds the driver's per-statement result buffer: a row-wide table (e.g. large evidence JSON) at ~4KB/row means 10k rows/page ≈ 40MB, so resource-constrained deployments can lower it to shrink each page's transient memory at the cost of more page round-trips; raising it only trades fewer round-trips for a larger transient buffer. The ceiling guards against being misconfigured into an effective whole-table scan (default 10000; 100-200000)
```

Capacity expectation (batch-3-W2 generational swap): the steady-state footprint of
the three derived tables (concept_clusters/communities/community_members) is about
**2x** the pre-generational size — the retired generation is deliberately kept for
one full round after a flip (the in-flight readers' grace period), and the only
reclamation channels are the next rebuild's pre-reclaim and startup recovery. The
longer the gap between rebuilds, the longer both generations coexist; plan disk
and autovacuum for 2x the row and index volume.

With startup preload enabled, `/api/ready` remains false during the
`preloading_indexes` phase. A corrupt required artifact, more live published indexes
than `SCALE_IDX_CACHE_MAX`, or more large published indexes than
`SCALE_IDX_CACHE_MAX_LARGE`, keeps startup not-ready instead of passing the cold load to
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

**Retrieval hot cache (VectorCache):** the in-process, version-keyed cache holding
embedding matrices, keyword token sets, rustworkx graphs and cluster maps — single
entries reach GB scale. It used to have only one process-wide cap of 32 entries, while a
single large notebook occupies a dozen or more, so two or three active notebooks crowded
each other out — and what got evicted was exactly the most expensive to reload. Entries
are now pooled per key family; the total cap is a backstop and memory is bounded by a
byte budget.

```text
VECTOR_CACHE_PER_FAMILY_ENTRIES # resident **notebooks** per key family (default 8, the same "how many notebooks are active at once" unit as SCALE_IDX_CACHE_MAX). The unit is notebooks, not entries: the `matrix` family stores 4 entries per notebook (four embedding tables), so its entry quota is 8x4=32. Lowering this lowers "how many notebooks stay warm at once".
VECTOR_CACHE_MAX_ENTRIES        # process-wide total entry backstop (default 128 ~ 16 families x 8 notebooks; it was 32 before this version, a value that would cancel out the pooling). It should not be the binding constraint — it only guards against an unexpected family explosion.
VECTOR_CACHE_MAX_BYTES          # **estimated** byte budget for resident entries (default 16GiB, 0=disabled). Over budget, entries are reclaimed by global LRU. Estimates are per type (real nbytes for numpy/scipy, nodes+edges for rustworkx, item count x a nominal unit price for containers) — an order-of-magnitude estimate, not exact accounting.
```

RAM sizing: these three knobs together with `SCALE_IDX_CACHE_MAX` bound the process's
resident memory. On small-memory deployments (<32GB), first lower
`VECTOR_CACHE_PER_FAMILY_ENTRIES` to 2-4 (i.e. "keep only 2-4 notebooks warm"), then set
`VECTOR_CACHE_MAX_BYTES` to roughly a third of physical RAM; with both lowered you do not
need to touch `VECTOR_CACHE_MAX_ENTRIES`. WARNING: do not set
`VECTOR_CACHE_PER_FAMILY_ENTRIES` below the **number of participant notebooks in one
question** (the active notebook plus its mounted reference libraries): evicting a matrix
makes relation semantic scoring skip entirely (logged as the `relation semantic scoring
skipped` warning), which degrades answer quality, not just latency.

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

A notebook import (`scripts/cli.sh notebook import`, docs/operations.md "Single-file notebook
import") writes behind a hidden sentinel row whose `updated_at` is its heartbeat; a failed
import larger than the two row bounds above is removed by the batched delete job instead of
one transaction:

```text
NOTEBOOK_IMPORT_STALE_SECONDS    # an import sentinel whose heartbeat is older than this is
                                 # reaped at server start (handed to the delete job): the
                                 # importing process died. Only notebook imports (empty
                                 # sync_origin), never a sync mirror (default 3600; 60..604800)
```

**Welcome question suggestions:**

Automatic notebook titles/descriptions also use the `notebook_metadata` workload.
`NOTEBOOK_METADATA_BATCH_CHARS` controls the per-batch source-metadata input
budget; `NOTEBOOK_METADATA_BATCH_SOURCES` controls stable group size. Larger
libraries are merged hierarchically so every source participates. Validated
intermediate summaries reuse the existing `LLM_CACHE_ENABLED` cache; disabling
it preserves coverage but forfeits reuse across sequential source completions.
It does not change the source-summary model's own input selection. Exact ranges
and output rails live in [the product reference](./product-and-api.md#notebook-titles-and-descriptions).
No extra credentials or workload binding is required.

The empty Ask welcome view reuses the `notebook_metadata` workload's configured
model service. No additional provider credentials are needed. An unavailable
service or generation failure preserves the existing template questions.
Suggestion results use a bounded in-process LRU per application runtime, shared
by sessions reading the same notebook; they are regenerated after eviction or
restart. The source/content and model/prompt fingerprint controls invalidation.
The input is explicitly a partial document projection, not a full-library read.
Each source field/excerpt also obeys the existing `EMBED_TRUNCATE_CHARS` budget.

```text
NOTEBOOK_QUESTION_SOURCE_LIMIT    # sampled local documents (default 20; range 1–200)
NOTEBOOK_QUESTION_INPUT_CHARS     # aggregate source JSON input budget (default 12000; range 1000–100000)
NOTEBOOK_QUESTION_CACHE_ENTRIES   # cached notebook generations per runtime (default 128; range 1–4096)
NOTEBOOK_QUESTION_RETRY_SECONDS   # failed-generation retry cooldown (default 60; range 1–3600)
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
ANSWER_CONTEXT_BUDGET_CHARS  # answer-context assembly char budget (default 6000; not read by a deep report's sections — see the retrieval behaviour-change note)
ASK_REPORT_CONTEXT_MAX_CHARS # Ask follow-up on a deep report: character cap of the report block (title + executive summary + every section heading + the selected section bodies) in the answer prompt (default 12000; 2000..60000, out-of-range values refuse to start); over it, whole sections are chosen by relevance to the question and the receipt says so
ASK_REPORT_EVIDENCE_BUDGET_CHARS # same feature: character cap of the evidence partition the report's references are restored into by id (default 8000; 2000..60000), apart from the turn's retrieval budget; what does not fit is counted as left out for space on the receipt
RETRIEVAL_RRF_ENABLED        # BM25(Okapi)+RRF ranking vs keyword+semantic fusion (default false)
RETRIEVAL_RRF_K              # reciprocal-rank-fusion k (default 60)
KG_QUERY_REFINE_ENABLED      # question-aware evidence refinement before answering (default true)
QUERY_REFINE_MAX_CHARS       # max chars of evidence fed to refinement (default 4000)
RELATION_RETRIEVAL_ENABLED   # startup preload also opens and checks the scale index's relation ANN (default false, opt-in pending eval); relation hits are never fused into graph seeds (that path no longer exists, and RELATION_SEED_TOP_N is gone), and the chunk overlay's relation retrieval does not depend on it
KG_CANONICAL_FOLD_ENABLED    # fold same-canonical fragmented KG nodes at retrieval (default false)
KG_ABOUT_DOWNWEIGHT_ENABLED  # rank-down-weight weak `about` edges in relation retrieval (default false)
KNOWHOW_KG_NODE_RETRIEVAL_ENABLED # projected Knowhow cell objects join reasoning/graph KG-node retrieval (default true; false disables only this direct-node path, not cell-chunk search)
REASONING_ENUM_TOOLS_ENABLED # reasoning Ask's typed collection-enumeration reflect tools, enumerate_elements/enumerate_kg_objects (default true; false disables both tools and the collection map, zero extra queries)
REASONING_CHUNK_SEARCH_ENABLED # gates reasoning Ask's first-class source-passage retrieval action, search_chunks, its first-round deterministic passage seed, and the passage half of the confirmed-direction coverage pass and add_subquery (independent of the knowledge graph: graph-bearing and no-graph runs share this one gate; default true; false removes the action from schema/prompt/allow-list, stops the seed and the passage backfill, and leaves REASONING_MAX_CHUNK_SEARCHES with no consumer; the off state still pays one graph-existence EXISTS query inside the per-request memo, independent of this switch. The order in which passages are assembled for synthesis and for deep-report section drafting (relevance sort, then a two-lane interleave of concept-walk and retrieved passages, then the exact prefix seat) is a fixed rule and is not controlled by this switch)
REASONING_CHUNK_RERANK_ENABLED # cross-encoder rerank inside the source-passage retrieval tool (ReasoningRetriever.search_chunks, shared by the search_chunks action, the first-round passage seed and direction backfill; default true; effective only when the retrieval_rerank workload is bound): after recall the top REASONING_CHUNK_RERANK_CANDIDATES baseline candidates by fused score go to the reranker, generated-question supplements are reranked separately after the baseline, the top k in rerank order are taken and the active-notebook floor still applies; rerank decides selection only and never rewrites relevance (grounding thresholds read it); false, unconfigured, a failed or timed-out rerank, or a pool no larger than k falls back to MMR selection byte-for-byte; failures emit a reasoning_chunk_rerank_fallback event (reason rerank_unavailable / rerank_failed / rerank_timeout) and never a banner; cancellation still stops the retrieval
REASONING_CHUNK_RERANK_CANDIDATES # candidate window for that rerank: the top N baseline candidates by fused score go to the cross-encoder (default 50; 0 = zero rerank calls, back to MMR); at most two rerank calls per sub-query (baseline once, supplements once when the baseline has fewer than k passages)
REASONING_CHUNK_RERANK_TIMEOUT_SECONDS # caller time budget for one selection's rerank (seconds, default 10; ge=0): the queue deadline and the caller's wait share one deadline, and exceeding it falls back to MMR with a rerank_timeout event; a positive value only tightens — above the priority's default queue deadline (30 s interactive) the default wins; it never rewrites the HTTP request timeout (an abandoned in-flight request may hold its slot until the service's own HTTP timeout, but a caller's budget never marks the shared rerank service unhealthy or opens its breaker); 0 = no caller budget
REASONING_OUTLINE_ENABLED    # reasoning Ask's outline scratchpad reflect action, update_outline (default true; only offered at the `exhaustive` effort tier regardless of this flag; false disables the action and section-by-section synthesis, reverting to byte-identical pre-feature behavior); also gates Deep Report's per-section deep-dive at its exhaustive depth tier (depth 16, see REPORT_MAX_SECTIONS below) — same flag, no report-specific toggle
REASONING_OUTLINE_KG_GAP_ENABLED # weak-support KG relation hint fed back into the outline scratchpad after each accepted update_outline (default true; layered on top of REASONING_OUTLINE_ENABLED; false stops the scratchpad from carrying weak-support relation hints, zero extra queries); applies identically inside Deep Report's per-section deep-dive once it reaches the exhaustive depth tier
AGENT_PROFILE_ENABLED        # single gate for "AI 对这个库的理解": plan/reflect injection, the background consolidation trigger, and both API surfaces' visibility (default true; false reverts everywhere to byte-identical pre-feature behavior — no injection, no trace step, no job is ever queued, the API reports enabled=false instead of 404)
AGENT_PROFILE_BASE_TRIGGER   # accumulated source changes before the shared base layer (corpus_shape/key_entities/corpus_gaps) is re-consolidated (default 5)
AGENT_PROFILE_OVERLAY_TRIGGER # completed Ask jobs before one member's private overlay (retrieval_notes/usage_gaps) is re-consolidated; a completed deep report reaches this threshold immediately (default 10)
AGENT_CALL_LOG_ENABLED       # records one row per notebook-scoped MCP tool call an Agent makes (who, when, under which capability), readable and clearable by that member alone under "Agent 记录" (default true; false writes nothing at all — the check precedes the transaction). Layered UNDER AGENT_PROFILE_ENABLED rather than independent of it: this ledger's only reader is a panel whose entry button does not render while that gate is off, so recording with it off would accumulate rows nobody can open. Reading and clearing an existing ledger follow NEITHER switch — flipping this off means "stop recording", never "hide or freeze what was already recorded". The ledger still never reaches a prompt (the consolidation read pins kind='note' in SQL) and never triggers consolidation
RETRIEVAL_EXPERIENCE_ENABLED # shared distillation gate for the retrieval-strategy experience library's two chains (Agentic Memory P2, partitioned by notebook since 2026-09-22): whether finished asks are ever read and distilled into retrieval_experiences at all (default true — a deployment may distill and observe without ever injecting, see RETRIEVAL_EXPERIENCE_INJECT_ENABLED below)
RETRIEVAL_EXPERIENCE_INJECT_ENABLED # independent injection gate for the same library, shared by both partitions: whether the distilled block is ever added to the plan/reflect prompt (**default true since 2026-09-22**, false before that — once the library is partitioned per notebook, what a run gets injected is first and foremost its own notebook's experience, and a local trial run verified the whole chain end to end: 10 reasoning asks in one notebook distilled entries, and with the gate on the trace showed an `experience` step with `notebook_entries>0`. Setting it to false returns to "distill but never inject": the injection side is byte-identical to the feature not existing — no read, no block, no trace step — while distillation keeps collecting data; note it also removes the consult_memory action from the reflect enum, see the line below)
RETRIEVAL_EXPERIENCE_TRIGGER # accumulated completed asks (across every notebook and user) before one GLOBAL-partition distillation batch runs (default 40; ge=1)
RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER # accumulated completed reasoning asks for a single notebook before one NOTEBOOK-partition distillation batch runs (added 2026-09-22; default 10; ge=1)
REASONING_CONSULT_MEMORY_ENABLED # per-scenario kill switch (defense in depth) for the consult_memory reflect action (Agentic Memory P4); the action's actual availability gate is `retrieval_effort` in {deep, thorough, exhaustive} AND RETRIEVAL_EXPERIENCE_INJECT_ENABLED being on — this flag alone flipping true never makes the action appear if the injection flag above is off (default true)
REASONING_MAX_CONSULT_MEMORY # max consult_memory calls per run (default 2; ge=0)
USER_SEARCH_PROFILE_ENABLED  # single gate for the per-user search/answer style preference document (Agentic Memory P3, B-line): background inference, Ask plan/answer injection, and PATCH /me/search-profile's writability all key off it (default true; false reverts everywhere to byte-identical pre-feature behavior on the injection/write side — no inference, no injection, PATCH 409s — but GET /me still returns any existing value already on the row rather than forging search_profile: null)
USER_SEARCH_PROFILE_TRIGGER  # completed asks for that user before the deterministic, zero-LLM answer_language inference job runs again (default 20; ge=1)
NAVIGATION_DETECTION_ENABLED # write side of the navigation-page marker (SQLite v101 / PostgreSQL 0082): whether chunk construction and the post-readiness background pass judge table-of-contents / list-of-figures chunks from the document's own heading structure and write chunks.navigation (default true). false: every build writes 0 and leaves sources.navigation_scanned_at NULL, and the background pass and manual re-runs do nothing, so switching it back on judges those sources at the next start; it does not clear flags already written (scripts/backfill_navigation_chunks.py --clear does)
RETRIEVAL_NAVIGATION_FILTER_ENABLED # read side of the navigation-page marker: whether every retrieval producer (chunk scoring, PPR, KG source passages, the exact-identifier channel, the selected-source graph lane, extension-proposed chunks, and the element arm, which drops every element a flagged chunk lists) drops chunks with chunks.navigation=1 (default true). false: flagged chunks and their elements are retrieved again, as before the marker, immediately and without touching the stored flags -- the stop-gap for a misjudgement; NAVIGATION_DETECTION_ENABLED and scripts/backfill_navigation_chunks.py --clear manage the flags themselves
CHUNK_RECALL                 # chunk 大召回数 (default 200; mix 候选池 / MMR 候选)
CHUNK_FEDERATION_ENABLED     # federated chunk recall: whether a mounted reference library's own source passages enter chunk retrieval alongside the active notebook's (default true). False is the single rollback switch: the participant set collapses to the active notebook alone, the federated module short-circuits to the pre-federation single-library lanes, and behaviour returns to active-only passage recall. Global Ask is the one exception — a run with no subject library keeps the federated path and degrades to a one-leg federation over the first resolved library rather than taking that short circuit. A peer library contributes only its currently visible sources — hidden Memory/Knowhow projections are structurally excluded from the cross-library passage channel — and a library the request unchecked contributes nothing at all
CHUNK_FEDERATION_MAX_PARTICIPANTS # upper bound on libraries one federated chunk arm searches (default 8; 1..8). Beyond it the set is truncated along the deterministic mount order and a content-free `chunk_federation_truncated` event is emitted; the set is never silently widened or silently narrowed. Start at 4 in production and watch that event plus the ask-stage latency distribution before raising it
CHUNK_FANOUT_MAX_WORKERS     # total worker slots for ONE chunk arm's flattened `(library × sub-query)` fan-out (default 8; 1..16). "Per library" and "per sub-query" share ONE executor rather than nesting, so one arm's concurrency is this value regardless of how many reference libraries are mounted; the default matches the fixed width the single-library multi-sub-query path used before federation existed. It bounds ONE arm, not a request: a deep report runs several section workers concurrently, and their combined leaf I/O is bounded by `REPORT_RETRIEVAL_FANOUT` instead, which every federated chunk leaf now acquires individually
CHUNK_FEDERATION_ACTIVE_RESERVE # the reserved share of the FINAL selected passages (default 0.25; 0..1; a value outside that range is refused at startup). Recall is federated, but the current notebook is the subject and mounted reference libraries are the supplement: whenever reference-library passages compete, the active notebook keeps at least `min(CHUNK_MMR_K, ceil(CHUNK_MMR_K × this))` seats (4 at the defaults), capped by how many qualified passages it actually has — and, on the mix surface (2), by the token budget the graph and exact-identifier seats leave, a qualified passage larger than that remaining budget being skipped rather than admitted — and reference libraries' strong hits take the rest. Qualified means: not an optional generated-question supplement, not reached through the knowledge graph alone (graph-only passages have their own `CHUNK_GRAPH_RESERVE`), and relevance at least 0.12 unless the exact-identifier channel fetched it; a weaker or graph-only own passage the ranking already selected still counts toward the share, and passages with identical text spend one seat, not one per copy. Without it a few short local notes (relevance 0.30–0.40) lose every seat to one reference library's 0.60–0.85 hits, so a question about the document the user just uploaded returns none of it. The reserve governs MECHANICAL cuts only — ranking plus a token or character budget — and never overrides a model's judgement (evidence refinement, outline binding), the same boundary as the exact-identifier seats. 0 disables surfaces (1)–(6) below and the active/per-library part of (7), restoring pure relevance ordering; a notebook with no mounted library is unaffected either way, because a pool with no reference-library passage leaves the rule inert. **This knob is the rollback switch for the seats**, not `CHUNK_FEDERATION_ENABLED`: with federation off, the KG overlay / PPR lanes still bring mounted libraries' passages into mix and reasoning pools, those passages still count as reference-library passages, and the seats stay active — only `CHUNK_FEDERATION_ACTIVE_RESERVE=0` switches every seat off. It does not switch off the exact-prefix part of the passage floor (7), which has no switch: that part applies to single notebooks too and changes only runs in which the structured blocks would otherwise crowd the exact-identifier passages out. Tuning: raise it when answers about a small local notebook keep citing only a large mounted library; lower it (or 0) when the mounted libraries are the primary material and the local notebook holds only notes. Surfaces, one mechanical cut each: (1) `chunk` mode's MMR and multi-sub-query quota branches — the reserved candidates are withheld during the cross-library merge rather than recovered after it, so a library large enough to fill the whole `CHUNK_RECALL` pool cannot discard them before selection runs (they are paid out of that budget, never on top of it); the seats are then enforced once on the FINISHED selection, because Ask merges further keyword/exact-lookup producers into the candidates after retrieval returns, by taking back the lowest-ranked reference-library rows (optional generated-question supplements first) without changing the selection's length; the reserved rows sit in those tail positions, so when the answer render's character budget cuts the selection, unreserved rows are dropped before them. (2) `chunk` mode's mix branch (rerank + token budget) — one more reserve rule inside the same budget, placed LAST after the graph and exact-identifier seats, so it never evicts what those settled, never enlarges the budget and never adds a second oversized-first-passage exception. (3) Global Ask's mix branch — a peer run has no subject library, so the same number of seats is shared across the participating libraries in best-hit order (the split the exact-identifier seats use), seats dealt one at a time round-robin, so the strongest library takes a seat too and, with more participating libraries than seats, the libraries ranked after the last seat get none; inert with a single participating library (a peer run's MMR and quota branches reserve nothing: their cross-library merge already applies the global per-library rails). (4) `reasoning` single-shot synthesis — after the exact-identifier prefix (`REASONING_EXACT_RESERVE`), the seats minus the active passages that prefix already holds move qualified active passages (first copy per text, current order) directly behind it; a reordering of what the character budget sees, nothing dropped; in a Global Ask run the seats are shared per library exactly as in (3). (5) Sectioned synthesis — the same prefix, inside each section's own outline-bound passages only; unlike exact hits, an unbound active passage is never injected into a section. (6) Deep-report section drafting — outline-bound passages first (an active one among them already spends a seat), then the exact prefix, then the active / per-library prefix among the unbound passages. (7) Passage floor — in reasoning synthesis the structured blocks assembled ahead of the passages (Knowhow preview, enumeration preview, document reads, spreadsheet block) render exactly as before unless their historical rendering would push part of the reserved prefix — exact seats AND active / per-library seats, measured as the passage segment really renders them — out of the prompt; only then is the structured side rendered again under a ceiling that leaves the prefix its room (up to half the partition), so they can no longer leave the reserved passages zero characters and each block's own coverage line still describes exactly what it shows; every other run is unchanged. No separate setting: the floor's size is a fixed rule documented with its numbers in `docs/product-and-api.md` ("Passage floor").
CHUNK_FEDERATION_PEER_FLOOR  # advanced tuning: cross-library merge floor, relative to the best-scoring library in this arm (default 0.5; 0..1). Every participant library runs the SAME chunk producer and therefore the same 0..1 fused scale, so their scores are directly comparable; a library whose own best hit falls below `best × this` gets no guaranteed slot (its hits still compete for the remaining budget), and the remaining budget ranks by `(score / best) / local rank` rather than by each library's own peak. Raise it to keep a focused question's budget on the libraries that actually answer it; 0 restores the earlier behaviour where every mounted library, however irrelevant, reserved one slot — which cost the strong library roughly one passage per mounted peer
CHUNK_FTS_WITH_ANN_ENABLED   # deep-report only: restore the generic lexical union after a healthy indexed chunk ANN result (default false; ordinary Ask and exact/fallback paths are unaffected)
POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS # PostgreSQL-only private deadline for one generic chunk lexical call (default 3.0 since 2026-09-14, was 1.0; >0, <=10 and <=POSTGRES_STATEMENT_TIMEOUT_SECONDS — a deployment whose POSTGRES_STATEMENT_TIMEOUT_SECONDS is below 3 must set this explicitly or startup validation refuses); a timeout opens a per-notebook circuit for the rest of that retrieval run
POSTGRES_KG_LEXICAL_TIMEOUT_SECONDS # PostgreSQL-only total KG lexical supplement budget (default 5.0 seconds; 0 disables this budget/circuit for quality rollback). Applies only after nonempty whole-notebook ANN candidates exist and the effective type scope covers all four core KG types; completed phases survive expiry. Source/type-restricted, lexical-only fallback, relation endpoint and exact lookup paths remain unbudgeted. Clamped at execution to a stricter caller timeout. Numeric rails and degradation semantics: product-and-api.md, "KG lexical supplement budget". Advanced tuning; not required in .env.example.
LEXICAL_LANGUAGE_GATE_ENABLED # drop all-CJK lexical terms when the notebook's sampled corpus holds no CJK character (default true; such probes are provably empty yet each costs a real PostgreSQL LATERAL probe — measured 64 terms/29.7s cold vs 3 terms/0.26s warm for the same 26 rows on a 7,026-chunk English corpus, with the ungated form timing out under parallel report sections. Never filters the user's quoted spans or the whole-sentence term, never applies in the Latin direction, and never applies to *genuinely* source-restricted runs, whose lexical arm is their only candidate generator and whose source predicate already bounds the scan. That exemption is keyed on narrowing — a producer's own genuinely narrow allow-list counts the same as the request's own checkbox narrowing — not on the mere presence of a non-`None` allow-list parameter: a default all-selected request also carries a frozen allow-list pushed down to every producer, but it spans the whole notebook, so exempting it on that shape alone would reinstate the unbounded probe set this gate exists to prevent (a materialized ceiling forwarded one call deeper, e.g. from direct-element fallback into its own chunk-recall sub-call, is exactly such a non-`None`-but-unnarrowed list and must not flip the exemption either). A frozen-universe drift — the live source universe no longer matching the frozen all-selected snapshot — re-bounds the predicate and re-enters the exemption the same way a genuine narrowing does; the once-per-retrieval-arm drift check that decides this is a routing read only; the source predicate itself is still pushed down to every producer unconditionally regardless of this gate's verdict. Set false to restore byte-identical pre-feature behaviour if a notebook's sampled language misjudges it)
POSTGRES_LEXICAL_KNN_ENABLED # adaptive GiST `<->` KNN early stop for PostgreSQL KG-name lexical probes (default true; false is the rollback; SQLite short-circuits at zero cost). On eligible unscoped large notebooks, only short non-CJK terms use the shape-matched global GiST KNN; CJK and long terms bypass ordered KNN and use notebook-scoped `%` and literal-ILIKE arms that are independently bounded before an exact union/top-k. A normally completed short KNN page reuses its exhausted `%` results and probes only literal-ILIKE before the same union/top-k; no second `%` scan. PostgreSQL may plan those arms with the composite GIN or another usable bitmap combination; the application does not force an index. No term is dropped and scores/budgets/final ordering are unchanged on a stable snapshot. Equal-similarity full-page KNN tie membership retains the registered GiST traversal jitter; deployments requiring bit-stable sets can set this false. See Operations for index DDL, production evidence, rollout, and rollback.
POSTGRES_LEXICAL_KNN_MIN_ROWS # minimum notebook size (nodes+chunks) at which short eligible terms may use KNN (default 500000). The global GiST has no notebook key, so set this above the largest non-dominant notebook. Below the floor every term uses the result-equivalent notebook-scoped split path.
POSTGRES_LEXICAL_KNN_MAX_TERM_CHARS # maximum length for a non-CJK term to use GiST KNN (default 32; 3..256). Longer terms bypass ordered KNN and use the split path. This changes only the access path, not recall terms, candidate quotas, scores, or ordering.
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
REPORT_PROBE_CHANNEL_CONCURRENCY # planning probes: concurrent KG/raw-element channels within one query (1..2, default 2); distinct queries share the report-wide REPORT_RETRIEVAL_FANOUT budget after ordered de-duplication
REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS / REPORT_SUFFICIENCY_MIN_FAMILIES / REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES / REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE # centralized report sufficiency policy; defaults preserve historical judgments, exact rails in product-and-api
REPORT_SECTION_MAX_TOKENS    # deep-report: per-section drafting completion cap (default 65536)
REPORT_SYNTHESIS_MAX_TOKENS  # deep-report: report-wide JSON blueprint completion cap (default 102400)
REPORT_SUMMARY_MAX_TOKENS    # deep-report: final read-only editor completion cap (default 102400)
REPORT_ALLOW_PARAMETRIC      # deep-report: allow 【通识】/general-knowledge tier, marked & unverified (default true)
REPORT_HIGH_RISK_DOWNGRADE_ENABLED # deep-report citation audit may cap a grounded section at overview when its unsupported ratio exceeds the contract threshold (default false; disclosure still runs when false)
REPORT_HIGH_RISK_UNSUPPORTED_RATIO # deep-report high-risk citation-audit threshold; the numeric contract is owned by docs/product-and-api.md
REASONING_MAX_PPR_RETRIEVES / REASONING_MAX_EXACT_LOOKUPS / REASONING_MAX_FOLLOW_CHAIN_ACTIONS / REASONING_COMMUNITY_PEERS_CAP_FACTOR / REASONING_MAX_OUTLINE_UPDATES # centralized reasoning action/expansion rails; defaults preserve historical behavior, exact rails in product-and-api
REASONING_MAX_CHUNK_SEARCHES # per-run call cap for reasoning Ask's search_chunks action (default 3, matching REASONING_MAX_PPR_RETRIEVES/REASONING_MAX_EXACT_LOOKUPS; ge=0; the first-round deterministic passage seed does not count against this cap)
REASONING_DOCUMENT_READ_ENABLED # gates reasoning Ask's per-document bounded original-text sampling reflect action, read_document (default true; false removes the action from schema/prompt/allow-list, zero extra queries, byte-identical to pre-feature behavior); shares its gate with the collection-enumeration switch REASONING_ENUM_TOOLS_ENABLED — when enumeration is off, or the request has narrowed retrieval scope by source, this action disappears too even if its own switch is on, because it can only read a source roster the run already listed
REASONING_MAX_DOCUMENT_READS # per-run call cap for reasoning Ask's read_document action (default 4; ge=0; zero is a second deployment-level kill switch); the element sampling pool reuses DOCUMENT_OVERVIEW_MAX_ELEMENTS above, and the character sampling pool is a quarter of the tier's chunk_context_chars — both are split as "remaining budget // reads still available", not independent per-request fields
REASONING_SECTION_NAV_ENABLED # master switch for reasoning Ask's in-document navigation: the reflect actions browse_sections / read_section and the section-hint block (default true; false removes them from schema/prompt/allow-list together, byte-identical to before). Shares passage search's gate — with REASONING_CHUNK_SEARCH_ENABLED off, or at the overview tier, the feature is absent anyway. **Reserved names:** browse_sections and read_section are now core reflect action ids, so a deployment plugin that declares a reflect action with either name is refused at startup after upgrading (ExtensionRegistryError) until the action is renamed
REASONING_MAX_SECTION_BROWSES / REASONING_MAX_SECTION_READS # per-run call caps for browse_sections and read_section; both at 0 withdraws the feature; defaults and bounds in product-and-api
REASONING_SECTION_READ_MAX_CHUNKS / REASONING_SECTION_READ_RESERVE # how many chunks one read_section reads back, and how many synthesis seats read-back passages keep (0 = none) behind the exact-identifier prefix; defaults and bounds in product-and-api
REASONING_SECTION_BROWSE_MAX_CHARS # character budget of one rendered browse_sections listing; default and bounds in product-and-api
REASONING_SECTION_HINT_MAX_ENTRIES / REASONING_SECTION_HINT_SOURCES / REASONING_SECTION_HINT_RELATIVE_FLOOR # size of the vector section-hint block (entries, sources covered, and the relative score threshold against the best chunk similarity the run already holds; either count at 0 shows no block); defaults and bounds in product-and-api
REASONING_SECTION_OUTLINE_MAX_ROWS # hard bound on how many chunks of one source are scanned for its outline and read for its vectors (0 withdraws navigation entirely); also bounds per-source vector memory (about rows x runtime dimension x 4 bytes); default and bounds in product-and-api
REASONING_SECTION_VECTOR_CACHE_MAX_BYTES # byte budget of the process-level section vector cache shared across runs (LRU; 0 disables it, so every run reads source vectors itself). Entries are keyed by database, library, source, the source's chunked_at, an outline fingerprint and the query dimension, so re-chunking retires them; a source without chunked_at or with chunks still missing vectors is not cached. Known limitation: swapping the embedding model for one of the same dimension, or re-embedding in place without re-chunking, is picked up only after eviction or a restart. Default and bounds in product-and-api
FIND_SOURCES_CHUNK_RECALL # document-level search find_sources: passages its semantic leg recalls per library, restricted to the sources left after the metadata filters (default 400; 1..2000); when the last recalled passage is still above FIND_SOURCES_RELATED_RELEVANCE the result reports semantic_recall_capped (more relevant documents may exist)
FIND_SOURCES_STRONG_RELEVANCE / FIND_SOURCES_RELATED_RELEVANCE # find_sources tier lines over a document's best fused passage relevance (0..1, not a cosine; related must not exceed strong): at or above the strong line a document is "strong", at or above the related line "related". They apply only to passages whose relevance carries a vector similarity (bounded brute force, ANN); on the FTS-degraded lane, or when the passages carried no vector similarity (query or passage embeddings unavailable; the lane is then reported as `<lane>:lexical_only`), a passage-only document needs two passages to be "related". The defaults (0.74 / 0.44) were calibrated on 2026-10-28 for `text-embedding-v4` on a real 80-paper notebook (6 topics, 360 candidates, model pre-labels reviewed by hand; topic recall 0.904 and strong-tier precision 0.842 within the candidate sets). The strong line meets its precision target only in a narrow band (0.74–0.79), so treat it as tied to this embedding model and corpus shape. On a notebook large enough to require a scale index (not copyable), the semantic leg reads only the ANN index: without a built scale index it falls back to bounded FTS and the documents are found by their metadata only, so build the notebook's scale index (`bash scripts/cli.sh scale build --notebook <id>`). Re-run the calibration after changing the embedding model: `cd backend && PYTHONPATH=. python -m app.eval.find_sources_calibrate candidates|prelabel|sweep ...` (`app/eval/find_sources_calibrate.py`; run it outside CI against a real papers notebook). `prelabel` spends chat-model quota, so without `--confirm` it only prints how many requests it would send and to which model; add `--confirm` only with the operator's explicit consent. The label file holds notebook titles and must live outside the repository
REASONING_FIND_SOURCES_ENABLED / REASONING_PAPER_CARDS_ENABLED / REASONING_EXTRACT_ENABLED # per-action gates (default true) for reasoning Ask's paper tools: find_sources, get_paper_cards and extract_from_sources. A closed gate removes that action from the prompt, schema and allow-list together; the answer_layout field exists only while the cards or the extraction gate is open (both closed leaves the reflect schema byte-for-byte unchanged). They do not touch the MCP tools, which follow the token tier. **Reserved names:** find_sources, get_paper_cards, extract_from_sources and answer_layout are now core reflect names, so a deployment plugin that declares a reflect action with any of them is refused at startup (ExtensionRegistryError) until it is renamed. **Startup warning:** if REASONING_MAX_DOCUMENT_READS x DOCUMENT_OVERVIEW_MAX_ELEMENTS exceeds 2000, read_document's evidence-key band would run into the paper-card band (k9001+); the read_document action is then withdrawn for the deployment and the backend logs a warning at startup — lower one of the two
REASONING_MAX_TOKENS # per-call output ceiling for the reasoning plan (query expansion) and every reflect turn (default 16384; ge=1). This is the shared Legacy deployment budget; workload output, retry and timeout boundaries still apply independently.
PAPER_CARD_ENABLED # deployment switch for lazily generated paper cards (per-paper structured key points cached in source_paper_cards, built only when a caller asks for them; default true). When false, cached cards that are still valid are served and nothing new is generated (those papers report failed/disabled). Cards reuse the paper_metadata model binding — no new workload, no [bindings] change. **It disables cards only:** `extract_from_sources` (agent action and MCP tool) is governed by REASONING_EXTRACT_ENABLED and the MCP tier, not by this switch
PAPER_CARD_INPUT_CHARS # character ceiling of one card's sampled source input (default 16000; ge=2000): 40% opening, then up to 6 tables cut to 1500 characters each, then evenly spread elements including the last one
PAPER_CARD_MAX_WORKERS # per-request card generation pool (default 4; 1..16); the effective pool is the smaller of this and the paper_metadata service's max_concurrency. A transient model failure (full or timed-out queue, open breaker, 429, 5xx, transport timeout) defers a paper instead of recording a failure, so raise that service's max_concurrency if cards are often deferred. **Capacity:** the `paper_metadata` binding now also serves interactive card generation and ad-hoc extraction (Ask and MCP) next to background paper-metadata extraction; interactive calls take priority, but a small max_concurrency makes cards come back `deferred` and extraction slow under load, so review it when enabling these features
PAPER_CARD_RETRY_SECONDS # cooldown after a content failure (malformed reply, a card empty after verification, or a failed metadata extraction) or a model failure deterministic for that document (context exceeded, the provider refusing the content, other non-auth 4xx) of the same content version (default 3600; ge=0); auth/config errors are not cooled down; after 3 consecutive failures the cooldown is fixed at 24 hours. Re-parsing the source (a new element generation) or a new card version starts over
```

Reflect uses only Legacy. The retired `REASONING_REFLECT_*` settings no longer
select a protocol, layout or measurement mode; remove them from deployment
configuration. This cleanup requires no data migration. Fixed reflect instructions
precede the question and candidate summary without adding a layout switch.

The three `REPORT_*_MAX_TOKENS` values are completion ceilings, not total-context
declarations or reserved output. The bound provider/model owns prompt+completion
compatibility; verify that it accepts each configured ceiling together with the
largest prompt that workload receives. Lower these values when a provider has a
smaller output or total-context limit.

**Behavior change (PR-5, no new flag):** each report section's deep-dive retrieval budget now follows the report's own `depth` value (1/2/4/8/16, clamped API-side to `[1, 16]`) mapped onto the same named effort tiers reasoning Ask uses (`overview`/`standard`/`deep`/`thorough`/`exhaustive`) rather than always running at the `standard` budget. Low depths therefore retrieve with a smaller budget than before this change and high depths with a larger one — this is an intentional alignment fix (same tier name, same budget in both Ask and Deep Report), not a regression. Reaching depth 16 (`exhaustive`) additionally activates the outline scratchpad and KG weak-support gap feedback described above inside that section's deep-dive only; see `docs/product-and-api.md`'s "Deep Report outline co-evolution" section for the full contract.

**Two-tier KB (Wave 1+2):** these have no `.env` toggles today.
A notebook's `tier` (`base` | `personal`, default `personal`) is data on the notebook
row, set via the repository's `mark_notebook_base()`; publishing a notebook to `base`
does not make it globally shared — every other notebook must explicitly mount it as a
reference library (persisted in `notebook_bases`, managed through `GET`/`PUT
/api/notebooks/{id}/bases`, discovered via `GET /api/notebooks/{id}/mountable`)
before it joins that notebook's retrieval participant set. Tier-aware federation
leaves retrieval scores unchanged: relevance is the primary ordering key, with `base`
used only as the secondary key for an exact score tie among mounted participants. The
base-wins contradiction rule is a separate answer-synthesis policy that remains active
for evidence from a mounted base notebook.
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
MINERU_RETURN_IMAGES    # retain source-image assets, including PDF/DOCX/PPTX/XLSX and Markdown/data-URI/ZIP images (default true; set 0/false to keep text and captions only)
MINERU_MAX_IMAGE_BYTES  # max size per embedded image (default 5MB; larger images are dropped)
MINERU_MAX_IMAGES_PER_SOURCE # max embedded images per source (default 200)
```

The optional mineru.net cloud path uses `MINERU_API_TOKEN` and `MINERU_API_BASE`.
Advanced cloud controls are `MINERU_CLOUD_MODEL_VERSION`, `MINERU_CLOUD_LANGUAGE`,
`MINERU_CLOUD_FORMULA_ENABLE`, and `MINERU_CLOUD_TABLE_ENABLE` for parsing choices,
plus `MINERU_CLOUD_TIMEOUT_SECONDS` and `MINERU_CLOUD_POLL_INTERVAL_SECONDS` for the
completion deadline and polling cadence. They do not enable cloud fallback when a
self-hosted path is configured. The independent `MINERU_BATCH_*` variables are consumed
only by `scripts/mineru_batch_parse.py`; its complete optional configuration is in
the [script reference](../scripts/README.md#mineru_batch_parsepy--批量-pdf-转-markdown).

**Source element enrichment (`source.element_enricher`, deployment plugin point):**

```text
SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS       # hard wall-clock deadline for the whole point, per parsed source (default 120.0, 0 < x <= 900)
SOURCE_ELEMENT_ENRICHER_MAX_PROPOSALS         # cap on enrichment proposals across every contribution, per parsed source (default 2048, 1..25000)
SOURCE_ELEMENT_ENRICHER_MAX_METADATA_BYTES    # cap on persisted bytes one contribution may add to one source's elements (default 1048576, 1024..16777216)
SOURCE_ELEMENT_ENRICHER_MAX_DESCRIPTION_CHARS # cap on one candidate's description length (default 8192, 1..65536)
```

Reuses `MINERU_MAX_IMAGE_BYTES` above as the per-image read cap for a contribution's asset
reader; there is no separate setting for it. See
[product-and-api.md § Source element enrichment](./product-and-api.md#source-element-enrichment-sourceelement_enricher)
for the mount point, fail-open semantics, and the three additional structural (non-deployment-configurable) bounds.

Parser routing is declared by one backend registry and projected through the authenticated
system-configuration response. It always prefers a configured self-hosted MinerU path,
then permits public cloud only when no self-hosted path is configured, and retains the
built-in parser as the format-specific fallback. The browser receives only capability,
execution-boundary, availability, and fixed reason enums—never endpoints or credentials.

**Selected-source graph rollout:** advanced deployments may add the following group
when preparing or evaluating the selected-source lane. The default shadow example
does not activate user-visible graph enrichment. Before changing to an active mode,
provide a trusted attestation and matching corpus/model pins using the
[evaluation procedure](./operations.md#selected-source-graph-quality-gate-scriptseval_selected_source_graphpy).
An empty pin fails closed for active modes.

```dotenv
SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow
SELECTED_SOURCE_GRAPH_ATTESTATION_PATH="" # trusted evaluation artifact
SELECTED_SOURCE_GRAPH_EXPECTED_CORPUS_SIGNATURE=""
SELECTED_SOURCE_GRAPH_EXPECTED_MODEL_JSON=""
SELECTED_SOURCE_GRAPH_NOTEBOOK_ALLOWLIST="" # comma-separated notebook ids for allowlist
SELECTED_SOURCE_GRAPH_ROLLOUT_PERCENT=0
SELECTED_SOURCE_GRAPH_ENRICHMENT_TOKENS=4000
```

The separate `SOURCE_SUBGRAPH_PPR_ENABLED`, `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED`,
and `SOURCE_PARTITIONED_PPR_ENABLED` switches govern the snapshot producer, companion
publication, and companion reader described in deployment setup above.
`SOURCE_PARTITIONED_PPR_MAX_ITERATIONS` limits sparse passes; the rollout's enrichment
budget cannot evict historical baseline evidence. Evaluate these settings as a group.

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
DIAGNOSTICS_DIR         # live diagnostics: runtime.json / thread dumps (default .local/diagnostics);
                        # must not exist yet or be a 0700 dir owned by the service user
SLOW_REQUEST_MS         # requests slower than this (ms) are flagged SLOW (default 3000)
SILICON_NOTEBOOK_CORS_ORIGINS
```

The sections above include advanced overrides intentionally absent from `.env.example`.
A dedicated reasoning model is selected by binding
`reasoning_agent` to a separate service in TOML, while its guardrails remain
`REASONING_MAX_STEPS`, `REASONING_MAX_SUBQUERIES`, `REASONING_TIMEOUT_SECONDS`,
`REASONING_MAX_RETRIES`, and `REASONING_MAX_TOKENS`. `DEBUG_LOGS_ENABLED` controls the
optional debug log viewer.

`USER_ACTIVITY_RETENTION_DAYS` controls how long the content-minimal user-analysis
projection survives after its notebook is deleted (default 180, accepted range 1–3650).
It never extends the lifetime of the notebook's own answer/source/report bodies,
citations, or reasoning traces. The same window is also how long the global Ask turns
that used the deleted notebook are kept: they stay readable to their owner during it and
are cleaned up afterwards (see "After a participant notebook is deleted" in
product-and-api). The setting is read at backend startup and applies to notebooks deleted
after that process starts; existing retained rows and already-stamped global Ask turns
keep their stamped expiry.

Professional workbook analysis is deployment-controlled by
`SPREADSHEET_ANALYSIS_ENABLED`. Its ingestion rails are
`SPREADSHEET_ANALYSIS_MAX_CELLS`, `SPREADSHEET_ANALYSIS_MAX_SHEETS`, and
`SPREADSHEET_ANALYSIS_MAX_CELL_CHARS`; Ask output/prompt rails are
`SPREADSHEET_ANALYSIS_RESULT_ROWS`, `SPREADSHEET_ANALYSIS_RESULT_CELLS`,
`SPREADSHEET_ANALYSIS_RESULT_BYTES`, `SPREADSHEET_ANALYSIS_PROMPT_ROWS`,
`SPREADSHEET_ANALYSIS_PROMPT_BYTES`, `SPREADSHEET_ANALYSIS_PLANNER_CATALOG_BYTES`,
`SPREADSHEET_ANALYSIS_MAX_TARGETS` (how many analyses one plan may run), and
`SPREADSHEET_ANALYSIS_PLANNER_TIMEOUT_SECONDS`. It reuses the existing
`reasoning_agent` workload and adds no model binding. `ANALYSIS_FAILURE_RETENTION_DAYS`
controls both the issue read boundary and quarantine-copy lifetime. Exact defaults and
validation ranges live in the [Product and API reference](./product-and-api.md#professional-excel-analysis-and-automatic-parsing-issue-archive).

`MODEL_JSON_REPAIR_MODE` applies only to `reasoning_agent`, `ask_answer` and the Deep
Report workloads (`report_outline`, `report_sufficiency`, `report_section`,
`report_summary`).
`off` keeps strict rejection, `shadow` records whether a response would be safely
repairable but still rejects it, and `on` accepts conservative repairs (the default).
It does not complete truncated output or relax schema/type/prose safety checks, and
a repair may restore delimiters only: it never drops, moves or authors text.
Independent of this setting, every chat workload accepts a complete object followed
only by stray closing punctuation (recorded as `model_json_repair` `status: "trimmed"`)
and reads a backslash that starts no JSON escape, such as LaTeX `50\%`, as the
characters written (`status: "escaped"`); neither is a repair. Repair
events are content-free and correlate through the model call's safe `support_id`.

Runtime chat services automatically negotiate strict JSON Schema on first scheduled
use. `MODEL_JSON_SCHEMA_PROBE_TIMEOUT_SECONDS` controls the optional probe budget;
its defaults, rails, identity/lifetime and fallback contract live in
[Model JSON recovery](./product-and-api.md#model-json-recovery-and-stream-liveness).
No model-name allowlist or additional workload binding is needed. Unsupported,
ignored or unverified endpoints keep ordinary JSON; supported services use formal
schemas where the hint can be represented without losing valid fields. Capacity
or label changes reuse the observation; changed endpoint/model/credential identity
or process restart observes again. This advanced budget is not in `.env.example`.

The same-origin `/api/*` rewrite has a finite proxy idle timeout. Ask therefore sends a
content-free blank NDJSON heartbeat every 5 seconds and returns anti-buffering headers;
do not configure an ingress to buffer `application/x-ndjson`. This addresses idle
timeouts. If a CDN/load balancer enforces an absolute request-duration ceiling, raise
that deployment setting above the longest supported Ask run or use the durable job to
reopen the completed conversation after a disconnect.

When the required chat workloads are unbound, summaries and answers fall back to deterministic behavior. Source parsing still completes offline, and KG extraction records a completed `no-llm` run without generating synthetic knowledge.

### Shared Python launch environment

`bash scripts/cli.sh ...` (also `npm run cli -- ...`) prepares the environment before
executing the selected Python command. The backend launches in `dev.sh`, `prod.sh`,
`backend.sh`, and packaged `start.sh` share `scripts/python_env.py` for Python module
paths. This fixes a distinction between reading `.env` as application settings after
Python has started and making its `PYTHONPATH` effective at interpreter startup.

The helper places the absolute repository `backend` first, then merges the process
`PYTHONPATH` and the selected dotenv file's `PYTHONPATH`, removes duplicates and empty
entries, and anchors relative entries to the repository root. Thus an inherited
`PYTHONPATH=backend` no longer discards dotenv plugin paths. It preserves the caller's
working directory and arguments. Dotenv uses python-dotenv syntax/interpolation, never
shell evaluation; ordinary process variables override file values. Existing shell
launchers still load their shell environment before invoking the helper; their prior
non-Python shell-setting precedence is unchanged, and already shell-expanded plugin
paths are retained alongside inherited paths. Use `${NAME}` syntax for dotenv variable
references shared with CLI; bare `$NAME` expansion is a shell-only convention.

The default file is repository `.env`; `SILICON_NOTEBOOK_ENV_FILE` selects another file
(relative overrides are anchored to the repository root), and an empty value disables
dotenv loading. Missing default files are tolerated by the helper; explicit missing or
unreadable files fail with a sanitized error. The existing full-stack `.env` preflight
is unchanged. Interpreter selection for `cli.sh` is explicit `PYTHON_BIN`, repository
`.venv/bin/python`, then `python3` on PATH; an invalid explicit interpreter fails.
Use `python scripts/cli.py ...` to select an interpreter directly.

Launchers never install plugins. Ordinary CLI commands do not start companion services;
service launchers start only the explicit `services` declarations described below. CLI tools
with independent environment contracts bypass dotenv preparation; see
[Operations → Unified command entry](./operations.md#unified-command-entry).
Direct legacy script calls retain their original environment requirements. For a
source-tree plugin, keep `PYTHONPATH=/path/to/plugin/src` in `.env` and use the unified
entry; alternatively install the plugin into the backend interpreter with
`python -m pip install -e /path/to/plugin`. Check configured plugin imports and settings
with `bash scripts/cli.sh extensions check`; this is not a service-readiness check.

### Deployment extensions (EXTENSIONS_CONFIG)

An unset `EXTENSIONS_CONFIG` means zero deployment plugins; the loaded topology is byte-identical to the built-in composition. Set but unreadable or unparseable (missing file, bad TOML, an unknown key, a malformed entry) is a **startup failure** — the process refuses to start, it never degrades. Offline CLI tools (`batch_ingest.py` and friends) load the same plugin topology, so the fix is changing the config, never clearing the variable — clearing it silently swaps in a different discovery/registry composition rather than restoring pre-plugin behavior.

```toml
[extensions."corp.ieee_search"]
bundle = "silicon_notebook_ieee.bundle:BUNDLE"
enabled = true

[extensions."corp.ieee_search".settings]
```

Three rules govern every entry: only a **named**, not `enabled = false`, plugin is ever imported — nothing here scans a directory, reads entry points, or consults a second environment variable; a plugin's own pydantic `settings_model` rejects an unknown key or a type error as a startup failure (core derives the accepted key set from the model rather than trusting `extra="forbid"`; a field carrying an `alias` is accepted **by its alias only**, matching pydantic's own default, unless the model sets `populate_by_name`/`validate_by_name`), so a secret should be referenced (e.g. by an env-var name field, mirroring `model-services.toml`'s `api_key_env`) rather than embedded as a raw value, and no settings value ever reaches a log, an event, or `GET /api/admin/extensions`; and the plugin package installs into the same `PYTHON_BIN` environment as the backend, not a separate interpreter. A plugin's `configure()` must be cheap and side-effect-free — no threads, no network/database connections, no blocking I/O; do that lazily on first use. A plugin capability name uses dot/underscore/hyphen separators only (`:` is reserved for core's own `point:name` capabilities). Editing this TOML — adding or removing an entry, changing `bundle`/`settings`, or flipping `enabled` in the file — always means a process restart; there is no hot reload for the loaded *topology*. That is a separate question from the runtime on/off switch an admin can flip for an already-loaded plugin without a restart — see "Runtime enable/disable" below.

An operator self-checks a deployment's live plugin topology against the paired frontend build with `EXTENSIONS_CONFIG=/etc/silicon/extensions.toml PYTHONPATH=backend python3 scripts/check_deployment_extension_parity.py --frontend-contract frontend/.local/ui-extension-contract.json` (exit `0` parity, `1` drift, `2` usage/environment error). A plugin package should run `python3 scripts/check_ui_vocabulary.py --extra-root <plugin-source-dir>` against its own source for the same Chinese-UI-copy guarantee core enforces on itself. `scripts/generate_ui_extension_contract.py` must be regenerated with `EXTENSIONS_CONFIG` empty — the committed fixture reflects the built-in topology only, never a site's deployment plugins. Step-by-step development, integration and operations procedure: [`docs/deployment-extensions-sop.md`](deployment-extensions-sop.md).

#### Companion-service configuration

Each plugin may add `services` alongside `bundle`, `enabled`, and `settings`.
This is deployment lifecycle configuration, not a value passed to `configure()`.
Only explicitly configured services run; the launcher never discovers arbitrary
scripts. Existing configurations without this table need no changes.

```toml
[extensions."corp.ieee_search".services.worker]
mode = "managed"
cwd = "./plugins/ieee-search"
command = ["./.venv/bin/python", "-m", "corp_ieee_search.server"]
healthcheck_url = "http://127.0.0.1:9100/health"

[extensions."corp.ieee_search".services.worker.env]
SERVICE_MODE = "production"

[extensions."corp.ieee_search".services.worker.env_from]
API_KEY = "CORP_IEEE_API_KEY"
```

`mode` defaults to `managed`; it requires explicit `cwd` and a nonempty argv
`command`. Relative `cwd` resolves against the TOML directory; relative executables
with a path resolve from that working directory. There is no implicit shell or
shell expansion. The child inherits the shared launch environment (including
merged Python paths); `env` adds literal strings and `env_from` maps destination
names to existing source environment names. The two maps cannot overlap. Reference
secrets through `env_from`, not command arguments or literal TOML values. Plugin
bundles still install in the backend interpreter; a companion executable may use
its own separately installed environment.

Declare exactly one `healthcheck_url` (HTTP/HTTPS, success is 2xx; no credentials,
query string, or fragment) or
`healthcheck_command` (argv, success is exit zero). `depends_on` is an optional
array of `plugin_id/service_id` references. Missing, disabled, self, and cyclic
dependencies fail validation. `startup_timeout_seconds`, `shutdown_timeout_seconds`,
and `healthcheck_timeout_seconds` use the defaults and accepted bounds in the
[product/API service rails](./product-and-api.md#deployment-extensions); unknown
keys and malformed values fail validation. Every enabled service is required.

For a service managed by systemd, a container platform, or another operator, set
`mode = "external"`, supply a readiness probe, and omit `command`, `cwd`, `env`,
and `env_from` (these are rejected in external mode). The launcher does not take
ownership of that process. Disabling the whole plugin with `enabled=false` omits
its service definitions. The runtime admin access toggle does not change processes.
See the [runnable example](../examples/extensions/managed-service/README.md) and
[service operations](./operations.md#plugin-companion-services).

**Runtime enable/disable:**

```text
EXTENSION_ADMISSION_REFRESH_SECONDS  # how often a serving process re-reads the admin runtime-toggle table
                                     # to converge its in-memory admission snapshot (default 3.0; range 1..300)
```

This is a second, database-backed switch layered on top of the TOML topology above, not an alternative to it: `EXTENSIONS_CONFIG`'s `enabled` key still means "load or don't load", still takes effect only on restart, and a plugin it never names or marks `enabled = false` never registers regardless of this switch. Once a `trust="deployment"` plugin *is* loaded, an admin can turn it on or off at `/admin/extensions` (`PATCH /api/admin/extensions/{plugin_id}`) without a restart; the current value lives in the `extension_runtime_toggles` table, keyed by plugin id, with no row meaning enabled. That row survives the plugin being dropped from `EXTENSIONS_CONFIG` and later reintroduced — a plugin unloaded and then re-loaded comes back with whatever runtime switch an admin last set for its id, not reset to enabled. The process that performs the admin write republishes its own in-memory snapshot immediately; every other serving process in the deployment (additional replicas) converges on its own `EXTENSION_ADMISSION_REFRESH_SECONDS` tick. A process with zero loaded `trust="deployment"` plugins starts no refresh thread at all — a stock deployment with no deployment plugins still pays the one-time startup prime (the same composition-time read every process already does; the table itself is migrated only by the service's startup or `database migrate`), it just never starts a recurring poll on top of it. Offline CLI and batch processes (`batch_ingest.py` and friends) prime this snapshot once, at startup composition, and never refresh it again during that run — a toggle an admin flips mid-run is not visible to an already-running batch job until its next invocation. See [`docs/deployment-extensions-sop.md`](deployment-extensions-sop.md) for the operational playbook.


## External authentication deployment

Install the optional [W3 example](../examples/extensions/w3-auth/README.md) into the same Python environment as the backend, then copy its disabled TOML outside the checkout. Configure its environment references and set that copy's `enabled=true`; preserve other plugin entries in `EXTENSIONS_CONFIG`. Core has no mandatory W3 package dependency. The example directory can be moved unchanged to an independent plugin repository.

`AUTH_PUBLIC_BASE_URL` is the public API origin; register its `/api/auth/sso/callback` with IDaaS. `AUTH_FRONTEND_BASE_URL` defaults to that origin and receives `/auth/sso/callback`. Both use HTTPS in production and the same hostname so the host-only SameSite browser proof is sent; a same-origin reverse proxy is recommended. Loopback HTTP with different ports is supported for development. Never derive either origin from untrusted request/forwarded headers. An intranet that only serves plain HTTP (for example `http://notebook.corp.example`) must set `AUTH_ALLOW_INSECURE_HTTP=true`; without it, non-loopback HTTP origins are rejected at startup and production reports `https_required`. The opt-in keeps every other origin check (same hostname, bare origin) but sends the authorization code and session token in clear text and drops the `Secure` browser-proof cookie, so enable it only on a trusted network and prefer TLS at the gateway when available. The two origins must also share one scheme (an empty `AUTH_FRONTEND_BASE_URL` counts as the public one): the browser-proof cookie is `Secure` exactly when the public origin is HTTPS, so an HTTPS API behind an HTTP frontend would fail every login; startup rejects that mix when the opt-in is on, and the SSO routes report the configuration as unavailable in every case.

**Enabling and rollback.** External authentication is on exactly while an `auth.provider` plugin is configured and enabled and the callback configuration is valid; there is no stored stage and no administrator step. Enable it by enabling the plugin copy (and the runtime toggle if used); roll back by disabling the plugin, which returns the site to local login and is the emergency path. Whether enabled through the configuration file or the runtime toggle, it requires an active administrator other than the built-in one: the admin runtime toggle refuses to enable such a plugin while the configuration is unusable or no such administrator exists, and a service started with the plugin enabled and no such administrator refuses to start (`no_sso_admin`; disable the plugin, make an administrator, enable again). Before enabling, ask the holders of accounts whose password is shared or known to others to change it, since an account that has never signed in through external login can be linked by anyone who knows its password; when nobody can get in any more, the rollback is `enabled = false` for the plugin entry in the extensions configuration plus a restart (see [operations](./operations.md#external-authentication-enablement-and-rollback)). A user whose provider username equals a site `username` exactly (case-sensitive) signs straight in; anyone else chooses after login between linking an older account by its username and password or creating a new account named after the provider username. The product/API reference owns the exact rules. While it is on, local login, registration and self-service password change are refused and existing local sessions stop working; administrator password reset stays available. Because the match is exact, check before enabling that the employee numbers the provider returns follow the site usernames you expect (for example lowercase `a12345678`), or users will be offered the choice instead of landing on their account. Missing/incompatible/disabled providers and invalid callback configuration are reported as errors and never fall back to local login silently.

Advanced budgets are `AUTH_TRANSACTION_TTL_SECONDS`, `AUTH_SSO_SESSION_SECONDS` and `AUTH_PROVIDER_TIMEOUT_SECONDS`; their exact defaults/ranges are in the paired product/API reference. Never derive a namespace from a plugin package version.

Keep W3-specific fields, TLS/CA details and secret names in the plugin's configuration reference. Reverse proxies, APM and frontend access logs must omit OAuth callback/handoff query values; core strips authentication queries from Uvicorn access records. This does not reconfigure upstream infrastructure.

The production Settings check still requires a non-default `SILICON_NOTEBOOK_ADMIN_PASSWORD`; the built-in `admin` remains a local account that can be used once the plugin is disabled.
