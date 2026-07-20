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
    # MinerU embedded-image-retention Task 8 adds two facade-module imports
    # (AssetService, make_persist_image_factory) above this compatibility
    # re-export, shifting it from line 116 to 118.
    ("backend/app/services/sqlite_repository.py", 118, "app.services.repository", "UploadedSourceFile"),
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
    # Line shifted 670->675 by Task 1 (memory-kg-extract)'s comments, then
    # 675->677 by Task 5's two-line comment expansions on the same
    # verify_repository_snapshot.py line-number allowlist entries below it,
    # then ->690 by later manifest-version comment growth, then ->731
    # by the merge_dbs INDEPENDENT_SQL_SITES / SQLITE_CONNECT_SITES additions
    # in that same file, and finally ->772 when the MinerU
    # embedded-image-retention feature branch was rebased onto that master
    # tip: the feature's own Task 8 additions to this file (AssetService /
    # make_persist_image_factory INDEPENDENT_SQL_SITES + SQLITE_CONNECT_SITES
    # entries) land on top of the already-merged merge_dbs.py reconciliation,
    # pushing this deferred import down by +41 more lines. 772->776: knowhow
    # anchor-grouping-display's INDEPENDENT_PRIVATE_SITES comment expansion
    # (api.py optimize_cell's `_runtime` site, 693->716, +4 net lines) shifts
    # it again.
    ("backend/tests/test_repository_callers_static.py", 776, "app.services.sqlite_repository", "SQLiteRepository"),
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
# patch); object-vector flush probes likewise target SourceEmbeddingService.
MASTER_V10_ALLOWED_PATCHES = set()
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
# Task 11 late-binding proofs: chunking observes the facade
# _mark_unified_kg_dirty seat and module _new_id seam that mints ck-* ids.
# Object-vector persistence probes target SourceEmbeddingService directly.
TASK11_ALLOWED_PATCHES = {
    ("backend/tests/test_source_chunking_service.py", 103, "_new_id", "sqlite_repository"),
    ("backend/tests/test_source_chunking_service.py", 119, "_mark_unified_kg_dirty", "repo"),
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
    ("scripts/verify_repository_snapshot.py", 59, "app.services.sqlite_repository", "SCHEMA_VERSION"),
    ("scripts/verify_repository_snapshot.py", 59, "app.services.sqlite_repository", "SQLiteRepository"),
    ("scripts/verify_repository_snapshot.py", 59, "app.services.sqlite_repository", "reset_request_user"),
    ("scripts/verify_repository_snapshot.py", 59, "app.services.sqlite_repository", "set_request_user"),
}
# Line numbers shifted +32 by Task 1 (memory-kg-extract)'s MIGRATION_MANIFEST
# v14 additions, then a further +28 by Task 5's v15 additions
# (SOURCES_PARSE_STATUS_TYPE_INDEX + every hop terminal bumped to 15 with that
# index + the new (14, 15) hop), then a further +83 by knowhow-tables Task 1's
# MIGRATION_MANIFEST v16 additions (KNOWHOW_TABLES + KNOWHOW_INDEXES +
# every hop terminal bumped to 16 with those objects + the new (15, 16) hop),
# then a further +65 by paper-metadata Task 1's MIGRATION_MANIFEST v17
# additions (PAPER_META_TABLES + PAPER_META_INDEXES + every hop terminal
# bumped to 17 with those objects + the new (16, 17) hop), then a further +45
# by knowhow-tables PR-2+3 Task 1's v18 additions (KNOWHOW_CELL_CODE_TABLE/
# _INDEX folded into every hop, terminals bumped to 18, + the new (17, 18)
# hop), then a further +57 by source-asset-linking Task 2's v19 additions
# (NOTEBOOK_ASSETS_SOURCE_ID_COLUMN/_INDEX folded into every hop, terminals
# bumped to 19, + the new (18, 19) hop).
TASK28_ALLOWED_CONSUMERS = {
    ("ask_job_detail", "scripts/verify_repository_snapshot.py:1333"),
    ("get_conversation", "scripts/verify_repository_snapshot.py:1326"),
    ("get_notebook", "scripts/verify_repository_snapshot.py:1311"),
    ("get_report", "scripts/verify_repository_snapshot.py:1338"),
    ("knowledge_types", "scripts/verify_repository_snapshot.py:1314"),
    ("list_conversations", "scripts/verify_repository_snapshot.py:1323"),
    ("list_knowledge", "scripts/verify_repository_snapshot.py:1317"),
    ("list_reports", "scripts/verify_repository_snapshot.py:1335"),
    ("list_sources", "scripts/verify_repository_snapshot.py:1312"),
    ("maintenance", "scripts/verify_repository_snapshot.py:1274"),
    ("maintenance", "scripts/verify_repository_snapshot.py:1278"),
    ("maintenance", "scripts/verify_repository_snapshot.py:1280"),
    ("maintenance", "scripts/verify_repository_snapshot.py:1305"),
    ("search_notebook", "scripts/verify_repository_snapshot.py:1347"),
    ("unified_kg_status", "scripts/verify_repository_snapshot.py:1321"),
}

# Task 1 (Memory): schema-version and migration tests add new compatibility
# facade consumers, while inserting the v11 assertion shifts the frozen legacy
# test sites. Keep both old and live exact sites out of the immutable baseline
# comparison.
TASK1_MEMORY_ALLOWED_CONSUMERS = {
    ("SCHEMA_VERSION", "backend/tests/test_legacy_db_compat.py:82"),
    ("SCHEMA_VERSION", "backend/tests/test_legacy_db_compat.py:88"),
    ("SCHEMA_VERSION", "backend/tests/test_memory_migration.py:33"),
    ("SQLiteRepository", "backend/tests/test_memory_migration.py:15"),
    ("_connect", "backend/tests/test_legacy_db_compat.py:58"),
    ("_connect", "backend/tests/test_legacy_db_compat.py:81"),
    ("_connect", "backend/tests/test_legacy_db_compat.py:87"),
    ("_connect", "backend/tests/test_memory_migration.py:34"),
    ("_migrate", "backend/tests/test_legacy_db_compat.py:80"),
    ("_migrate", "backend/tests/test_legacy_db_compat.py:86"),
    ("_write", "backend/tests/test_legacy_db_compat.py:65"),
    ("_write", "backend/tests/test_legacy_db_compat.py:71"),
    ("_write", "backend/tests/test_memory_migration.py:52"),
    ("_write", "backend/tests/test_memory_migration.py:62"),
    ("_write", "backend/tests/test_memory_migration.py:91"),
    ("_write", "backend/tests/test_memory_migration.py:101"),
    ("_write", "backend/tests/test_memory_migration.py:131"),
}
TASK1_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_migration.py", name)
    for name in {
        "SQLiteRepository", "SCHEMA_VERSION", "_connect", "_write",
        "close_local", "settings",
    }
}

# Task 2 (Memory): the lifecycle service/store tests intentionally exercise
# the new facade delegates and existing composition seams.  They post-date the
# immutable pre-Memory facade fixture, so keep these exact new test consumers
# out of the historical comparison while still checking them in the dedicated
# Memory boundary contract.
TASK2_MEMORY_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_service.py", 10, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_memory_service.py", 10, "app.services.sqlite_repository", "reset_request_user"),
    ("backend/tests/test_memory_service.py", 10, "app.services.sqlite_repository", "set_request_user"),
    ("backend/tests/test_memory_repository_boundaries.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK2_MEMORY_ALLOWED_NEW_MEMBERS = {
    "confirm_memory",
    "create_memory_candidate",
    "create_memory_from_answer",
    "deprecate_memory",
    "get_memory",
    "list_memories",
    "memory_revisions",
    "reject_memory",
    "update_memory",
}
TASK2_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_service.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_runtime", "_write", "create_notebook",
        "create_user", "embedder", "reset_request_user", "set_request_user",
    }
} | {
    ("backend/tests/test_memory_repository_boundaries.py", name)
    for name in {"SQLiteRepository", "_runtime", "embedder"}
}

# Task 3 (Memory): API-level tests compose real Ask rows and shared membership
# through the compatibility facade.  The new answer projection is a public
# one-hop adapter used by the Memory API; all other lifecycle members were
# already admitted by Task 2.
TASK3_MEMORY_ALLOWED_NEW_MEMBERS = {"answer_memory_source"}
TASK3_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_api.py", name)
    for name in {"_runtime", "_write", "add_member", "remove_member"}
} | {
    ("backend/tests/test_memory_preview.py", name)
    for name in {"_runtime", "llm_client"}
}

# Task 5 (Memory): retrieval integration tests intentionally compose the real
# facade so notebook/Ask/report projections share one request identity and
# database. Keep these new test-only compatibility consumers out of the frozen
# pre-Memory surface comparison.
TASK5_MEMORY_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_retrieval.py", 10, "app.services.sqlite_repository", name)
    for name in {"SQLiteRepository", "reset_request_user", "set_request_user"}
}
TASK5_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_retrieval.py", name)
    for name in {
        "SQLiteRepository", "_reasoning_llm_client", "_runtime", "_write",
        "add_member", "create_notebook", "create_user", "llm_client",
        "reset_request_user", "retrieval", "set_request_user", "settings",
    }
}

# Task 6 (Memory): Agent token tests compose a real repository/runtime to prove
# owner isolation and notebook membership revocation across the service/store
# boundary. These are test-only compatibility consumers added after the frozen
# pre-Memory facade manifest.
TASK6_MEMORY_ALLOWED_IMPORTS = {
    ("backend/tests/test_agent_tokens.py", 11, "app.services.sqlite_repository", name)
    for name in {"SQLiteRepository", "reset_request_user", "set_request_user"}
}
TASK6_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_agent_tokens.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "create_notebook", "create_user",
        "reset_request_user", "set_request_user",
    }
}
TASK6_MEMORY_ALLOWED_NEW_MEMBERS = {
    "create_agent_profile",
    "issue_agent_token",
    "list_agent_profiles",
    "list_agent_tokens",
    "require_agent_access",
    "resolve_agent_token",
    "revoke_agent_token",
    "update_agent_profile",
}

# Task 7 (Memory): the MCP adapter is a new consumer of established public
# facade delegates.  It adds one source-checked one-hop delegate for the
# two-plane Memory retriever; no SQL or private runtime state is exposed.
# MCP HTTPS opt-in Task 1 adds a stdlib `logging` import plus a module logger
# above these call sites (see LINE_NUMBER_INSENSITIVE_FILES), shifting every
# one of them down by exactly 21 lines; re-pinned to their current lines.
# MCP HTTPS opt-in Task 2 threads `require_https` through
# `AgentBearerMiddleware.__init__` (+4 lines) and its `__call__` scheme check
# (+2 lines), both also above these call sites, shifting every one of them
# down by exactly 6 more lines; re-pinned to their current lines.
# Whole-branch review fix (MCP_REQUIRE_HTTPS Important finding) widens
# `validate_mcp_deployment`'s warning to fire whenever Host/Origin validation
# is relaxed, not just over plain HTTP, adding 20 net lines above these call
# sites; re-pinned to their current lines.
TASK7_MEMORY_ALLOWED_CONSUMERS = {
    ("user_can_read_notebook", "backend/app/api/mcp_server.py:656"),
    ("get_notebook", "backend/app/api/mcp_server.py:661"),
    ("user_can_read_notebook", "backend/app/api/mcp_server.py:693"),
    ("get_notebook", "backend/app/api/mcp_server.py:698"),
    ("unified_kg_status", "backend/app/api/mcp_server.py:699"),
    ("agent_memory_hits", "backend/app/api/mcp_server.py:743"),
    ("search_notebook", "backend/app/api/mcp_server.py:813"),
    ("ask", "backend/app/api/mcp_server.py:909"),
}
TASK7_MEMORY_ALLOWED_NEW_MEMBERS = {
    "agent_memory_hits",
    "refresh_agent_principal",
}

# Task 8 (Memory): governed Memory-to-KG promotion adds one owner-scoped
# facade delegate and a focused full-stack test that intentionally composes
# established repository seams. The immutable manifest predates Memory.
TASK8_MEMORY_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_promotion.py", 12, "app.services.sqlite_repository", name)
    for name in {"SQLiteRepository", "reset_request_user", "set_request_user"}
}
TASK8_MEMORY_ALLOWED_NEW_MEMBERS = {
    "propose_memory_promotion",
    "approve_promotion_as_reviewer",
    "reject_promotion_as_reviewer",
}
TASK8_MEMORY_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_promotion.py", name)
    for name in {
        "SQLiteRepository", "_connect", "_runtime", "_test_insert_object", "_write",
        "add_member", "approve_promotion", "confirm_memory", "create_memory_candidate",
        "create_notebook", "create_user", "deprecate_memory", "get_memory",
        "list_promotion_queue", "mark_notebook_base", "memory_revisions",
        "propose_memory_promotion", "reject_memory", "reject_promotion", "remove_member",
        "approve_promotion_as_reviewer",
        "propose_promotion",
        "reset_request_user", "set_request_user",
    }
} | {
    ("backend/app/api/memory_routes.py", "propose_memory_promotion"),
    ("backend/app/api/routes.py", "approve_promotion_as_reviewer"),
    ("backend/app/api/routes.py", "reject_promotion_as_reviewer"),
}

# Task 1 (memory-kg-extract, a distinct later feature branch from the Memory
# tasks above): the sources.memory_id migration/schema test composes the real
# facade + migrator directly to prove fresh-DB and upgraded-DB (v13->v14)
# schema convergence. Test-only compatibility consumers added after the
# frozen pre-Memory-KG facade manifest.
TASK1_MEMORY_KG_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_kg_schema.py", 17, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK1_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_kg_schema.py", name)
    for name in {
        "SCHEMA_VERSION", "SQLiteRepository", "_connect", "_migrate", "_runtime", "_write",
    }
}

# Task 2 (memory-kg-extract): the source_ingestion-domain memory-derived
# source primitive tests compose the real facade + runtime directly (same
# repo/repo_factory pattern as test_source_ingestion_service.py) to prove
# memory_kg_eligible / ingest_memory_source / remove_memory_source against a
# real SourceIngestionService instance. Test-only compatibility consumers
# added after the frozen pre-Memory-KG facade manifest.
TASK2_MEMORY_KG_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_source_ingestion.py", 33, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_memory_source_ingestion.py", 33, "app.services.sqlite_repository", "_now"),
}
TASK2_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_source_ingestion.py", name)
    for name in {
        "SQLiteRepository", "_now", "_runtime", "_write",
        "create_notebook", "mark_notebook_base",
    }
}

# Task 3 (memory-kg-extract): the MemoryService lifecycle-hook tests compose
# the real facade + runtime directly (same repo fixture pattern as the Task
# 1/2 files above) to prove confirm/create_from_answer/update/deprecate
# scheduling a `_KgStub` in place of the real SourceIngestionService. New
# facade member `memory_kg_eligible` (one-hop delegate to
# `self._runtime.source_ingestion.memory_kg_eligible`) postdates the frozen
# facade_surface fixture, so it is exempted from the consumer-scan
# comparison entirely — exactly like STARTUP_READINESS_ALLOWED_NEW_MEMBERS
# does for warm_open_path_caches — instead of regenerating the frozen golden.
TASK3_MEMORY_KG_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_kg_lifecycle.py", 32, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_memory_kg_lifecycle.py", 32, "app.services.sqlite_repository", "reset_request_user"),
    ("backend/tests/test_memory_kg_lifecycle.py", 32, "app.services.sqlite_repository", "set_request_user"),
}
TASK3_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_kg_lifecycle.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_write", "create_notebook",
        "create_user", "reset_request_user", "set_request_user",
    }
}
TASK3_MEMORY_KG_ALLOWED_NEW_MEMBERS = {"memory_kg_eligible"}

# Task 4 (memory-kg-extract): the API-surface tests extend two pre-existing
# Memory route test files (test_memory_api.py / test_memory_preview.py, from
# the earlier agent-memory feature) rather than adding new ones. Their new
# eligibility-gate assertions reach two facade members for the first time in
# those specific files: test_memory_api.py's new notebook-listing test calls
# mark_notebook_base (previously only exercised via routes.py / other test
# files), and test_memory_preview.py's new eligibility test is the first in
# that file to seed KG state via repo._write() directly (its existing tests
# only ever read/patch repo.llm_client). Both are ordinary frozen facade
# members whose consumer set simply grows by one file, not new members.
TASK4_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_api.py", "mark_notebook_base"),
    ("backend/tests/test_memory_preview.py", "_write"),
}

# Task 5 (memory-kg-extract): the source-visibility filter tests compose the
# real facade + runtime directly (same repo fixture pattern as the Task 1-3
# files above) to prove list_sources / list_sources_page / NotebookSummary's
# source count / notebook_analytics' parse_status distribution all exclude
# source_type='memory' synthetic sources, while get_source (a SourceStore
# method reached via repo._runtime.source_store, not a facade member) keeps
# resolving them. Test-only compatibility consumers added after the frozen
# pre-Memory-KG facade manifest.
TASK5_MEMORY_KG_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_source_visibility.py", 33, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK5_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_source_visibility.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_write", "create_notebook",
        "get_notebook", "notebook_analytics", "search_notebook",
        "shared_preview",
    }
}

# Task 6 (memory-kg-extract): the deep-copy-clears-memory_id tests add two new
# repo._runtime.source_store reaches in test_notebook_share_copy.py (seeding a
# genuine memory-derived source row via insert_source, same accessor pattern
# Task 2/5 already use in their own files) — the first "_runtime" reaches in
# this specific file, so the pre-existing exact-line REVIEW_FIX_ALLOWED_CONSUMERS
# entry for this member+file (a single stale call site) is superseded by this
# broad allowance, robust to this file's line numbers shifting again later.
TASK6_MEMORY_KG_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_notebook_share_copy.py", "_runtime"),
}

# Task 1 (knowhow-tables-pr1, a distinct later feature branch from the
# Memory-KG tasks above): the knowhow-tables schema-migration test composes
# the real facade + migrator directly to prove fresh-DB and upgraded-DB
# (v15->v16) schema convergence for the five new knowhow_*/notebook_assets
# tables — same pattern as TASK1_MEMORY_KG_ALLOWED_IMPORTS above. Test-only
# compatibility consumer added after the frozen pre-Memory-KG facade
# manifest.
TASK1_KNOWHOW_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_schema.py", 21, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK1_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_schema.py", name)
    for name in {
        "SCHEMA_VERSION", "SQLiteRepository", "_connect", "_migrate", "_write",
    }
}

# Task 2 (knowhow-tables-pr1): the new knowhow_store repository module's own
# test composes the real facade (to prove the one-hop delegates reach the
# SAME runtime-owned KnowhowStore) alongside direct-store tests, and reuses
# create_notebook to seed a notebook_id fixture — same pattern as
# TASK1_KNOWHOW_ALLOWED_IMPORTS/_MEMBER_FILES above for the sibling schema
# test. The eleven new facade members themselves (create_knowhow_table /
# list_knowhow_tables / get_knowhow_table / add_knowhow_row /
# update_knowhow_cell / delete_knowhow_table / set_knowhow_row_projection /
# set_knowhow_hidden_source / bump_knowhow_mutation_seq /
# insert_notebook_asset / get_notebook_asset) predate no frozen fixture, so
# they are exempted from the consumer-scan comparison entirely below (exactly
# like SQLITE_CONN_REUSE_ALLOWED_NEW_MEMBERS does for close_local) rather than
# pinned to exact lines.
TASK2_KNOWHOW_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_store.py", 15, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK2_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_store.py", name)
    for name in {"SQLiteRepository", "_runtime", "create_notebook"}
}
TASK2_KNOWHOW_ALLOWED_NEW_MEMBERS = {
    "create_knowhow_table", "list_knowhow_tables", "get_knowhow_table",
    "add_knowhow_row", "update_knowhow_cell", "delete_knowhow_table",
    "set_knowhow_row_projection", "set_knowhow_hidden_source",
    "bump_knowhow_mutation_seq", "insert_notebook_asset", "get_notebook_asset",
}
# Task 1 (knowhow-tables PR-2+3): twelve more KnowhowStore one-hop delegates
# (table-meta/anchor/column/row editing + cell-code CRUD). Brand-new facade
# members predating no frozen fixture — exempted from the consumer-scan
# comparison entirely, exactly like TASK2_KNOWHOW_ALLOWED_NEW_MEMBERS above.
TASK1_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS = {
    "update_knowhow_table_meta", "set_knowhow_anchor_column",
    "add_knowhow_column", "rename_knowhow_column", "set_knowhow_column_kind",
    "delete_knowhow_column", "delete_knowhow_row", "validate_cell_target",
    "upsert_knowhow_cell_code", "get_knowhow_cell_code",
    "delete_knowhow_cell_code", "list_knowhow_cell_code",
}

# Task 4 (knowhow-tables-pr1): the asset-store/authed-serving routes' own test
# composes the real facade the same way Task 1/2's sibling tests do — seeding
# a notebook via HTTP then a read-only member via direct repo.add_member
# (there is no HTTP "add member by id" endpoint) — same pattern as
# TASK1_KNOWHOW_ALLOWED_IMPORTS/TASK2_KNOWHOW_ALLOWED_IMPORTS above. Unlike
# Task 2's brand-new facade members, `add_member`/`storage_dir` are
# pre-existing frozen members with real recorded consumers, so their new call
# sites here (AssetService mirrors SourceFileStore's storage_dir convention)
# are registered as allowed consumers rather than exempted wholesale.
TASK4_KNOWHOW_ALLOWED_IMPORTS = {
    ("backend/tests/test_notebook_assets.py", 12, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_KNOWHOW_ALLOWED_CONSUMERS = {
    ("add_member", "backend/tests/test_notebook_assets.py:110"),
    ("storage_dir", "backend/app/services/knowhow/assets.py:73"),
}
# The facade-import consumer scan (test_static_repository_consumer_scan_
# matches_manifest_exactly) tracks SQLiteRepository itself as a member with
# its own recorded consumer sites; Task 1/2's sibling test files clear it via
# the broad (file, member) allowance below rather than TASK4_KNOWHOW_ALLOWED_
# IMPORTS above (that set only feeds the separate compatibility-exports scan).
TASK4_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_notebook_assets.py", "SQLiteRepository"),
}

# Task 5 (knowhow-tables-pr1): the deterministic projector's own test composes
# the real facade the same way Task 1/2/4's sibling tests do — seeding a
# notebook via create_notebook, reaching into _runtime for the stores/services
# KnowhowProjector is constructed from directly (it is a plain service, not
# itself a facade member — Task 6's import/table API is what will eventually
# wire it onto the facade), swapping in a fake embedder via the `embedder`
# setter (mirrors test_ask_embed_cache.py), and using `_connect`/`settings`
# for direct-DB assertions and the embedder_configured probe. Same broad
# (file, member) allowance style as TASK2_KNOWHOW_ALLOWED_MEMBER_FILES rather
# than pinning exact lines.
# Line 18->24: PR-2+3 Task 2 rewrites this test file end to end for the
# cell-level node model (case/procedure/tool -> dynamic per-column types),
# expanding the module docstring/imports above this exact import line by 6
# net lines. Not itself API surface — the member/file pair is unchanged.
TASK5_KNOWHOW_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_projection.py", 24, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK5_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_projection.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "create_notebook", "_connect",
        "embedder", "settings",
    }
}

# Task 6 (knowhow-tables-pr1): the import/table API's own HTTP-level test
# composes the real facade the same way Task 1/2/4/5's sibling tests do —
# seeding notebooks/members via the real facade (create_notebook happens
# through HTTP here; add_member has no HTTP equivalent) and reaching
# `_connect` for direct-DB cascade-delete assertions (chunks/knowledge_
# objects/sources rows are actually gone, not just "the API says so"). Same
# broad (file, member) allowance style as TASK5_KNOWHOW_ALLOWED_MEMBER_FILES.
# `add_member`/`_connect` are pre-existing frozen members (unlike Task 2's
# brand-new facade members) but — unlike Task 4's exact-consumer choice for
# THIS SAME pair of members — this test file's calls are exempted via the
# broad file allowance, which is equally valid for test files (frozen=True
# and frozen=False both resolve through the same ALL_TASK_ALLOWED_MEMBER_
# FILES broad_match for a backend/tests/ path — see _member_file_site_
# allowed's `path.startswith("backend/tests/")` branch).
TASK6_KNOWHOW_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_api.py", 23, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK6_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_api.py", name)
    for name in {"SQLiteRepository", "_connect", "add_member"}
}
# app/services/knowhow/api.py (Task 6's orchestration module, a PRODUCTION
# file) reaches two pre-existing frozen members building the plain
# KnowhowProjector directly (see test_repository_callers_static.py's
# INDEPENDENT_PRIVATE_SITES for the sibling `_runtime` registration there) —
# `settings` isn't private so it only needs registering HERE, and `_runtime`
# is registered both places since the two guards scan independently.
# Mirrors Task 4's exact-consumer choice for `storage_dir` in assets.py.
TASK6_KNOWHOW_ALLOWED_CONSUMERS = {
    ("_runtime", "backend/app/services/knowhow/api.py:185"),
    ("settings", "backend/app/services/knowhow/api.py:187"),
}

# Task 10 (knowhow-tables-pr1): the end-to-end projection -> retrieval
# integration test composes the real facade the same way Task 5/6's sibling
# tests do — except it grabs the APP's own repository singleton (mirrors
# test_trackF_governance_promotion.py's `client._repo = repository()` trick,
# needed so the background projection job's embedder swap is visible to the
# app) rather than constructing a fresh SQLiteRepository. It reaches
# `_runtime`/`_connect` for direct-DB assertions plus the pre-existing frozen
# `_retrieve_chunks`/`ask_chunk` retrieval entry points this task exists to
# prove knowhow content actually flows through. Same broad (file, member)
# allowance style as TASK5/TASK6_KNOWHOW_ALLOWED_MEMBER_FILES.
TASK10_KNOWHOW_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_retrieval.py", name)
    for name in {
        "_runtime", "_connect", "_retrieve_chunks", "ask_chunk",
        # Final-review blocker regression (kg/rebuild must not touch the
        # knowhow projection): the two new tests drive the real rebuild path —
        # delete_notebook_kg (the wipe) and rebuild_notebook_kg (delete+build)
        # with _run_extraction stubbed to a recorder and llm_client forced
        # configured — mirroring test_kg_rebuild_relink_api.py's own facade
        # consumption. Same broad (file, member) allowance style as the four
        # frozen members above.
        "delete_notebook_kg", "rebuild_notebook_kg", "llm_client", "_run_extraction",
    }
}

# Task 1 (paper-metadata-extraction, a distinct later feature branch from the
# knowhow-tables tasks above): the paper-metadata schema-migration test
# composes the real facade + migrator directly to prove fresh-DB and
# upgraded-DB (v16->v17) schema convergence for the two new
# source_paper_meta/source_authors tables — same pattern as
# TASK1_KNOWHOW_ALLOWED_IMPORTS/_MEMBER_FILES above for the sibling schema
# test. Test-only compatibility consumer added after the frozen
# pre-knowhow-tables facade manifest.
TASK1_PAPER_META_ALLOWED_IMPORTS = {
    ("backend/tests/test_paper_meta_schema.py", 13, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK1_PAPER_META_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_paper_meta_schema.py", name)
    for name in {
        "SCHEMA_VERSION", "SQLiteRepository", "_connect", "_migrate", "_write",
    }
}

# Task 3 (paper-metadata-extraction): the new SourceStore persistence/
# hydration/search test file constructs the real facade directly (same
# fixture shape as test_source_store_component.py's TASK10_ALLOWED_MEMBER_
# FILES entry) — SQLiteRepository(...), repo._runtime.source_store,
# repo.create_notebook(...), repo._write() for the cascade-delete test.
TASK3_PAPER_META_ALLOWED_IMPORTS = {
    ("backend/tests/test_paper_meta_store.py", 9, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK3_PAPER_META_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_paper_meta_store.py", name)
    for name in {"SQLiteRepository", "create_notebook", "_runtime", "_write"}
}

# Task 4 (paper-metadata-extraction): the new service-integration test file
# constructs the real facade directly (same fixture shape as
# test_batch_ingest.py / test_kg_llm_client.py) — SQLiteRepository(...),
# repo.embedder, repo._runtime.{source_store,source_ingestion}, repo.
# create_notebook(...), repo._write() for the element-seeding helper,
# repo.settings (toggling paper_meta_enabled), repo._kg_llm_client (faking
# the KG LLM seam) and repo._run_extraction (driving the historical-source
# catch-up mount). get_paper_meta/sources_missing_paper_meta/backfill_paper_
# metadata are brand-new facade delegates this task adds — their only
# consumer is this test file, which postdates the frozen fixture, so they go
# in TASK4_PAPER_META_ALLOWED_NEW_MEMBERS below instead (exempt entirely,
# same as SQLITE_CONN_REUSE_ALLOWED_NEW_MEMBERS did for close_local).
TASK4_PAPER_META_ALLOWED_IMPORTS = {
    ("backend/tests/test_paper_meta_service.py", 18, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK4_PAPER_META_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_paper_meta_service.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "_runtime", "_write", "embedder",
        "settings", "_kg_llm_client", "_run_extraction",
    }
}
TASK4_PAPER_META_ALLOWED_NEW_MEMBERS = {
    "get_paper_meta", "sources_missing_paper_meta", "backfill_paper_metadata",
}

# Task 1 (paper-meta-status-dashboard, a later feature building on the
# paper-metadata-extraction work above): paper_meta_backfilling /
# paper_meta_backfill_progress are brand-new one-hop facade delegates onto
# the SourceIngestionService in-memory backfill-progress dict (nb_id ->
# {"total","done"}), added so later tasks (NotebookSummary field / pending-
# actions surfacing / frontend polling) can observe an in-flight backfill.
# Their only consumers today are the new test_paper_meta_service.py runtime
# tests, which postdate the frozen facade_surface fixture — exempt both
# members from the consumer-scan comparison entirely, same as
# TASK4_PAPER_META_ALLOWED_NEW_MEMBERS did for backfill_paper_metadata et al.
PAPER_META_STATUS_ALLOWED_NEW_MEMBERS = {
    "paper_meta_backfilling", "paper_meta_backfill_progress",
}

# Task 4 (paper-meta-status-dashboard): the new notebook_analytics()
# paper_meta_counts test file constructs the real facade directly — same
# SQLiteRepository(...)/repo.create_notebook(...)/repo._runtime.source_store
# fixture shape as TASK3_PAPER_META_ALLOWED_MEMBER_FILES
# (test_paper_meta_store.py) above. notebook_analytics itself is NOT a new
# member (it already has tracked consumers in facade_surface.json — this
# task only adds call sites there), so only the incidental fixture-plumbing
# members are broadly file-allowed here.
PAPER_META_STATUS_TASK4_ALLOWED_IMPORTS = {
    ("backend/tests/test_analytics.py", 16, "app.services.sqlite_repository", "SQLiteRepository"),
}
PAPER_META_STATUS_TASK4_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_analytics.py", name)
    for name in {"SQLiteRepository", "create_notebook", "_runtime"}
}

# sqlite connection reuse: Change-4 line shift in test_node_context_steps.py +
# new close_local member + new test_sqlite_connection_reuse.py consumers.
# close_local is a brand-new facade delegate (SqliteDatabase.close_local()
# wired through wire_knowledge_lifecycle); exempt it from the consumer-scan
# comparison entirely, exactly like TASK3_ALLOWED_NEW_MEMBERS does for other
# never-frozen members, instead of trying to match its consumer sites against
# a fixture that predates it.
SQLITE_CONN_REUSE_ALLOWED_NEW_MEMBERS = {"close_local"}
# startup readiness: warm_open_path_caches is a brand-new facade delegate
# (knowledge_counts_cache.warm_all wired through the startup daemon) that primes
# the per-process open-path count caches for every notebook behind the readiness
# gate. Its sole consumer (app/services/startup_warmup.py) postdates the frozen
# facade_surface fixture, so exempt the member from the consumer-scan comparison
# entirely — exactly like SQLITE_CONN_REUSE_ALLOWED_NEW_MEMBERS does for
# close_local — instead of regenerating the frozen golden.
STARTUP_READINESS_ALLOWED_NEW_MEMBERS = {"warm_open_path_caches"}
# The startup-readiness unit test drives the real repository through two frozen
# public/test-only members (create_notebook to seed notebooks, _test_insert_object
# to seed a KO); those consumer sites postdate the frozen fixture, so drop them
# from the consumer-scan comparison exactly like the other component test files
# above (warm_open_path_caches itself is handled by the NEW_MEMBERS pop).
STARTUP_READINESS_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_startup_warmup.py", name)
    for name in {"create_notebook", "_test_insert_object"}
}
# The new connection-reuse test suite imports the frozen compatibility facade
# at a fresh site to exercise the close_local delegate end-to-end.
SQLITE_CONN_REUSE_ALLOWED_IMPORTS = {
    ("backend/tests/test_sqlite_connection_reuse.py", 164, "app.services.sqlite_repository", "SQLiteRepository"),
}

# merge_dbs (PR#276): the offline two-DB merge tool's test builds a fresh
# current-schema fixture DB through the facade (_fresh_db constructs
# SQLiteRepository, then close_local()s to flush WAL before copying the file).
# A single import consumer, mirroring test_sqlite_connection_reuse above.
MERGE_DBS_ALLOWED_IMPORTS = {
    ("backend/tests/test_merge_dbs.py", 30, "app.services.sqlite_repository", "SQLiteRepository"),
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
    # MCP HTTPS opt-in (MCP_REQUIRE_HTTPS): validate_mcp_deployment +
    # create_memory_mcp + AgentBearerMiddleware gain a require_https param and a
    # module logger above this file's facade consumers, shifting those call
    # sites' lines without changing the surface. Internal line numbers here are
    # not API surface.
    "backend/app/api/mcp_server.py",
    # FastAPI composition root: the startup-readiness lifespan + gate middleware
    # add ~60 lines above its sole facade consumer (repository().pending_actions),
    # shifting that call's line without changing the surface. Like the other API
    # entry files above, main.py's internal line numbers are not API surface.
    "backend/app/main.py",
    "backend/tests/test_architecture_module_boundaries.py",
    "backend/tests/test_repository_runtime.py",
    # Asset-GC trigger test: hooks database.write to prove the sweep re-checks
    # references INSIDE the write transaction (a cell save committing between
    # scan and delete must not lose its image). Reaching _runtime is the only
    # way to intervene at that boundary; its internal line numbers are not API
    # surface, and pinning them would make every future edit to this test file
    # a manifest failure.
    "backend/tests/test_knowhow_asset_gc_trigger.py",
    # kg-ingest-count fix: run_all 分三批(new/resume/reparse)+ EOF 新增 reparse 回归
    # 测试,移动了本文件既有 consumer sites 行号并新增 monkeypatch 调用点。测试内部
    # 行号非 API surface(同上面几个 test 文件)。
    "backend/tests/test_batch_ingest.py",
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
    "backend/tests/test_notebook_counts_batched.py",
    "backend/tests/test_ask_reconnect.py",
    "backend/tests/test_ask_redesign.py",
    # Task 12b (knowhow citation-jump widening) adds a `knowhow_refs_for`
    # method to _MinimalEvidence (the AskService port-boundary fixture),
    # shifting this file's later facade-consumer call sites without changing
    # the surface.
    "backend/tests/test_ask_service_boundary.py",
    "backend/tests/test_conversations.py",
    "backend/tests/test_cross_tier_reasoning.py",
    "backend/tests/test_followup_retrieval_grounding.py",
    "backend/tests/test_graph_src_chunks.py",
    "backend/tests/test_indexed_only_principle.py",
    "backend/tests/test_kg_mutation_phase_matrix.py",
    "backend/tests/test_kg_repository.py",
    "backend/tests/test_kg_search_api.py",
    "backend/tests/test_knowledge_governance_delegation.py",
    "backend/tests/test_knowledge_store_contract.py",
    "backend/tests/test_model_provider_runtime.py",
    "backend/tests/test_notebook_copy_service.py",
    "backend/tests/test_notebook_summary_query.py",
    "backend/tests/test_overlay_guard_order.py",
    "backend/tests/test_p4_kg_shrink.py",
    "backend/tests/test_reasoning_ppr.py",
    "backend/tests/test_rebuild_cache.py",
    "backend/tests/test_rebuild_desc_cache.py",
    "backend/tests/test_rebuild_streaming.py",
    "backend/tests/test_relation_ann.py",
    "backend/tests/test_report_engine.py",
    "backend/tests/test_repository_callers_static.py",
    "backend/tests/test_repository_ports.py",
    "backend/tests/test_retrieval_service.py",
    "backend/tests/test_runtime_dim_scale_index.py",
    "backend/tests/test_scale_builder_failure_boundaries.py",
    "backend/tests/test_scale_delta_policy.py",
    "backend/tests/test_scale_idx_disk_cache.py",
    "backend/tests/test_scale_index_repo.py",
    "backend/tests/test_scale_index_version_probe.py",
    "backend/tests/test_scale_version_probe.py",
    "backend/tests/test_scale_xlayer_bridge_delta.py",
    "backend/tests/test_trackF_governance_promotion.py",
    "backend/tests/test_two_tier_federated.py",
    "backend/tests/test_viz_index_wire.py",
    # Task 5 (memory-kg-extract) adds a covering-index EXPLAIN test + a new
    # index existence assertion here, shifting the internal line numbers of its
    # many facade-consumer sites (_connect / _write / create_notebook /
    # notebook_analytics / _extraction_warning ...) without changing which
    # members it exercises. Line numbers here are not API surface.
    "backend/tests/test_sqlite_indexes.py",
    # Task 6 (memory-kg-extract) adds two copy_notebook tests (deep copy must
    # null out sources.memory_id) to this file, shifting the internal line
    # numbers of its many facade-consumer sites (copy_notebook / share_notebook
    # / notebook_copy_stats / ...) without changing which members it
    # exercises. The _COPY_CHUNK / _insert_row patch_targets are hand-remapped
    # in facade_surface.json instead (that scan has no line-insensitivity
    # lever). Line numbers here are not API surface.
    "backend/tests/test_notebook_share_copy.py",
}

ALL_TASK_ALLOWED_MEMBER_FILES = (
    TASK2_ALLOWED_MEMBER_FILES
    | TASK1_MEMORY_ALLOWED_MEMBER_FILES
    | TASK2_MEMORY_ALLOWED_MEMBER_FILES
    | TASK3_MEMORY_ALLOWED_MEMBER_FILES
    | TASK5_MEMORY_ALLOWED_MEMBER_FILES
    | TASK6_MEMORY_ALLOWED_MEMBER_FILES
    | TASK8_MEMORY_ALLOWED_MEMBER_FILES
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
    | STARTUP_READINESS_ALLOWED_MEMBER_FILES
    | TASK1_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK2_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK3_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK4_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK5_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK6_MEMORY_KG_ALLOWED_MEMBER_FILES
    | TASK1_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK2_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK4_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK5_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK6_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK10_KNOWHOW_ALLOWED_MEMBER_FILES
    | TASK1_PAPER_META_ALLOWED_MEMBER_FILES
    | TASK3_PAPER_META_ALLOWED_MEMBER_FILES
    | TASK4_PAPER_META_ALLOWED_MEMBER_FILES
    | PAPER_META_STATUS_TASK4_ALLOWED_MEMBER_FILES
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
    ("resolve_session", "backend/app/api/deps.py:57"),
    ("current_user", "backend/app/api/deps.py:61"),
    ("llm_client", "backend/app/api/deps.py:109"),
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
    # ("_runtime", test_notebook_share_copy.py) formerly pinned this one exact
    # (now stale, line-shifted) call site; Task 6 (memory-kg-extract) replaced
    # it with the broad TASK6_MEMORY_KG_ALLOWED_MEMBER_FILES allowance above,
    # which stays correct across future edits to this file.
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
    # Task 6 independent-review regressions. These are exact new test
    # consumers; the frozen production surface and its coordinates stay intact.
    ("create_notebook", "backend/tests/test_in_batching.py:74"),
    ("_connect", "backend/tests/test_in_batching.py:80"),
    ("_knowledge_objects", "backend/tests/test_in_batching.py:81"),
    ("_IN_CHUNK", "backend/tests/test_in_batching.py:85"),
    ("_connect", "backend/tests/test_in_batching.py:87"),
    ("_knowledge_objects", "backend/tests/test_in_batching.py:89"),
    (
        "_edge_centrality_map",
        "backend/tests/test_retrieval_snapshot_cache_runtime.py:66",
    ),
    (
        "_notebook_langs_cache",
        "backend/tests/test_retrieval_snapshot_cache_runtime.py:73",
    ),
    (
        "_notebook_langs_cache",
        "backend/tests/test_retrieval_snapshot_cache_runtime.py:77",
    ),
    (
        "backfill_chunk_fts",
        "backend/tests/test_retrieval_snapshot_cache_runtime.py:78",
    ),
    # Final-review Fix 2 (paper-metadata): ensure_paper_metadata's try/except
    # now wraps the whole body; the new setup-phase-failure test checks
    # source_paper_meta directly (rather than via repo.get_paper_meta, which
    # is monkeypatched in that test) — a fresh repo._connect() consumer site.
    ("_connect", "backend/tests/test_paper_meta_service.py:294"),
}

# sqlite connection reuse: Change-4 line shift in test_node_context_steps.py +
# new close_local member + new test_sqlite_connection_reuse.py consumers.
# These are exact new test consumers; the frozen production surface and its
# coordinates stay intact. create_notebook/_test_insert_object/node_context
# land on their new post-rewrite lines inside
# test_node_context_legacy_fallback_query_is_bound_by_section_path (the old
# recorded coordinates are filtered via FROZEN_ONLY_MOVED_CONSUMERS below);
# _connect gains three fresh call sites spying on the reused thread-local
# connection (node_context's own _connect() call plus the new facade
# close_local delegate test).
SQLITE_CONN_REUSE_ALLOWED_CONSUMERS = {
    ("create_notebook", "backend/tests/test_node_context_steps.py:62"),
    ("_test_insert_object", "backend/tests/test_node_context_steps.py:63"),
    ("_connect", "backend/tests/test_node_context_steps.py:67"),
    ("node_context", "backend/tests/test_node_context_steps.py:70"),
    ("_connect", "backend/tests/test_sqlite_connection_reuse.py:170"),
    ("_connect", "backend/tests/test_sqlite_connection_reuse.py:172"),
}

FROZEN_ONLY_MOVED_CONSUMERS = {
    # Test-suite cleanup: deleted redundant tests removed these monkeypatch/consumer sites
    # from the frozen fixture's recorded set (member+file coverage retained by kept tests).
    ('USABLE_STATUSES', 'backend/tests/test_cross_tier_reasoning.py:<line>'),
    ('_COPY_CHUNK', 'backend/tests/test_architecture_module_boundaries.py:<line>'),
    ('_REQUEST_USER', 'backend/tests/test_architecture_module_boundaries.py:<line>'),
    ('_connect', 'backend/tests/test_two_tier_federated.py:<line>'),
    ('_open_scale_ann', 'backend/tests/test_scale_index_repo.py:<line>'),
    ('_remap_json_ids', 'backend/tests/test_architecture_module_boundaries.py:<line>'),
    ('_scale_idx_cache', 'backend/tests/test_scale_idx_disk_cache.py:<line>'),
    ('_viz_index_dir', 'backend/tests/test_viz_index_wire.py:<line>'),
    ('notebook_copy_stats', 'backend/tests/test_notebook_share_copy.py:<line>'),
    ('reset_request_user', 'backend/tests/test_architecture_module_boundaries.py:<line>'),
    ('set_request_user', 'backend/tests/test_architecture_module_boundaries.py:<line>'),
    # Authenticated promotion routes now call explicit reviewer-aware adapters;
    # the frozen facade methods retain their original signatures for callers.
    ("approve_promotion", "backend/app/api/routes.py:<line>"),
    ("reject_promotion", "backend/app/api/routes.py:<line>"),
    # KG build routes now prepare durable jobs and submit the task-scoped
    # executor; extraction resolves the explicit client through the service.
    ("build_notebook_kg", "backend/app/api/routes.py:<line>"),
    ("rebuild_notebook_kg", "backend/app/api/routes.py:<line>"),
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
    # sqlite connection reuse: Change 4 rewrote
    # test_node_context_legacy_fallback_query_is_bound_by_section_path to spy
    # via conn.set_trace_callback() on the reused thread-local connection
    # instead of monkeypatching sqlite3.connect, shifting these three frozen-
    # recorded call sites down to :62/:63/:70 (new sites registered in
    # SQLITE_CONN_REUSE_ALLOWED_CONSUMERS above).
    ("create_notebook", "backend/tests/test_node_context_steps.py:60"),
    ("_test_insert_object", "backend/tests/test_node_context_steps.py:61"),
    ("node_context", "backend/tests/test_node_context_steps.py:74"),
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


# Test-suite cleanup: deleting redundant tests shifted these compat-import lines.
TEST_CLEANUP_SHIFTED_IMPORTS = {
    ('backend/tests/test_notebook_share_copy.py', 60, 'app.services.sqlite_repository', '_remap_json_ids'),
    ('backend/tests/test_notebook_share_copy.py', 86, 'app.services.sqlite_repository', '_now'),
    ('backend/tests/test_kg_repository.py', 367, 'app.services.sqlite_repository', '_now'),
    # 656->661: knowhow-tables Task 1's MIGRATION_MANIFEST v16 comment
    # expansions (INDEPENDENT_SQL_SITES + SQLITE_CONNECT_SITES, +5 net lines)
    # in test_repository_callers_static.py shift this import site further.
    # 661->670: knowhow-tables Task 6's new INDEPENDENT_PRIVATE_SITES entry
    # (the api.py `_runtime` registration, +9 net lines) shifts it again.
    # 670->690: paper-metadata Task 1's v17 comment expansions, knowhow-tables
    # PR-2+3 Task 1's v18 regenerated line pins (INDEPENDENT_SQL_SITES +
    # SQLITE_CONNECT_SITES), and PR-2+3 Task 8's new INDEPENDENT_PRIVATE_SITES
    # entry (the api.py optimize_cell `_runtime` registration) in
    # test_repository_callers_static.py cumulatively shift this import site.
    # 690->695: source-asset-linking Task 2's v19 comment expansions in
    # test_repository_callers_static.py (SQLITE_CONNECT_SITES +2 net lines,
    # then INDEPENDENT_SQL_SITES' verify_repository_snapshot.py allowlist +3
    # net lines) cumulatively shift it again.
    # 695->731: MinerU embedded-image-retention's merge_dbs.py reconciliation
    # adds a new INDEPENDENT_SQL_SITES entry (scripts/merge_dbs.py's 21
    # execute-shaped call sites, +30 net lines) and two new SQLITE_CONNECT_SITES
    # entries (+6 net lines) to test_repository_callers_static.py, cumulatively
    # shifting this import site by +36.
    # 731->772: rebasing the MinerU embedded-image-retention feature branch
    # onto that master tip replays the feature's own Task 8 additions to this
    # file (source-asset-linking's AssetService / make_persist_image_factory
    # INDEPENDENT_SQL_SITES + SQLITE_CONNECT_SITES entries) on top of the
    # already-merged merge_dbs.py reconciliation, adding +41 more lines above
    # this deferred import.
    # 772->776: knowhow anchor-grouping-display's INDEPENDENT_PRIVATE_SITES
    # comment expansion (api.py optimize_cell's `_runtime` site, 693->716,
    # +4 net lines) shifts it again.
    ('backend/tests/test_repository_callers_static.py', 776, 'app.services.sqlite_repository', 'SQLiteRepository'),
    ('backend/tests/test_followup_retrieval_grounding.py', 102, 'app.services.sqlite_repository', 'SQLiteRepository'),
}

# KG task circuit-breaker: these regression tests intentionally compose the
# compatibility facade while the new durable-job entry points post-date the
# frozen surface fixture.
KG_BUILD_CIRCUIT_ALLOWED_IMPORTS = {
    (
        "backend/tests/test_kg_build_circuit_breaker.py",
        14,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
    (
        "backend/tests/test_kg_build_job_store.py",
        9,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
    (
        "backend/tests/test_kg_rebuild_relink_api.py",
        184,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
}
KG_BUILD_CIRCUIT_ALLOWED_NEW_MEMBERS = {
    "prepare_notebook_kg_job",
    "fail_notebook_kg_job_submission",
    "execute_notebook_kg_job",
}
KG_BUILD_CIRCUIT_ALLOWED_MEMBER_FILES = {
    (path, member)
    for path, members in {
        "backend/tests/test_kg_build_circuit_breaker.py": {
            "SQLiteRepository", "_connect", "_kg_llm_client", "_runtime",
            "_write", "create_notebook", "embedder",
            "execute_notebook_kg_job", "get_notebook",
            "prepare_notebook_kg_job",
        },
        "backend/tests/test_kg_build_job_store.py": {
            "SQLiteRepository", "_runtime", "create_notebook", "current_user",
        },
        "backend/tests/test_kg_building_flag.py": {
            "SQLiteRepository", "_kg_building", "_mark_unified_kg_dirty",
            "_runtime", "build_notebook_kg", "create_notebook", "embedder",
            "get_notebook", "llm_client", "rebuild_notebook_kg",
        },
        "backend/tests/test_index_build_consolidation.py": {
            "SQLiteRepository", "_dequeue_scale_idle", "_runtime",
            "_scale_building", "_scale_building_lock", "_scale_idle_queue",
            "_scale_idx_cache", "_write", "build_scale_index",
            "cancel_scale_index", "create_notebook", "embedder",
            "get_notebook", "index_status", "rebuild_unified_kg",
            "scale_index_status", "settings", "unified_kg_status",
        },
        "backend/tests/test_kg_llm_client.py": {
            "SQLiteRepository", "_kg_llm_client", "_run_extraction",
            "_runtime", "_write", "create_notebook", "embedder",
            "kg_llm_client", "llm_client", "source_elements",
        },
        "backend/tests/test_kg_rebuild_relink_api.py": {
            "SQLiteRepository", "_kg_llm_client", "_runtime", "llm_client",
            "rebuild_notebook_kg", "relink_notebook_kg",
        },
        "backend/tests/test_resolve_notebook_conflicts.py": {
            "SQLiteRepository", "_connect", "_runtime", "build_notebook_kg",
            "create_notebook", "embedder", "llm_client",
            "pending_conflicts", "relations_for_notebook",
            "resolve_notebook_conflicts", "settings", "store_kg",
        },
    }.items()
    for member in members
}
ALL_TASK_ALLOWED_MEMBER_FILES = (
    ALL_TASK_ALLOWED_MEMBER_FILES | KG_BUILD_CIRCUIT_ALLOWED_MEMBER_FILES
)


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
                    or site in TASK2_MEMORY_ALLOWED_IMPORTS
                    or site in TASK5_MEMORY_ALLOWED_IMPORTS
                    or site in TASK6_MEMORY_ALLOWED_IMPORTS
                    or site in TASK8_MEMORY_ALLOWED_IMPORTS
                    or site in SQLITE_CONN_REUSE_ALLOWED_IMPORTS
                    or site in TEST_CLEANUP_SHIFTED_IMPORTS
                    or site in TASK1_MEMORY_KG_ALLOWED_IMPORTS
                    or site in TASK2_MEMORY_KG_ALLOWED_IMPORTS
                    or site in TASK3_MEMORY_KG_ALLOWED_IMPORTS
                    or site in TASK5_MEMORY_KG_ALLOWED_IMPORTS
                    or site in TASK1_KNOWHOW_ALLOWED_IMPORTS
                    or site in TASK2_KNOWHOW_ALLOWED_IMPORTS
                    or site in TASK4_KNOWHOW_ALLOWED_IMPORTS
                    or site in TASK5_KNOWHOW_ALLOWED_IMPORTS
                    or site in TASK6_KNOWHOW_ALLOWED_IMPORTS
                    or site in TASK1_PAPER_META_ALLOWED_IMPORTS
                    or site in TASK3_PAPER_META_ALLOWED_IMPORTS
                    or site in TASK4_PAPER_META_ALLOWED_IMPORTS
                    or site in TASK3_KNOWHOW_PR23_ALLOWED_IMPORTS
                    or site in TASK14_KNOWHOW_PR23_ALLOWED_IMPORTS
                    or site in TASK8_KNOWHOW_PR23_ALLOWED_IMPORTS
                    or site in TASK13_KNOWHOW_PR23_ALLOWED_IMPORTS
                    or site in TASK12B_KNOWHOW_PR23_ALLOWED_IMPORTS
                    or site in MERGE_DBS_ALLOWED_IMPORTS
                    or site in TASK2_SOURCE_ASSET_ALLOWED_IMPORTS
                    or site in TASK3_SOURCE_ASSET_ALLOWED_IMPORTS
                    or site in PAPER_META_STATUS_TASK4_ALLOWED_IMPORTS
                    or site in ASSET_GC_TRIGGER_ALLOWED_IMPORTS
                    or site in KNOWHOW_TRANSFER_STORE_ALLOWED_IMPORTS
                    or site in KNOWHOW_TRANSFER_SERVICE_ALLOWED_IMPORTS
                    or site in KNOWHOW_TRANSFER_ROUTES_ALLOWED_IMPORTS
                    or site in MEMORY_TRANSFER_STORE_ALLOWED_IMPORTS
                    or site in MEMORY_TRANSFER_SERVICE_ALLOWED_IMPORTS
                    or site in MEMORY_TRANSFER_ROUTES_ALLOWED_IMPORTS
                    or site in KG_BUILD_CIRCUIT_ALLOWED_IMPORTS
                )


EXPECTED_PATCH_DELTAS = {
    'recorded_only': {
        ('backend/tests/test_answer_context_budget.py', 29, '_concept_cluster_id', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 30, 'node_context', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 44, '_concept_cluster_id', 'repo'),
        ('backend/tests/test_answer_context_budget.py', 45, 'node_context', 'repo'),
        ('backend/tests/test_architecture_hardening.py', 55, '_retrieve_chunks', 'repo'),
        ('backend/tests/test_architecture_hardening.py', 102, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_ask_modes.py', 24, 'ask_chunk', 'repo'),
        ('backend/tests/test_ask_modes.py', 24, 'ask_graph', 'repo'),
        ('backend/tests/test_ask_modes.py', 24, 'ask_reasoning', 'repo'),
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
        ('backend/tests/test_batch_ingest.py', 284, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 287, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 288, '_set_source_status', 'repo'),
        ('backend/tests/test_batch_ingest.py', 289, '_mark_unified_kg_dirty', 'repo'),
        ('backend/tests/test_batch_ingest.py', 290, 'relink_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 302, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 310, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 311, '_set_source_status', 'repo'),
        ('backend/tests/test_batch_ingest.py', 312, '_mark_unified_kg_dirty', 'repo'),
        ('backend/tests/test_batch_ingest.py', 313, 'relink_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 458, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 462, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 464, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 486, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 489, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 490, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 503, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 517, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 540, 'extract_source', 'repo'),
        ('backend/tests/test_batch_ingest.py', 542, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 590, 'build_scale_index', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1179, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1192, 'build_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1200, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1208, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1211, '_run_extraction', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1217, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1243, 'rebuild_unified_kg', 'SQLiteRepository'),
        ('backend/tests/test_bm25_rrf.py', 135, '_rrf_scored', 'repo'),
        ('backend/tests/test_bm25_rrf.py', 150, '_rrf_scored', 'repo'),
        ('backend/tests/test_chunk_bruteforce_guard.py', 73, '_gather_chunks', 'repo'),
        ('backend/tests/test_chunk_bruteforce_guard.py', 134, '_embed_query', 'repo'),
        ('backend/tests/test_chunk_embed.py', 99, '_run_extraction', 'repo'),
        ('backend/tests/test_chunk_retrieval.py', 240, 'ask_chunk', 'repo'),
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
        ('backend/tests/test_notebook_share_copy.py', 339, '_COPY_CHUNK', 'sr'),
        ('backend/tests/test_notebook_share_copy.py', 355, '_insert_row', 'repo'),
        ('backend/tests/test_notebook_share_copy.py', 400, '_COPY_CHUNK', 'sr'),
        ('backend/tests/test_notebook_share_copy.py', 411, '_insert_row', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 63, 'federated_retrieve', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 63, 'federated_retrieve_relations', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 71, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_overlay_guard_order.py', 93, 'notebook_copy_stats', 'repo'),
        ('backend/tests/test_p4_kg_shrink.py', 82, '_run_extraction', 'repo'),
        ('backend/tests/test_p4_kg_shrink.py', 96, '_run_extraction', 'repo'),
        ('backend/tests/test_pending_actions.py', 104, 'scale_index_status', 'repo'),
        ('backend/tests/test_pending_actions.py', 116, 'scale_index_status', 'repo'),
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
        ('backend/tests/test_rebuild_cache.py', 222, '_now', 'repo_mod'),
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
        ('backend/tests/test_scale_index_repo.py', 231, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_scale_index_repo.py', 871, 'fold_scale_index_delta', 'repo'),
        ('backend/tests/test_scale_index_repo.py', 872, 'build_scale_index', 'repo'),
        ('backend/tests/test_scale_version_probe.py', 79, '_connect', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 188, '_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 221, '_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 222, '_delta_vector_matrix', 'repo'),
        ('backend/tests/test_scale_xlayer_bridge_delta.py', 248, '_scale_xlayer_bridge_edges', 'repo'),
        ('backend/tests/test_sources_page_batched.py', 183, '_connect', 'repo'),
        ('backend/tests/test_sqlite_write_optimization.py', 121, '_write', 'embed_repo'),
        ('backend/tests/test_viz_bounded.py', 118, '_unified_graph_full', 'repo'),
    },
    'actual_only': {
        ('backend/tests/test_kg_llm_client.py', 88, 'source_elements', 'repo'),
        ('backend/tests/test_batch_ingest.py', 288, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 293, '_mark_unified_kg_dirty', 'repo'),
        ('backend/tests/test_batch_ingest.py', 306, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 316, '_mark_unified_kg_dirty', 'repo'),
        ('backend/tests/test_batch_ingest.py', 462, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 468, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 490, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 494, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 507, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 522, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 549, 'extract_source', 'repo'),
        ('backend/tests/test_batch_ingest.py', 551, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 599, 'build_scale_index', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1188, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1201, 'build_notebook_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1209, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1217, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1226, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1252, 'rebuild_unified_kg', 'SQLiteRepository'),
        ('backend/tests/test_batch_ingest.py', 1446, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1464, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1480, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1499, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1548, 'llm_client', 'repo'),
        ('backend/tests/test_batch_ingest.py', 1565, 'rebuild_unified_kg', 'repo'),
        ('backend/tests/test_embedding_store_component.py', 59, '_write', 'repo'),
        ('backend/tests/test_embedding_store_component.py', 135, '_write', 'repo'),
        ('backend/tests/test_in_batching.py', 85, '_IN_CHUNK', 'SQLiteRepository'),
        # PR-2+3 Task 13 (full deep-copy with id remap): failure-injection
        # test mirrors test_notebook_copy_service.py's own _new_id/_insert_row
        # monkeypatch idiom immediately above/below (same "actual but not a
        # SurfaceMember consumer record" bucket, not a new pattern).
        ('backend/tests/test_knowhow_copy.py', 492, '_new_id', 'sr'),
        ('backend/tests/test_knowhow_copy.py', 501, '_insert_row', 'repo'),
        # PR review round 3 P1-1 removed this entry: the A4 fault-injection
        # test used to patch SQLiteRepository.delete_knowhow_table (the whole
        # class, so the boom applied regardless of which SQLiteRepository
        # instance the route handler's repository() singleton resolves to —
        # same idea as test_in_batching.py's SQLiteRepository-base entry
        # above). move_table no longer calls repo.delete_knowhow_table at
        # all — its cleanup delete now goes through KnowhowTransferStore.
        # delete_table_if_unchanged (the new atomic conditional delete; see
        # that method's docstring and transfer.py's move_table), so the test
        # was updated to patch THAT class instead (still class-level, same
        # reason — the route handler's repository() singleton is a different
        # SQLiteRepository instance, and therefore a different
        # KnowhowTransferStore instance, than this test's own `repo`
        # fixture). KnowhowTransferStore is not SQLiteRepository and not a
        # "repo"/"*_repo"-named variable, so `_static_repository_patches()`
        # (this set's own scanner — see its `direct_repo`/`repository_class`
        # gating) does not observe that new patch site at all; there is
        # nothing to add here, only this stale entry to remove.
        ('backend/tests/test_knowledge_governance_delegation.py', 131, 'set_conflict_status', 'repo'),
        ('backend/tests/test_notebook_copy_service.py', 93, '_new_id', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 118, '_COPY_CHUNK', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 146, '_insert_row', 'repo'),
        ('backend/tests/test_notebook_copy_service.py', 167, '_COPY_CHUNK', 'sqlite_repository'),
        ('backend/tests/test_notebook_copy_service.py', 177, '_insert_row', 'repo'),
        ('backend/tests/test_notebook_share_copy.py', 318, '_COPY_CHUNK', 'sr'),
        ('backend/tests/test_notebook_share_copy.py', 334, '_insert_row', 'repo'),
        ('backend/tests/test_notebook_share_copy.py', 379, '_COPY_CHUNK', 'sr'),
        ('backend/tests/test_notebook_share_copy.py', 390, '_insert_row', 'repo'),
        ('backend/tests/test_rebuild_cache.py', 215, '_now', 'repo_mod'),
        ('backend/tests/test_repository_runtime.py', 19, '_now', 'sqlite_repository'),
        ('backend/tests/test_scale_index_repo.py', 209, 'rebuild_unified_kg', 'repo'),
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
        ('backend/tests/test_sqlite_write_optimization.py', 128, '_write', 'embed_repo'),
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


# Task 10 (knowhow-tables PR-2+3): the agent surface. get_knowhow_row_location
# is a brand-new facade delegate (KnowhowStore.get_knowhow_row_location,
# resolving a bare row_id to its {table_id, notebook_id} for the new
# session-or-agent-token HTTP/MCP surface, which carries no notebook_id/
# table_id in its own URL at all) — exempt its consumer-site comparison
# entirely, exactly like TASK1_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS does for the
# sibling store methods Task 1 added (its own many call sites, across
# knowhow_agent_routes.py/mcp_server.py/services/knowhow/api.py, need no
# per-site registration once the member itself is popped from comparison).
TASK10_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS = {"get_knowhow_row_location"}
# app.api.deps's new _resolve_session_user (the "session OR Agent token"
# dependency's session branch, appended at EOF of deps.py — see that file's
# own Task 10 section header) reaches two PRE-EXISTING frozen members
# (resolve_session/current_user) a SECOND time, at new lines distinct from
# get_current_user's own already-registered call sites. deps.py is itself
# line-number-insensitive (LINE_NUMBER_INSENSITIVE_FILES above), but that
# only governs the FINAL normalized-string comparison — the raw exact-line
# pre-filter (_member_file_site_allowed's frozen=False branch, production
# files require an exact ACTIVE_PRODUCTION_MEMBER_SITES/allowed_sites match)
# still needs this SECOND real-line pin registered, mirroring the pattern
# TASK6_KNOWHOW_ALLOWED_CONSUMERS uses for api.py's own _runtime/settings
# reaches.
TASK10_KNOWHOW_PR23_ALLOWED_CONSUMERS = {
    ("resolve_session", "backend/app/api/deps.py:142"),
    ("current_user", "backend/app/api/deps.py:147"),
}

# Followup A (anchor-grouping display spec §6「整组批量写单事务，不半改」):
# update_knowhow_cells is a brand-new facade delegate (KnowhowStore's batch
# sibling of update_knowhow_cell — upserts the SAME column across MULTIPLE
# rows in ONE write transaction, so a merged/shared cell edit is all-or-
# nothing instead of the frontend's old best-effort per-row Promise.all).
# Exempt its consumer-site comparison entirely, exactly like
# TASK10_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS does above for
# get_knowhow_row_location — its one call site (the new batch PATCH
# .../knowhow/{table_id}/cells endpoint in routes.py) needs no per-site
# registration once the member itself is popped from comparison.
FOLLOWUP_A_ALLOWED_NEW_MEMBERS = {"update_knowhow_cells"}


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
        for file, line, _module, member in TASK2_ALLOWED_IMPORTS | TASK2_MEMORY_ALLOWED_IMPORTS | TASK5_MEMORY_ALLOWED_IMPORTS | TASK6_MEMORY_ALLOWED_IMPORTS | TASK8_MEMORY_ALLOWED_IMPORTS | TASK7_ALLOWED_IMPORTS | TASK8_ALLOWED_IMPORTS | TASK9_ALLOWED_IMPORTS | TASK23_ALLOWED_IMPORTS | TASK26_ALLOWED_IMPORTS | TASK27_ALLOWED_IMPORTS | TASK28_ALLOWED_IMPORTS | SQLITE_CONN_REUSE_ALLOWED_IMPORTS | MERGE_DBS_ALLOWED_IMPORTS | KG_BUILD_CIRCUIT_ALLOWED_IMPORTS
    }
    allowed_sites |= TASK1_MEMORY_ALLOWED_CONSUMERS | TASK7_MEMORY_ALLOWED_CONSUMERS | TASK2_ALLOWED_CONSUMERS | TASK7_ALLOWED_CONSUMERS | TASK8_ALLOWED_CONSUMERS | TASK9_ALLOWED_CONSUMERS | TASK12_ALLOWED_CONSUMERS | TASK27_ALLOWED_CONSUMERS | TASK28_ALLOWED_CONSUMERS | REVIEW_FIX_ALLOWED_CONSUMERS | SQLITE_CONN_REUSE_ALLOWED_CONSUMERS | TASK4_KNOWHOW_ALLOWED_CONSUMERS | TASK6_KNOWHOW_ALLOWED_CONSUMERS | TASK8_KNOWHOW_PR23_ALLOWED_CONSUMERS | TASK10_KNOWHOW_PR23_ALLOWED_CONSUMERS
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
    for name in (
        TASK2_MEMORY_ALLOWED_NEW_MEMBERS
        | TASK3_MEMORY_ALLOWED_NEW_MEMBERS
        | TASK6_MEMORY_ALLOWED_NEW_MEMBERS
        | TASK7_MEMORY_ALLOWED_NEW_MEMBERS
        | TASK8_MEMORY_ALLOWED_NEW_MEMBERS
        | SQLITE_CONN_REUSE_ALLOWED_NEW_MEMBERS
        | STARTUP_READINESS_ALLOWED_NEW_MEMBERS
        | TASK3_MEMORY_KG_ALLOWED_NEW_MEMBERS
        | TASK2_KNOWHOW_ALLOWED_NEW_MEMBERS
        | TASK4_PAPER_META_ALLOWED_NEW_MEMBERS
        | TASK1_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS
        | TASK10_KNOWHOW_PR23_ALLOWED_NEW_MEMBERS
        | TASK3_SOURCE_ASSET_ALLOWED_NEW_MEMBERS
        | FOLLOWUP_A_ALLOWED_NEW_MEMBERS
        | PAPER_META_STATUS_ALLOWED_NEW_MEMBERS
        | KG_BUILD_CIRCUIT_ALLOWED_NEW_MEMBERS
    ):
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


# PR-2+3 Task 3 (knowhow-tables editing API + ProjectionScheduler): its own
# HTTP-level test composes the real facade the same way Task 1/2/4/5/6's
# sibling tests do (register/login via HTTP, then `repo.add_member` directly
# for the read-only-member fixture — there is no HTTP "add member by id"
# endpoint — and `repo._connect` for direct knowledge_objects-count
# assertions). Appended at EOF (not interleaved into the existing
# TASK*_KNOWHOW blocks above) so this registration cannot shift any of the
# other exact-line-pinned entries already in this very large file — see
# TASK6_KNOWHOW_ALLOWED_CONSUMERS above, whose own line numbers this task
# already had to bump once for exactly that reason.
TASK3_KNOWHOW_PR23_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_editing_api.py", 23, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK3_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_editing_api.py", name)
    for name in {"SQLiteRepository", "_connect", "add_member"}
}
# ALL_TASK_ALLOWED_MEMBER_FILES (defined far above, near the other
# TASKn_ALLOWED_MEMBER_FILES unions) is a plain module-level tuple-expression
# assignment evaluated at import time — it cannot forward-reference a name
# defined here at EOF the way the OR-chain inside a function body can (that
# one resolves lazily, at call time). Re-binding it here, AFTER this
# constant exists, keeps this task's registration a pure EOF append with
# zero risk of shifting any of the many exact-line-pinned entries earlier in
# this file, at the cost of one extra rebinding statement instead of an
# inline union.
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK3_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# PR-2+3 Task 14 (asset GC): its own test composes the real facade the same
# way Task 1/2/3/4/5/6's sibling tests do — repo.create_notebook to seed
# fixtures, repo.delete_notebook to exercise the notebook-delete asset-dir
# sweep (both pre-existing frozen members; new call sites here are registered
# as a broad allowance rather than pinned to exact lines, same style as
# TASK2_KNOWHOW_ALLOWED_MEMBER_FILES/TASK5_KNOWHOW_ALLOWED_MEMBER_FILES
# above). sweep_orphan_assets itself lives on SQLiteMaintenanceAdapter
# (backend/app/repositories/sqlite/maintenance.py), reached only through
# repo.maintenance.sweep_orphan_assets(...) — the `maintenance` property
# Task 27 already exempted wholesale (TASK27_ALLOWED_NEW_MEMBERS), and
# sweep_orphan_assets is never itself a SQLiteRepository member, so nothing
# new to register for that half. Appended at EOF for the same zero-line-shift
# reason as TASK3_KNOWHOW_PR23_ALLOWED_IMPORTS above.
TASK14_KNOWHOW_PR23_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_asset_gc.py", 25, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK14_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_asset_gc.py", name)
    for name in {"SQLiteRepository", "create_notebook", "delete_notebook"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK14_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# PR-2+3 Task 8 (LLM cell rewrite): app/services/knowhow/api.py's optimize_cell
# reaches `_runtime` a second, independent time (see
# test_repository_callers_static.py's INDEPENDENT_PRIVATE_SITES for the
# sibling registration) to resolve the per-user rewrite LLM client +
# note_model_error — the same narrow-runtime-port pattern build_projector's
# own `_runtime`/`settings` registration (TASK6_KNOWHOW_ALLOWED_CONSUMERS
# above) already uses. `settings` isn't re-reached here so only `_runtime`
# needs a new entry. 682->693: the get_scheduler weakref fix added lines
# above. 693->716: anchor-grouping-display's forward_fill_column import plus
# import_table's/commit_append's forward-fill additions add +23 net lines
# further above optimize_cell.
TASK8_KNOWHOW_PR23_ALLOWED_CONSUMERS = {
    ("_runtime", "backend/app/services/knowhow/api.py:880"),
}
# Its own HTTP-level test reaches the live app repository singleton via
# app.api.deps.repository() (not a freshly constructed SQLiteRepository) to
# inject a fake rewrite LLM client in-process and, for the reader-permission
# case, repo.add_member — mirrors Task 3/6's sibling test files' own
# SQLiteRepository/add_member direct-facade need, just reached through the
# app's own singleton accessor instead of constructing a second instance
# (this test's fake LLM client must be visible to the SAME repository object
# routes.py's dependency injection resolves, which a second, separately
# constructed SQLiteRepository would not be).
TASK8_KNOWHOW_PR23_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_optimize.py", 15, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK8_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_optimize.py", name)
    for name in {"SQLiteRepository", "add_member", "_rewrite_llm_client"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK8_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# PR-2+3 Task 13 (full deep-copy with id remap, zero re-embed): its own test
# composes the real facade the same way Task 1/2/3/4/5/6/8/14's sibling test
# files do (SQLiteRepository + a fake embedder mirroring
# test_knowhow_projection.py's own fixture, plus repo._connect/_write/
# _insert_row/_new_id direct-DB peeks for id-remap/compensation assertions —
# same broad "this whole test file may reference this member name" allowance
# style as TASK14_KNOWHOW_PR23_ALLOWED_MEMBER_FILES above, not a new pattern).
# Appended at EOF for the same zero-line-shift reason as every other
# TASKN_KNOWHOW_PR23_* block above.
TASK13_KNOWHOW_PR23_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_copy.py", 38, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_knowhow_copy.py", 38, "app.services.sqlite_repository", "_now"),
}
TASK13_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_copy.py", name)
    for name in {
        "SQLiteRepository", "_now", "_connect", "_write", "_insert_row", "_new_id",
        "_runtime", "copy_notebook", "create_notebook", "embedder", "settings",
        "storage_dir",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK13_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# PR-2+3 Task 15 (cross-task safety net: code-isolation guard + PR-2/3
# integration tests + permission-matrix extension). Its two new test files
# both mirror test_knowhow_retrieval.py's own fixture — the app's real
# repository singleton via app.api.routes.repository(), never a freshly
# constructed SQLiteRepository (needed so a background projection job, which
# runs against that SAME singleton, actually sees the fake/recording
# embedder installed on it) — so, unlike Task 13's file, no
# SQLiteRepository/_now import registration is needed here at all; only the
# direct facade calls each file makes for its own DB-level assertions:
# repo._connect (raw peeks at source_elements/chunks/knowledge_objects/
# knowledge_relations/knowhow_cell_code), repo._retrieve_chunks +
# repo._runtime.knowledge.chunk_fts_search + repo.ask_chunk (the isolation
# file's "ask 上下文组装" surface), and repo._citations_from (the
# integration file's real-element citation-enrichment scenario). Its
# companion edit to test_knowhow_editing_api.py's permission matrix (same
# commit) adds zero NEW facade-member call sites — only TestClient HTTP
# verbs plus a local openpyxl import — so it needs no new registration
# beyond its own pre-existing Task 3 entry above. Appended at EOF for the
# same zero-line-shift reason as every other TASKN_KNOWHOW_PR23_* block.
TASK15_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_code_isolation.py", name)
    for name in {"_connect", "_retrieve_chunks", "_runtime", "ask_chunk"}
} | {
    ("backend/tests/test_knowhow_pr23_integration.py", name)
    for name in {"_connect", "_retrieve_chunks", "_citations_from"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK15_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# PR-2+3 Task 12b (citation-jump widening: chunk/graph modes + anchor path).
# test_knowhow_citation.py, previously a pure-fake unit test file (no facade
# at all — _Notebooks/_Knowledge/_SpySources fakes only), gains two real-
# SQLite integration tests mirroring test_knowhow_projection.py's own
# repo/embedder/projector fixture convention (chunk-mode: create_notebook +
# a fresh SQLiteRepository + embedder + create_knowhow_table/add_knowhow_row
# via repo._runtime.knowhow_store, then repo.ask_chunk) and
# test_graph_src_chunks.py's raw-SQL-seed + stub-LLM convention (graph-mode:
# repo._write for the seed rows, repo.settings/repo.llm_client/
# repo._reasoning_llm_client, then repo.ask_graph) — plus a small
# repo._runtime.source_store.evidence_elements call-count spy shared by both,
# reached via repo._runtime the same way TASK6_KNOWHOW_ALLOWED_CONSUMERS'
# build_projector helper already does. Its own import of SQLiteRepository is
# a genuinely NEW site (T12's original file never imported it), so — unlike
# Task 15's file above — this DOES need an ALLOWED_IMPORTS entry too, wired
# into the OR-chain inside test_compatibility_exports_and_import_consumers_
# are_complete (a function-body edit, safe: nothing outside this guard file
# tracks ITS OWN internal line numbers). Appended at EOF for the same
# zero-line-shift reason as every other TASKN_KNOWHOW_PR23_* block.
TASK12B_KNOWHOW_PR23_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_citation.py", 45, "app.services.sqlite_repository", "SQLiteRepository"),
    # knowhow KG-node retrieval, gate ii (test_knowhow_graph_anchor.py):
    # imports SQLiteRepository (line 37) to build an isolated real repo for the
    # `_federated_rx_graph._load` gate integration + mode=graph e2e tests. Every
    # facade member is reached through a `store`-named handle returned from a
    # `_new_repo()` factory — never a `repo`/`*_repo`-named handle nor a local
    # assigned straight from `SQLiteRepository(...)` — so the consumer scan
    # records ONLY this import site (covered by the SQLiteRepository member-file
    # entry below); no private-member churn. Same file/line pin caveat as every
    # other entry here: if the import line moves, update 37.
    ("backend/tests/test_knowhow_graph_anchor.py", 37, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK12B_KNOWHOW_PR23_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_citation.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_write", "ask_chunk", "ask_graph",
        "create_notebook", "embedder", "llm_client", "_reasoning_llm_client",
        "settings",
    }
} | {
    # gate ii (see the import block above): the consumer scan records this test
    # file's single facade touch — the SQLiteRepository import — under member
    # name "SQLiteRepository"; every real repo member access is evaded via the
    # `store`/`_new_repo()` pattern, so nothing else needs listing (line-
    # insensitive, so it survives any test-line renumbering).
    ("backend/tests/test_knowhow_graph_anchor.py", "SQLiteRepository"),
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK12B_KNOWHOW_PR23_ALLOWED_MEMBER_FILES

# source-asset-linking Task 2 (_migration_19: notebook_assets.source_id):
# test_source_asset_migration.py is a genuinely NEW test file (new facade
# import site), constructed via the same repo(tmp_path)-style fixture
# convention as test_knowhow_schema.py / test_memory_kg_schema.py — an
# explicit `from app.services.sqlite_repository import SCHEMA_VERSION,
# SQLiteRepository` (line 18) plus repo._connect()/._write()/._migrate()
# calls. Wired into the OR-chain inside
# test_compatibility_exports_and_import_consumers_are_complete (function-body
# edit, appended at EOF here for the same zero-line-shift reason as every
# other TASKN_* block above).
TASK2_SOURCE_ASSET_ALLOWED_IMPORTS = {
    ("backend/tests/test_source_asset_migration.py", 18, "app.services.sqlite_repository", "SCHEMA_VERSION"),
    ("backend/tests/test_source_asset_migration.py", 18, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK2_SOURCE_ASSET_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_source_asset_migration.py", name)
    for name in {"SCHEMA_VERSION", "SQLiteRepository", "_connect", "_migrate", "_write"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK2_SOURCE_ASSET_ALLOWED_MEMBER_FILES

# source-asset-linking Task 3 (KnowhowStore.insert_notebook_asset gains a
# source_id param + two new one-hop delegates, source_asset_ids/
# delete_source_asset_rows, for per-source query/delete of MinerU-extracted
# embedded-image asset rows): test_source_asset_store.py is a genuinely NEW
# test file exercising the real facade the same way test_knowhow_store.py's
# sibling Task 2 (knowhow-tables-pr1) test does above
# (TASK2_KNOWHOW_ALLOWED_IMPORTS/_MEMBER_FILES/_NEW_MEMBERS) — an explicit
# `from app.services.sqlite_repository import SQLiteRepository` (line 17)
# plus create_notebook to seed a notebook_id fixture. insert_notebook_asset/
# get_notebook_asset are also called here but need no new allowance of their
# own: they are already wholesale-exempted from the consumer-scan comparison
# by TASK2_KNOWHOW_ALLOWED_NEW_MEMBERS (their signature/consumer set was
# never added to the frozen facade_surface.json fixture in the first place).
# source_asset_ids/delete_source_asset_rows are brand-new members that
# likewise predate no frozen fixture, so they get the same wholesale
# consumer-scan exemption here. Wired into the OR-chain inside
# test_compatibility_exports_and_import_consumers_are_complete and the
# ALLOWED_NEW_MEMBERS pop-loop inside
# test_static_repository_consumer_scan_matches_manifest_exactly (both
# function-body edits, appended at EOF here for the same zero-line-shift
# reason as every other TASKN_* block above).
TASK3_SOURCE_ASSET_ALLOWED_IMPORTS = {
    ("backend/tests/test_source_asset_store.py", 17, "app.services.sqlite_repository", "SQLiteRepository"),
}
TASK3_SOURCE_ASSET_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_source_asset_store.py", name)
    for name in {"SQLiteRepository", "create_notebook"}
}
TASK3_SOURCE_ASSET_ALLOWED_NEW_MEMBERS = {
    "source_asset_ids", "delete_source_asset_rows",
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | TASK3_SOURCE_ASSET_ALLOWED_MEMBER_FILES

# MinerU cloud file-upload fallback (Task 2 of the mineru-cloud-file-upload
# plan; Task 1 was mineru_cloud_client.py's own parse_file_with_images method,
# which touches no facade surface at all). process_source's file-upload
# branch gains a symmetric local-off+cloud-configured path mirroring the
# existing URL-source branch. Its own two new tests construct a real
# SQLiteRepository via the same repo/_seed_queued_pdf fixture convention as
# this file's other process_source tests, then monkeypatch
# repo.mineru_cloud_client.parse_file_with_images and read back
# repo.source_elements — both genuinely new call sites for this file. Every
# other facade member these two tests touch (SQLiteRepository, _now, _write,
# create_notebook, process_source, get_source) is already covered by
# TASK12_ALLOWED_MEMBER_FILES's existing broad entry for this same file
# above; source_asset_ids needs no entry either — it is already a wholesale
# TASK3_SOURCE_ASSET_ALLOWED_NEW_MEMBERS exemption. Appended at EOF for the
# same zero-line-shift reason as every other TASKN_* block above.
MINERU_CLOUD_UPLOAD_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_source_ingestion_service.py", name)
    for name in {"mineru_cloud_client", "source_elements"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | MINERU_CLOUD_UPLOAD_ALLOWED_MEMBER_FILES

# Orphan-asset GC trigger: sweep_orphan_assets shipped with no production
# caller, so orphaned notebook_assets were never reclaimed. The caller added in
# app/services/knowhow/api.py (run_projection_and_sweep, riding the debounced
# projection scheduler under a per-notebook throttle) needs NOTHING registered
# here for its own half — it reaches the sweep only through repo.maintenance,
# the property Task 27 already exempted wholesale (see the Task 14 block
# above). What DOES need registering is its test, which composes the real
# facade the same way the sibling test_knowhow_asset_gc.py does: seed a
# notebook + a projectable knowhow table, then assert the sweep's effect via
# get_notebook_asset. Appended at EOF for the same zero-line-shift reason as
# every other block above.
ASSET_GC_TRIGGER_ALLOWED_IMPORTS = {
    (
        "backend/tests/test_knowhow_asset_gc_trigger.py",
        32,
        "app.services.sqlite_repository",
        "SQLiteRepository",
    ),
}
ASSET_GC_TRIGGER_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_asset_gc_trigger.py", name)
    for name in {
        "SQLiteRepository",
        "_runtime",
        "update_knowhow_cell",
        "delete_notebook",
        "create_notebook",
        "create_knowhow_table",
        "get_knowhow_table",
        "add_knowhow_row",
        "get_notebook_asset",
        "update_knowhow_cells",
        "list_knowhow_tables",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | ASSET_GC_TRIGGER_ALLOWED_MEMBER_FILES
# knowhow cross-notebook copy/move Task A1 (KnowhowTransferStore): its own
# store test composes the real facade the same way Task 2/3/4/5/6's sibling
# knowhow/paper-meta tests do — SQLiteRepository(...) to build the runtime,
# repo._runtime to reach the new knowhow_transfer_store, repo.create_notebook +
# repo.create_knowhow_table + repo.add_knowhow_row + repo.get_knowhow_table to
# seed a one-row table fixture, and repo._connect to assert the insert_transfer
# rollback left no half-written copy. Every one of these is a frozen facade
# member consumed at a fresh site this test file postdates, so it takes the
# same broad (file, member) allowance as TASK3_PAPER_META_ALLOWED_MEMBER_FILES.
# The lone import consumer (SQLiteRepository) additionally needs the IMPORTS
# entry below, folded into the import-completeness OR-chain (which resolves
# lazily at call time, so defining it here at EOF is fine). Appended at EOF for
# the same zero-line-shift reason as every other TASKN_* block above.
KNOWHOW_TRANSFER_STORE_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_transfer_store.py", 4, "app.services.sqlite_repository", "SQLiteRepository"),
}
KNOWHOW_TRANSFER_STORE_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_transfer_store.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_connect", "create_notebook",
        "create_knowhow_table", "add_knowhow_row", "get_knowhow_table",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | KNOWHOW_TRANSFER_STORE_ALLOWED_MEMBER_FILES

# knowhow cross-notebook copy/move Task A2 (transfer.py's copy_table
# orchestration): its own service test builds the real facade the same way
# Task A1's store test does — SQLiteRepository(...) to build the runtime,
# repo.create_notebook + repo.create_knowhow_table + repo.add_knowhow_row +
# repo.get_knowhow_table to seed/read a one-row table fixture, and
# repo.embedder (swapped for a fake to count embed calls — the K-1
# zero-re-embed assertion) — every one a frozen facade member consumed at a
# fresh site this test file postdates, so it takes the same broad (file,
# member) allowance as KNOWHOW_TRANSFER_STORE_ALLOWED_MEMBER_FILES above.
# Unlike Task A1's store test, this file never reaches repo._runtime/
# repo._connect directly — transfer.py itself does (see the
# ACTIVE_PRODUCTION_MEMBER_SITES addition below). The lone import consumer
# (SQLiteRepository) additionally needs the IMPORTS entry, folded into the
# import-completeness OR-chain (resolves lazily at call time, so defining it
# here at EOF is fine, same as KNOWHOW_TRANSFER_STORE_ALLOWED_IMPORTS).
# Appended at EOF for the same zero-line-shift reason as every other TASKN_*
# block above.
KNOWHOW_TRANSFER_SERVICE_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_transfer_service.py", 5, "app.services.sqlite_repository", "SQLiteRepository"),
}
KNOWHOW_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_transfer_service.py", name)
    for name in {
        "SQLiteRepository", "create_notebook", "create_knowhow_table",
        "add_knowhow_row", "get_knowhow_table", "embedder",
        # A2 review follow-up: the lexical/vector retrievability regression
        # test reaches repo._connect + repo._runtime.knowledge.chunk_fts_search
        # (the same FTS primitive production retrieval uses), exactly the idiom
        # test_knowhow_retrieval.py:280-281 already established.
        "_connect", "_runtime",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | KNOWHOW_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES

# transfer.py itself (production, not a test file) is a genuinely new facade
# consumer the same way communities.py/reasoning_retrieval.py/routes.py:605
# already are above (ACTIVE_PRODUCTION_MEMBER_SITES, defined near the top of
# this file) — copy_table's own `repo._runtime.knowhow_transfer_store` and
# _remap's `repo._runtime.seams` are two independent narrow-runtime-port
# reaches (mirrors app/services/knowhow/api.py's build_projector/
# optimize_cell precedent; registered separately in
# test_repository_callers_static.py's INDEPENDENT_PRIVATE_SITES), plus
# _remap's `repo.get_notebook_asset(...)` and copy_table's `repo.storage_dir`
# — both ordinary public facade members. Folded in here (not inline in the
# ACTIVE_PRODUCTION_MEMBER_SITES literal above) to keep this a zero-line-shift
# EOF append like every other TASKN_* block; the union resolves lazily at
# call time inside _member_file_site_allowed, same as
# ALL_TASK_ALLOWED_MEMBER_FILES's own EOF folds above.
#
# final-fix-wave update: the copy_table asset-loop fix (Minor: copied asset
# rows must not keep a foreign source_id — sets row["source_id"] = None)
# added lines inside _remap, ahead of copy_table's own `_runtime`/
# `storage_dir` reaches, shifting those two line numbers (185->193,
# 204->212). _remap's own two sites (`_runtime` seam extraction at 29,
# `get_notebook_asset` at 89) sit before the inserted lines and are
# unaffected.
#
# PR review round 6 P1-A update: the stale-derived-artifact skip guards
# added to _remap's elements/chunks/chunk_embeddings loops (a deleted
# business row/column's leftover source_elements/chunks must be dropped, not
# KeyError-crash the whole transfer — see transfer.py's own comment on that
# loop) sit ahead of copy_table's `_runtime`/`storage_dir` reaches too,
# shifting those two line numbers again (193->221, 212->240). _remap's own
# two sites (29, 89) again sit before the inserted lines and are unaffected;
# move_table's own third `_runtime` reach (previously 292) shifts the same
# way — see KNOWHOW_TRANSFER_SERVICE_P1_2_ACTIVE_PRODUCTION_SITES below.
KNOWHOW_TRANSFER_SERVICE_ACTIVE_PRODUCTION_SITES = {
    ("_runtime", "backend/app/services/knowhow/transfer.py:29"),
    ("_runtime", "backend/app/services/knowhow/transfer.py:221"),
    ("get_notebook_asset", "backend/app/services/knowhow/transfer.py:89"),
    ("storage_dir", "backend/app/services/knowhow/transfer.py:240"),
}
ACTIVE_PRODUCTION_MEMBER_SITES = ACTIVE_PRODUCTION_MEMBER_SITES | KNOWHOW_TRANSFER_SERVICE_ACTIVE_PRODUCTION_SITES

# knowhow cross-notebook copy/move Task A4 (REST endpoint): its own route test
# builds the real facade the same way Task A1/A2's sibling store/service tests
# do — SQLiteRepository(...) to build the runtime shared with the TestClient
# app (repository() lru_cache resolves to a DB-equivalent instance), plus
# repo.create_knowhow_table/get_knowhow_table/add_knowhow_row to seed a
# one-row table fixture (the same _table() idiom Task A1/A2's own fixtures
# use). Every one of these is a frozen facade member consumed at a fresh site
# this test file postdates, so it takes the same broad (file, member)
# allowance as KNOWHOW_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES above. The lone
# import consumer (SQLiteRepository) additionally needs the IMPORTS entry,
# folded into the import-completeness OR-chain (resolves lazily at call time,
# so defining it here at EOF is fine, same as the two sibling IMPORTS sets).
# The 409 fault-injection test used to patch `SQLiteRepository.
# delete_knowhow_table` and was registered separately in EXPECTED_PATCH_
# DELTAS['actual_only'] (test_static_repository_patch_scan_matches_manifest_
# exactly is a distinct scan from the member/site consumer comparison this
# block feeds). PR review round 3 P1-1 moved move_table's cleanup delete off
# repo.delete_knowhow_table onto the new KnowhowTransferStore.
# delete_table_if_unchanged (atomic conditional delete), so the test's fault
# injection moved with it — it now patches KnowhowTransferStore (imported
# locally in the test function, not SQLiteRepository) at the class level, a
# class the patch-scan's class_names set doesn't track, so that entry was
# removed from EXPECTED_PATCH_DELTAS['actual_only'] rather than replaced.
# Appended at EOF for the same zero-line-shift reason as every other TASKN_*
# block above.
KNOWHOW_TRANSFER_ROUTES_ALLOWED_IMPORTS = {
    ("backend/tests/test_knowhow_transfer_routes.py", 7, "app.services.sqlite_repository", "SQLiteRepository"),
}
KNOWHOW_TRANSFER_ROUTES_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_knowhow_transfer_routes.py", name)
    for name in {
        "SQLiteRepository", "create_knowhow_table", "add_knowhow_row", "get_knowhow_table",
        # A4 评审 Important 补的四条访问控制用例：只读成员那条要先把 bob 加成
        # 成员（test_notebook_share_readonly.py 用的同一个 add_member 惯用法），
        # 才能覆盖「copy 用读守卫 / move 用写守卫」这条此前完全没被测到的接线。
        "add_member",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | KNOWHOW_TRANSFER_ROUTES_ALLOWED_MEMBER_FILES

# memory cross-notebook copy/move Task B1 (MemoryStore.create_copy_with_
# initial_revision): its own store test builds the real facade the same way
# Task A1's knowhow-transfer store test does — SQLiteRepository(...) to build
# the runtime, set_request_user/reset_request_user (both already
# COMPATIBILITY_EXPORTS-registered names re-exported from
# app.services.sqlite_repository) to scope create_notebook per-owner,
# repo.create_user to seed the fixture user, repo._runtime to reach the new
# memory_store, and repo._connect to read back memory_embeddings/
# memory_revisions row counts. Every one of these is a frozen facade member
# consumed at a fresh site this test file postdates, so it takes the same
# broad (file, member) allowance as KNOWHOW_TRANSFER_STORE_ALLOWED_MEMBER_FILES
# above. The three import consumers (SQLiteRepository, set_request_user,
# reset_request_user) share one physical `from ... import (...)` statement, so
# all three aliases attribute to the same node.lineno and all three need
# IMPORTS entries at that one line, folded into the import-completeness
# OR-chain (resolves lazily at call time, so defining it here at EOF is fine,
# same as KNOWHOW_TRANSFER_STORE_ALLOWED_IMPORTS). Appended at EOF for the
# same zero-line-shift reason as every other TASKN_* block above.
MEMORY_TRANSFER_STORE_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_transfer_store.py", 5, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_memory_transfer_store.py", 5, "app.services.sqlite_repository", "set_request_user"),
    ("backend/tests/test_memory_transfer_store.py", 5, "app.services.sqlite_repository", "reset_request_user"),
}
MEMORY_TRANSFER_STORE_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_transfer_store.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "_connect", "create_notebook",
        "create_user", "set_request_user", "reset_request_user",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | MEMORY_TRANSFER_STORE_ALLOWED_MEMBER_FILES

# memory cross-notebook copy/move Task B2 (MemoryService.transfer + the
# transfer_memories facade delegate): its own service test builds the real
# facade the same way Task B1's store test does — SQLiteRepository(...) to
# build the runtime, set_request_user/reset_request_user (COMPATIBILITY_
# EXPORTS-registered, re-exported from app.services.sqlite_repository) to
# scope create_notebook per-owner, repo.create_user to seed the two fixture
# users (alice/bob), repo._runtime to reach memory_service (swap in
# synchronous embedding_scheduler/kg_ingest_scheduler, and to fault-inject
# store.delete_memory / memory_kg.remove_memory_source for the Amendment-1
# ordering guard and the Amendment-2 cleanup-failure regression tests), and
# repo.transfer_memories itself. Every one of these except transfer_memories
# is a frozen facade member consumed at a fresh site this test file
# postdates, so it takes the same broad (file, member) allowance as
# MEMORY_TRANSFER_STORE_ALLOWED_MEMBER_FILES above. transfer_memories has NO
# frozen consumers at all — it is a brand-new facade member that predates no
# frozen fixture entry (same situation as TASK3_SOURCE_ASSET_ALLOWED_NEW_
# MEMBERS's source_asset_ids/delete_source_asset_rows) — but since this one
# test file is its only consumer so far (B3 wires the REST route on top of it
# later), the same (file, member) allowance covers it too; no wholesale
# *_ALLOWED_NEW_MEMBERS pop-loop exemption is needed. The three import
# consumers (SQLiteRepository, set_request_user, reset_request_user) share
# one physical `from ... import (...)` statement, so all three aliases
# attribute to the same node.lineno and all three need IMPORTS entries at
# that one line, folded into the import-completeness OR-chain (resolves
# lazily at call time, so defining it here at EOF is fine, same as MEMORY_
# TRANSFER_STORE_ALLOWED_IMPORTS). Appended at EOF for the same
# zero-line-shift reason as every other TASKN_* block above.
MEMORY_TRANSFER_SERVICE_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_transfer_service.py", 6, "app.services.sqlite_repository", "SQLiteRepository"),
    ("backend/tests/test_memory_transfer_service.py", 6, "app.services.sqlite_repository", "set_request_user"),
    ("backend/tests/test_memory_transfer_service.py", 6, "app.services.sqlite_repository", "reset_request_user"),
}
MEMORY_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_transfer_service.py", name)
    for name in {
        "SQLiteRepository", "_runtime", "create_notebook",
        "create_user", "set_request_user", "reset_request_user",
        "transfer_memories",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | MEMORY_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES

# memory cross-notebook copy/move Task B3 (POST /memories/transfer REST route):
# this is an HTTP-level test — unlike B1/B2's store/service tests, notebooks
# and users are created through the real API (client.post("/api/notebooks"...),
# _login's register/login roundtrip), so it does NOT need create_notebook/
# create_user/set_request_user/reset_request_user allowances. Its only two
# static-scan hits (confirmed by running _static_repository_consumers() and
# filtering for this file) are the module-level `from
# app.services.sqlite_repository import SQLiteRepository` used by the `repo`
# fixture (same boilerplate as every other transfer-task test file), and one
# `repo._runtime` access inside the `_seeded_service` helper — used purely to
# reach the real memory_service and swap in synchronous embedding_scheduler/
# kg_ingest_scheduler for candidate/confirm setup, the same pre-existing
# fixture pattern B1/B2 already established (not a new production consumer;
# the route itself only ever calls the frozen facade member
# transfer_memories, already covered by MEMORY_TRANSFER_SERVICE_ALLOWED_
# MEMBER_FILES's declaration of that member — no *_ALLOWED_NEW_MEMBERS
# exemption needed here either). SQLiteRepository is also consumed as an
# import, so it needs both the IMPORTS entry (import-completeness OR-chain)
# and the MEMBER_FILES entry (broad per-file consumer-scan check), same
# two-set split as every sibling transfer-task block above. Appended at EOF
# for the same zero-line-shift reason as every other TASKN_* block above.
MEMORY_TRANSFER_ROUTES_ALLOWED_IMPORTS = {
    ("backend/tests/test_memory_transfer_routes.py", 16, "app.services.sqlite_repository", "SQLiteRepository"),
}
MEMORY_TRANSFER_ROUTES_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_transfer_routes.py", name)
    for name in {
        "SQLiteRepository", "_runtime",
    }
}
ALL_TASK_ALLOWED_MEMBER_FILES = ALL_TASK_ALLOWED_MEMBER_FILES | MEMORY_TRANSFER_ROUTES_ALLOWED_MEMBER_FILES

# knowhow cross-notebook copy/move PR review round 2 P1-2 (data loss):
# move_table's own snapshot-vs-delete concurrent-edit guard reaches
# `repo._runtime.knowhow_transfer_store` a THIRD, independent time (the
# other two, lines 29/193 originally, now 29/221 after round 6's P1-A fix —
# see KNOWHOW_TRANSFER_SERVICE_ACTIVE_PRODUCTION_SITES above and the matching
# INDEPENDENT_PRIVATE_SITES fold in test_repository_callers_static.py) — this
# file's own consumer-scan mirror of that same new site: move_table needs
# table_fingerprint() both before copy_table runs and again right before the
# source delete. Appended at EOF for the same zero-line-shift reason as
# every other TASKN_* block above.
#
# PR review round 6 P1-A update: same _remap skip-guard lines that shifted
# copy_table's own two sites (193->221, 212->240 above) sit ahead of this
# site too, shifting it 292->320.
#
# PR review round 10 P1-A update: SourceCleanupFailed's docstring/__init__
# grew a `reason` param (source_changed vs cleanup_error) ahead of
# move_table, shifting this site again (320->330).
KNOWHOW_TRANSFER_SERVICE_P1_2_ACTIVE_PRODUCTION_SITES = {
    ("_runtime", "backend/app/services/knowhow/transfer.py:330"),
}
ACTIVE_PRODUCTION_MEMBER_SITES = (
    ACTIVE_PRODUCTION_MEMBER_SITES | KNOWHOW_TRANSFER_SERVICE_P1_2_ACTIVE_PRODUCTION_SITES
)

# memory cross-notebook copy/move PR review round 5 P1-2 (data loss —
# promotion_candidates orphan guard): the new tests pinning "move rejects a
# Memory with an active promotion proposal" need to actually EXERCISE the
# Track-F promotion state machine to set up a 'proposed' Memory and to prove
# 'approved' is deliberately NOT blocked — three more `repo.*` sites in
# test_memory_transfer_service.py, all on facade members that are already
# frozen elsewhere (same "fresh consumer of a pre-existing member" shape as
# every other MEMORY_TRANSFER_SERVICE_ALLOWED_MEMBER_FILES entry above, so
# it takes the same broad (file, member) allowance rather than a precise
# line pin): `repo._connect()` (read the promotion_candidates row directly,
# to assert it is neither deleted nor orphaned), `repo.mark_notebook_base()`
# + `repo.approve_promotion()` (both needed only to drive a candidate to
# 'approved' for the companion "approved is not blocked" guard — mirrors
# test_memory_promotion.py's own promotion_setup fixture). Appended at EOF
# for the same zero-line-shift reason as every other TASKN_* block above.
MEMORY_TRANSFER_SERVICE_ROUND5_ALLOWED_MEMBER_FILES = {
    ("backend/tests/test_memory_transfer_service.py", name)
    for name in {"_connect", "mark_notebook_base", "approve_promotion"}
}
ALL_TASK_ALLOWED_MEMBER_FILES = (
    ALL_TASK_ALLOWED_MEMBER_FILES | MEMORY_TRANSFER_SERVICE_ROUND5_ALLOWED_MEMBER_FILES
)
