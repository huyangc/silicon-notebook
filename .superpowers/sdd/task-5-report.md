# Task 5 report — embedding/rerank traffic and capacity cleanup

## Result

Task 5 is implemented in two commits:

- `abdfd7fa refactor: bind embedding workloads to model services`
- `47af578d refactor: schedule embedding and rerank workloads`

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

Additional regression coverage:

```text
test_scheduled_model_clients.py + test_embed_provider_casing.py +
test_pool_report.py + test_knowhow_reformat.py + test_knowhow_optimize.py

76 passed in 4.18s
```

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
endpoint kwargs remaining in repository fixture generator/verifier scripts are
also owned by Task 6's required fixture-regeneration step. They are not runtime
fallbacks.
