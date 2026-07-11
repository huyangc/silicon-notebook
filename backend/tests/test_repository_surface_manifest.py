from __future__ import annotations

import ast
from collections import defaultdict
import importlib.util
import inspect
import json
from pathlib import Path
import re
import typing

import pytest

from app.services import repository, sqlite_repository
from app.services.sqlite_repository import SQLiteRepository
from app.repositories.ownership_manifest import (
    OWNER_BY_MEMBER,
    validate_ownership_manifest,
)
from tests.test_repository_facade_contract import (
    MODULE_SURFACE_OWNER_EXCEPTIONS,
    NON_CALLABLE_INSTANCE_SURFACE,
    OWNER_CONTRACT_EXCEPTIONS,
    facade_contract_subject_names,
    facade_delegate_evidence,
    manifest_delegate_mismatches,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"

REQUIRED_GENERATOR_CALLABLES = {
    "collect_facade_surface",
    "generate_v9_fixture",
    "normalized_repository_snapshot",
    "generate_ask_goldens",
    "generate_api_contract",
    "main",
}
REQUIRED_MEMBER_FIELDS = {
    "kind",
    "signature",
    "consumers",
    "owner",
    "patch_targets",
}

COMPATIBILITY_EXPORTS = {
    "KnowledgeGraphTooLargeError": "app.services.sqlite_repository",
    "NotebookRepository": "app.services.repository",
    "RetrievedKnowledge": "app.services.sqlite_repository",
    "SCHEMA_VERSION": "app.services.sqlite_repository",
    "SQLiteRepository": "app.services.sqlite_repository",
    "USABLE_STATUSES": "app.services.sqlite_repository",
    "UploadedSourceFile": "app.services.repository",
    "_ASK_MODEL_ERRORS": "app.services.sqlite_repository",
    "_COPY_CHUNK": "app.services.sqlite_repository",
    "_REQUEST_USER": "app.services.sqlite_repository",
    "_concept_desc_sig": "app.services.sqlite_repository",
    "_fast_loads": "app.services.sqlite_repository",
    "_new_id": "app.services.sqlite_repository",
    "_now": "app.services.sqlite_repository",
    "_remap_json_ids": "app.services.sqlite_repository",
    "KNOWLEDGE_STATUSES": "app.services.sqlite_repository",
    "parse_source_file": "app.services.sqlite_repository",
    "reset_request_user": "app.services.sqlite_repository",
    "set_request_user": "app.services.sqlite_repository",
}

EXPLICIT_OWNERS = {
    "KnowledgeGraphTooLargeError": "KnowledgeLifecycleService",
    "NotebookRepository": "RepositoryPorts",
    "UploadedSourceFile": "RepositoryPorts",
    "_REQUEST_USER": "RequestContext",
    "reset_request_user": "RequestContext",
    "set_request_user": "RequestContext",
    "approve_promotion": "KnowledgeGovernanceService",
    "list_promotion_queue": "KnowledgeGovernanceService",
    "propose_promotion": "KnowledgeGovernanceService",
    "reject_promotion": "KnowledgeGovernanceService",
}

# Task 2 moves these imports to the typed ports while retaining the old
# compatibility modules elsewhere.  Only these exact import sites are allowed
# to differ from the frozen master consumer manifest.
TASK2_ALLOWED_IMPORTS = {
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "set_request_user"),
    ("backend/app/api/deps.py", 11, "app.services.sqlite_repository", "reset_request_user"),
    ("backend/app/eval/speed.py", 98, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_trackA_eval_connect.py", 19, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_trackA_eval_connect.py", 25, "app.services.repository", "NotebookRepository"),
    ("backend/tests/test_repository_ports.py", 5, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_ALLOWED_IMPORTS = {
    ("backend/app/api/deps.py", 12, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/services/sqlite_repository.py", 112, "app.services.repository", "UploadedSourceFile"),
    ("backend/tests/test_sqlite_database_component.py", 6, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/services/sqlite_repository.py", 113, "app.services.repository", "UploadedSourceFile"),
}
TASK7_ALLOWED_IMPORTS = {
    ("backend/app/api/routes.py", 19, "app.services.sqlite_repository", "KnowledgeGraphTooLargeError"),
    ("backend/app/api/routes.py", 21, "app.services.sqlite_repository", "KnowledgeGraphTooLargeError"),
    ("backend/app/api/routes.py", 91, "app.services.repository", "NotebookRepository"),
    ("backend/app/api/routes.py", 91, "app.services.repository", "UploadedSourceFile"),
    ("backend/app/api/routes.py", 93, "app.services.repository", "NotebookRepository"),
    ("backend/app/api/routes.py", 93, "app.services.repository", "UploadedSourceFile"),
    ("backend/tests/test_architecture_module_boundaries.py", 4, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_architecture_module_boundaries.py", 6, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_identity_store_component.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_query_store_component.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_model_provider_runtime.py", 8, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK8_ALLOWED_IMPORTS = {
    ("backend/tests/test_notebook_store_component.py", 8, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_notebook_summary_query.py", 8, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK9_ALLOWED_IMPORTS: set[tuple[str, int, str, str]] = set()
# Task 10 adds two store imports above the facade's UploadedSourceFile import,
# shifting the frozen (Task 4) compatibility import site down.
TASK10_ALLOWED_IMPORTS = {
    ("backend/app/services/sqlite_repository.py", 114, "app.services.repository", "UploadedSourceFile"),
}
# Task 11 swaps two facade import lines in place (notebook_catalog loses
# _delete_source_file, the ChunkWrite import becomes the source_files
# safe_filename import), so no frozen compatibility import site shifts.
TASK11_ALLOWED_IMPORTS: set[tuple[str, int, str, str]] = set()
# Task 12 inserts the SourcePipelineHooks import above the facade's frozen
# UploadedSourceFile compatibility import (shifting it to line 115) and the
# two new ingestion test files + the event-logging append import the
# compatibility exports at fresh sites.
TASK12_ALLOWED_IMPORTS = {
    ("backend/app/services/sqlite_repository.py", 115, "app.services.repository", "UploadedSourceFile"),
    ("backend/tests/test_event_logging.py", 176, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_source_ingestion_service.py", 28, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_source_ingestion_service.py", 28, "app.services.sqlite_repository", "_now"),
    ("backend/tests/test_source_ingestion_failure_boundaries.py", 21, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_source_ingestion_failure_boundaries.py", 21, "app.services.sqlite_repository", "_now"),
}
# Task 13: the three new knowledge-domain test files import the compatibility
# exports at fresh sites (the facade's own frozen import lines are untouched).
TASK13_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowledge_store_contract.py", 13, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_knowledge_store_contract.py", 13, "app.services.sqlite_repository", "_now"),
    ("backend/tests/test_schema_registry_service.py", 18, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_schema_registry_service.py", 18, "app.services.sqlite_repository", "_now"),
    ("backend/tests/test_repository_module_boundaries.py", 99, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 14: the two new KG-mutation phase test files import the compatibility
# export at fresh sites (the facade's own frozen import lines are untouched).
TASK14_ALLOWED_IMPORTS = {
    ("backend/tests/test_kg_mutation_phase_matrix.py", 54, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_kg_mutation_failure_boundaries.py", 33, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 15: the new knowledge-lifecycle delegation test file imports the
# compatibility export at a fresh site (the facade's own frozen import lines
# are untouched).
TASK15_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowledge_lifecycle_delegation.py", 20, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 16: the new knowledge-governance delegation test file imports the
# compatibility export at a fresh site (the facade's own frozen import lines
# are untouched).
TASK16_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowledge_governance_delegation.py", 21, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 17: the new retrieval-snapshot runtime test file imports the
# compatibility export at a fresh site (the facade's own frozen import lines
# are untouched).
TASK17_ALLOWED_IMPORTS = {
    ("backend/tests/test_retrieval_snapshot_cache_runtime.py", 18, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 18: the new scale-artifact catalog test file imports the compatibility
# export at a fresh site (the facade's own frozen import lines are untouched;
# the artifact-compatibility test file consumes only the filesystem store).
TASK18_ALLOWED_IMPORTS = {
    ("backend/tests/test_scale_artifact_catalog.py", 22, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 19: the new scale-builder failure-boundary suite imports the frozen
# compatibility facade to assemble an end-to-end runtime fixture.
TASK19_ALLOWED_IMPORTS = {
    ("backend/tests/test_scale_builder_failure_boundaries.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/app/services/sqlite_repository.py", 116, "app.services.repository", "UploadedSourceFile"),
}
# Task 26: the two consolidation contract suites (facade surface + runtime
# identity) import the compatibility facade at fresh sites.
TASK26_ALLOWED_IMPORTS = {
    ("backend/tests/test_repository_facade_contract.py", 22, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_repository_runtime_identity.py", 13, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_repository_surface_manifest.py", 15, "app.services.sqlite_repository", "SQLiteRepository"),
}
# Task 27: the CLI composition roots keep their concrete-facade import while
# their request-context imports move to the canonical app.core.request_context
# home (dropping names shifts the surviving compatibility import sites); the
# new static caller suite imports the facade at a fresh site.
TASK27_ALLOWED_IMPORTS = {
    ("backend/app/services/batch_ingest.py", 28, "app.services.repository", "UploadedSourceFile"),
    ("backend/app/services/batch_ingest.py", 29, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/smoke_backend.py", 22, "app.services.repository", "UploadedSourceFile"),
    ("scripts/smoke_backend.py", 24, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/kg_product_smoke.py", 16, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/backfill_kg_embeddings.py", 21, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/replay_retrieval.py", 40, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_repository_callers_static.py", 666, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_ALLOWED_MEMBER_FILES = {
    ("backend/app/api/deps.py", name)
    for name in {"set_request_user", "reset_request_user", "user_can_access_notebook", "user_can_read_notebook"}
} | {
    ("backend/tests/test_repository_context.py", name)
    for name in {"_REQUEST_USER", "set_request_user", "reset_request_user"}
} | {
    ("backend/tests/test_repository_runtime.py", name)
    for name in {"SQLiteRepository", "settings", "_now", "_runtime"}
} | {
    ("backend/app/services/background_jobs.py", "_REQUEST_USER"),
    ("backend/app/services/sqlite_repository.py", "_runtime"),
}

TASK2_ALLOWED_CONSUMERS = {
    ("upload_sources", "backend/app/eval/speed.py:80"),
    ("parse_source", "backend/app/eval/speed.py:82"),
    ("delete_notebook", "backend/app/eval/speed.py:85"),
    ("delete_notebook", "backend/app/eval/speed.py:90"),
    ("extract_source", "backend/app/eval/speed.py:114"),
    ("user_can_access_notebook", "backend/app/api/deps.py:58"),
    ("user_can_access_notebook", "backend/app/api/deps.py:70"),
    ("user_can_read_notebook", "backend/app/api/deps.py:70"),
    ("user_can_read_notebook", "backend/app/api/deps.py:82"),
    ("NotebookRepository", "backend/app/api/deps.py:10"),
    ("_run_extraction", "backend/app/eval/speed.py:109"),
}
TASK7_ALLOWED_CONSUMERS = {
    ("create_user", "backend/app/api/auth_routes.py:17"),
    ("create_session", "backend/app/api/auth_routes.py:21"),
    ("authenticate_user", "backend/app/api/auth_routes.py:27"),
    ("create_session", "backend/app/api/auth_routes.py:30"),
    ("delete_session", "backend/app/api/auth_routes.py:41"),
    ("list_user_usage", "backend/app/api/routes.py:1378"),
    ("list_user_notebooks", "backend/app/api/routes.py:1388"),
}
# Task 8 moves the notebook-catalog routes onto the typed
# notebook_catalog_repository() accessor; these are the frozen repository()
# call sites they replace.
TASK8_ALLOWED_CONSUMERS = {
    ("list_notebook_templates", "backend/app/api/routes.py:237"),
    ("list_notebooks", "backend/app/api/routes.py:242"),
    ("create_notebook", "backend/app/api/routes.py:253"),
    ("notebook_analytics", "backend/app/api/routes.py:267"),
    ("update_notebook", "backend/app/api/routes.py:278"),
    ("delete_notebook", "backend/app/api/routes.py:286"),
    ("search_notebook", "backend/app/api/routes.py:544"),
    ("mark_notebook_base", "backend/app/api/routes.py:785"),
    ("set_notebook_personal", "backend/app/api/routes.py:787"),
}
# Task 9 moves the sharing/access routes onto the typed
# notebook_sharing_repository()/notebook_access_repository() accessors; these
# are the frozen repository() call sites they replace.
TASK9_ALLOWED_CONSUMERS = {
    ("shared_by_me", "backend/app/api/routes.py:248"),
    ("user_can_read_source", "backend/app/api/routes.py:367"),
    ("source_owner", "backend/app/api/routes.py:377"),
    ("user_can_read_source", "backend/app/api/routes.py:387"),
    ("source_owner", "backend/app/api/routes.py:397"),
    ("conversation_owner", "backend/app/api/routes.py:704"),
    ("conversation_owner", "backend/app/api/routes.py:714"),
    ("conversation_owner", "backend/app/api/routes.py:725"),
    ("share_notebook", "backend/app/api/routes.py:797"),
    ("unshare_notebook", "backend/app/api/routes.py:806"),
    ("find_notebook_by_share_token", "backend/app/api/routes.py:813"),
    ("shared_preview", "backend/app/api/routes.py:816"),
    ("find_notebook_by_share_token", "backend/app/api/routes.py:822"),
    ("notebook_copy_stats", "backend/app/api/routes.py:825"),
    ("copy_notebook", "backend/app/api/routes.py:827"),
    ("find_notebook_by_share_token", "backend/app/api/routes.py:834"),
    ("notebook_copy_stats", "backend/app/api/routes.py:837"),
    ("join_shared", "backend/app/api/routes.py:839"),
    ("leave_notebook", "backend/app/api/routes.py:845"),
    ("user_can_read_answer", "backend/app/api/routes.py:1051"),
}
# Task 12 moves the source ingestion routes onto the typed
# source_repository() accessor; these are the frozen repository() call sites
# they replace.
TASK12_ALLOWED_CONSUMERS = {
    ("list_sources_page", "backend/app/api/routes.py:298"),
    ("import_sources", "backend/app/api/routes.py:309"),
    ("get_source", "backend/app/api/routes.py:370"),
    ("parse_source", "backend/app/api/routes.py:380"),
    ("source_elements", "backend/app/api/routes.py:390"),
    ("delete_source", "backend/app/api/routes.py:400"),
}
TASK2_ALLOWED_MEMBER_FILES = {
    ("backend/app/api/deps.py", name)
    for name in {
        "resolve_session", "current_user", "set_request_user", "reset_request_user",
        "user_can_access_notebook", "user_can_read_notebook", "llm_client", "SQLiteRepository",
    }
} | {
    ("backend/app/eval/speed.py", name)
    for name in {"upload_sources", "parse_source", "extract_source", "delete_notebook", "_run_extraction", "create_notebook", "llm_client", "SQLiteRepository"}
} | {
    ("backend/app/services/repository.py", name)
    for name in {"UploadedSourceFile", "NotebookRepository"}
}
TASK3_ALLOWED_NEW_MEMBERS = {"load_notebook_scale_facts", "start_ask_stream"}
# master v10(rebuild 可续跑轨道, schema 9→10)在冻结之后新增的 facade 成员:
# kg_rebuild_checkpoint 迁移 + 4 个运行时 ckpt helper + 节点向量增量 flush。
# 均为上游合法演进、非本重构产物;Gate 5(KG 域)搬迁时随 rebuild_unified_kg 移动。
MASTER_V10_ALLOWED_NEW_MEMBERS = {
    "_migration_10",
    "_rebuild_ckpt_gc",
    "_rebuild_ckpt_clear",
    "_rebuild_ckpt_load",
    "_rebuild_ckpt_put",
    "_flush_object_vectors",
}
TASK7_ALLOWED_NEW_MEMBERS = {"pending_actions_projection_rows"}
# Task 12 new facade members: the fresh per-call hooks builder plus the
# TEMPORARY KG/catalog SQL callbacks the ingestion service calls back through
# (Task 13/15 move them into KnowledgeStore / the notebook & source stores).
TASK12_ALLOWED_NEW_MEMBERS = {
    "_source_pipeline_hooks",
    "_begin_extraction_run",
    "_finish_extraction_run",
    "_notebook_tier",
    "_notebook_meta_row",
    "_notebook_meta_sources",
    "_apply_notebook_meta",
}
TASK4_ALLOWED_PATCHES = {
    ("backend/tests/test_repository_runtime.py", 19, "_now", "sqlite_repository"),
}
TASK5_ALLOWED_PATCHES = {
    ("backend/tests/test_sqlite_database_component.py", 0, "_write", "repo"),
}
# master v10 新成员上的测试探针(成员本身经 MASTER_V10_ALLOWED_NEW_MEMBERS 豁免,
# fixture 冻结成员集不扩)。Gate 5(Task 13)兑现了"搬迁时 patch 座随成员迁到组件
# seam"的约定:test_rebuild_checkpoint 的两个 _rebuild_ckpt_put 座已迁到
# repo._runtime.unified_kg.checkpoint_put(store seam,静态扫描不再计为 facade
# patch),故此处只剩 _flush_object_vectors(Task 11 保留的 facade 座)。
MASTER_V10_ALLOWED_PATCHES = {
    ("backend/tests/test_node_embed_incremental.py", 56, "_flush_object_vectors", "repo"),
    ("backend/tests/test_node_embed_incremental.py", 63, "_flush_object_vectors", "repo"),
}
# Task 13: the schema-registry characterization suite swaps the llm client
# through the frozen mutable llm_client property (production-compatible seam —
# the setter writes the runtime model provider the SchemaRegistryService
# consumes).
TASK13_ALLOWED_PATCHES = {
    ("backend/tests/test_schema_registry_service.py", 157, "llm_client", "repo"),
    ("backend/tests/test_schema_registry_service.py", 177, "llm_client", "repo"),
    ("backend/tests/test_schema_registry_service.py", 180, "llm_client", "repo"),
    ("backend/tests/test_schema_registry_service.py", 187, "llm_client", "repo"),
}
# Task 8 migrates the list_notebooks query-count spy from the facade _connect
# patch seat onto the runtime SqliteDatabase.connect component seam.
TASK8_ALLOWED_PATCHES = {
    ("backend/tests/test_notebook_counts_batched.py", 115, "_connect", "repo"),
}
# Task 9 late-binding proofs: the copy service must observe post-construction
# patches of the module seams (_new_id / _COPY_CHUNK) and the facade
# _insert_row seat (failure injection for compensation coverage).
TASK9_ALLOWED_PATCHES = {
    ("backend/tests/test_notebook_copy_service.py", 115, "_new_id", "sqlite_repository"),
    ("backend/tests/test_notebook_copy_service.py", 140, "_COPY_CHUNK", "sqlite_repository"),
    ("backend/tests/test_notebook_copy_service.py", 168, "_insert_row", "repo"),
    ("backend/tests/test_notebook_copy_service.py", 189, "_COPY_CHUNK", "sqlite_repository"),
    ("backend/tests/test_notebook_copy_service.py", 199, "_insert_row", "repo"),
}
# Task 10: the C5 batched-lookup spy migrates from the facade _connect patch
# seat onto the runtime SqliteDatabase.connect component seam (old frozen site
# exempted); the embedding-store component tests probe the late-bound facade
# _write seat (transaction counting + late-binding failure injection).
TASK10_ALLOWED_PATCHES = {
    ("backend/tests/test_sources_page_batched.py", 183, "_connect", "repo"),
    ("backend/tests/test_embedding_store_component.py", 59, "_write", "repo"),
    ("backend/tests/test_embedding_store_component.py", 135, "_write", "repo"),
}
# Task 11 late-binding proofs: the embed/chunk services must observe
# post-construction patches of the facade _flush_object_vectors seat (spy +
# propagating failure injection — the incremental-commit/resume contract),
# the facade _mark_unified_kg_dirty seat, and the module _new_id seam that
# mints ck-* chunk ids.
TASK11_ALLOWED_PATCHES = {
    ("backend/tests/test_source_chunking_service.py", 103, "_new_id", "sqlite_repository"),
    ("backend/tests/test_source_chunking_service.py", 119, "_mark_unified_kg_dirty", "repo"),
    ("backend/tests/test_source_embedding_service.py", 181, "_flush_object_vectors", "repo"),
    ("backend/tests/test_source_embedding_service.py", 202, "_flush_object_vectors", "repo"),
}
# Task 12 migrates every _run_extraction / _set_source_status /
# _source_raw_text facade patch seat onto the canonical
# SourceIngestionService / SourceFileStore components (frozen sites below
# stop appearing in the static scan); the two new ingestion test files pin
# the fresh-hooks late-binding proof on the facade _run_extraction seat and
# replay parse_source_file through the module compatibility namespace.
TASK12_ALLOWED_PATCHES = {
    # migrated frozen seats (facade wrappers are no longer test patch targets)
    ("backend/tests/test_batch_ingest.py", 219, "_run_extraction", "SQLiteRepository"),
    ("backend/tests/test_batch_ingest.py", 247, "_run_extraction", "repo"),
    ("backend/tests/test_batch_ingest.py", 248, "_set_source_status", "repo"),
    ("backend/tests/test_batch_ingest.py", 287, "_run_extraction", "repo"),
    ("backend/tests/test_batch_ingest.py", 288, "_set_source_status", "repo"),
    ("backend/tests/test_batch_ingest.py", 310, "_run_extraction", "repo"),
    ("backend/tests/test_batch_ingest.py", 311, "_set_source_status", "repo"),
    ("backend/tests/test_batch_ingest.py", 462, "_run_extraction", "repo"),
    ("backend/tests/test_batch_ingest.py", 489, "_run_extraction", "repo"),
    ("backend/tests/test_batch_ingest.py", 1211, "_run_extraction", "repo"),
    ("backend/tests/test_chunk_embed.py", 99, "_run_extraction", "repo"),
    ("backend/tests/test_kg_llm_client.py", 53, "_source_raw_text", "repo"),
    ("backend/tests/test_kg_relink_repository.py", 188, "_run_extraction", "repo"),
    ("backend/tests/test_kg_repository.py", 380, "_run_extraction", "repo"),
    ("backend/tests/test_p4_kg_shrink.py", 82, "_run_extraction", "repo"),
    ("backend/tests/test_p4_kg_shrink.py", 96, "_run_extraction", "repo"),
    ("backend/tests/test_resolve_notebook_conflicts.py", 309, "_run_extraction", "repo"),
    ("backend/tests/test_resolve_notebook_conflicts.py", 331, "_run_extraction", "repo"),
    # fresh Task-12 probes (fresh-hooks proof + module parse seam replay)
    ("backend/tests/test_source_ingestion_failure_boundaries.py", 80, "parse_source_file", "facade_mod"),
    ("backend/tests/test_source_ingestion_service.py", 155, "parse_source_file", "facade_mod"),
    ("backend/tests/test_source_ingestion_service.py", 253, "parse_source_file", "facade_mod"),
    ("backend/tests/test_source_ingestion_service.py", 260, "_run_extraction", "repo"),
    ("backend/tests/test_source_ingestion_service.py", 332, "parse_source_file", "facade_mod"),
}
TASK5_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {"db_path", "_write_lock", "_connect", "_write"}
} | {
    ("backend/tests/test_sqlite_database_component.py", name)
    for name in {"SQLiteRepository", "_write_lock", "_runtime", "db_path"}
}
TASK6_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_migrate", "_migrate_legacy", "_add_column_if_missing",
        "_migration_1", "_migration_2", "_migration_3", "_migration_4",
        "_migration_5", "_migration_6", "_migration_7", "_migration_8",
        "_migration_9", "_recover_interrupted_jobs", "_recover_interrupted_jobs_legacy",
        "_seed", "_seed_legacy", "_migrator",
    }
}
TASK7_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/model_provider.py", name)
    for name in {
        "settings", "current_user", "resolve_model_config", "llm_client",
        "reasoning_llm_client", "rewrite_llm_client", "kg_llm_client",
        "rerank_client", "_system_llm_for", "_user_llm_cached",
        "_llm_for_role", "_system_llm_client", "_user_llm_clients",
        "_reasoning_llm_client", "_rewrite_llm_client", "_kg_llm_client",
        "_system_rerank_client", "_user_rerank_clients",
    }
} | {
    ("backend/app/services/sqlite_identity.py", name)
    for name in {
        "current_user", "get_user_model_settings", "set_user_model_settings",
        "_user_profile",
        "resolve_model_config", "create_user", "authenticate_user",
        "create_session", "resolve_session", "delete_session",
        "list_user_usage", "list_user_notebooks", "_user_profile", "_connect",
        "_write", "settings", "_user_model_cfg_cache",
    }
} | {
    ("backend/app/api/deps.py", "_runtime"),
    ("backend/tests/test_model_provider_runtime.py", "_ASK_MODEL_ERRORS"),
} | {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "current_user", "_user_profile", "get_user_model_settings", "set_user_model_settings",
        "resolve_model_config", "create_user", "authenticate_user",
        "create_session", "resolve_session", "delete_session",
        "list_user_usage", "list_user_notebooks", "notebook_analytics",
        "search_notebook", "load_notebook_scale_facts", "_note_model_error",
        "_system_llm_for", "_user_llm_cached", "_llm_for_role",
        "llm_client", "reasoning_llm_client", "rewrite_llm_client",
        "kg_llm_client", "rerank_client", "_system_llm_client",
        "_reasoning_llm_client", "_rewrite_llm_client", "_kg_llm_client",
        "_system_rerank_client", "_user_llm_clients", "_user_rerank_clients",
    }
} | {
    ("backend/app/services/repository_runtime.py", name)
    for name in {"settings", "_runtime"}
} | {
    ("backend/tests/test_identity_store_component.py", name)
    for name in {"SQLiteRepository", "current_user", "_runtime"}
} | {
    ("backend/tests/test_query_store_component.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "search_notebook",
        "load_notebook_scale_facts", "_runtime",
    }
} | {
    ("backend/tests/test_model_provider_runtime.py", name)
    for name in {
        "SQLiteRepository", "llm_client", "rerank_client",
        "_note_model_error", "_runtime", "event_log",
        "_user_model_cfg_cache", "_user_llm_clients", "_user_rerank_clients",
    }
} | {
    ("backend/tests/test_sources_pagination.py", name)
    for name in {
        "create_notebook", "_write", "search_notebook",
    }
}
# Task 8: notebook rows/summary projection/catalog orchestration move to the
# NotebookStore + NotebookSummaryQuery + NotebookCatalogService components; the
# facade keeps frozen-signature delegates, so its own internal call sites for
# the moved projection helpers disappear.  The two component test files and the
# migrated query-count spy consume the facade/new seams at fresh sites.
TASK8_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_NOTEBOOK_COUNT_TYPES", "_base_notebook_info",
        "_count_pending_kg_sources", "_knowledge_type_counts",
        "_notebook_from_row",
    }
} | {
    ("backend/tests/test_notebook_counts_batched.py", name)
    for name in {"_connect", "_runtime", "list_notebooks"}
} | {
    ("backend/tests/test_notebook_store_component.py", name)
    for name in {"SQLiteRepository", "create_notebook", "_runtime"}
} | {
    ("backend/tests/test_notebook_summary_query.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "current_user", "get_notebook",
        "list_notebooks", "mark_notebook_base", "notebook_analytics",
        "search_notebook", "_kg_building", "_runtime",
    }
}
# Task 9: the sharing/deep-copy mixin body is recomposed into SharingStore +
# NotebookCopyService/NotebookSharingService; the facade keeps
# frozen-signature delegates, so the mixin module's internal self-call sites
# disappear.  The facade's wire_sharing lambda adds one _insert_row seat
# reference; the two component test files consume the facade at fresh sites.
TASK9_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_notebook_sharing.py", name)
    for name in {
        "_connect", "_insert_row", "_notebook_from_row",
        "_scale_index_version", "_sweep_stuck_copies", "_vector_cache",
        "_write", "add_member", "get_notebook", "is_member", "list_members",
        "notebook_copy_stats", "remove_member", "settings", "storage_dir",
        "user_can_access_notebook", "user_can_read_notebook",
    }
} | {
    ("backend/app/services/sqlite_repository.py", "_insert_row"),
} | {
    ("backend/tests/test_sharing_store_component.py", name)
    for name in {"SQLiteRepository", "_runtime"}
} | {
    ("backend/tests/test_notebook_copy_service.py", name)
    for name in {
        "SQLiteRepository", "_COPY_CHUNK", "_insert_row", "_new_id",
        "_runtime", "copy_notebook", "storage_dir",
    }
}
# Task 10: sources/source_elements/chunks rows and the four vector tables move
# to SourceStore + ChunkStore + EmbeddingStore; the facade keeps
# frozen-signature delegates, so the moved bodies' internal self-call sites
# disappear from the facade file. The three component test files and the
# migrated N+1 spy consume the facade/new seams at fresh sites.
TASK10_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_count", "_extraction_warning", "_source_from_row", "_source_has_kg",
        "_sources_from_rows",
    }
} | {
    ("backend/tests/test_sources_page_batched.py", name)
    for name in {"_connect", "_runtime"}
} | {
    ("backend/tests/test_source_store_component.py", name)
    for name in {"SQLiteRepository", "create_notebook", "_runtime", "_write"}
} | {
    ("backend/tests/test_embedding_store_component.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "store_kg", "_connect",
        "_runtime", "_write",
    }
} | {
    ("backend/tests/test_chunk_store_component.py", name)
    for name in {"SQLiteRepository", "create_notebook", "_connect", "_runtime", "_write"}
}
# Task 11: the source-file / embedding / chunking bodies move to
# SourceFileStore + SourceEmbeddingService + SourceChunkingService; the facade
# keeps frozen-signature delegates, so _embed_chunks_batch's only internal
# facade call site (inside _embed_chunks_for_source) disappears.  The three
# component test files consume the facade/new seams at fresh sites, and the
# two extended concurrency suites gain appended thread-name-prefix pins.
TASK11_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", "_embed_chunks_batch"),
} | {
    ("backend/tests/test_source_file_store.py", name)
    for name in {
        "SQLiteRepository", "_delete_file", "_runtime", "_source_raw_text",
        "storage_dir",
    }
} | {
    ("backend/tests/test_source_embedding_service.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "embedder", "_connect",
        "_write", "_runtime", "_embed_source", "_embed_objects_batch",
        "_embed_relations_batch", "_embed_chunks_for_source",
        "_embed_chunks_batch", "_build_chunks_for_source",
    }
} | {
    ("backend/tests/test_source_chunking_service.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "_connect", "_write",
        "_runtime", "_build_chunks_for_source", "_mark_unified_kg_dirty",
        "_new_id",
    }
} | {
    ("backend/tests/test_embed_concurrency.py", name)
    for name in {"create_notebook", "embedder", "_embed_source"}
} | {
    ("backend/tests/test_kg_object_embed_concurrency.py", name)
    for name in {"create_notebook", "embedder", "_embed_objects_batch"}
}
# Task 12: the ingestion orchestration (import/URL/upload/process/parse/
# delete, status machine, metadata augmentation, URL-local parse and
# per-source extraction) moves to SourceIngestionService behind fresh
# per-call hooks; the facade keeps frozen-signature delegates, so the moved
# bodies' internal self-call sites disappear from the facade file.  The
# migrated patch seats' consumer residue, the modified suites' service-level
# probes (repo._runtime.source_ingestion...) and the two new ingestion test
# files consume the facade/new seams at fresh sites.
TASK12_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "UploadedSourceFile", "_build_chunks_for_source", "_delete_file",
        "_embed_chunks_for_source", "_embed_source", "_parse_url_via_local",
        "_relink_extra_relations", "_source_raw_text", "get_source",
        "process_source",
    }
} | {
    ("backend/tests/test_batch_ingest.py", name)
    for name in {"_run_extraction", "_set_source_status", "_runtime"}
} | {
    ("backend/tests/test_chunk_embed.py", name)
    for name in {"_run_extraction", "_runtime"}
} | {
    ("backend/tests/test_event_logging.py", name)
    for name in {"SQLiteRepository", "event_log", "_runtime"}
} | {
    ("backend/tests/test_kg_llm_client.py", name)
    for name in {"_source_raw_text", "_runtime"}
} | {
    ("backend/tests/test_kg_relink_repository.py", name)
    for name in {"_run_extraction", "_runtime"}
} | {
    ("backend/tests/test_kg_repository.py", name)
    for name in {"_run_extraction", "_runtime"}
} | {
    ("backend/tests/test_kg_source_status.py", name)
    for name in {"_set_source_status", "create_notebook", "event_log", "get_source"}
} | {
    ("backend/tests/test_p4_kg_shrink.py", name)
    for name in {"_run_extraction", "_runtime"}
} | {
    ("backend/tests/test_pipeline_concurrency.py", name)
    for name in {
        "_connect", "_runtime", "create_notebook", "create_user", "embedder",
        "process_source",
    }
} | {
    ("backend/tests/test_resolve_notebook_conflicts.py", name)
    for name in {"_run_extraction", "_runtime"}
} | {
    ("backend/tests/test_url_sources.py", name)
    for name in {"_connect", "add_url_sources", "create_notebook"}
} | {
    ("backend/tests/test_source_ingestion_service.py", name)
    for name in {
        "SQLiteRepository", "_now", "_connect", "_write", "_run_extraction",
        "_runtime", "create_notebook", "embedder", "event_log", "get_source",
        "llm_client", "parse_source_file", "process_source",
        "relations_for_notebook", "settings", "upload_sources",
    }
} | {
    ("backend/tests/test_source_ingestion_failure_boundaries.py", name)
    for name in {
        "SQLiteRepository", "_now", "_augment_notebook_meta", "_connect",
        "_write", "_runtime", "add_url_sources", "create_notebook",
        "delete_source", "embedder", "get_notebook", "get_source",
        "llm_client", "mineru_client", "mineru_cloud_client",
        "parse_source_file", "process_source", "settings", "upload_sources",
    }
}

# Task 13: knowledge/governance/unified-KG persistence moves to
# KnowledgeStore + GovernanceStore + UnifiedKgStore and schema orchestration
# to SchemaRegistryService; the facade keeps frozen-signature delegates, so
# the moved bodies' internal self-call sites disappear from the facade file.
# communities.py consumes the unified store instead of repo._connect, the two
# modified suites (checkpoint-seat migration, chunk-FTS import move) reference
# the runtime seam at fresh sites, and the three new Task-13 test files
# consume the facade at fresh sites.
TASK13_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_delete_knowledge_object_sources", "_delete_relations_for_source",
        "_find_base_dedup_match", "_find_stale_knowledge_ids_for_source",
        "_mark_source_index_backfilled", "_merge_evidence_lists",
        "_object_schema_from_row", "_seed_fn_for", "_source_index_backfilled",
        "_upsert_knowledge_object_sources", "list_object_schemas",
    }
} | {
    ("backend/app/services/communities.py", name)
    for name in {"_connect", "_runtime", "event_log", "settings"}
} | {
    ("backend/tests/test_rebuild_checkpoint.py", name)
    for name in {
        "_cluster_input_version", "_runtime", "_write", "create_notebook",
        "rebuild_unified_kg", "settings", "store_kg",
    }
} | {
    ("backend/tests/test_chunk_retrieval.py", name)
    for name in {"_connect", "_runtime"}
} | {
    # the fake-repo suite composes a real UnifiedKgStore for the Task-13
    # communities seam — its frozen event_log assertion sites shift lines
    ("backend/tests/test_community_peers.py", "event_log"),
} | {
    ("backend/tests/test_knowledge_store_contract.py", name)
    for name in {
        "KNOWLEDGE_STATUSES", "KnowledgeGraphTooLargeError", "SQLiteRepository",
        "USABLE_STATUSES", "_connect", "_mark_unified_kg_dirty", "_now",
        "_runtime", "_test_insert_object", "_write", "create_notebook",
    }
} | {
    ("backend/tests/test_schema_registry_service.py", name)
    for name in {
        "SQLiteRepository", "_now", "_runtime", "_write", "create_notebook",
        "create_object_schema", "delete_object_schema", "effective_schemas",
        "list_object_schemas", "llm_client", "propose_schemas", "settings",
        "update_object_schema",
    }
} | {
    ("backend/tests/test_repository_module_boundaries.py", "SQLiteRepository"),
}

# Task 14: the KG mutation coordinator's phase-matrix and failure-boundary
# suites consume the facade at fresh sites.  Observation/injection seams are
# component seams (runtime.database.write / runtime.kg_mutations /
# runtime.source_embedding / runtime.embedding_store / runtime.governance /
# runtime.knowledge), so NO fresh facade patch targets are minted — only
# consumer sites (the mutation entry points under test plus the identity
# assertions over the coordinator-held facade cache objects).
TASK14_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_kg_mutation_phase_matrix.py", name)
    for name in {
        "SQLiteRepository", "_auto_index_checked", "_connect",
        "_notebook_langs_cache", "_runtime", "_test_insert_object",
        "_unified_cache", "_vector_cache", "append_clusters",
        "apply_conflict_resolution", "approve_promotion", "confirm_conflict",
        "confirm_merge", "copy_notebook", "create_notebook", "embedder",
        "mark_notebook_base", "merge_knowledge", "propose_promotion",
        "rebuild_unified_kg", "reject_merge", "relink_notebook_kg",
        "review_pending_merges", "set_edge_review", "store_kg",
        "update_knowledge", "write_clusters", "write_conflict_candidate",
        "write_merge_candidate",
    }
} | {
    ("backend/tests/test_kg_mutation_failure_boundaries.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_runtime", "_test_insert_object",
        "approve_promotion", "confirm_conflict", "create_notebook", "embedder",
        "mark_notebook_base", "propose_promotion", "store_kg",
        "update_knowledge", "write_clusters", "write_conflict_candidate",
    }
}

# Task 15 migrates the KG-lifecycle internal-caller patch seats onto the
# canonical KnowledgeLifecycleService / KnowledgeGovernanceService components
# (repo._runtime.knowledge_lifecycle / repo._runtime.knowledge_governance —
# the frozen facade sites below stop appearing in the static scan, exactly
# like Task 12's _run_extraction migration).
TASK15_ALLOWED_PATCHES = {
    ("backend/tests/test_batch_ingest.py", 290, "relink_notebook_kg", "repo"),
    ("backend/tests/test_batch_ingest.py", 313, "relink_notebook_kg", "repo"),
    ("backend/tests/test_kg_building_flag.py", 67, "delete_notebook_kg", "repo"),
    ("backend/tests/test_rebuild_cache.py", 69, "_stream_seed_reps", "repo"),
    ("backend/tests/test_rebuild_checkpoint.py", 224, "_write_cluster_map_streamed", "repo"),
    ("backend/tests/test_rebuild_checkpoint.py", 243, "_write_cluster_map_streamed", "repo"),
    ("backend/tests/test_rebuild_wires_communities.py", 47, "rebuild_communities", "repo"),
    ("backend/tests/test_rebuild_wires_communities.py", 60, "rebuild_communities", "repo"),
    ("backend/tests/test_resolve_notebook_conflicts.py", 303, "resolve_notebook_conflicts", "repo"),
    ("backend/tests/test_resolve_notebook_conflicts.py", 326, "resolve_notebook_conflicts", "repo"),
    ("backend/tests/test_viz_bounded.py", 118, "_unified_graph_full", "repo"),
}

# Task 16: the delegation suite's compound-flow proof patches the facade
# set_conflict_status wrapper (production-compatible seat — confirm_conflict
# routes the candidate-status transaction through it by contract, exactly
# like the frozen test_repository_phase_contracts probe whose `repository`
# fixture name this static scan cannot see).
TASK16_ALLOWED_PATCHES = {
    ("backend/tests/test_knowledge_governance_delegation.py", 131, "set_conflict_status", "repo"),
}

# Task 15: the KG lifecycle / unified-KG orchestration moves to
# KnowledgeLifecycleService (+ the KnowledgeGovernanceService seed carrying
# resolve_notebook_conflicts); the facade keeps frozen-signature delegates, so
# the moved bodies' internal self-call sites disappear from the facade file.
# The migrated patch seats' consumer residue, the modified suites' service-
# level probes (repo._runtime.knowledge_lifecycle...) and the new delegation
# test file consume the facade/new seams at fresh sites.
TASK15_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "append_clusters", "build_notebook_kg", "delete_notebook_kg",
        "rebuild_canonical_relations", "rebuild_communities",
        "rebuild_mention_bridge", "relink_notebook_kg",
        "resolve_notebook_conflicts", "store_kg",
        "_cluster_input_version", "_kg_neighbors_db", "_object_meta",
        "_stream_seed_reps", "_tier2_bridge_candidates_ann",
        "_unified_graph_bounded", "_viz_dict", "_viz_node",
        "_write_cluster_map_streamed",
    }
} | {
    ("backend/tests/test_kg_building_flag.py", name)
    for name in {"delete_notebook_kg", "_runtime"}
} | {
    ("backend/tests/test_kg_rebuild_relink_api.py", name)
    for name in {"build_notebook_kg", "delete_notebook_kg", "_runtime"}
} | {
    ("backend/tests/test_rebuild_wires_communities.py", name)
    for name in {"rebuild_communities", "_runtime"}
} | {
    ("backend/tests/test_resolve_notebook_conflicts.py", "resolve_notebook_conflicts"),
    ("backend/tests/test_batch_ingest.py", "relink_notebook_kg"),
    ("backend/tests/test_rebuild_checkpoint.py", "_write_cluster_map_streamed"),
} | {
    ("backend/tests/test_rebuild_cache.py", name)
    for name in {"_stream_seed_reps", "_runtime"}
} | {
    ("backend/tests/test_viz_bounded.py", name)
    for name in {"_unified_graph_full", "_runtime"}
} | {
    ("backend/tests/test_knowledge_lifecycle_delegation.py", name)
    for name in {
        "SQLiteRepository", "_kg_building", "_kg_building_lock", "_runtime",
        "_unified_cache", "_viz_building", "append_clusters",
        "build_notebook_kg", "delete_notebook_kg", "get_community_reports",
        "incremental_fuse_source", "kg_neighbors", "list_communities",
        "rebuild_canonical_relations", "rebuild_communities",
        "rebuild_mention_bridge", "rebuild_notebook_kg", "rebuild_unified_kg",
        "relink_notebook_kg", "resolve_notebook_conflicts", "store_kg",
        "summarize_communities", "unified_graph", "unified_kg_status",
        "write_clusters",
    }
}

# Task 16: the governance orchestration moves to KnowledgeGovernanceService
# (extending the Task-15 seed instance); the facade keeps frozen-signature
# delegates, so the moved bodies' internal self-call sites disappear from the
# facade file (incl. the Task-15 temporary conflict-port lambdas and the
# _REVIEW_STATUSES / static-helper references).  The new delegation test file
# consumes the facade at fresh sites.
TASK16_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_REVIEW_STATUSES", "_has_pending_merges", "_knowledge_ref",
        "_knowledge_similarity", "_payload_join", "_pending_merges_batch",
        "_promotion_row_to_dict", "apply_conflict_resolution",
        "get_conflict_candidate", "review_pending_merges", "set_edge_review",
        "set_merge_decision", "update_knowledge", "write_conflict_candidate",
    }
} | {
    ("backend/tests/test_knowledge_governance_delegation.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_has_pending_merges",
        "_pending_merges_batch", "_runtime", "_test_insert_object",
        "apply_conflict_resolution", "approve_promotion",
        "concept_whitelist_add", "concept_whitelist_list",
        "concept_whitelist_remove", "concept_whitelist_terms",
        "confirm_conflict", "confirm_merge", "create_notebook",
        "decided_pairs", "decided_seed_pairs", "find_duplicates",
        "get_conflict_candidate", "list_promotion_queue", "merge_knowledge",
        "merge_review_job_status", "pending_conflicts", "pending_merges",
        "propose_promotion", "reject_conflict", "reject_merge",
        "reject_promotion", "resolve_notebook_conflicts",
        "review_pending_merges", "review_queue", "run_merge_review_job",
        "set_conflict_status", "set_edge_review", "set_merge_decision",
        "update_knowledge", "write_conflict_candidate",
        "write_merge_candidate",
    }
}

# Task 17: the runtime owns the retrieval snapshot caches (RetrievalSnapshot-
# Cache); the facade's `_vector_cache` / `_unified_cache` handles become
# write-through descriptors over the SAME objects, so the constructor's
# direct `self._unified_cache` sites (inline dict + the two wire kwargs)
# disappear from the facade file — `self._vector_cache` keeps its read sites.
# The new runtime suite and the extended invalidation suite consume the
# facade at fresh sites.
TASK17_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", "_unified_cache"),
} | {
    ("backend/tests/test_retrieval_snapshot_cache_runtime.py", name)
    for name in {
        "SQLiteRepository", "_invalidate_unified_cache", "_runtime",
        "_unified_cache", "_vector_cache",
    }
} | {
    ("backend/tests/test_vector_cache_invalidation.py", name)
    for name in {"_invalidate_unified_cache", "_runtime", "_vector_cache"}
}

# Task 18: the scale/viz artifact READ adapters move behind the runtime
# (IndexProjectionStore / ScaleArtifactStore / ScaleArtifactCatalog); the
# facade keeps frozen-signature delegates, so the moved bodies' internal
# self-call sites disappear from the facade file (`_read_manifest_version` /
# `_viz_index_dir` were only ever called from the moved read paths).  The new
# catalog test file consumes the facade at fresh sites.
TASK18_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {"_read_manifest_version", "_viz_index_dir"}
} | {
    ("backend/tests/test_scale_artifact_catalog.py", name)
    for name in {
        "SQLiteRepository", "_gather_kg_graph", "_runtime", "_scale_index",
        "_scale_index_version", "build_scale_index", "create_notebook",
        "embedder", "settings", "store_kg",
    }
}

# Task 19: direct runtime-builder coverage intentionally crosses the facade
# only to seed fixtures and assert the pre-Task-20 cache/build-state identity.
TASK19_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_build_viz_graph_arrays", "_derive_object_graph_lite", "_runtime_dim",
        "_viz_arrays_from_graph", "incremental_fuse_source",
    }
} | {
    ("backend/tests/test_scale_builder_failure_boundaries.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_scale_building", "_scale_idx_cache",
        "_scale_index", "_write", "build_scale_index", "create_notebook",
        "embedder", "rebuild_unified_kg",
    }
} | {
    ("backend/tests/test_ppr_retrieve.py", name)
    for name in {
        "_runtime", "_vector_cache", "_write", "build_scale_index",
        "rebuild_unified_kg", "scale_ppr", "settings",
    }
} | {
    ("backend/tests/test_runtime_dim_scale_index.py", name)
    for name in {
        "_retrieve_chunks_ann", "_runtime", "_scale_index", "build_scale_index",
        "event_log", "fold_scale_index_delta", "rebuild_unified_kg", "settings",
    }
} | {
    ("backend/tests/test_scale_index_repo.py", name)
    for name in {"_runtime", "build_scale_index"}
}

# Task 22: the new ask-state store contract suite imports the compatibility
# facade at a fresh site (the facade's own frozen import lines are untouched —
# the facade reaches the store through the runtime and one function-local
# import inside _read_ask_trace).
TASK22_ALLOWED_IMPORTS = {
    (
        "backend/tests/test_ask_state_store.py",
        23,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
}

# Task 23: dropping the three imports the extracted ask execution made dead in
# routes.py (contextvars / threading / AskCancelled) shifts the frozen
# NotebookRepository/UploadedSourceFile compatibility import site up to line
# 90; the new coordinator contract suite imports the compatibility facade at a
# fresh site.
TASK23_ALLOWED_IMPORTS = {
    ("backend/app/api/routes.py", 90, "app.services.repository", "NotebookRepository"),
    ("backend/app/api/routes.py", 90, "app.services.repository", "UploadedSourceFile"),
    (
        "backend/tests/test_ask_execution_coordinator.py",
        40,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
}

# Task 24: the Ask mode engines + synthesis move to the runtime-owned
# AskService; the facade keeps frozen-signature delegates, so the moved
# bodies' internal self-call sites disappear from the facade file (the
# engines now consume the retrieval/evidence-context/ask-state/model ports
# directly).  The new boundary suite imports the compatibility exports at a
# fresh site.
TASK24_ALLOWED_IMPORTS = {
    ("backend/tests/test_ask_service_boundary.py", 32, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_ask_service_boundary.py", 32, "app.services.sqlite_repository", "set_request_user"),
    ("backend/tests/test_ask_service_boundary.py", 32, "app.services.sqlite_repository", "reset_request_user"),
    # the coordinator suite's Task-24 docstring note shifts its frozen
    # compatibility import down from the Task-23 site (line 40).
    ("backend/tests/test_ask_execution_coordinator.py", 44, "app.services.sqlite_repository", "SQLiteRepository"),
}

TASK24_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_MIX_KG_KEY_BASE", "_MIX_PROMPT_BUFFER_TOKENS", "_answer_chunks",
        "_answer_context", "_answer_mix", "_answer_reasoning",
        "_answer_with_retry", "_any_base_notebook_has_kg",
        "_build_chunk_retrieval_plan", "_chunk_answer_context",
        "_citations_from", "_conversation_history", "_ensure_conversation",
        "_federated_graph_is_large", "_federated_rx_graph",
        "_graph_seed_fusion", "_keyword_chunk_candidates", "_kg_source_chunks",
        "_knowledge_headline", "_mix_retrieve", "_mmr_select_chunks",
        "_needs_index", "_notebook_langs", "_parse_answer_anchors",
        "_ppr_retrieve", "_refine_context", "_retrieve_chunks",
        "_retrieve_chunks_multi", "_rewrite_followup_query", "_save_answer",
        "_tier_map_for", "_truncate_kg_block", "_unconfigured_model_response",
        "_union_chunk_candidates", "federated_retrieve",
        "get_community_reports",
    }
} | {
    ("backend/tests/test_ask_service_boundary.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_runtime", "ask", "ask_chunk",
        "create_notebook", "current_user", "embedder", "reset_request_user",
        "set_request_user", "set_user_model_settings",
    }
} | {
    ("backend/tests/test_ask_modes_api.py", name)
    for name in {"_runtime", "current_user"}
}

# Task 20: ScaleArtifactRuntime becomes the one owner of the Task-18/19
# caches, locks, build markers and scheduling state.  Compatibility facade
# attributes become write-through properties and the focused probes patch the
# canonical runtime/builder owners.  Keep these allowances exact by member +
# file; production consumers are not hidden behind a directory allowlist.
TASK20_ALLOWED_IMPORTS = {
    (
        "backend/tests/test_scale_artifact_runtime.py",
        15,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
}

TASK20_ALLOWED_PATCHES = {
    ("backend/tests/test_auto_scale_index.py", line, "trigger_scale_index_rebuild", "repo")
    for line in {55, 67, 81, 86, 99, 118, 132, 144, 162, 193}
} | {
    ("backend/tests/test_scale_index_repo.py", 871, "fold_scale_index_delta", "repo"),
    ("backend/tests/test_scale_index_repo.py", 872, "build_scale_index", "repo"),
    ("backend/tests/test_scale_delta_policy.py", 104, "_ensure_scale_scheduler", "repo"),
    ("backend/tests/test_scale_delta_policy.py", 115, "_ensure_scale_scheduler", "repo"),
    ("backend/tests/test_scale_delta_policy.py", 127, "_ensure_scale_scheduler", "repo"),
    ("backend/tests/test_index_build_consolidation.py", 30, "_spawn_viz_build", "repo"),
    ("backend/tests/test_rebuild_communities.py", 195, "_scale_index", "repo"),
    ("backend/tests/test_rebuild_communities.py", 206, "_scale_index", "repo"),
    ("backend/tests/test_auto_scale_index.py", 242, "notebook_copy_stats", "repo"),
}

# Task 21 migrates the frozen answer-context monkeypatch seats from the facade
# to the canonical graph/evidence owner. These are exact removed sites; new
# production patch targets are never hidden.
TASK21_ALLOWED_PATCHES = {
    ("backend/tests/test_answer_context_budget.py", 29, "_concept_cluster_id", "repo"),
    ("backend/tests/test_answer_context_budget.py", 30, "node_context", "repo"),
    ("backend/tests/test_answer_context_budget.py", 44, "_concept_cluster_id", "repo"),
    ("backend/tests/test_answer_context_budget.py", 45, "node_context", "repo"),
    ("backend/tests/test_kg_quality.py", 59, "cluster_map", "repo"),
    ("backend/tests/test_overlay_guard_order.py", 63, "federated_retrieve", "repo"),
    ("backend/tests/test_overlay_guard_order.py", 63, "federated_retrieve_relations", "repo"),
    ("backend/tests/test_overlay_guard_order.py", 71, "notebook_copy_stats", "repo"),
    ("backend/tests/test_overlay_guard_order.py", 93, "notebook_copy_stats", "repo"),
    ("backend/tests/test_indexed_only_principle.py", 325, "_IN_CHUNK", "SQLiteRepository"),
    ("backend/tests/test_scale_xlayer_bridge_delta.py", 222, "_delta_vector_matrix", "repo"),
    ('backend/tests/test_architecture_hardening.py', 55, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_architecture_hardening.py', 102, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_bm25_rrf.py', 135, '_rrf_scored', 'repo'),
    ('backend/tests/test_bm25_rrf.py', 150, '_rrf_scored', 'repo'),
    ('backend/tests/test_chunk_bruteforce_guard.py', 73, '_gather_chunks', 'repo'),
    ('backend/tests/test_chunk_bruteforce_guard.py', 134, '_embed_query', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 124, '_mix_retrieve', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 125, '_retrieve_chunks_multi', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 126, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 194, '_retrieve_chunks_multi', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 224, '_mmr_select_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 307, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 326, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 337, '_retrieve_chunks_fts_degraded', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 346, '_gather_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 381, '_retrieve_chunks_ann', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 390, '_gather_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 451, '_mix_retrieve', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 501, '_mix_retrieve', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 525, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 527, '_mmr_select_chunks', 'repo'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 575, '_mix_retrieve', 'repo'),
    ('backend/tests/test_chunk_retrieval_plan.py', 61, '_notebook_has_kg', 'repo'),
    ('backend/tests/test_chunk_retrieval_plan.py', 62, '_any_base_notebook_has_kg', 'repo'),
    ('backend/tests/test_chunk_retrieval_plan.py', 71, '_notebook_has_kg', 'repo'),
    ('backend/tests/test_chunk_retrieval_plan.py', 72, '_any_base_notebook_has_kg', 'repo'),
    ('backend/tests/test_graph_k_binding.py', 130, '_retrieve_scored', 'repo'),
    ('backend/tests/test_indexed_only_principle.py', 235, '_vector_matrix', 'repo'),
    ('backend/tests/test_language_policy.py', 289, '_keyword_chunk_candidates', 'repo'),
    ('backend/tests/test_large_lib_index_required.py', 46, '_gather_chunks', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 108, 'scale_ppr', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 109, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 118, '_ppr_graph', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 135, 'scale_ppr', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 136, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 145, '_ppr_graph', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 162, 'scale_ppr', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 165, '_ppr_graph', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 200, '_embed_query', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 201, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 234, '_retrieve_chunks', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 235, '_open_scale_ann', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 239, '_vector_matrix', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 277, '_federated_rx_graph', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 296, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 334, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_ppr_fallback_guard.py', 372, '_vector_matrix', 'repo'),
    ('backend/tests/test_query_hotpath_cache.py', 168, '_connect', 'repo'),
    ('backend/tests/test_relation_ann.py', 382, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_relation_ann.py', 412, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_relation_retrieval.py', 267, '_relations_with_names', 'repo'),
    ('backend/tests/test_relation_retrieval.py', 291, '_IN_CHUNK', 'repo'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 69, '_vector_matrix', 'repo'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 78, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 98, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 122, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 139, 'notebook_copy_stats', 'repo'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', 188, '_vector_matrix', 'repo'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', 221, '_vector_matrix', 'repo'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', 248, '_scale_xlayer_bridge_edges', 'repo'),
    # c9ddf31 single-owner cache follow-up：冻结 fixture 记录的 _loader_spy 旧 patch 座
    # （repo._connect@134）已迁到 runtime.database.connect（等行数替换），retired。
    ('backend/tests/test_incremental_fuse_perf.py', 134, '_connect', 'repo'),
}

TASK20_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {
        "_auto_index_checked", "_dequeue_scale_idle", "_ensure_scale_scheduler",
        "_compute_scale_version_cold", "_maybe_enqueue_scale_fold",
        "_notify_index_done", "_open_scale_ann", "_probe_scale_version_signal",
        "_process_idle_queue",
        "_notebook_name", "_resolve_index_owner", "_resolve_scale_mode",
        "_run_scale_op", "_scale_building",
        "_scale_building_lock", "_scale_idle_queue", "_scale_idx_cache",
        "_scale_idx_load_lock", "_scale_idx_load_locks", "_scale_index",
        "_scale_index_version", "_scale_scheduler_started", "_scale_ver_cache",
        "_scale_ver_lock", "_scale_ver_locks", "_spawn_viz_build",
        "_viz_building", "_viz_building_lock", "_viz_idx_cache", "_viz_index",
        "_viz_index_probe", "build_scale_index", "build_viz_index",
        "cancel_scale_index", "fold_scale_index_delta", "index_status",
        "_scale_index_eligible", "maybe_auto_index", "scale_index_status",
        "trigger_scale_index_rebuild", "unified_kg_status",
    }
} | {
    ("backend/tests/test_scale_artifact_runtime.py", name)
    for name in {
        "SQLiteRepository", "_auto_index_checked", "_runtime", "_scale_building",
        "_scale_building_lock", "_scale_idle_queue", "_scale_idx_cache",
        "_scale_idx_load_lock", "_scale_idx_load_locks", "_scale_ver_cache",
        "_scale_ver_lock", "_scale_ver_locks", "_viz_building",
        "_viz_building_lock", "_viz_idx_cache", "build_scale_index",
        "create_notebook", "embedder", "settings", "store_kg",
    }
} | {
    ("backend/tests/test_auto_scale_index.py", name)
    for name in {"_runtime", "notebook_copy_stats", "trigger_scale_index_rebuild"}
} | {
    ("backend/tests/test_scale_index_repo.py", name)
    for name in {"_runtime", "build_scale_index", "fold_scale_index_delta"}
} | {
    ("backend/tests/test_scale_delta_policy.py", name)
    for name in {"_runtime", "_ensure_scale_scheduler"}
} | {
    ("backend/tests/test_index_build_consolidation.py", name)
    for name in {"_runtime", "_spawn_viz_build"}
} | {
    ("backend/tests/test_rebuild_communities.py", name)
    for name in {"_runtime", "_scale_index"}
}

TASK21_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/reasoning_retrieval.py", name)
    for name in {"reasoning_llm_client", "retrieval"}
} | {
    ("backend/app/services/sqlite_repository.py", "retrieval"),
} | {
    ("backend/tests/test_answer_context_budget.py", name)
    for name in {
        "_answer_context", "_concept_cluster_id", "create_notebook",
        "node_context", "retrieval", "settings",
    }
} | {
    ("backend/tests/test_reasoning_ppr.py", name)
    for name in {
        "_MIX_KG_KEY_BASE", "_answer_context", "_answer_reasoning",
        "_ppr_retrieve", "_reasoning_llm_client", "_retrieve_scored",
        "ask", "llm_client", "settings",
    }
} | {
    ("backend/tests/test_kg_quality.py", name)
    for name in {"_retrieve_scored", "cluster_map", "retrieval", "settings"}
} | {
    ("backend/tests/test_overlay_guard_order.py", name)
    for name in {
        "_chunk_kg_overlay", "create_notebook", "federated_retrieve",
        "federated_retrieve_relations", "notebook_copy_stats", "retrieval",
    }
} | {
    ("backend/tests/test_quota_reuse.py", "retrieval"),
    ("backend/tests/test_reasoning_ppr_prefetch.py", "retrieval"),
    ("backend/tests/test_two_tier_federated.py", "_retrieve_scored"),
    ("backend/tests/test_two_tier_federated.py", "retrieval"),
    ("backend/tests/test_report_engine.py", "settings"),
} | {
    ("backend/tests/test_communities.py", name)
    for name in {
        "create_notebook", "embedder", "list_communities",
        "rebuild_communities", "settings", "store_kg",
    }
} | {
    ("backend/tests/test_repository_runtime.py", name)
    for name in {
        "_answer_context", "_chunk_answer_context", "_runtime", "_test_insert_object",
        "create_notebook", "retrieval",
    }
}

# Task 25: the deep-report domain moves off the facade — ReportEngine keeps
# only narrow ports (its frozen repo.* call sites disappear), the facade's
# report CRUD becomes ReportStore delegates (the internal _report_row_to_dict
# self-calls move into the store), routes' launch helpers delegate to the
# runtime ReportExecutionCoordinator (repo.settings leaves routes.py; the new
# facade `report_execution` property is the coordinator handle) and the report
# test files re-seat their stubs on the canonical owners
# (repo.retrieval / repo._runtime.*).
TASK25_ALLOWED_IMPORTS = {
    ("backend/tests/test_report_store.py", 21, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_report_execution.py", 223, "app.services.sqlite_repository", "SQLiteRepository"),
}

TASK25_ALLOWED_NEW_MEMBERS = {"report_execution"}

TASK25_ALLOWED_PATCHES = {
    ("backend/tests/test_report_engine.py", 155, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 182, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 322, "_retrieve_neighbors", "eng.repo"),
    ("backend/tests/test_report_engine.py", 476, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 477, "_ppr_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 511, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 544, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 562, "federated_retrieve", "repo"),
    ("backend/tests/test_report_engine.py", 598, "get_report", "repo"),
}

TASK25_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/report_engine.py", name)
    for name in {
        "_answer_context", "_chunk_answer_context", "_connect",
        "_note_model_error", "_ppr_retrieve", "federated_retrieve",
        "get_report", "reasoning_llm_client", "rewrite_llm_client",
        "update_report",
    }
} | {
    ("backend/app/services/sqlite_repository.py", "_report_row_to_dict"),
    ("backend/app/api/routes.py", "settings"),
} | {
    ("backend/tests/test_report_engine.py", name)
    for name in {
        "_note_model_error", "_ppr_retrieve", "_retrieve_neighbors", "_write",
        "_runtime", "create_report", "federated_retrieve", "get_report",
        "llm_client", "retrieval", "update_report",
    }
} | {
    ("backend/tests/test_report_store.py", name)
    for name in {
        "SQLiteRepository", "_report_row_to_dict", "_runtime",
        "create_notebook", "create_report", "delete_report", "export_reports",
        "get_report", "list_reports", "update_report",
    }
} | {
    ("backend/tests/test_report_execution.py", name)
    for name in {"SQLiteRepository", "_runtime"}
}

# Task 26: the last three test-only facade-private patch seats migrate to
# their canonical components (source-embedding backfill / candidate gather /
# graph-side index delta); the frozen facade sites below stop appearing in
# the static scan.
TASK26_ALLOWED_PATCHES = {
    ("backend/tests/test_ask_vector_matrix.py", 125, "_backfill_knowledge_embeddings", "repo"),
    ("backend/tests/test_dedup_scale.py", 32, "_gather_elements", "repo"),
    ("backend/tests/test_scale_idx_disk_cache.py", 190, "_index_delta", "repo"),
}

# Task 27: the expanded write audit (every primary SQLite adapter) grows the
# test_all_writes_go_through_write_lock body, shifting the embed-transaction
# spy's frozen `_write` patch seat down within the same file.
TASK27_ALLOWED_PATCHES = {
    ("backend/tests/test_sqlite_write_optimization.py", 121, "_write", "embed_repo"),
    ("backend/tests/test_sqlite_write_optimization.py", 136, "_write", "embed_repo"),
}

# Task 27: production callers migrate onto ports / repo.maintenance — the
# private facade reaches below are frozen residue (their sites disappear from
# the static scan); the compatibility request-context imports move to
# app.core.request_context; the facade's _backfill_relation_embeddings body
# moves to the maintenance adapter (its internal _relations_with_names call
# site disappears); the new static suite imports the facade at a fresh site.
TASK27_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/batch_ingest.py", name)
    for name in {
        "_backfill_knowledge_embeddings", "_connect", "_embed_chunks_batch",
        "_embed_chunks_for_source", "_mark_source_index_backfilled",
        "_mark_unified_kg_dirty", "_run_extraction", "_scale_index",
        "_set_source_status", "_source_ids_from_evidence", "_user_profile",
        "_write", "reset_request_user", "set_request_user",
    }
} | {
    ("backend/app/eval/retrieval_metrics.py", name)
    for name in {"_retrieve_relations_scored", "_retrieve_scored"}
} | {
    ("backend/app/scripts/backfill_relation_embeddings.py", "_backfill_relation_embeddings"),
    ("backend/app/scripts/gen_recall_gold.py", "_connect"),
    ("backend/app/scripts/gen_recall_gold.py", "_relations_with_names"),
} | {
    ("backend/app/scripts/reembed_kg.py", name)
    for name in {
        "_backfill_relation_embeddings", "_connect", "_embed_objects_batch",
        "_mark_unified_kg_dirty", "_write",
    }
} | {
    ("scripts/backfill_kg_embeddings.py", name)
    for name in {"_backfill_knowledge_embeddings", "_connect"}
} | {
    ("scripts/build_chunks.py", name)
    for name in {"_chunk_and_embed_source", "_connect"}
} | {
    ("scripts/denoise_reextract_nb.py", name)
    for name in {"_connect", "_run_extraction"}
} | {
    ("scripts/diag_base_report.py", name)
    for name in {
        "_answer_context", "_connect", "_ppr_retrieve", "_retrieve_scored",
        "_scale_index",
    }
} | {
    ("scripts/kg_product_smoke.py", name)
    for name in {"_connect", "_now", "_run_extraction"}
} | {
    ("scripts/smoke_backend.py", name)
    for name in {"_connect", "_invalidate_unified_cache", "_now"}
} | {
    ("scripts/replay_retrieval.py", name)
    for name in {"reset_request_user", "set_request_user"}
} | {
    ("backend/app/services/sqlite_repository.py", "_relations_with_names"),
    ("backend/tests/test_repository_callers_static.py", "SQLiteRepository"),
} | {
    # the surviving compatibility imports of these composition roots shift
    # lines (TASK27_ALLOWED_IMPORTS carries the exact live sites; these broad
    # entries retire the frozen ones).
    ("backend/app/services/batch_ingest.py", "SQLiteRepository"),
    ("backend/app/services/batch_ingest.py", "UploadedSourceFile"),
    ("scripts/backfill_kg_embeddings.py", "SQLiteRepository"),
    ("scripts/kg_product_smoke.py", "SQLiteRepository"),
    ("scripts/replay_retrieval.py", "SQLiteRepository"),
    ("scripts/smoke_backend.py", "SQLiteRepository"),
    ("scripts/smoke_backend.py", "UploadedSourceFile"),
}

# Task 27: the maintenance adapter handle is the one new facade member; the
# two callers that switch onto the public retrieval port gain exact live
# ledger sites (their files are line-normalized, the ledger stays exact).
TASK27_ALLOWED_NEW_MEMBERS = {"maintenance"}
TASK27_ALLOWED_CONSUMERS = {
    ("retrieval", "backend/app/eval/retrieval_metrics.py:47"),
    ("retrieval", "scripts/diag_base_report.py:116"),
    ("retrieval", "scripts/diag_base_report.py:172"),
}

# Task 28: the backup-only snapshot verifier is a new read-only composition
# root under scripts/. It consumes the facade compatibility exports plus the
# public read surface (representative reads on the temporary backup) at exact
# ledger sites; it never touches private facade seams.
TASK28_ALLOWED_IMPORTS = {
    ("scripts/verify_repository_snapshot.py", 58, "app.services.sqlite_repository", "SCHEMA_VERSION"),
    ("scripts/verify_repository_snapshot.py", 58, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/verify_repository_snapshot.py", 58, "app.services.sqlite_repository", "reset_request_user"),
    ("scripts/verify_repository_snapshot.py", 58, "app.services.sqlite_repository", "set_request_user"),
}
TASK28_ALLOWED_CONSUMERS = {
    ("ask_job_detail", "scripts/verify_repository_snapshot.py:601"),
    ("get_conversation", "scripts/verify_repository_snapshot.py:594"),
    ("get_notebook", "scripts/verify_repository_snapshot.py:579"),
    ("get_report", "scripts/verify_repository_snapshot.py:606"),
    ("knowledge_types", "scripts/verify_repository_snapshot.py:582"),
    ("list_conversations", "scripts/verify_repository_snapshot.py:591"),
    ("list_knowledge", "scripts/verify_repository_snapshot.py:585"),
    ("list_reports", "scripts/verify_repository_snapshot.py:603"),
    ("list_sources", "scripts/verify_repository_snapshot.py:580"),
    ("maintenance", "scripts/verify_repository_snapshot.py:542"),
    ("maintenance", "scripts/verify_repository_snapshot.py:546"),
    ("maintenance", "scripts/verify_repository_snapshot.py:548"),
    ("maintenance", "scripts/verify_repository_snapshot.py:573"),
    ("search_notebook", "scripts/verify_repository_snapshot.py:615"),
    ("unified_kg_status", "scripts/verify_repository_snapshot.py:589"),
}

# Task 26: the consolidated facade delegates its last SQL bodies to the
# stores, so two facade-internal helper call sites disappear (_in_batches
# now feeds retrieval_objects as a batch size; storage_dir is the runtime
# SourceFileStore's path object, not a fresh _resolve_path result); the
# migrated patch seats leave `_runtime`/`retrieval` residue at their exact
# frozen lines; the two new contract suites and the hardening composition
# pin consume the facade at fresh sites.
TASK26_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", "_in_batches"),
    ("backend/app/services/sqlite_repository.py", "_resolve_path"),
    ("backend/tests/test_ask_vector_matrix.py", "_backfill_knowledge_embeddings"),
    ("backend/tests/test_ask_vector_matrix.py", "_runtime"),
    ("backend/tests/test_dedup_scale.py", "_gather_elements"),
    ("backend/tests/test_dedup_scale.py", "retrieval"),
    ("backend/tests/test_scale_idx_disk_cache.py", "_index_delta"),
    ("backend/tests/test_scale_idx_disk_cache.py", "retrieval"),
    ("backend/tests/test_architecture_hardening.py", "_runtime"),
    ("backend/tests/test_architecture_hardening.py", "settings"),
} | {
    ("backend/tests/test_repository_facade_contract.py", name)
    for name in {
        "SQLiteRepository", "SCHEMA_VERSION", "UploadedSourceFile",
        "KNOWLEDGE_STATUSES", "KnowledgeGraphTooLargeError",
        "RetrievedKnowledge", "USABLE_STATUSES", "_COPY_CHUNK",
        "_REQUEST_USER", "_fast_loads", "_new_id", "_now",
        "_remap_json_ids", "reset_request_user", "set_request_user",
    }
} | {
    ("backend/tests/test_repository_runtime_identity.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "settings", "storage_dir",
        "event_log", "db_path", "_write_lock", "_user_model_cfg_cache",
        "embedder", "llm_client", "rerank_client", "retrieval",
        "_vector_cache", "_unified_cache", "_scale_idx_cache",
        "_viz_idx_cache", "_scale_ver_cache", "_scale_ver_lock",
        "_scale_ver_locks", "_scale_idx_load_lock", "_scale_idx_load_locks",
        "_scale_idle_queue", "_viz_building", "_viz_building_lock",
        "_scale_building", "_scale_building_lock", "_auto_index_checked",
        "_scale_scheduler_started", "_kg_building", "_kg_building_lock",
        "_notebook_langs_cache", "_ask_cancel_events", "_ask_cancel_lock",
        "report_execution",
    }
}

TASK21_ALLOWED_MEMBER_FILES |= {
    ('backend/app/services/sqlite_repository.py', '_CJK_RE'),
    ('backend/app/services/sqlite_repository.py', '_LATIN_RE'),
    ('backend/app/services/sqlite_repository.py', '_MIX_FANOUT'),
    ('backend/app/services/sqlite_repository.py', '_MIX_NODE_SEEDS'),
    ('backend/app/services/sqlite_repository.py', '_MIX_REL_SEEDS'),
    ('backend/app/services/sqlite_repository.py', '_PPR_RERANK_SCHEMA'),
    ('backend/app/services/sqlite_repository.py', '_active_kg_delta'),
    ('backend/app/services/sqlite_repository.py', '_chunk_kg_overlay'),
    ('backend/app/services/sqlite_repository.py', '_delta_vector_matrix'),
    ('backend/app/services/sqlite_repository.py', '_elem_chunk_map'),
    ('backend/app/services/sqlite_repository.py', '_element_texts'),
    ('backend/app/services/sqlite_repository.py', '_ent_chunk_map'),
    ('backend/app/services/sqlite_repository.py', '_gather_chunks'),
    ('backend/app/services/sqlite_repository.py', '_gather_elements'),
    ('backend/app/services/sqlite_repository.py', '_gather_kg_graph'),
    ('backend/app/services/sqlite_repository.py', '_gather_vector_chunks'),
    ('backend/app/services/sqlite_repository.py', '_hydrate_chunk_candidates'),
    ('backend/app/services/sqlite_repository.py', '_index_delta'),
    ('backend/app/services/sqlite_repository.py', '_keyword_token_sets'),
    ('backend/app/services/sqlite_repository.py', '_kg_object_candidates'),
    ('backend/app/services/sqlite_repository.py', '_mention_extra_edges'),
    ('backend/app/services/sqlite_repository.py', '_participant_notebook_ids'),
    ('backend/app/services/sqlite_repository.py', '_ppr_fact_rerank'),
    ('backend/app/services/sqlite_repository.py', '_ppr_graph'),
    ('backend/app/services/sqlite_repository.py', '_ppr_reset_vector'),
    ('backend/app/services/sqlite_repository.py', '_relation_ann_candidates'),
    ('backend/app/services/sqlite_repository.py', '_retrieve_chunks_ann'),
    ('backend/app/services/sqlite_repository.py', '_retrieve_chunks_fts_degraded'),
    ('backend/app/services/sqlite_repository.py', '_retrieve_relations_scored'),
    ('backend/app/services/sqlite_repository.py', '_retrieve_scored'),
    ('backend/app/services/sqlite_repository.py', '_rrf_scored'),
    ('backend/app/services/sqlite_repository.py', '_scale_combined_graph'),
    ('backend/app/services/sqlite_repository.py', '_scale_xlayer_bridge_edges'),
    ('backend/app/services/sqlite_repository.py', '_vector_matrix'),
    ('backend/app/services/sqlite_repository.py', '_vector_matrix_version'),
    ('backend/app/services/sqlite_repository.py', '_vector_matrix_warm'),
    ('backend/app/services/sqlite_repository.py', 'federated_retrieve_relations'),
    ('backend/app/services/sqlite_repository.py', 'node_context'),
    ('backend/app/services/sqlite_repository.py', 'scale_ppr'),
    ('backend/tests/test_architecture_hardening.py', '_ppr_graph'),
    ('backend/tests/test_architecture_hardening.py', '_retrieve_chunks'),
    ('backend/tests/test_architecture_hardening.py', '_retrieve_chunks_multi'),
    ('backend/tests/test_architecture_hardening.py', 'notebook_copy_stats'),
    ('backend/tests/test_architecture_hardening.py', 'retrieval'),
    ('backend/tests/test_ask_vector_matrix.py', '_retrieve_scored'),
    ('backend/tests/test_ask_vector_matrix.py', '_vector_matrix'),
    ('backend/tests/test_ask_vector_matrix.py', 'retrieval'),
    ('backend/tests/test_bm25_rrf.py', '_retrieve_scored'),
    ('backend/tests/test_bm25_rrf.py', '_rrf_scored'),
    ('backend/tests/test_bm25_rrf.py', 'retrieval'),
    ('backend/tests/test_chunk_bruteforce_guard.py', '_embed_query'),
    ('backend/tests/test_chunk_bruteforce_guard.py', '_gather_chunks'),
    ('backend/tests/test_chunk_bruteforce_guard.py', '_retrieve_chunks'),
    ('backend/tests/test_chunk_bruteforce_guard.py', 'retrieval'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_gather_chunks'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_mix_retrieve'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_mmr_select_chunks'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_retrieve_chunks'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_retrieve_chunks_ann'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_retrieve_chunks_fts_degraded'),
    ('backend/tests/test_chunk_retrieval_characterization.py', '_retrieve_chunks_multi'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 'notebook_copy_stats'),
    ('backend/tests/test_chunk_retrieval_characterization.py', 'retrieval'),
    ('backend/tests/test_chunk_retrieval_plan.py', '_any_base_notebook_has_kg'),
    ('backend/tests/test_chunk_retrieval_plan.py', '_build_chunk_retrieval_plan'),
    ('backend/tests/test_chunk_retrieval_plan.py', '_notebook_has_kg'),
    ('backend/tests/test_chunk_retrieval_plan.py', 'retrieval'),
    ('backend/tests/test_graph_k_binding.py', '_federated_rx_graph'),
    ('backend/tests/test_graph_k_binding.py', '_retrieve_scored'),
    ('backend/tests/test_graph_k_binding.py', 'retrieval'),
    ('backend/tests/test_in_batching.py', '_in_batches'),
    ('backend/tests/test_in_batching.py', 'retrieval'),
    ('backend/tests/test_indexed_only_principle.py', '_relation_ann_candidates'),
    ('backend/tests/test_indexed_only_principle.py', '_retrieve_scored'),
    ('backend/tests/test_indexed_only_principle.py', '_vector_matrix'),
    ('backend/tests/test_indexed_only_principle.py', '_IN_CHUNK'),
    ('backend/tests/test_indexed_only_principle.py', '_runtime'),
    ('backend/tests/test_indexed_only_principle.py', 'retrieval'),
    ('backend/tests/test_indexed_only_principle.py', 'scale_ppr'),
    ('backend/tests/test_language_policy.py', '_keyword_chunk_candidates'),
    ('backend/tests/test_language_policy.py', 'retrieval'),
    ('backend/tests/test_large_lib_index_required.py', '_gather_chunks'),
    ('backend/tests/test_large_lib_index_required.py', '_retrieve_chunks'),
    ('backend/tests/test_large_lib_index_required.py', 'retrieval'),
    ('backend/tests/test_ppr_fallback_guard.py', '_chunk_kg_overlay'),
    ('backend/tests/test_ppr_fallback_guard.py', '_embed_query'),
    ('backend/tests/test_ppr_fallback_guard.py', '_federated_rx_graph'),
    ('backend/tests/test_ppr_fallback_guard.py', '_open_scale_ann'),
    ('backend/tests/test_ppr_fallback_guard.py', '_ppr_graph'),
    ('backend/tests/test_ppr_fallback_guard.py', '_ppr_retrieve'),
    ('backend/tests/test_ppr_fallback_guard.py', '_retrieve_chunks'),
    ('backend/tests/test_ppr_fallback_guard.py', '_vector_matrix'),
    ('backend/tests/test_ppr_fallback_guard.py', 'notebook_copy_stats'),
    ('backend/tests/test_ppr_fallback_guard.py', 'retrieval'),
    ('backend/tests/test_ppr_fallback_guard.py', 'scale_ppr'),
    ('backend/tests/test_query_hotpath_cache.py', '_connect'),
    ('backend/tests/test_query_hotpath_cache.py', '_elem_chunk_map'),
    ('backend/tests/test_query_hotpath_cache.py', '_ent_chunk_map'),
    ('backend/tests/test_query_hotpath_cache.py', '_kg_source_chunks'),
    ('backend/tests/test_query_hotpath_cache.py', '_runtime'),
    ('backend/tests/test_query_hotpath_cache.py', 'retrieval'),
    ('backend/tests/test_relation_ann.py', 'notebook_copy_stats'),
    ('backend/tests/test_relation_ann.py', 'retrieval'),
    ('backend/tests/test_relation_retrieval.py', '_IN_CHUNK'),
    ('backend/tests/test_relation_retrieval.py', '_embed_query'),
    ('backend/tests/test_relation_retrieval.py', '_mix_retrieve'),
    ('backend/tests/test_relation_retrieval.py', '_relations_with_names'),
    ('backend/tests/test_relation_retrieval.py', '_retrieve_relations_scored'),
    ('backend/tests/test_relation_retrieval.py', '_vector_matrix'),
    ('backend/tests/test_relation_retrieval.py', 'retrieval'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', '_retrieve_relations_scored'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', '_vector_matrix'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 'notebook_copy_stats'),
    ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 'retrieval'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_scale_combined_graph'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_scale_xlayer_bridge_edges'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_vector_matrix'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_delta_vector_matrix'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_active_kg_delta'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', '_scale_index'),
    ('backend/tests/test_scale_xlayer_bridge_delta.py', 'retrieval'),
}

# Task 22: the ask/answer/conversation/job/trace persistence moves to the
# runtime-owned AskStateStore; the facade keeps frozen-signature delegates, so
# only the trace-list helper loses BOTH of its facade-internal call sites
# (ask_job_detail and get_conversation now read the trace inside the store) —
# every other ask member keeps at least one facade site (mode engines, the
# cancel registry orchestration and the fail-open trace coordinator stay until
# Tasks 23/24).  The new store contract suite and the two appended
# delegation/scoping proofs consume the facade at fresh sites.
TASK22_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", "_read_ask_trace"),
} | {
    ("backend/tests/test_ask_state_store.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_runtime", "append_ask_trace",
        "create_notebook", "current_user",
    }
} | {
    ("backend/tests/test_ask_jobs.py", name)
    for name in {
        "_ask_cancel_events", "_runtime", "begin_ask_job", "current_user",
        "finish_ask_job", "get_conversation",
    }
} | {
    ("backend/tests/test_conversations.py", name)
    for name in {"_connect", "_runtime", "_write", "current_user"}
}

# Task 23: the cancel-event registry moves to the runtime-owned
# AskCancellationRegistry — the facade's frozen _ask_cancel_events/
# _ask_cancel_lock attributes become read-only compatibility properties over
# the SAME objects, so their facade-internal orchestration sites disappear
# from the facade file; the streaming execution orchestration moves to
# AskExecutionCoordinator, so routes.py stops consuming begin_ask_job/
# append_ask_trace/finish_ask_job (frozen fixture sites vanish from the
# scan — the cancel endpoint keeps cancel_ask_job/ask_job_detail).  The new
# coordinator contract suite and the appended reconnect proof consume the
# facade at fresh sites.
TASK23_ALLOWED_MEMBER_FILES = {
    ("backend/app/services/sqlite_repository.py", name)
    for name in {"_ask_cancel_events", "_ask_cancel_lock"}
} | {
    ("backend/app/api/routes.py", name)
    for name in {"append_ask_trace", "begin_ask_job", "finish_ask_job"}
} | {
    ("backend/tests/test_ask_execution_coordinator.py", name)
    for name in {
        "SQLiteRepository", "_ask_cancel_events", "_ask_cancel_lock",
        "_runtime", "begin_ask_job", "cancel_ask_job", "create_notebook",
        "current_user", "finish_ask_job",
    }
} | {
    ("backend/tests/test_ask_reconnect.py", name)
    for name in {
        "_runtime", "ask_job_status", "create_notebook", "current_user",
        "get_conversation",
    }
}

TASK7_COMPAT_PROPERTIES = {
    "_system_llm_client": True,
    "_reasoning_llm_client": True,
    "_rewrite_llm_client": True,
    "_kg_llm_client": True,
    "_system_rerank_client": True,
    "_user_model_cfg_cache": True,
    "_user_llm_clients": True,
    "_user_rerank_clients": True,
}

# Task 17: the facade's retrieval-cache handles become mutable write-through
# properties over the runtime-owned RetrievalSnapshotCache (the read-only
# fixture keeps their frozen instance_attribute kind).
TASK17_COMPAT_PROPERTIES = {
    "_vector_cache": True,
    "_unified_cache": True,
}

TASK20_COMPAT_PROPERTIES = {
    name: True
    for name in {
        "_auto_index_checked", "_scale_building", "_scale_building_lock",
        "_scale_idle_queue", "_scale_idx_cache", "_scale_idx_load_lock",
        "_scale_idx_load_locks", "_scale_scheduler_started", "_scale_ver_cache",
        "_scale_ver_lock", "_scale_ver_locks", "_viz_building",
        "_viz_building_lock", "_viz_idx_cache",
    }
}

# Task 23: the WS2a cancel-event registry has one owner (the runtime-held
# AskCancellationRegistry); the facade's frozen instance attributes become
# read-only compatibility handles over the registry's dict/lock objects (the
# frozen tests only ever read them, so no setter).
TASK23_COMPAT_PROPERTIES = {
    "_ask_cancel_events": False,
    "_ask_cancel_lock": False,
}

# Remediation Task 2 transfers the three mutable operational values from
# facade instance attributes to write-through properties over RepositoryRuntime.
REMEDIATION_TASK2_COMPAT_PROPERTIES = {
    "_notebook_langs_cache": True,
    "embedder": True,
    "storage_dir": True,
}

# Internal line numbers in this source file are intentionally not API surface:
# Task 3 adds the scale-profile construction/import and shifts later private
# implementation lines. Keep exact member+path coverage while normalizing only
# this known edited source path.
LINE_NUMBER_INSENSITIVE_FILES = {
    "backend/app/services/sqlite_repository.py",
    "backend/app/services/sqlite_notebook_sharing.py",
    "backend/app/services/sqlite_identity.py",
    "backend/app/services/background_jobs.py",
    "backend/app/services/report_engine.py",
    "backend/app/api/deps.py",
    "backend/app/api/auth_routes.py",
    "backend/app/api/routes.py",
    "backend/tests/test_architecture_module_boundaries.py",
    "backend/tests/test_repository_runtime.py",
    # Task 27 migrates every production caller onto ports / repo.maintenance —
    # these known edited caller files keep exact member+path coverage while
    # their internal line numbers stop being API surface.
    "backend/app/services/batch_ingest.py",
    "backend/app/eval/retrieval_metrics.py",
    "backend/app/scripts/backfill_relation_embeddings.py",
    "backend/app/scripts/gen_recall_gold.py",
    "backend/app/scripts/reembed_kg.py",
    "scripts/backfill_kg_embeddings.py",
    "scripts/build_chunks.py",
    "scripts/denoise_reextract_nb.py",
    "scripts/diag_base_report.py",
    "scripts/kg_product_smoke.py",
    "scripts/replay_retrieval.py",
    "scripts/smoke_backend.py",
    "backend/tests/test_sqlite_write_optimization.py",
}

ALL_TASK_ALLOWED_MEMBER_FILES = (
    TASK2_ALLOWED_MEMBER_FILES
    | TASK4_ALLOWED_MEMBER_FILES
    | TASK5_ALLOWED_MEMBER_FILES
    | TASK6_ALLOWED_MEMBER_FILES
    | TASK7_ALLOWED_MEMBER_FILES
    | TASK8_ALLOWED_MEMBER_FILES
    | TASK9_ALLOWED_MEMBER_FILES
    | TASK10_ALLOWED_MEMBER_FILES
    | TASK11_ALLOWED_MEMBER_FILES
    | TASK12_ALLOWED_MEMBER_FILES
    | TASK13_ALLOWED_MEMBER_FILES
    | TASK14_ALLOWED_MEMBER_FILES
    | TASK15_ALLOWED_MEMBER_FILES
    | TASK16_ALLOWED_MEMBER_FILES
    | TASK17_ALLOWED_MEMBER_FILES
    | TASK18_ALLOWED_MEMBER_FILES
    | TASK19_ALLOWED_MEMBER_FILES
    | TASK20_ALLOWED_MEMBER_FILES
    | TASK21_ALLOWED_MEMBER_FILES
    | TASK22_ALLOWED_MEMBER_FILES
    | TASK23_ALLOWED_MEMBER_FILES
    | TASK24_ALLOWED_MEMBER_FILES
    | TASK25_ALLOWED_MEMBER_FILES
    | TASK26_ALLOWED_MEMBER_FILES
    | TASK27_ALLOWED_MEMBER_FILES
)

# Broad member+file allowances are safe for tests and the three deliberately
# transitional compatibility facades only.  Every other production consumer
# must match one exact current site so adding a fresh facade dependency in the
# same file cannot disappear behind an old task allowance.
LEGACY_COMPATIBILITY_MEMBER_ALLOWLIST_FILES = {
    "backend/app/services/sqlite_repository.py",
    "backend/app/services/sqlite_identity.py",
    "backend/app/services/sqlite_notebook_sharing.py",
}
ACTIVE_PRODUCTION_MEMBER_SITES = {
    ("SQLiteRepository", "backend/app/api/deps.py:12"),
    ("_runtime", "backend/app/api/deps.py:20"),
    ("_runtime", "backend/app/api/deps.py:23"),
    ("_runtime", "backend/app/api/deps.py:26"),
    ("_runtime", "backend/app/api/deps.py:29"),
    ("_runtime", "backend/app/api/deps.py:32"),
    ("resolve_session", "backend/app/api/deps.py:58"),
    ("current_user", "backend/app/api/deps.py:62"),
    ("upload_sources", "backend/app/eval/speed.py:80"),
    ("parse_source", "backend/app/eval/speed.py:82"),
    ("delete_notebook", "backend/app/eval/speed.py:90"),
    ("SQLiteRepository", "backend/app/eval/speed.py:98"),
    ("llm_client", "backend/app/eval/speed.py:102"),
    ("create_notebook", "backend/app/eval/speed.py:109"),
    ("extract_source", "backend/app/eval/speed.py:114"),
    ("_runtime", "backend/app/services/communities.py:28"),
    ("_runtime", "backend/app/services/communities.py:34"),
    ("event_log", "backend/app/services/communities.py:40"),
    ("event_log", "backend/app/services/communities.py:45"),
    ("_runtime", "backend/app/services/communities.py:77"),
    ("settings", "backend/app/services/communities.py:78"),
    ("_runtime", "backend/app/services/communities.py:86"),
    ("_runtime", "backend/app/services/communities.py:92"),
    ("event_log", "backend/app/services/communities.py:98"),
    ("event_log", "backend/app/services/communities.py:103"),
    ("_runtime", "backend/app/services/communities.py:135"),
    ("settings", "backend/app/services/communities.py:136"),
    ("_runtime", "backend/app/services/communities.py:87"),
    ("_runtime", "backend/app/services/communities.py:93"),
    ("event_log", "backend/app/services/communities.py:99"),
    ("event_log", "backend/app/services/communities.py:104"),
    ("_runtime", "backend/app/services/communities.py:136"),
    ("settings", "backend/app/services/communities.py:137"),
    ("retrieval", "backend/app/services/reasoning_retrieval.py:131"),
    ("_runtime", "backend/app/services/reasoning_retrieval.py:132"),
    ("_runtime", "backend/app/services/reasoning_retrieval.py:135"),
    ("_runtime", "backend/app/services/reasoning_retrieval.py:136"),
    ("event_log", "backend/app/services/reasoning_retrieval.py:137"),
    # Task 23: _stream_ask_events reaches the runtime-owned
    # AskExecutionCoordinator through the repo it is handed (frozen signature).
    ("_runtime", "backend/app/api/routes.py:605"),
}
REVIEW_FIX_ALLOWED_CONSUMERS = {
    ("_runtime", "backend/tests/test_notebook_share_copy.py:431"),
    ("share_notebook", "backend/tests/test_notebook_share_copy.py:441"),
    (
        "find_notebook_by_share_token",
        "backend/tests/test_notebook_share_copy.py:445",
    ),
    # c9ddf31 single-owner cache follow-up：_loader_spy 探针座从 repo._connect 迁到
    # runtime.database.connect（cluster_map loader 实际经过的连接边界）。旧 _connect
    # 站点留在冻结 fixture、新 _runtime 站点是等行数替换后的现役座。
    ("_connect", "backend/tests/test_incremental_fuse_perf.py:110"),
    ("_connect", "backend/tests/test_incremental_fuse_perf.py:134"),
    ("_runtime", "backend/tests/test_incremental_fuse_perf.py:110"),
    ("_runtime", "backend/tests/test_incremental_fuse_perf.py:134"),
}

FROZEN_ONLY_MOVED_CONSUMERS = {
    # Task 1 adds ownership imports above this test module's own facade import.
    ("SQLiteRepository", "backend/tests/test_repository_surface_manifest.py:13"),
    ("_augment_notebook_meta", "backend/app/services/sqlite_repository.py:<line>"),
    ("_cleanup_empty_conversation", "backend/app/services/sqlite_repository.py:<line>"),
    ("_edge_support_map", "backend/app/services/sqlite_repository.py:<line>"),
    ("_embed_query", "backend/app/services/sqlite_repository.py:<line>"),
    ("_enrich_evidence", "backend/app/services/sqlite_repository.py:<line>"),
    ("_fold_hits_to_canonical", "backend/app/services/sqlite_repository.py:<line>"),
    ("_has_kg", "backend/app/services/sqlite_repository.py:<line>"),
    ("_hydrate_search_hits", "backend/app/services/sqlite_repository.py:<line>"),
    ("_kg_headline", "backend/app/services/sqlite_repository.py:<line>"),
    ("_knowledge_record", "backend/app/services/sqlite_repository.py:<line>"),
    ("_semantic_search", "backend/app/services/sqlite_repository.py:<line>"),
    ("_should_extract_kg", "backend/app/services/sqlite_repository.py:<line>"),
    ("_unified_graph_full", "backend/app/services/sqlite_repository.py:<line>"),
    ("_vector_cache", "backend/app/services/sqlite_repository.py:<line>"),
    ("ask_job_status", "backend/app/services/sqlite_repository.py:<line>"),
    ("effective_schemas", "backend/app/services/sqlite_repository.py:<line>"),
    ("scale_index_status", "backend/tests/test_pending_actions.py:104"),
    ("scale_index_status", "backend/tests/test_pending_actions.py:116"),
    ("ask_chunk", "backend/tests/test_ask_modes.py:24"),
    ("ask_graph", "backend/tests/test_ask_modes.py:24"),
    ("ask_reasoning", "backend/tests/test_ask_modes.py:24"),
    ("ask_chunk", "backend/tests/test_chunk_retrieval.py:240"),
}


def _normalize_consumer_site(site: str) -> str:
    path = site.rsplit(":", 1)[0]
    return f"{path}:<line>" if path in LINE_NUMBER_INSENSITIVE_FILES else site


def _member_file_site_allowed(name: str, site: str, *, frozen: bool) -> bool:
    path = site.rsplit(":", 1)[0]
    broad_match = (path, name) in ALL_TASK_ALLOWED_MEMBER_FILES
    if frozen:
        return broad_match
    if path.startswith("backend/tests/") or path in LEGACY_COMPATIBILITY_MEMBER_ALLOWLIST_FILES:
        return broad_match
    return (name, site) in ACTIVE_PRODUCTION_MEMBER_SITES

CONSUMER_ROOTS = (
    ROOT / "backend" / "app" / "api",
    ROOT / "backend" / "app" / "main.py",
    ROOT / "backend" / "app" / "services",
    ROOT / "backend" / "app" / "eval",
    ROOT / "backend" / "app" / "scripts",
    ROOT / "scripts",
    ROOT / "backend" / "tests",
)


def _consumer_files():
    for root in CONSUMER_ROOTS:
        paths = [root] if root.is_file() else root.rglob("*.py")
        for path in paths:
            if path == GENERATOR or "__pycache__" in path.parts:
                continue
            yield path


def _dotted_name(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _literal_strings(node: ast.AST, loop_values: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value.rsplit(".", 1)[-1],)
    if isinstance(node, ast.Name):
        return loop_values.get(node.id, ())
    return ()


def _static_repository_patches() -> set[tuple[str, int, str, str]]:
    """Independently scan the patch patterns used by the current test suite."""
    class_names = {
        name
        for cls in SQLiteRepository.__mro__[:-1]
        for name, value in cls.__dict__.items()
        if isinstance(value, (property, staticmethod, classmethod))
        or inspect.isfunction(value)
        or not name.startswith("__")
    }
    module_names = {
        name
        for name, module in COMPATIBILITY_EXPORTS.items()
        if module == "app.services.sqlite_repository"
    }
    found: set[tuple[str, int, str, str]] = set()

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        if not relative.startswith("backend/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        module_aliases = {"sqlite_repository"}
        class_aliases = {"SQLiteRepository"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.sqlite_repository":
                        module_aliases.add(alias.asname or "sqlite_repository")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "app.services":
                    module_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "sqlite_repository"
                    )
                elif node.module == "app.services.sqlite_repository":
                    class_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "SQLiteRepository"
                    )

        helper_targets: dict[str, tuple[int, int, str]] = {}
        for fn in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
            params = [arg.arg for arg in fn.args.args]
            for call in (node for node in ast.walk(fn) if isinstance(node, ast.Call)):
                if not _dotted_name(call.func).endswith(("monkeypatch.setattr", "patch.object")):
                    continue
                if len(call.args) < 2 or not isinstance(call.args[1], ast.Name):
                    continue
                if call.args[1].id not in params:
                    continue
                base = _dotted_name(call.args[0])
                if not re.fullmatch(
                    r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)", base
                ):
                    continue
                helper_targets[fn.name] = (params.index(call.args[1].id), call.lineno, base)

        helper_values: dict[str, set[str]] = {name: set() for name in helper_targets}
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            helper = helper_targets.get(_dotted_name(call.func))
            if helper is None:
                continue
            target_index, _line, _base = helper
            if target_index < len(call.args):
                helper_values[_dotted_name(call.func)].update(
                    _literal_strings(call.args[target_index], {})
                )
        for helper_name, values in helper_values.items():
            _index, line, base = helper_targets[helper_name]
            for target in values:
                if target in class_names:
                    found.add((relative, line, target, base))

        def visit(node: ast.AST, loop_values: dict[str, tuple[str, ...]]) -> None:
            local_values = loop_values
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                values = tuple(
                    item.value
                    for item in getattr(node.iter, "elts", ())
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                if values:
                    local_values = {**loop_values, node.target.id: values}
            if isinstance(node, ast.Call) and len(node.args) >= 2:
                call_name = _dotted_name(node.func)
                if call_name.endswith(("monkeypatch.setattr", "patch.object")):
                    base = _dotted_name(node.args[0])
                    targets = _literal_strings(node.args[1], local_values)
                    direct_repo = bool(
                        re.fullmatch(
                            r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)",
                            base,
                        )
                    )
                    repository_class = base in class_aliases or base.endswith("repo.__class__")
                    compatibility_module = base in module_aliases
                    for target in targets:
                        if (
                            (direct_repo or repository_class) and target in class_names
                        ) or (compatibility_module and target in module_names):
                            found.add((relative, node.lineno, target, base))
            for child in ast.iter_child_nodes(node):
                visit(child, local_values)

        visit(tree, {})
    return found


def _static_repository_consumers() -> dict[str, set[str]]:
    """Reverse-scan current import, attribute, registry, and patch patterns."""
    class_names = {
        name
        for cls in SQLiteRepository.__mro__[:-1]
        for name, value in cls.__dict__.items()
        if isinstance(value, (property, staticmethod, classmethod))
        or inspect.isfunction(value)
        or not name.startswith("__")
    }
    source_tree = ast.parse(
        (ROOT / "backend/app/services/sqlite_repository.py").read_text(
            encoding="utf-8"
        )
    )
    instance_names = {
        node.targets[0].attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "self"
    }
    instance_names |= {
        node.target.attr
        for node in ast.walk(source_tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.target.value, ast.Name)
        and node.target.value.id == "self"
    }
    facade_names = class_names | instance_names
    module_names = set(COMPATIBILITY_EXPORTS)
    consumers: dict[str, set[str]] = defaultdict(set)
    facade_classes = {
        "SQLiteRepository",
        "SQLiteIdentityMixin",
        "SQLiteNotebookSharingMixin",
    }
    repo_pattern = re.compile(
        r"(?:[A-Za-z_]\w*\.)?(?:repo|[A-Za-z_]\w*_repo)"
    )

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        module_aliases = {"sqlite_repository"}
        class_aliases = {"SQLiteRepository"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.services.sqlite_repository":
                        module_aliases.add(alias.asname or "sqlite_repository")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "app.services":
                    module_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "sqlite_repository"
                    )
                elif node.module == "app.services.sqlite_repository":
                    class_aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "SQLiteRepository"
                    )
        repo_variables = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Call)
            and (
                _dotted_name(node.value.func) in class_aliases
                or _dotted_name(node.value.func).rsplit(".", 1)[-1] == "repository"
            )
        }

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "app.services.repository",
                "app.services.sqlite_repository",
            }:
                continue
            for alias in node.names:
                if COMPATIBILITY_EXPORTS.get(alias.name) == node.module:
                    consumers[alias.name].add(f"{relative}:{node.lineno}")

        class ConsumerVisitor(ast.NodeVisitor):
            def __init__(self):
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node):
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def visit_Attribute(self, node):
                dotted = _dotted_name(node.value)
                if dotted in module_aliases and node.attr in module_names:
                    consumers[node.attr].add(f"{relative}:{node.lineno}")
                elif node.attr in facade_names:
                    facade_base = (
                        bool(repo_pattern.fullmatch(dotted))
                        or dotted in class_aliases
                        or dotted in repo_variables
                        or dotted.endswith("repo.__class__")
                        or (
                            isinstance(node.value, ast.Call)
                            and _dotted_name(node.value.func).rsplit(".", 1)[-1]
                            == "repository"
                        )
                        or (
                            isinstance(node.value, ast.Name)
                            and node.value.id == "self"
                            and bool(self.class_stack)
                            and self.class_stack[-1] in facade_classes
                        )
                    )
                    if facade_base:
                        consumers[node.attr].add(f"{relative}:{node.lineno}")
                self.generic_visit(node)

        ConsumerVisitor().visit(tree)

    for name, module in COMPATIBILITY_EXPORTS.items():
        consumers[name].add(f"compatibility:{module}")
    for handler in ("ask_chunk", "ask_reasoning", "ask_graph"):
        consumers[handler].add("ASK_MODES[*].handler")
    for file, line, target, _base in _static_repository_patches():
        consumers[target].add(f"{file}:{line}")
    return {name: sites for name, sites in consumers.items() if sites}


def _surface() -> dict[str, dict[str, object]]:
    assert FIXTURE.is_file(), f"missing frozen facade surface: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_facade_surface_manifest_is_complete_and_owned():
    surface = _surface()

    assert surface
    assert {"create_notebook", "ask", "ask_chunk", "llm_client"} <= set(surface)
    for name, record in surface.items():
        assert REQUIRED_MEMBER_FIELDS <= set(record), name
        assert record["kind"] in {
            "method",
            "private_wrapper",
            "property",
            "mutable_property",
            "instance_attribute",
            "constant",
        }
        assert isinstance(record["signature"], str)
        assert record["consumers"], name
        assert isinstance(record["owner"], str) and record["owner"], name
        assert isinstance(record["patch_targets"], list), name


def test_every_patch_target_has_a_migration_record():
    patch_targets = [
        patch
        for record in _surface().values()
        for patch in record["patch_targets"]
    ]

    assert patch_targets
    for patch in patch_targets:
        assert set(patch) == {
            "base",
            "file",
            "line",
            "target",
            "compatibility",
        }
        assert patch["file"].startswith("backend/tests/")
        assert isinstance(patch["line"], int) and patch["line"] > 0
        assert patch["target"]
        assert patch["base"]
        assert patch["compatibility"] in {
            "production-compatible",
            "test-only",
        }


def test_compatibility_exports_and_import_consumers_are_complete():
    surface = _surface()

    for name, module in COMPATIBILITY_EXPORTS.items():
        assert name in surface, name
        record = surface[name]
        assert record["scope"] == "module", name
        assert record["modules"] == [module], name

    expected_module_names = {
        name for name, record in surface.items() if record.get("scope") == "module"
    }
    assert expected_module_names == set(COMPATIBILITY_EXPORTS)

    for path in _consumer_files():
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in {
                "app.services.repository",
                "app.services.sqlite_repository",
            }:
                continue
            for alias in node.names:
                assert COMPATIBILITY_EXPORTS.get(alias.name) == node.module, (
                    relative,
                    node.lineno,
                    node.module,
                    alias.name,
                )
                site = (relative, node.lineno, node.module, alias.name)
                assert (
                    f"{relative}:{node.lineno}" in surface[alias.name]["consumers"]
                    or site in TASK2_ALLOWED_IMPORTS
                    or site in TASK4_ALLOWED_IMPORTS
                    or site in TASK7_ALLOWED_IMPORTS
                    or site in TASK8_ALLOWED_IMPORTS
                    or site in TASK9_ALLOWED_IMPORTS
                    or site in TASK10_ALLOWED_IMPORTS
                    or site in TASK11_ALLOWED_IMPORTS
                    or site in TASK12_ALLOWED_IMPORTS
                    or site in TASK13_ALLOWED_IMPORTS
                    or site in TASK14_ALLOWED_IMPORTS
                    or site in TASK15_ALLOWED_IMPORTS
                    or site in TASK16_ALLOWED_IMPORTS
                    or site in TASK17_ALLOWED_IMPORTS
                    or site in TASK18_ALLOWED_IMPORTS
                    or site in TASK19_ALLOWED_IMPORTS
                    or site in TASK20_ALLOWED_IMPORTS
                    or site in TASK22_ALLOWED_IMPORTS
                    or site in TASK23_ALLOWED_IMPORTS
                    or site in TASK24_ALLOWED_IMPORTS
                    or site in TASK25_ALLOWED_IMPORTS
                    or site in TASK26_ALLOWED_IMPORTS
                    or site in TASK27_ALLOWED_IMPORTS
                    or site in TASK28_ALLOWED_IMPORTS
                )


EXPECTED_PATCH_DELTAS = {
    'recorded_only': {
        ('backend/tests/test_ask_modes.py', 24, 'ask_chunk', 'repo'),
        ('backend/tests/test_ask_modes.py', 24, 'ask_graph', 'repo'),
        ('backend/tests/test_ask_modes.py', 24, 'ask_reasoning', 'repo'),
        ('backend/tests/test_chunk_retrieval.py', 240, 'ask_chunk', 'repo'),
        ('backend/tests/test_pending_actions.py', 104, 'scale_index_status', 'repo'),
        ('backend/tests/test_pending_actions.py', 116, 'scale_index_status', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 29, '_concept_cluster_id', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 30, 'node_context', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 44, '_concept_cluster_id', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 45, 'node_context', 'repo'),
        ('backend/tests/test_architecture_hardening.py', 55, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_architecture_hardening.py', 102, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ask_vector_matrix.py', 125, '_backfill_knowledge_embeddings', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 55, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 67, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 81, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 86, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 99, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 118, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 132, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 144, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 162, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 193, 'trigger_scale_index_rebuild', 'repo'),
        ('backend/tests/test_auto_scale_index.py', 242, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_batch_ingest.py', 219, '_run_extraction', 'SQLiteRepository'),
        ('backend/tests/test_batch_ingest.py', 247, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 248, '_set_source_status', 'repo'),
        ('backend/tests/test_batch_ingest.py', 287, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 288, '_set_source_status', 'repo'),
        ('backend/tests/test_batch_ingest.py', 290, 'relink_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 310, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 311, '_set_source_status', 'repo'),
        ('backend/tests/test_batch_ingest.py', 313, 'relink_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 462, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 489, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1211, '_run_extraction', 'repo'),
        ('backend/tests/test_bm25_rrf.py', 135, '_rrf_scored', 'repo'),
        ('backend/tests/test_bm25_rrf.py', 150, '_rrf_scored', 'repo'),
        ('backend/tests/test_chunk_bruteforce_guard.py', 73, '_gather_chunks', 'repo'),
        ('backend/tests/test_chunk_bruteforce_guard.py', 134, '_embed_query', 'repo'),
        ('backend/tests/test_chunk_embed.py', 99, '_run_extraction', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 124, '_mix_retrieve', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 125, '_retrieve_chunks_multi', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 126, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 194, '_retrieve_chunks_multi', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 224, '_mmr_select_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 307, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 326, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 337, '_retrieve_chunks_fts_degraded', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 346, '_gather_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 381, '_retrieve_chunks_ann', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 390, '_gather_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 451, '_mix_retrieve', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 501, '_mix_retrieve', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 525, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 527, '_mmr_select_chunks', 'repo'),
        ('backend/tests/test_chunk_retrieval_characterization.py', 575, '_mix_retrieve', 'repo'),
        ('backend/tests/test_chunk_retrieval_plan.py', 61, '_notebook_has_kg', 'repo'),
        ('backend/tests/test_chunk_retrieval_plan.py', 62, '_any_base_notebook_has_kg', 'repo'),
        ('backend/tests/test_chunk_retrieval_plan.py', 71, '_notebook_has_kg', 'repo'),
        ('backend/tests/test_chunk_retrieval_plan.py', 72, '_any_base_notebook_has_kg', 'repo'),
        ('backend/tests/test_dedup_scale.py', 32, '_gather_elements', 'repo'),
        ('backend/tests/test_graph_k_binding.py', 130, '_retrieve_scored', 'repo'),
        ('backend/tests/test_incremental_fuse_perf.py', 134, '_connect', 'repo'),
        ('backend/tests/test_index_build_consolidation.py', 30, '_spawn_viz_build', 'repo'),
        ('backend/tests/test_indexed_only_principle.py', 235, '_vector_matrix', 'repo'),
        ('backend/tests/test_indexed_only_principle.py', 325, '_IN_CHUNK', 'SQLiteRepository'),
        ('backend/tests/test_kg_building_flag.py', 67, 'delete_notebook_kg', 'repo'),
        ('backend/tests/test_kg_llm_client.py', 53, '_source_raw_text', 'repo'),
        ('backend/tests/test_kg_quality.py', 59, 'cluster_map', 'repo'),
        ('backend/tests/test_kg_relink_repository.py', 188, '_run_extraction', 'repo'),
        ('backend/tests/test_kg_repository.py', 380, '_run_extraction', 'repo'),
        ('backend/tests/test_language_policy.py', 289, '_keyword_chunk_candidates', 'repo'),
        ('backend/tests/test_large_lib_index_required.py', 46, '_gather_chunks', 'repo'),
        ('backend/tests/test_notebook_counts_batched.py', 115, '_connect', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 63, 'federated_retrieve', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 63, 'federated_retrieve_relations', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 71, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 93, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_p4_kg_shrink.py', 82, '_run_extraction', 'repo'),
        ('backend/tests/test_p4_kg_shrink.py', 96, '_run_extraction', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 108, 'scale_ppr', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 109, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 118, '_ppr_graph', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 135, 'scale_ppr', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 136, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 145, '_ppr_graph', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 162, 'scale_ppr', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 165, '_ppr_graph', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 200, '_embed_query', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 201, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 234, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 235, '_open_scale_ann', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 239, '_vector_matrix', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 277, '_federated_rx_graph', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 296, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 334, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ppr_fallback_guard.py', 372, '_vector_matrix', 'repo'),
        ('backend/tests/test_query_hotpath_cache.py', 168, '_connect', 'repo'),
        ('backend/tests/test_rebuild_cache.py', 69, '_stream_seed_reps', 'repo'),
        ('backend/tests/test_rebuild_checkpoint.py', 224, '_write_cluster_map_streamed', 'repo'),
        ('backend/tests/test_rebuild_checkpoint.py', 243, '_write_cluster_map_streamed', 'repo'),
        ('backend/tests/test_rebuild_communities.py', 195, '_scale_index', 'repo'),
        ('backend/tests/test_rebuild_communities.py', 206, '_scale_index', 'repo'),
        ('backend/tests/test_rebuild_wires_communities.py', 47, 'rebuild_communities', 'repo'),
        ('backend/tests/test_rebuild_wires_communities.py', 60, 'rebuild_communities', 'repo'),
        ('backend/tests/test_relation_ann.py', 382, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_relation_ann.py', 412, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_relation_retrieval.py', 267, '_relations_with_names', 'repo'),
        ('backend/tests/test_relation_retrieval.py', 291, '_IN_CHUNK', 'repo'),
        ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 69, '_vector_matrix', 'repo'),
        ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 78, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 98, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 122, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_relation_scoring_cold_matrix_guard.py', 139, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_report_engine.py', 155, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 182, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 322, '_retrieve_neighbors', 'eng.repo'),
        ('backend/tests/test_report_engine.py', 476, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 477, '_ppr_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 511, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 544, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 562, 'federated_retrieve', 'repo'),
        ('backend/tests/test_report_engine.py', 598, 'get_report', 'repo'),
        ('backend/tests/test_resolve_notebook_conflicts.py', 303, 'resolve_notebook_conflicts', 'repo'),
        ('backend/tests/test_resolve_notebook_conflicts.py', 309, '_run_extraction', 'repo'),
        ('backend/tests/test_resolve_notebook_conflicts.py', 326, 'resolve_notebook_conflicts', 'repo'),
        ('backend/tests/test_resolve_notebook_conflicts.py', 331, '_run_extraction', 'repo'),
        ('backend/tests/test_scale_delta_policy.py', 104, '_ensure_scale_scheduler', 'repo'),
        ('backend/tests/test_scale_delta_policy.py', 115, '_ensure_scale_scheduler', 'repo'),
        ('backend/tests/test_scale_delta_policy.py', 127, '_ensure_scale_scheduler', 'repo'),
        ('backend/tests/test_scale_idx_disk_cache.py', 190, '_index_delta', 'repo'),
        ('backend/tests/test_scale_index_repo.py', 871, 'fold_scale_index_delta', 'repo'),
        ('backend/tests/test_scale_index_repo.py', 872, 'build_scale_index', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 188, '_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 221, '_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 222, '_delta_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 248, '_scale_xlayer_bridge_edges', 'repo'),
        ('backend/tests/test_sources_page_batched.py', 183, '_connect', 'repo'),
        ('backend/tests/test_sqlite_write_optimization.py', 121, '_write', 'embed_repo'),
        ('backend/tests/test_viz_bounded.py', 118, '_unified_graph_full', 'repo'),
    },
    'actual_only': {
        ('backend/tests/test_embedding_store_component.py', 59, '_write', 'repo'),
        ('backend/tests/test_embedding_store_component.py', 135, '_write', 'repo'),
        ('backend/tests/test_knowledge_governance_delegation.py', 131, 'set_conflict_status', 'repo'),
        ('backend/tests/test_notebook_copy_service.py', 115, '_new_id', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 140, '_COPY_CHUNK', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 168, '_insert_row', 'repo'),
        ('backend/tests/test_notebook_copy_service.py', 189, '_COPY_CHUNK', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 199, '_insert_row', 'repo'),
        ('backend/tests/test_repository_runtime.py', 19, '_now', 'sqlite_repository'),
        ('backend/tests/test_schema_registry_service.py', 157, 'llm_client', 'repo'),
        ('backend/tests/test_schema_registry_service.py', 177, 'llm_client', 'repo'),
        ('backend/tests/test_schema_registry_service.py', 180, 'llm_client', 'repo'),
        ('backend/tests/test_schema_registry_service.py', 187, 'llm_client', 'repo'),
        ('backend/tests/test_source_chunking_service.py', 103, '_new_id', 'sqlite_repository'),
        ('backend/tests/test_source_chunking_service.py', 119, '_mark_unified_kg_dirty', 'repo'),
        ('backend/tests/test_source_ingestion_failure_boundaries.py', 80, 'parse_source_file', 'facade_mod'),
        ('backend/tests/test_source_ingestion_service.py', 155, 'parse_source_file', 'facade_mod'),
        ('backend/tests/test_source_ingestion_service.py', 253, 'parse_source_file', 'facade_mod'),
        ('backend/tests/test_source_ingestion_service.py', 332, 'parse_source_file', 'facade_mod'),
        ('backend/tests/test_sqlite_write_optimization.py', 136, '_write', 'embed_repo'),
    },
}


def test_static_repository_patch_scan_matches_manifest_exactly():
    recorded = {
        (patch["file"], patch["line"], patch["target"], patch["base"])
        for record in _surface().values()
        for patch in record["patch_targets"]
    }

    actual = _static_repository_patches()
    assert recorded - actual == EXPECTED_PATCH_DELTAS["recorded_only"]
    assert actual - recorded == EXPECTED_PATCH_DELTAS["actual_only"]
    assert (
        "backend/tests/test_scale_index_repo.py",
        1094,
        "__init__",
        "hnswlib.Index",
    ) not in recorded


def test_static_repository_consumer_scan_matches_manifest_exactly():
    recorded = {
        name: set(record["consumers"]) for name, record in _surface().items()
    }
    # The concrete test-only insertion helper remains in SQLiteRepository for
    # compatibility, but Task 2 intentionally removes it from production
    # Protocol consumers.
    recorded.pop("eval_insert_source_for_test", None)

    actual = _static_repository_consumers()
    allowed_sites = {
        (member, f"{file}:{line}")
        for file, line, _module, member in TASK2_ALLOWED_IMPORTS | TASK7_ALLOWED_IMPORTS | TASK8_ALLOWED_IMPORTS | TASK9_ALLOWED_IMPORTS | TASK23_ALLOWED_IMPORTS | TASK26_ALLOWED_IMPORTS | TASK27_ALLOWED_IMPORTS | TASK28_ALLOWED_IMPORTS
    }
    allowed_sites |= TASK2_ALLOWED_CONSUMERS | TASK7_ALLOWED_CONSUMERS | TASK8_ALLOWED_CONSUMERS | TASK9_ALLOWED_CONSUMERS | TASK12_ALLOWED_CONSUMERS | TASK27_ALLOWED_CONSUMERS | TASK28_ALLOWED_CONSUMERS | REVIEW_FIX_ALLOWED_CONSUMERS
    for name, sites in list(actual.items()):
        actual[name] = {
            site for site in sites
                if (name, site) not in allowed_sites
                    and not _member_file_site_allowed(name, site, frozen=False)
        }
    for name, sites in list(recorded.items()):
        recorded[name] = {
            site for site in sites
                if (name, site) not in allowed_sites | FROZEN_ONLY_MOVED_CONSUMERS
                    and (name, _normalize_consumer_site(site))
                        not in FROZEN_ONLY_MOVED_CONSUMERS
                    and not _member_file_site_allowed(name, site, frozen=True)
        }
    actual = {name: sites for name, sites in actual.items() if sites}
    recorded = {name: sites for name, sites in recorded.items() if sites}
    actual = {
        name: {_normalize_consumer_site(site) for site in sites}
        for name, sites in actual.items()
    }
    recorded = {
        name: {_normalize_consumer_site(site) for site in sites}
        for name, sites in recorded.items()
    }
    for name in TASK3_ALLOWED_NEW_MEMBERS | MASTER_V10_ALLOWED_NEW_MEMBERS:
        actual.pop(name, None)
        recorded.pop(name, None)
    for name in TASK7_ALLOWED_NEW_MEMBERS | TASK12_ALLOWED_NEW_MEMBERS:
        actual.pop(name, None)
        recorded.pop(name, None)
    for name in TASK25_ALLOWED_NEW_MEMBERS | TASK27_ALLOWED_NEW_MEMBERS:
        actual.pop(name, None)
        recorded.pop(name, None)
    assert recorded == actual


def test_ambiguous_surface_members_have_explicit_owners():
    surface = _surface()

    for name, owner in EXPLICIT_OWNERS.items():
        assert surface[name]["owner"] == owner, name


def test_ownership_manifest_validates_delegate_evidence():
    assert MODULE_SURFACE_OWNER_EXCEPTIONS == {
        name for name, record in _surface().items() if record.get("scope") == "module"
    }
    delegates = facade_delegate_evidence(SQLiteRepository, OWNER_BY_MEMBER)
    mismatch_sites = manifest_delegate_mismatches(SQLiteRepository, OWNER_BY_MEMBER)
    mismatch_names = {site[2].split(":", 1)[0] for site in mismatch_sites}
    subjects = facade_contract_subject_names(
        SQLiteRepository, OWNER_BY_MEMBER
    )
    assert subjects == (
        set(OWNER_BY_MEMBER)
        - MODULE_SURFACE_OWNER_EXCEPTIONS
        - NON_CALLABLE_INSTANCE_SURFACE
        - OWNER_CONTRACT_EXCEPTIONS
    )
    assert set(delegates) | mismatch_names == subjects

    matching_delegates = {
        name: owner
        for name, owner in delegates.items()
        if OWNER_BY_MEMBER[name] == owner
    }
    assert validate_ownership_manifest(delegates=matching_delegates) == OWNER_BY_MEMBER
    if delegates != matching_delegates:
        with pytest.raises(ValueError, match="ownership/delegate mismatch"):
            validate_ownership_manifest(delegates=delegates)
    with pytest.raises(ValueError, match="ownership/delegate mismatch"):
        validate_ownership_manifest(delegates={"ask": "WrongOwner"})


def test_member_file_allowlist_does_not_hide_new_production_sites():
    assert _member_file_site_allowed(
        "_runtime", "backend/app/services/communities.py:28", frozen=False
    )
    assert not _member_file_site_allowed(
        "_runtime", "backend/app/services/communities.py:9999", frozen=False
    )


def test_frozen_members_still_exist_with_the_same_callable_signatures():
    for name, record in _surface().items():
        kind = record["kind"]
        if record.get("scope") == "module":
            module = (
                repository
                if record["modules"] == ["app.services.repository"]
                else sqlite_repository
            )
            member = getattr(module, name)
            if kind == "constant":
                continue
            assert callable(member), name
            signature = str(inspect.signature(member))
            if name in {"set_request_user", "reset_request_user"}:
                continue
            assert signature == record["signature"], name
            continue
        if kind == "constant":
            assert hasattr(SQLiteRepository, name), name
            continue
        if name in {"db_path", "_write_lock"}:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property) and member.fset is not None, name
            continue
        if name in TASK7_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (member.fset is not None) is TASK7_COMPAT_PROPERTIES[name], name
            continue
        if name in TASK17_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (member.fset is not None) is TASK17_COMPAT_PROPERTIES[name], name
            continue
        if name in TASK20_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (member.fset is not None) is TASK20_COMPAT_PROPERTIES[name], name
            continue
        if name in TASK23_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (member.fset is not None) is TASK23_COMPAT_PROPERTIES[name], name
            continue
        if name in REMEDIATION_TASK2_COMPAT_PROPERTIES:
            member = inspect.getattr_static(SQLiteRepository, name)
            assert isinstance(member, property), name
            assert (
                member.fset is not None
            ) is REMEDIATION_TASK2_COMPAT_PROPERTIES[name], name
            continue
        if kind in {"instance_attribute", "mutable_property"} and not hasattr(
            SQLiteRepository, name
        ):
            continue

        member = inspect.getattr_static(SQLiteRepository, name)
        if kind == "property":
            assert isinstance(member, property) and member.fset is None, name
            signature = str(inspect.signature(member.fget))
        elif kind == "mutable_property":
            assert isinstance(member, property) and member.fset is not None, name
            signature = str(inspect.signature(member.fget))
        else:
            assert callable(member), name
            signature = str(inspect.signature(member))
        assert signature == record["signature"], name


def test_generator_exposes_the_frozen_public_callable_set():
    assert GENERATOR.is_file(), f"missing fixture generator: {GENERATOR}"
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"), filename=str(GENERATOR))
    callables = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert REQUIRED_GENERATOR_CALLABLES <= callables


def test_snapshot_generator_annotation_resolves_to_the_facade_type():
    spec = importlib.util.spec_from_file_location("repository_fixture_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    hints = typing.get_type_hints(module.normalized_repository_snapshot)
    assert hints == {
        "repo": SQLiteRepository,
        "notebook_id": str,
        "return": dict[str, object],
    }
