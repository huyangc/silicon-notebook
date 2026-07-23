# Task 5 report — embedding/rerank traffic and capacity cleanup

## Result

Task 5 is implemented in two commits:

- `abdfd7fa refactor: bind embedding workloads to model services`
- `47af578d refactor: schedule embedding and rerank workloads`

The review-hardening follow-up in this report's commit also removes the last
live consumers of the deleted endpoint Settings properties from KG semantic
search, health, relation maintenance, and operational scripts.

Product embedding and rerank traffic now resolves an exact workload from the
system provider. Physical-service `max_concurrency` is the only provider
throughput authority; producer pools use `parallelism(workload_id)` only as a
submission hint, while the shared per-service scheduler enforces the actual
cross-user/cross-feature peak.

## TDD evidence

Initial RED exposed the expected retired `EMBED_*` fixture failures in
`test_batch_ingest.py`: `SystemModelServiceRegistry` rejected the legacy
activation variables before repositories could be built. Fixtures were
migrated to explicit `RecordingModelProvider` workload bindings; no legacy env
compatibility was restored.

Review RED added direct regressions for KG semantic routing, sanitized health,
safe KG re-embedding, replay preflight, offline snapshot verification, the
batch-ingest error contract, and relation backfill. The first focused run was
`8 failed, 1 error`: every failure was the intended deleted-Settings consumer
or missing provider injection. After migration the same focused set was
`9 passed`.

Focused GREEN:

```text
PYTHONPATH=backend python -m pytest -q -n0 \
  backend/tests/test_embed_concurrency.py backend/tests/test_kg_object_embed_concurrency.py \
  backend/tests/test_source_embedding_service.py backend/tests/test_embedding.py \
  backend/tests/test_rerank_client.py backend/tests/test_memory_retrieval.py \
  backend/tests/test_memory_service.py backend/tests/test_knowhow_projection.py \
  backend/tests/test_batch_ingest.py backend/tests/test_kg_scheduler.py \
  backend/tests/test_parallel_extraction_wiring.py backend/tests/test_backfill_knowhow_md.py \
  backend/tests/test_env_aliases.py

289 passed in 19.53s
```

The post-review rerun remained green (`289 passed in 24.71s`).

Additional regression coverage:

```text
test_scheduled_model_clients.py + test_embed_provider_casing.py +
test_pool_report.py + test_knowhow_reformat.py + test_knowhow_optimize.py

76 passed in 4.18s
```

The post-review rerun remained green (`76 passed in 5.11s`). All changed-path
regressions, including the complete snapshot-verifier and relation-embedding
files, passed together (`49 passed in 8.20s`).

The final offline-benchmark review added one more strict boundary test. It was
RED (`1 failed`) because an ambient checked-in example registry bound model
workloads in the supposedly offline benchmark, then GREEN (`1 passed`) after
the benchmark composition root pinned an explicit empty provider. The Task 5
suite plus the real scheduled-client regressions subsequently passed together
(`311 passed in 20.96s`).

A fresh controller run then exposed stale Task 2/3 test construction after the
Task 5 Settings cleanup (`26 failed, 125 passed`). Raw protocol and cache tests
now supply service URL/key/model explicitly, the former global-fallback test
instead proves retired environment endpoints are ignored, and the injected
repository provider uses the complete test-provider interface including
`parallelism("kg_extract")`. The exact controller command is GREEN
(`151 passed in 2.23s`); the Task 5 suite plus the offline-benchmark regression
also remained GREEN (`290 passed in 20.14s`). No production compatibility
fallback was added.

The real-runtime three-source peak test binds multiple embedding workloads to
one physical service and observes a raw upstream peak exactly equal to that
service's configured maximum (`2`), proving the service scheduler—not each
producer pool—is authoritative.

## Workload mapping

| Operation | Workload |
|---|---|
| Retrieval, graph, knowledge search and Memory recall query vectors | `retrieval_query_embedding` |
| Source elements | `source_element_embedding` |
| Source chunks | `chunk_embedding` |
| Knowhow chunks | `knowhow_embedding` |
| KG objects | `knowledge_object_embedding` |
| KG relations | `relation_embedding` |
| Memory persistence | `memory_embedding` |
| Retrieval ordering | `retrieval_rerank` |

Memory persistence and Memory recall use distinct adapters. Query paths never
reuse a source/document embedding workload. Scheduled rerank splits raw
protocol batches, schedules each batch independently, and merges scores
locally.

## Capacity and configuration cleanup

Removed:

- `model_concurrency.py`, its gates/executors/wrappers, and its test suite.
- Batch `_batch_concurrency_scope`, `--llm-conc`, and `--embed-conc`.
- Settings capacity fields `kg_extract_workers`, `embed_concurrency`, and
  `kg_ask_reserve`.
- Legacy endpoint Settings fields/properties for chat variants, embedding and
  rerank.
- `model_config.py`, per-user rerank resolution tests, and old KG role-fallback
  tests.
- Script-level `EMBED_CONCURRENCY` overrides.

Kept intentionally:

- Batch `--workers` and `kg_job_concurrency` for document/business
  orchestration only.
- Timeout, retry, token, dimension/batch, feature and domain settings.

Raw protocol constructors receive explicit service URL/key/model/protocol and
connection capacity. `make_embedder()` has no Settings endpoint fallback.

KG semantic search now asks the injected provider for exactly
`retrieval_query_embedding`; a configured regression proves it executes the
semantic ANN path without the generic retrieval embedder. Only typed provider
failures are treated as an optional-overlay failure. Programming/index errors
are no longer swallowed by a broad exception.

Relation maintenance checks the runtime-composed provider's
`configured("relation_embedding")` port and resolves the exact relation
adapter. It does not inspect Settings or implicitly reach back through the
repository facade.

KG window planning and the KG producer pool derive their width from
`parallelism("kg_extract")`. Knowhow Markdown `--use-llm` requests the system
`knowhow_reformat` workload and never resolves a notebook owner's endpoint.

## CLI verification

```text
PYTHONPATH=backend python scripts/batch_ingest.py --help
```

The help contains `--workers`; it contains neither `--llm-conc` nor
`--embed-conc`. User-facing help identifies missing system workload bindings
rather than retired `EMBED_*` configuration.

`reembed_kg` now proves both `knowledge_object_embedding` and
`relation_embedding` are bound before deleting either vector table.
`replay_retrieval` likewise requires `retrieval_query_embedding` before doing
any work. The repository snapshot verifier constructs Settings with only an
empty `MODEL_SERVICES_CONFIG`, validates the full registry workload catalog is
unbound, and pins an empty registry while the offline repository is alive so
hostile process/`.env` legacy variables cannot activate a provider. The
fixture generator no longer passes deleted Settings fields.

The SQLite write benchmark is also offline by construction: it passes
`model_services_config=""` and an explicit empty registry/provider rather than
building from ambient `Settings()`. A direct probe with
`MODEL_SERVICES_CONFIG=model-services.example.toml` and all example credential
variables populated stored `4/4` rows with zero errors and no bound workload or
model-network path.

## Audit and Task 6 handoff

Production imports of `app.services.model_config` and
`app.services.model_concurrency` are zero. The old modules are deleted.

`legacy_model_status_types.py` is deliberately short-lived until Task 6
replaces the old per-user status table. It contains only an inert status DTO
and fingerprint helper: `configured` is always false, and it performs no URL,
credential, provider, client, registry or fallback resolution. Identity's
temporary compatibility method likewise returns only this unconfigured
description, so no product traffic can read legacy user/system endpoints.

Remaining repository-test imports of the deleted modules are confined to the
Task 6 deletion/refactor list (`test_model_status_store.py`,
`test_model_status_resolution.py`, `test_model_status_service.py`,
`test_model_config_resolve.py`, and `test_user_llm_client_resolve.py`). Legacy
status persistence remains owned by Task 6; this follow-up intentionally did
not migrate it. A production/script scan found zero live reads or constructor
kwargs for the deleted endpoint Settings fields and zero stale `KG_LLM_*` /
`EMBED_*` operator guidance.
