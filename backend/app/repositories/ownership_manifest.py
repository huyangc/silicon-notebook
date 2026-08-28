from dataclasses import dataclass
from typing import Literal, Mapping
SurfaceKind = Literal[
    "method",
    "property",
    "mutable_property",
    "constant",
    "private_wrapper",
    "instance_attribute",
]
ConsumerKind = Literal[
    "attribute",
    "compatibility",
    "import",
    "patch",
    "registry",
]


@dataclass(frozen=True, order=True)
class ConsumerSite:
    path: str
    scope: str
    kind: ConsumerKind
    target: str


@dataclass(frozen=True)
class SurfaceMember:
    name: str
    owner: str
    kind: SurfaceKind
    consumers: tuple[ConsumerSite, ...]
    patches: tuple[ConsumerSite, ...] = ()

SURFACE_MEMBERS = (
    SurfaceMember(
        name='KNOWLEDGE_STATUSES',
        owner='KnowledgeGovernanceService',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='KNOWLEDGE_STATUSES'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='KnowledgeGraphTooLargeError',
        owner='KnowledgeLifecycleService',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='KnowledgeGraphTooLargeError'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='NotebookRepository',
        owner='RepositoryPorts',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.repository', scope='<module>', kind='compatibility', target='NotebookRepository'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='RetrievedKnowledge',
        owner='RetrievalService',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='RetrievedKnowledge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='SCHEMA_VERSION',
        owner='SqliteDatabase',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='SCHEMA_VERSION'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SCHEMA_VERSION'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='SQLiteRepository',
        owner='RepositoryFacade',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='SQLiteRepository'),
            ConsumerSite(path='backend/app/eval/inference.py', scope='<module>.run_inference', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='backend/app/eval/run_all.py', scope='<module>.main', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='backend/app/eval/sa_calibration.py', scope='<module>._run_arm', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>.measure_speed', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='backend/app/scripts/gen_recall_gold.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='backend/tests/test_replay_retrieval.py', scope='<module>.test_record_run_requires_retrieval_query_embedding', kind='patch', target='SQLiteRepository'),
            ConsumerSite(path='scripts/backfill_knowhow_md.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='scripts/bench_sqlite_writes.py', scope='<module>._make_repo', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='scripts/replay_retrieval.py', scope='<module>.record_run', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>', kind='import', target='app.services.sqlite_repository:SQLiteRepository'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_replay_retrieval.py', scope='<module>.test_record_run_requires_retrieval_query_embedding', kind='patch', target='SQLiteRepository'),
        ),
    ),
    SurfaceMember(
        name='USABLE_STATUSES',
        owner='KnowledgeGovernanceService',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='USABLE_STATUSES'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='UploadedSourceFile',
        owner='RepositoryPorts',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.repository', scope='<module>', kind='compatibility', target='UploadedSourceFile'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>', kind='import', target='app.services.repository:UploadedSourceFile'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>', kind='import', target='app.services.repository:UploadedSourceFile'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>', kind='import', target='app.services.repository:UploadedSourceFile'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>', kind='import', target='app.services.repository:UploadedSourceFile'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_api_layer', kind='import', target='app.services.repository:UploadedSourceFile'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_ASK_MODEL_ERRORS',
        owner='ModelProvider',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_ASK_MODEL_ERRORS'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_COPY_CHUNK',
        owner='NotebookSharingService',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/app/services/sqlite_notebook_sharing.py', scope='<module>._copy_chunk_size', kind='import', target='app.services.sqlite_repository:_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_copy_observes_copy_chunk_patched_after_construction', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_first_chunk_compensates_rows_and_files', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_chunked_transactions_release_lock_between_chunks', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_crash_midway_leaves_no_visible_notebook_and_self_heals', kind='patch', target='_COPY_CHUNK'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_copy_observes_copy_chunk_patched_after_construction', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_first_chunk_compensates_rows_and_files', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_chunked_transactions_release_lock_between_chunks', kind='patch', target='_COPY_CHUNK'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_crash_midway_leaves_no_visible_notebook_and_self_heals', kind='patch', target='_COPY_CHUNK'),
        ),
    ),
    SurfaceMember(
        name='_IN_CHUNK',
        owner='QueryStore',
        kind='constant',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._in_batches', kind='attribute', target='_IN_CHUNK'),
            ConsumerSite(path='backend/tests/test_in_batching.py', scope='<module>.test_delta_sites_equivalent_when_batched', kind='patch', target='_IN_CHUNK'),
            ConsumerSite(path='backend/tests/test_incremental_fuse_bounded.py', scope='<module>.test_insert_clusters_batches_member_probe', kind='patch', target='_IN_CHUNK'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_in_batching.py', scope='<module>.test_delta_sites_equivalent_when_batched', kind='patch', target='_IN_CHUNK'),
            ConsumerSite(path='backend/tests/test_incremental_fuse_bounded.py', scope='<module>.test_insert_clusters_batches_member_probe', kind='patch', target='_IN_CHUNK'),
        ),
    ),
    SurfaceMember(
        name='_REQUEST_USER',
        owner='RequestContext',
        kind='constant',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_REQUEST_USER'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_annotate_edge_support',
        owner='KnowledgeQueryService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_annotate_edge_support'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_apply_notebook_meta',
        owner='NotebookStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_apply_notebook_meta'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_as_retrieved',
        owner='KnowledgeQueryService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_as_retrieved'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_begin_extraction_run',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_begin_extraction_run'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_bump_cluster_mutation_seq',
        owner='KgMutationCoordinator',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_bump_cluster_mutation_seq'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_clear_source_extraction_state',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_clear_source_extraction_state'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_concept_desc_sig',
        owner='KnowledgeLifecycleService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_concept_desc_sig'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_connect',
        owner='SqliteDatabase',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_connect'),
            ConsumerSite(path='backend/tests/test_language_policy.py', scope='<module>.test_notebook_langs_is_cached', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_node_context.py', scope='<module>.test_element_texts_does_not_scan_entire_notebook', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_notebook_copy_stats_memo.py', scope='<module>.test_copy_stats_memo_hit_runs_loader_once', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_query_hotpath_cache.py', scope='<module>.test_edge_centrality_bounded_load_no_full_objects_query', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_relation_retrieval.py', scope='<module>._connect_spy', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_relation_retrieval.py', scope='<module>._execute_spy', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_probe.py', scope='<module>.test_unchanged_seq_skips_noncluster_aggregates', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_concurrent_cold_callers_compute_once', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_loader_exception_propagates_and_nothing_cached', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_no_deadlock_with_reentrant_call_for_different_notebook', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_seq_bump_forces_exactly_one_recompute', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_trackF_governance_promotion.py', scope='<module>.TestPromotionStateMachine.test_list_promotion_queue_batches_object_lookup_not_n_plus_1', kind='patch', target='_connect'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_language_policy.py', scope='<module>.test_notebook_langs_is_cached', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_node_context.py', scope='<module>.test_element_texts_does_not_scan_entire_notebook', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_notebook_copy_stats_memo.py', scope='<module>.test_copy_stats_memo_hit_runs_loader_once', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_query_hotpath_cache.py', scope='<module>.test_edge_centrality_bounded_load_no_full_objects_query', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_relation_retrieval.py', scope='<module>._connect_spy', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_relation_retrieval.py', scope='<module>._execute_spy', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_probe.py', scope='<module>.test_unchanged_seq_skips_noncluster_aggregates', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_concurrent_cold_callers_compute_once', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_loader_exception_propagates_and_nothing_cached', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_no_deadlock_with_reentrant_call_for_different_notebook', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_scale_index_version_singleflight.py', scope='<module>.test_seq_bump_forces_exactly_one_recompute', kind='patch', target='_connect'),
            ConsumerSite(path='backend/tests/test_trackF_governance_promotion.py', scope='<module>.TestPromotionStateMachine.test_list_promotion_queue_batches_object_lookup_not_n_plus_1', kind='patch', target='_connect'),
        ),
    ),
    SurfaceMember(
        name='_edge_centrality_map',
        owner='KnowledgeQueryService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_edge_centrality_map'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_embed_knowledge',
        owner='SourceEmbeddingService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_embed_knowledge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_embed_objects_batch',
        owner='SourceEmbeddingService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_embed_objects_batch'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_embed_relations_batch',
        owner='SourceEmbeddingService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_embed_relations_batch'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_fast_loads',
        owner='QueryStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_fast_loads'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_finish_extraction_run',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_finish_extraction_run'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_insert_row',
        owner='SharingStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_knowhow_copy.py', scope='<module>.test_copy_failure_compensation_removes_assets_dir', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_first_chunk_compensates_rows_and_files', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_sentinel_compensates_only_destination', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_chunked_transactions_release_lock_between_chunks', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_crash_midway_leaves_no_visible_notebook_and_self_heals', kind='patch', target='_insert_row'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_knowhow_copy.py', scope='<module>.test_copy_failure_compensation_removes_assets_dir', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_first_chunk_compensates_rows_and_files', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_failure_after_sentinel_compensates_only_destination', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_chunked_transactions_release_lock_between_chunks', kind='patch', target='_insert_row'),
            ConsumerSite(path='backend/tests/test_notebook_share_copy.py', scope='<module>.test_copy_notebook_crash_midway_leaves_no_visible_notebook_and_self_heals', kind='patch', target='_insert_row'),
        ),
    ),
    SurfaceMember(
        name='_invalidate_unified_cache',
        owner='KgMutationCoordinator',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_invalidate_unified_cache'),
            ConsumerSite(path='backend/tests/test_edge_review_queue.py', scope='<module>.test_federated_version_key_covers_review_flip_without_eviction', kind='patch', target='_invalidate_unified_cache'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_edge_review_queue.py', scope='<module>.test_federated_version_key_covers_review_flip_without_eviction', kind='patch', target='_invalidate_unified_cache'),
        ),
    ),
    SurfaceMember(
        name='_kg_building',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='_kg_building'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_kg_building_lock',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='_kg_building_lock'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_knowledge_objects',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_knowledge_objects'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_mark_unified_kg_dirty',
        owner='KgMutationCoordinator',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_build_notebook_kg_concurrent_reports_progress', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_build_notebook_kg_isolates_source_failure', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_kg_building_flag.py', scope='<module>.test_kg_building_set_during_build_and_cleared_after', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_source_chunking_service.py', scope='<module>.test_build_chunks_marks_unified_dirty_via_facade_seat', kind='patch', target='_mark_unified_kg_dirty'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_build_notebook_kg_concurrent_reports_progress', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_build_notebook_kg_isolates_source_failure', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_kg_building_flag.py', scope='<module>.test_kg_building_set_during_build_and_cleared_after', kind='patch', target='_mark_unified_kg_dirty'),
            ConsumerSite(path='backend/tests/test_source_chunking_service.py', scope='<module>.test_build_chunks_marks_unified_dirty_via_facade_seat', kind='patch', target='_mark_unified_kg_dirty'),
        ),
    ),
    SurfaceMember(
        name='_mark_unified_kg_dirty_in_tx',
        owner='KgMutationCoordinator',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_mark_unified_kg_dirty_in_tx'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_migrator',
        owner='SqliteDatabase',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.__init__', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migrate', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migrate_legacy', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._recover_interrupted_jobs', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._recover_interrupted_jobs_legacy', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._run_migration', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._seed', kind='attribute', target='_migrator'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._seed_legacy', kind='attribute', target='_migrator'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_new_id',
        owner='QueryStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_new_id'),
            ConsumerSite(path='backend/app/services/sqlite_notebook_sharing.py', scope='<module>._repository_new_id', kind='import', target='app.services.sqlite_repository:_new_id'),
            ConsumerSite(path='backend/tests/test_knowhow_copy.py', scope='<module>.test_copy_failure_compensation_removes_assets_dir', kind='patch', target='_new_id'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_copy_observes_new_id_patched_after_construction', kind='patch', target='_new_id'),
            ConsumerSite(path='backend/tests/test_source_chunking_service.py', scope='<module>.test_build_chunks_id_minting_rides_module_seam', kind='patch', target='_new_id'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_knowhow_copy.py', scope='<module>.test_copy_failure_compensation_removes_assets_dir', kind='patch', target='_new_id'),
            ConsumerSite(path='backend/tests/test_notebook_copy_service.py', scope='<module>.test_copy_observes_new_id_patched_after_construction', kind='patch', target='_new_id'),
            ConsumerSite(path='backend/tests/test_source_chunking_service.py', scope='<module>.test_build_chunks_id_minting_rides_module_seam', kind='patch', target='_new_id'),
        ),
    ),
    SurfaceMember(
        name='_note_model_error',
        owner='ModelProvider',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_note_model_error'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_notebook_has_kg',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_notebook_has_kg'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_notebook_langs_cache',
        owner='RepositoryRuntime.notebook_languages',
        kind='mutable_property',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='_notebook_langs_cache'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_notebook_meta_row',
        owner='NotebookStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_notebook_meta_row'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_notebook_meta_sources',
        owner='SourceStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_notebook_meta_sources'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_notebook_tier',
        owner='NotebookStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_notebook_tier'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_now',
        owner='QueryStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_now'),
            ConsumerSite(path='backend/tests/test_rebuild_cache.py', scope='<module>._freeze_now', kind='patch', target='_now'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_rebuild_cache.py', scope='<module>._freeze_now', kind='patch', target='_now'),
        ),
    ),
    SurfaceMember(
        name='_recover_interrupted_jobs',
        owner='SqliteDatabase',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/startup_warmup.py', scope='<module>.run_startup', kind='attribute', target='_recover_interrupted_jobs'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.verify_snapshot', kind='attribute', target='_recover_interrupted_jobs'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_remap_json_ids',
        owner='NotebookSharingService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='_remap_json_ids'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_rule_card',
        owner='KnowledgeQueryService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_rule_card'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_run_extraction',
        owner='SourceIngestionService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_run_extraction'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>._call_extraction_compat', kind='attribute', target='_run_extraction'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_run_migration',
        owner='SqliteDatabase',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_1', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_10', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_19', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_2', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_27', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_3', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_4', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_5', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_6', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_7', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_8', kind='attribute', target='_run_migration'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._migration_9', kind='attribute', target='_run_migration'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_runtime',
        owner='RepositoryRuntime',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.admin_query_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.content_overview_service', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.group_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.identity_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.memory_preview_client', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.model_provider_if_initialized', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.model_service_binding_summary', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.model_status_service', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.notebook_access_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.notebook_catalog_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.notebook_sharing_repository', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.notebook_store_port', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.build_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.build_kg.run', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._report_llm_ready', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.export_reports_endpoint', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.generate_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._backfill_page_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._backfill_vectors_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.backfill_paper_metadata', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.build_projector', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.complete_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.optimize_cell', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.reformat_cell', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>._remap', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>.copy_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>.move_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._annotate_edge_support', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._answer_chunks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._answer_context', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._answer_mix', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._answer_reasoning', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._answer_with_retry', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._any_base_notebook_has_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._apply_notebook_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ask_cancel_events', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ask_cancel_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._augment_notebook_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._auto_index_checked', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._backfill_knowledge_embeddings', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._begin_extraction_run', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._build_chunks_for_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._build_viz_graph_arrays', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._bump_cluster_mutation_seq', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._chunk_and_embed_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._chunk_answer_context', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._citations_from', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._cleanup_empty_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._clear_source_extraction_state', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._cluster_input_version', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._compute_scale_version_cold', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._connect', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._conversation_history', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._count', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._count_knowledge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._count_pending_kg_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._delete_file', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._delete_knowledge_object_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._delete_relations_for_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._dequeue_scale_idle', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._derive_object_graph_lite', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._edge_centrality_map', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._element_texts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_chunks_batch', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_chunks_for_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_knowledge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_objects_batch', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_relations_batch', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._enrich_evidence', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ensure_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ensure_scale_scheduler', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._extraction_warning', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._find_base_dedup_match', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._find_stale_knowledge_ids_for_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._finish_extraction_run', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._flush_object_vectors', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._fold_hits_to_canonical', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._gather_kg_graph', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._has_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._has_pending_merges', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._hydrate_search_hits', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._index_delta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._insert_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._invalidate_unified_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._kg_headline', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._kg_neighbors_db', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._knowledge_objects', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._knowledge_record', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._knowledge_ref', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._knowledge_similarity', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._knowledge_type_counts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mark_source_index_backfilled', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mark_unified_kg_dirty', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mark_unified_kg_dirty_in_tx', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._maybe_enqueue_scale_fold', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._merge_evidence_lists', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mounted_bases', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._needs_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._note_model_error', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_from_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_has_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_langs_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_meta_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_meta_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_name', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_tier', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notify_index_done', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._object_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._open_scale_ann', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._parse_answer_anchors', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._parse_url_via_local', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._pending_merges_batch', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._probe_scale_version_signal', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._process_idle_queue', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._read_ask_trace', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._read_manifest_version', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rebuild_ckpt_clear', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rebuild_ckpt_gc', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rebuild_ckpt_load', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rebuild_ckpt_put', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._refine_context', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._relink_extra_relations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._report_row_to_dict', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._resolve_index_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._resolve_path', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._resolve_scale_mode', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rewrite_followup_query', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rule_card', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._run_extraction', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._run_scale_op', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._save_answer', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_building', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_building_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_idle_queue', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_idx_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_idx_load_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_idx_load_locks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_index_eligible', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_index_version', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_scheduler_started', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_ver_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_ver_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_ver_locks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._seed_fn_for', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._semantic_search', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._set_source_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._should_extract_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_from_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_has_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_ids_from_evidence', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_index_backfilled', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_pipeline_hooks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_raw_text', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._source_type_from_name', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._sources_from_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._spawn_viz_build', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._stream_seed_reps', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._summarize_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._sweep_stuck_copies', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._test_insert_object', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._tier2_bridge_candidates_ann', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._tier_map_for', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._truncate_kg_block', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._unconfigured_model_response', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._unified_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._unified_graph_bounded', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._unified_graph_full', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._upsert_knowledge_object_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._user_profile', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._vector_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_arrays_from_graph', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_building', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_building_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_dict', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_idx_cache', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_index_dir', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_index_probe', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._viz_node', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._write', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._write_cluster_map_streamed', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_knowhow_column', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_knowhow_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_member', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_relations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_url_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.agent_memory_hits', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.agent_observations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.agent_profile', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.agent_profile_jobs', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.all_visible_source_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.answer_memory_links', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.answer_memory_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.answer_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.append_ask_trace', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.append_clusters', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.append_knowhow_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.apply_conflict_resolution', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.approve_promotion', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.approve_promotion_as_reviewer', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_answer_detail', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_chunk', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_graph', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_job_detail', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_job_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.ask_reasoning', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.audit_labels_for_user_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.authenticate_user', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.backfill_chunk_fts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.backfill_kg_fts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.backfill_paper_metadata', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.begin_ask_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.build_notebook_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.build_scale_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.build_viz_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.bulk_delete_conversations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.bulk_delete_memories', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.bump_knowhow_mutation_seq', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.cancel_ask_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.cancel_scale_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.chat', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.claim_report_generation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.claim_report_intent', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.close', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.collection_catalog', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.collection_enumeration', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.command_catalog', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.concept_detail', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.concept_whitelist_add', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.concept_whitelist_list', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.concept_whitelist_remove', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.concept_whitelist_terms', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.configured', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.confirm_conflict', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.confirm_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.confirm_merge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.conflict_resolution_admitted', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.conversation_creator', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.conversation_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.conversation_share_state', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.copy_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_agent_profile', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_knowhow_milestone', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_knowhow_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_knowhow_table_with_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_memory_candidate', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_memory_from_answer', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_notebook_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_session', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.create_user', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.current_user', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.decided_pairs', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.decided_seed_pairs', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_knowhow_cell_code', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_knowhow_column', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_knowhow_milestone', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_knowhow_row', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_knowhow_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_notebook_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_notebook_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_session', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.delete_source_asset_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.deprecate_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.effective_document_limit', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.effective_schemas', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.evidence_elements', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.execute_indexing_pipeline_rebuild', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.execute_notebook_kg_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.export_reports', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.extract_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.fail_conflict_resolution_submission', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.fail_notebook_kg_job_submission', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.fail_notebook_relink_submission', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.fail_unified_kg_rebuild_submission', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.find_duplicates', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.find_notebook_by_share_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.finish_ask_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.fold_scale_index_delta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_community_reports', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_conflict_candidate', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_knowhow_cell_code', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_knowhow_change', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_knowhow_row_location', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_knowhow_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_notebook_asset', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_paper_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.get_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.global_document_limit_default', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.hidden_source_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.import_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.incremental_fuse_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.index_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.indexing_pipeline_options', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.insert_notebook_asset', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.is_member', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.issue_agent_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.join_shared', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_analysis', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_cluster_size_histogram', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_community_overview', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_largest_clusters', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_neighbors', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_relation_provenance_counts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kg_search', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.kick_all_members', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowhow_cell_history', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowhow_changes_between', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowhow_history_head_seq', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowhow_history_page', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowledge_graph', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.knowledge_types', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.leave_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_agent_profiles', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_agent_tokens', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_communities', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_conversations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_knowhow_cell_code', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_knowhow_changes', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_knowhow_milestones', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_knowhow_tables', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_knowledge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_members', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_memories', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_notebook_bases', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_notebook_object_schemas', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_notebook_templates', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_notebooks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_object_schemas', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_promotion_queue', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_reports', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_sources_page', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_user_activity', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_user_notebooks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.list_user_usage', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.load_notebook_scale_facts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.mark_notebook_base', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.maybe_auto_index', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.memory_kg_eligible', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.memory_revisions', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.merge_knowledge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.merge_review_job_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.mountable_notebooks', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.mounted_by_count', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.node_context', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.notebook_analytics', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.notebook_copy_stats', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.notebook_exists_for_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.notebook_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.notebook_relink_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.paper_meta_backfill_progress', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.paper_meta_backfilling', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.parallelism', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.parse_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.participant_notebook_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.pending_actions', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.pending_actions_projection_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.pending_conflicts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.pending_merges', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.prepare_notebook_kg_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.preview_reasoning_intent', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.process_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.propose_memory_promotion', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.propose_promotion', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.propose_schemas', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.prune_knowhow_history', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.public_conversation_by_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.public_report_by_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_canonical_relations', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_communities', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_mention_bridge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_notebook_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_unified_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.refresh_agent_principal', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.reject_conflict', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.reject_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.reject_merge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.reject_promotion', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.reject_promotion_as_reviewer', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.relations_for_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.relink_notebook_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.remove_member', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rename_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rename_knowhow_column', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.replace_notebook_bases', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.report_execution', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.report_share_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.report_source_identity_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.report_source_rows', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.require_agent_access', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.require_indexing_pipeline_write', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.resolve_agent_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.resolve_notebook_conflicts', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.resolve_session', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.retrieval', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.retrieval_experiences', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.revert_knowhow_table', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.review_pending_merges', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.review_queue', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.revoke_agent_token', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.run_conflict_resolution_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.run_merge_review_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.run_notebook_relink_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.run_unified_kg_rebuild_job', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.scale_index_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.search_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_conflict_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_edge_review', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_indexing_pipeline', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_knowhow_anchor_column', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_knowhow_column_kind', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_knowhow_hidden_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_merge_decision', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_notebook_personal', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.share_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.share_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.share_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.share_state', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.shared_by_me', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.shared_preview', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_asset_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_elements', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_elements_page', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_id_by_hash', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_metadata', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_notebook_id', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_owner', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.source_parse_busy', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.sources_missing_paper_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.start_ask_stream', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.start_conflict_resolution', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.start_notebook_relink', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.start_unified_kg_rebuild', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.storage_dir', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.store_kg', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.submit_feedback', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.summarize_communities', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.transfer_memories', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.trigger_scale_index_rebuild', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unified_graph', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unified_kg_rebuild_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unified_kg_status', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unshare_conversation', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unshare_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.unshare_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_agent_profile', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowhow_cell', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowhow_cells', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowhow_cells_bulk_guarded', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowhow_cells_guarded_atomic', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowhow_table_meta', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_knowledge', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_memory', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_notebook_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_object_schema', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.update_report', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.upload_sources', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.upsert_knowhow_cell_code', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_can_access_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_can_admin_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_can_read_answer', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_can_read_notebook', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_can_read_source', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.user_document_limit_override', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.validate_cell_target', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.validate_reasoning_submission', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.visible_document_count', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.visible_source_count', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.visible_source_ids', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.visible_source_scope_snapshot', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.warm_open_path_caches', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.write_clusters', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.write_conflict_candidate', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.write_merge_candidate', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.__init__', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._connect', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._write_lock', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.checkup', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.close_local', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.db_path', kind='attribute', target='_runtime'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.maintenance', kind='attribute', target='_runtime'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_set_source_status',
        owner='SourceIngestionService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_set_source_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_source_ids_from_evidence',
        owner='KnowledgeStore',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_source_ids_from_evidence'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_source_type_from_name',
        owner='SourceIngestionService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_source_type_from_name'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='_summarize_source',
        owner='SourceIngestionService',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_summarize_source'),
            ConsumerSite(path='backend/tests/test_chunk_embed.py', scope='<module>.test_process_source_builds_chunks', kind='patch', target='_summarize_source'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_chunk_embed.py', scope='<module>.test_process_source_builds_chunks', kind='patch', target='_summarize_source'),
        ),
    ),
    SurfaceMember(
        name='_write',
        owner='SqliteDatabase',
        kind='private_wrapper',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='_write'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='add_knowhow_column',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.add_knowhow_column', kind='attribute', target='add_knowhow_column'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='add_knowhow_row',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.add_knowhow_row', kind='attribute', target='add_knowhow_row'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='add_url_sources',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>.register_source_tools.add_source_url.run', kind='attribute', target='add_url_sources'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.import_url_sources', kind='attribute', target='add_url_sources'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='agent_memory_hits',
        owner='RepositoryRuntime.memory_retriever',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.search_agent_memory.load', kind='attribute', target='agent_memory_hits'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='agent_observations',
        owner='RepositoryRuntime.agent_observations',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/agent_profile_routes.py', scope='<module>.clear_agent_observations', kind='attribute', target='agent_observations'),
            ConsumerSite(path='backend/app/api/agent_profile_routes.py', scope='<module>.get_agent_observations', kind='attribute', target='agent_observations'),
            ConsumerSite(path='backend/app/api/mcp_tools/_shared.py', scope='<module>._record_agent_call', kind='attribute', target='agent_observations'),
            ConsumerSite(path='backend/app/api/mcp_tools/profiles.py', scope='<module>.register_profile_tools.add_observation.run', kind='attribute', target='agent_observations'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='agent_profile',
        owner='RepositoryRuntime.agent_profile',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/agent_profile_routes.py', scope='<module>._profile_store', kind='attribute', target='agent_profile'),
            ConsumerSite(path='backend/app/api/mcp_tools/profiles.py', scope='<module>.register_profile_tools.add_observation.run', kind='attribute', target='agent_profile'),
            ConsumerSite(path='backend/app/api/mcp_tools/profiles.py', scope='<module>.register_profile_tools.get_notebook_profile.load', kind='attribute', target='agent_profile'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='agent_profile_jobs',
        owner='RepositoryRuntime.agent_profile_jobs',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/agent_profile_routes.py', scope='<module>.rebuild_understanding', kind='attribute', target='agent_profile_jobs'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='all_visible_source_ids',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._validate_source_scope', kind='attribute', target='all_visible_source_ids'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='append_knowhow_rows',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.commit_append', kind='attribute', target='append_knowhow_rows'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='approve_promotion_as_reviewer',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.approve_promotion', kind='attribute', target='approve_promotion_as_reviewer'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.ask', kind='attribute', target='ask'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.ask_notebook.run_ask', kind='attribute', target='ask'),
            ConsumerSite(path='backend/app/eval/inference.py', scope='<module>.run_inference', kind='attribute', target='ask'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='ask'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='ask'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask_answer_detail',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.get_admin_user_ask_detail', kind='attribute', target='ask_answer_detail'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask_chunk',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/ask_modes.py', scope='<module>.ASK_MODES', kind='registry', target='ask_chunk'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask_graph',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/ask_modes.py', scope='<module>.ASK_MODES', kind='registry', target='ask_graph'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask_job_detail',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.get_admin_user_ask_detail', kind='attribute', target='ask_job_detail'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.cancel_ask_job', kind='attribute', target='ask_job_detail'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.get_ask_job', kind='attribute', target='ask_job_detail'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='ask_job_detail'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='ask_reasoning',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/ask_modes.py', scope='<module>.ASK_MODES', kind='registry', target='ask_reasoning'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='audit_labels_for_user_ids',
        owner='IdentityStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/audit.py', scope='<module>.resolve_audit_labels', kind='attribute', target='audit_labels_for_user_ids'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='backfill_paper_metadata',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.backfill_paper_metadata', kind='attribute', target='backfill_paper_metadata'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='build_notebook_kg',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/scripts/build_kg.py', scope='<module>.main', kind='attribute', target='build_notebook_kg'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg', kind='attribute', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_alone_forces_rebuild', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_interrupt_cancels_and_drains_accepted_workers', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_interrupt_during_submission_drains_already_accepted_worker', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_uses_durable_termination_signals', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_interrupt_after_a_committed_build_still_reports_130', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_interrupt_exits_cleanly_with_shell_convention', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_disables_fusion_and_rebuilds', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_plain_no_rebuild_without_llm_is_noop', kind='patch', target='build_notebook_kg'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_alone_forces_rebuild', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_interrupt_cancels_and_drains_accepted_workers', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_interrupt_during_submission_drains_already_accepted_worker', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_limited_kg_uses_durable_termination_signals', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_interrupt_after_a_committed_build_still_reports_130', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_interrupt_exits_cleanly_with_shell_convention', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_disables_fusion_and_rebuilds', kind='patch', target='build_notebook_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_plain_no_rebuild_without_llm_is_noop', kind='patch', target='build_notebook_kg'),
        ),
    ),
    SurfaceMember(
        name='build_scale_index',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='build_scale_index'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_index', kind='attribute', target='build_scale_index'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg._finalize', kind='attribute', target='build_scale_index'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_index_prints_stage_timings', kind='patch', target='build_scale_index'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_index_prints_stage_timings', kind='patch', target='build_scale_index'),
        ),
    ),
    SurfaceMember(
        name='bulk_delete_conversations',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.bulk_delete_conversations', kind='attribute', target='bulk_delete_conversations'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='cancel_ask_job',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.cancel_ask_job', kind='attribute', target='cancel_ask_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='cancel_scale_index',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.cancel_scale_index', kind='attribute', target='cancel_scale_index'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='chat',
        owner='ModelProvider',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/sa_calibration.py', scope='<module>._run_arm', kind='attribute', target='chat'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.main', kind='attribute', target='chat'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='checkup',
        owner='CheckupService',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.checkup_service', kind='attribute', target='checkup'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='claim_report_generation',
        owner='ReportEngine',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.generate_report', kind='attribute', target='claim_report_generation'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='claim_report_intent',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.confirm_report_intent', kind='attribute', target='claim_report_intent'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='close',
        owner='RepositoryRuntime.close',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.shutdown_repository_if_initialized', kind='attribute', target='close'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='cluster_map',
        owner='RetrievalService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/retrieval_metrics.py', scope='<module>.run_recall', kind='attribute', target='cluster_map'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='cluster_map'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='command_catalog',
        owner='RepositoryRuntime.command_catalog',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.command_catalog_service', kind='attribute', target='command_catalog'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='concept_detail',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.get_concept_detail', kind='attribute', target='concept_detail'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='concept_whitelist_add',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.add_concept_whitelist', kind='attribute', target='concept_whitelist_add'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='concept_whitelist_list',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.list_concept_whitelist', kind='attribute', target='concept_whitelist_list'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='concept_whitelist_remove',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.delete_concept_whitelist', kind='attribute', target='concept_whitelist_remove'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='concept_whitelist_terms',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='concept_whitelist_terms'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='configured',
        owner='ModelProvider',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._backfill_vectors_job', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.backfill_vectors', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>.measure_speed', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/scripts/reembed_kg.py', scope='<module>.main', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._dispatch_main', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_chunk_embeddings', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_element_embeddings', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_node_embeddings', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_metadata', kind='attribute', target='configured'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_question_index', kind='attribute', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_chunks_once_too', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_once_per_source', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_once_then_hydrates_by_page', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_embeds_under_per_source_lock', kind='patch', target='configured'),
            ConsumerSite(path='scripts/backfill_kg_embeddings.py', scope='<module>.main', kind='attribute', target='configured'),
            ConsumerSite(path='scripts/replay_retrieval.py', scope='<module>.record_run', kind='attribute', target='configured'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_chunks_once_too', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_once_per_source', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_discovers_once_then_hydrates_by_page', kind='patch', target='configured'),
            ConsumerSite(path='backend/tests/test_checkup_service.py', scope='<module>.test_backfill_job_embeds_under_per_source_lock', kind='patch', target='configured'),
        ),
    ),
    SurfaceMember(
        name='confirm_conflict',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.confirm_conflict', kind='attribute', target='confirm_conflict'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='confirm_merge',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.confirm_merge', kind='attribute', target='confirm_merge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='conflict_resolution_admitted',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='conflict_resolution_admitted'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='conversation_creator',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._own_conversation_or_404', kind='attribute', target='conversation_creator'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='conversation_share_state',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.get_conversation_share_route', kind='attribute', target='conversation_share_state'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_knowhow_milestone',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.create_knowhow_milestone', kind='attribute', target='create_knowhow_milestone'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_knowhow_table',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.create_table', kind='attribute', target='create_knowhow_table'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_knowhow_table_with_rows',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.import_table', kind='attribute', target='create_knowhow_table_with_rows'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_memory_candidate',
        owner='MemoryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.propose_memory.create', kind='attribute', target='create_memory_candidate'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_notebook',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>.measure_speed', kind='attribute', target='create_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.ensure_notebook', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/bench_sqlite_writes.py', scope='<module>.main', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.main', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_pipeline_event_logging', kind='attribute', target='create_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='create_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_notebook_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.create_notebook_object_schema', kind='attribute', target='create_notebook_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.create_object_schema', kind='attribute', target='create_object_schema'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='create_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='create_report',
        owner='ReportApplicationService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.create_report', kind='attribute', target='create_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='current_user',
        owner='IdentityStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._own_conversation_or_404', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._stream_ask_events', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._validate_source_scope', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.cancel_ask_job', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.get_ask_job', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>._resolve_session_user', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.get_current_user', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._launch_generate_job', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._launch_plan_job', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._own_report_or_404', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.export_reports_endpoint', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.list_reports', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.upload_notebook_asset', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.main', kind='attribute', target='current_user'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='current_user'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='current_user'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='db_path',
        owner='SqliteDatabase',
        kind='mutable_property',
        consumers=(
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.__init__', kind='attribute', target='db_path'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='decided_seed_pairs',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='decided_seed_pairs'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_conversation',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.delete_conversation', kind='attribute', target='delete_conversation'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='delete_conversation'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_knowhow_cell_code',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.delete_cell_code', kind='attribute', target='delete_knowhow_cell_code'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_knowhow_column',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.delete_knowhow_column', kind='attribute', target='delete_knowhow_column'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_knowhow_milestone',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.delete_knowhow_milestone', kind='attribute', target='delete_knowhow_milestone'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_knowhow_row',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.delete_knowhow_row', kind='attribute', target='delete_knowhow_row'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_knowhow_table',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.delete_knowhow_table', kind='attribute', target='delete_knowhow_table'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_notebook',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>._cleanup', kind='attribute', target='delete_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_notebook_kg',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>.main', kind='attribute', target='delete_notebook_kg'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_notebook_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.delete_notebook_object_schema', kind='attribute', target='delete_notebook_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.delete_object_schema', kind='attribute', target='delete_object_schema'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='delete_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_report',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.delete_report', kind='attribute', target='delete_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='delete_source',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>.register_source_tools.delete_source.run', kind='attribute', target='delete_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='delete_source'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='effective_document_limit',
        owner='IdentityStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._document_capacity_limit', kind='attribute', target='effective_document_limit'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='effective_schemas',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='effective_schemas'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='event_log',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='event_log'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='evidence_elements',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/citations.py', scope='<module>.register_citation_tools.get_cited_element.load', kind='attribute', target='evidence_elements'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='execute_indexing_pipeline_rebuild',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.set_indexing_pipeline', kind='attribute', target='execute_indexing_pipeline_rebuild'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='execute_notebook_kg_job',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.build_kg', kind='attribute', target='execute_notebook_kg_job'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_kg', kind='attribute', target='execute_notebook_kg_job'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.build_kg.run', kind='attribute', target='execute_notebook_kg_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='export_reports',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.export_reports_endpoint', kind='attribute', target='export_reports'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='extract_source',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>.measure_speed', kind='attribute', target='extract_source'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='extract_source'),
            ConsumerSite(path='backend/app/services/reextract.py', scope='<module>.reextract_notebook', kind='attribute', target='extract_source'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_resumes_existing_without_kg', kind='patch', target='extract_source'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_resumes_existing_without_kg', kind='patch', target='extract_source'),
        ),
    ),
    SurfaceMember(
        name='fail_conflict_resolution_submission',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='fail_conflict_resolution_submission'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='fail_notebook_kg_job_submission',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.build_kg', kind='attribute', target='fail_notebook_kg_job_submission'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_kg', kind='attribute', target='fail_notebook_kg_job_submission'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.build_kg.run', kind='attribute', target='fail_notebook_kg_job_submission'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='fail_notebook_relink_submission',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.relink_kg', kind='attribute', target='fail_notebook_relink_submission'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='fail_unified_kg_rebuild_submission',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_unified_kg', kind='attribute', target='fail_unified_kg_rebuild_submission'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='find_duplicates',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.find_duplicates', kind='attribute', target='find_duplicates'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='find_duplicates'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_conversation',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._intent_history', kind='attribute', target='get_conversation'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.get_conversation', kind='attribute', target='get_conversation'),
            ConsumerSite(path='backend/tests/test_admin_user_activity_api.py', scope='<module>.test_ask_detail_does_not_load_full_conversation_history', kind='patch', target='get_conversation'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='get_conversation'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='get_conversation'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_admin_user_activity_api.py', scope='<module>.test_ask_detail_does_not_load_full_conversation_history', kind='patch', target='get_conversation'),
        ),
    ),
    SurfaceMember(
        name='get_knowhow_cell_code',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.get_cell_code', kind='attribute', target='get_knowhow_cell_code'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.put_cell_code', kind='attribute', target='get_knowhow_cell_code'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_knowhow_change',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.create_knowhow_milestone', kind='attribute', target='get_knowhow_change'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.get_knowhow_change', kind='attribute', target='get_knowhow_change'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_knowhow_row_location',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_agent_routes.py', scope='<module>._resolve_row_notebook', kind='attribute', target='get_knowhow_row_location'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.get_knowhow_row.load', kind='attribute', target='get_knowhow_row_location'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.put_knowhow_cell_code.run', kind='attribute', target='get_knowhow_row_location'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.delete_cell_code', kind='attribute', target='get_knowhow_row_location'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.get_cell_code', kind='attribute', target='get_knowhow_row_location'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.put_cell_code', kind='attribute', target='get_knowhow_row_location'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_knowhow_table',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_agent_routes.py', scope='<module>.get_agent_discrimination', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_agent_routes.py', scope='<module>.get_agent_row', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.add_knowhow_column', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.add_knowhow_row', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.import_knowhow_table', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cell', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cells_batch', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_column', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.get_knowhow_discrimination.load', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.get_knowhow_row.load', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.get_cell_code', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.get_table_in_notebook', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.list_tables_for_agent', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.put_cell_code', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>.move_table', kind='attribute', target='get_knowhow_table'),
            ConsumerSite(path='scripts/backfill_knowhow_md.py', scope='<module>.plan_backfill', kind='attribute', target='get_knowhow_table'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_memory',
        owner='MemoryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.get_memory.load', kind='attribute', target='get_memory'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.search_agent_memory.load', kind='attribute', target='get_memory'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_notebook',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.ask', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.ask_stream', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.preview_ask_intent.run_preview', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.build_kg', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_kg', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_unified_kg', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.relink_kg', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.review_all_unified_kg_merges', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.ask_notebook.run_ask', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/session.py', scope='<module>.register_session_tools.list_notebooks.load', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/session.py', scope='<module>.register_session_tools.select_notebook.load', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._report_scope_recheck', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.confirm_report_intent', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.create_report', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.generate_report', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._report_kg_build_already_running', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.ensure_notebook', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_chunk_elements', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_source_facts', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_source_index', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg._finalize', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_question_index', kind='attribute', target='get_notebook'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='get_notebook'),
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>.main', kind='attribute', target='get_notebook'),
            ConsumerSite(path='scripts/replay_retrieval.py', scope='<module>.record_run', kind='attribute', target='get_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='get_notebook'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='get_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_notebook_asset',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.public_conversation_asset_route', kind='attribute', target='get_notebook_asset'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.get_notebook_asset_file', kind='attribute', target='get_notebook_asset'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>._remap', kind='attribute', target='get_notebook_asset'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_report',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._own_report_or_404', kind='attribute', target='get_report'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.cancel_report_endpoint', kind='attribute', target='get_report'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='get_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='get_source',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>._own_source', kind='attribute', target='get_source'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.reparse_sources', kind='attribute', target='get_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>._source_evidence', kind='attribute', target='get_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_pipeline_event_logging', kind='attribute', target='get_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='get_source'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='hidden_source_ids',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._validate_source_scope', kind='attribute', target='hidden_source_ids'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='index_status',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.index_status', kind='attribute', target='index_status'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.get_build_status.load', kind='attribute', target='index_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='indexing_pipeline_options',
        owner='RepositoryRuntime.indexing_pipeline',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.get_indexing_pipeline', kind='attribute', target='indexing_pipeline_options'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='kg_analysis',
        owner='KgAnalysisService',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.kg_analysis_service', kind='attribute', target='kg_analysis'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='kg_neighbors',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.object_neighbors', kind='attribute', target='kg_neighbors'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='kg_search',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.kg_search', kind='attribute', target='kg_search'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='knowhow_cell_history',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.get_knowhow_cell_history', kind='attribute', target='knowhow_cell_history'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='knowhow_changes_between',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.get_knowhow_history_diff', kind='attribute', target='knowhow_changes_between'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='knowhow_history_page',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.get_knowhow_history', kind='attribute', target='knowhow_history_page'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='knowledge_graph',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.knowledge_graph', kind='attribute', target='knowledge_graph'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.main', kind='attribute', target='knowledge_graph'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='knowledge_graph'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='knowledge_types',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.knowledge_types', kind='attribute', target='knowledge_types'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='knowledge_types'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='knowledge_types'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='knowledge_types'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_agent_profiles',
        owner='MemoryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/agent_profile_routes.py', scope='<module>._observation_agent_names', kind='attribute', target='list_agent_profiles'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_conversations',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.list_conversations', kind='attribute', target='list_conversations'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='list_conversations'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='list_conversations'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_knowhow_cell_code',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_agent_routes.py', scope='<module>.get_agent_discrimination', kind='attribute', target='list_knowhow_cell_code'),
            ConsumerSite(path='backend/app/api/knowhow_agent_routes.py', scope='<module>.get_agent_row', kind='attribute', target='list_knowhow_cell_code'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.get_knowhow_discrimination.load', kind='attribute', target='list_knowhow_cell_code'),
            ConsumerSite(path='backend/app/api/mcp_tools/knowhow.py', scope='<module>.register_knowhow_tools.get_knowhow_row.load', kind='attribute', target='list_knowhow_cell_code'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_knowhow_tables',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.list_tables_for_agent', kind='attribute', target='list_knowhow_tables'),
            ConsumerSite(path='scripts/backfill_knowhow_md.py', scope='<module>.plan_backfill', kind='attribute', target='list_knowhow_tables'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_knowledge',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.list_knowledge', kind='attribute', target='list_knowledge'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='list_knowledge'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='list_knowledge'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='list_knowledge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_notebook_bases',
        owner='NotebookStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.list_notebook_bases_route', kind='attribute', target='list_notebook_bases'),
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.set_notebook_bases_route', kind='attribute', target='list_notebook_bases'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_notebook_object_schemas',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.list_notebook_object_schemas', kind='attribute', target='list_notebook_object_schemas'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_notebooks',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='list_notebooks'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_object_schemas',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.list_object_schemas', kind='attribute', target='list_object_schemas'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='list_object_schemas'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_promotion_queue',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.list_promotion_queue', kind='attribute', target='list_promotion_queue'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_reports',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.list_reports', kind='attribute', target='list_reports'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='list_reports'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_sources',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='list_sources'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='list_sources'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='list_sources_page',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.get_admin_user_notebook_sources', kind='attribute', target='list_sources_page'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='load_notebook_scale_facts',
        owner='QueryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/notebook_scale.py', scope='<module>.NotebookScaleProfile.facts', kind='attribute', target='load_notebook_scale_facts'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='maintenance',
        owner='SQLiteMaintenanceAdapter',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._backfill_vectors_job', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.backfill_vectors', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/scripts/backfill_relation_embeddings.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/scripts/gen_recall_gold.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/scripts/reembed_kg.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._backfill_chunk_elements_for_notebook', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._backfill_source_facts_for_notebook', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._backfill_source_index_for_notebook', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._backfill_table_to_blob', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._count_missing_chunk_vectors', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._count_missing_element_vectors', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._count_missing_node_vectors', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._require_notebook_owner', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._resolve_owner_profile', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.already_ingested', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_chunk_embeddings', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_element_embeddings', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.backfill_node_embeddings', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_chunk_elements', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_source_facts', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_backfill_source_index', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_metadata', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_metadata._one', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_question_index', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_reparse', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.source_id_by_hash', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.maybe_sweep_orphan_assets._sweep', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/reextract.py', scope='<module>.reextract_notebook', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository._backfill_relation_embeddings', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.checkup.<lambda>', kind='attribute', target='maintenance'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.eval_insert_source_for_test', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/backfill_kg_embeddings.py', scope='<module>._counts', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/backfill_kg_embeddings.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/backfill_knowhow_md.py', scope='<module>._run_apply_from_plan', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/build_chunks.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>._run_status', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>.main._extract', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.grounding_ok', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.insert_source', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/kg_product_smoke.py', scope='<module>.main', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>._insert_rule', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>._latest_extraction_run', kind='attribute', target='maintenance'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='maintenance'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='mark_notebook_base',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='mark_notebook_base'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='maybe_auto_index',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='maybe_auto_index'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='merge_knowledge',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.merge_knowledge', kind='attribute', target='merge_knowledge'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='merge_knowledge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='merge_review_job_status',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.merge_review_job', kind='attribute', target='merge_review_job_status'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.review_all_unified_kg_merges', kind='attribute', target='merge_review_job_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='mineru_client',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='mineru_client'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='mineru_client'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='mineru_cloud_client',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='mineru_cloud_client'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='mineru_cloud_client'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='mountable_notebooks',
        owner='NotebookStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.mountable_notebooks_route', kind='attribute', target='mountable_notebooks'),
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.set_notebook_bases_route', kind='attribute', target='mountable_notebooks'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='mounted_by_count',
        owner='NotebookStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.mounted_by_count_route', kind='attribute', target='mounted_by_count'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='node_context',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.object_context', kind='attribute', target='node_context'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='notebook_analytics',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='notebook_analytics'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='notebook_copy_stats',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='notebook_copy_stats'),
            ConsumerSite(path='backend/tests/test_kg_analysis_precompute.py', scope='<module>.test_refused_large_graph_build_writes_no_artifacts', kind='patch', target='notebook_copy_stats'),
            ConsumerSite(path='backend/tests/test_rebuild_communities.py', scope='<module>.test_large_no_index_builds_with_igraph', kind='patch', target='notebook_copy_stats'),
            ConsumerSite(path='backend/tests/test_rebuild_communities.py', scope='<module>.test_large_no_index_refuses_without_igraph', kind='patch', target='notebook_copy_stats'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_kg_analysis_precompute.py', scope='<module>.test_refused_large_graph_build_writes_no_artifacts', kind='patch', target='notebook_copy_stats'),
            ConsumerSite(path='backend/tests/test_rebuild_communities.py', scope='<module>.test_large_no_index_builds_with_igraph', kind='patch', target='notebook_copy_stats'),
            ConsumerSite(path='backend/tests/test_rebuild_communities.py', scope='<module>.test_large_no_index_refuses_without_igraph', kind='patch', target='notebook_copy_stats'),
        ),
    ),
    SurfaceMember(
        name='notebook_owner',
        owner='IdentityStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._document_capacity_limit', kind='attribute', target='notebook_owner'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='notebook_relink_status',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.relink_kg_status', kind='attribute', target='notebook_relink_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='parallelism',
        owner='ModelProvider',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>.measure_speed', kind='attribute', target='parallelism'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='parse_source',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>._insert_source', kind='attribute', target='parse_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_pipeline_event_logging', kind='attribute', target='parse_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='parse_source'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='participant_notebook_ids',
        owner='NotebookStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/citations.py', scope='<module>.register_citation_tools.get_cited_element.load', kind='attribute', target='participant_notebook_ids'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='pending_actions',
        owner='PendingActionsService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/system_routes.py', scope='<module>.me_pending_actions', kind='attribute', target='pending_actions'),
            ConsumerSite(path='backend/app/api/system_routes.py', scope='<module>.me_pending_stream.gen', kind='attribute', target='pending_actions'),
            ConsumerSite(path='backend/app/main.py', scope='<module>.create_app.<lambda>', kind='attribute', target='pending_actions'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='pending_conflicts',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.get_pending_conflicts', kind='attribute', target='pending_conflicts'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='pending_merges',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.get_pending_merges', kind='attribute', target='pending_merges'),
            ConsumerSite(path='backend/tests/test_merge_review_job.py', scope='<module>.test_run_merge_review_job_bounded_sql_queries', kind='patch', target='pending_merges'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_merge_review_job.py', scope='<module>.test_run_merge_review_job_bounded_sql_queries', kind='patch', target='pending_merges'),
        ),
    ),
    SurfaceMember(
        name='prepare_notebook_kg_job',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.build_kg', kind='attribute', target='prepare_notebook_kg_job'),
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_kg', kind='attribute', target='prepare_notebook_kg_job'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.build_kg.run', kind='attribute', target='prepare_notebook_kg_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='preview_reasoning_intent',
        owner='AskService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.preview_ask_intent.run_preview', kind='attribute', target='preview_reasoning_intent'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='process_source',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>._upload_agent_source.<lambda>', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>.register_source_tools.add_source_url.run.<lambda>', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>.register_source_tools.reparse_source.run', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.import_url_sources.<lambda>', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.reparse_sources', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.upload_sources.<lambda>', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all._sched', kind='attribute', target='process_source'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_reparse', kind='attribute', target='process_source'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='process_source'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='propose_promotion',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.propose_promotion', kind='attribute', target='propose_promotion'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='propose_schemas',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.propose_schemas', kind='attribute', target='propose_schemas'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='propose_schemas'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='prune_knowhow_history',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.prune_knowhow_history', kind='attribute', target='prune_knowhow_history'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='public_conversation_by_token',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._public_conversation_or_404', kind='attribute', target='public_conversation_by_token'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='public_report_by_token',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.public_report_route', kind='attribute', target='public_report_by_token'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='rebuild_unified_kg',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/scripts/recluster_kg.py', scope='<module>.main', kind='attribute', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg._finalize', kind='attribute', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_reparse', kind='attribute', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_alone_forces_rebuild', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_flag_clears_checkpoint', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_all_ingests_then_runs_kg', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_kg_fresh_dispatches_force_and_fresh', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_fresh_flag_forces_rebuild', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_pipelines_new_sources', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_reparses_existing_source_missing_elements', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_resumes_existing_without_kg', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_disables_fusion_and_rebuilds', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_finalizer_failure_marks_durable_job_failed', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_limit_extracts_subset', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_plain_no_rebuild_without_llm_is_noop', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_and_run_all_do_not_reconfigure_scheduler', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_disables_incremental_fusion_during_run', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_only_targets_sources_missing_elements', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_scale_index_repo.py', scope='<module>.test_run_kg_no_rebuild_skips_clustering', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='scripts/denoise_reextract_nb.py', scope='<module>.main', kind='attribute', target='rebuild_unified_kg'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_alone_forces_rebuild', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_kg_fresh_flag_clears_checkpoint', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_all_ingests_then_runs_kg', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_main_kg_fresh_dispatches_force_and_fresh', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_fresh_flag_forces_rebuild', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_pipelines_new_sources', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_reparses_existing_source_missing_elements', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_all_resumes_existing_without_kg', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_disables_fusion_and_rebuilds', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_finalizer_failure_marks_durable_job_failed', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_limit_extracts_subset', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_kg_plain_no_rebuild_without_llm_is_noop', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_and_run_all_do_not_reconfigure_scheduler', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_disables_incremental_fusion_during_run', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_reparse_only_targets_sources_missing_elements', kind='patch', target='rebuild_unified_kg'),
            ConsumerSite(path='backend/tests/test_scale_index_repo.py', scope='<module>.test_run_kg_no_rebuild_skips_clustering', kind='patch', target='rebuild_unified_kg'),
        ),
    ),
    SurfaceMember(
        name='refresh_agent_principal',
        owner='MemoryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/_shared.py', scope='<module>._live_principal', kind='attribute', target='refresh_agent_principal'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='reject_conflict',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.reject_conflict', kind='attribute', target='reject_conflict'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='reject_merge',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.reject_merge', kind='attribute', target='reject_merge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='reject_promotion_as_reviewer',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/admin_routes.py', scope='<module>.reject_promotion', kind='attribute', target='reject_promotion_as_reviewer'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='relations_for_notebook',
        owner='KnowledgeQueryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='relations_for_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='rename_conversation',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.rename_conversation', kind='attribute', target='rename_conversation'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='rename_conversation'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='rename_knowhow_column',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_column', kind='attribute', target='rename_knowhow_column'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='replace_notebook_bases',
        owner='NotebookStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.set_notebook_bases_route', kind='attribute', target='replace_notebook_bases'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='report_execution',
        owner='ReportEngine',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._launch_generate_job', kind='attribute', target='report_execution'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>._launch_plan_job', kind='attribute', target='report_execution'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='report_share_token',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.get_report_share_route', kind='attribute', target='report_share_token'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='require_agent_access',
        owner='MemoryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/_shared.py', scope='<module>._selected_notebook', kind='attribute', target='require_agent_access'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.ask_notebook.check_memory_scope', kind='attribute', target='require_agent_access'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.get_memory.load', kind='attribute', target='require_agent_access'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.search_agent_memory.load', kind='attribute', target='require_agent_access'),
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.search_notebook_context.load', kind='attribute', target='require_agent_access'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='require_indexing_pipeline_write',
        owner='RepositoryRuntime.indexing_pipeline',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._build_chunks_for_source', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._chunk_and_embed_source', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.add_url_sources', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.build_notebook_kg', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.import_sources', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.prepare_notebook_kg_job', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.process_source', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_notebook_kg', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.rebuild_unified_kg', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.start_unified_kg_rebuild', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.trigger_scale_index_rebuild', kind='attribute', target='require_indexing_pipeline_write'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.upload_sources', kind='attribute', target='require_indexing_pipeline_write'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='reset_request_user',
        owner='RequestContext',
        kind='method',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='reset_request_user'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>', kind='import', target='app.services.sqlite_repository:reset_request_user'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='resolve_session',
        owner='IdentityStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>._resolve_session_user', kind='attribute', target='resolve_session'),
            ConsumerSite(path='backend/app/api/deps.py', scope='<module>.get_current_user', kind='attribute', target='resolve_session'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='retrieval',
        owner='RetrievalService',
        kind='property',
        consumers=(
            ConsumerSite(path='backend/app/eval/retrieval_metrics.py', scope='<module>.run_recall', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._active_kg_delta', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._build_chunk_retrieval_plan', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._chunk_kg_overlay', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._concept_cluster_id', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._delta_vector_matrix', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._edge_support_map', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._elem_chunk_map', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._embed_query', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ent_chunk_map', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._federated_graph_is_large', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._federated_rx_graph', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._follow_chain', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._gather_chunks', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._gather_elements', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._gather_vector_chunks', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._graph_seed_fusion', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._hydrate_chunk_candidates', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._in_batches', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._keyword_chunk_candidates', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._keyword_token_sets', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._kg_object_candidates', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._kg_source_chunks', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mention_extra_edges', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mix_retrieve', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._mmr_select_chunks', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._notebook_langs', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ppr_fact_rerank', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ppr_graph', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ppr_reset_vector', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._ppr_retrieve', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._preload_scale_retrieval_artifacts', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._relation_ann_candidates', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._relations_with_names', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_chunks', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_chunks_ann', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_chunks_fts_degraded', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_chunks_multi', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_elements', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_neighbors', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_relations_scored', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._retrieve_scored', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._rrf_scored', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._runtime_dim', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_combined_graph', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._scale_xlayer_bridge_edges', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._vector_matrix', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._vector_matrix_version', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade._vector_matrix_warm', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.cluster_map', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.federated_retrieve', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.federated_retrieve_relations', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.scale_ppr', kind='attribute', target='retrieval'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.maintenance.<lambda>', kind='attribute', target='retrieval'),
            ConsumerSite(path='scripts/replay_retrieval.py', scope='<module>.record_run', kind='attribute', target='retrieval'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='revert_knowhow_table',
        owner='KnowhowHistoryStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/history.py', scope='<module>.revert_table', kind='attribute', target='revert_knowhow_table'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='review_pending_merges',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.review_unified_kg_merges', kind='attribute', target='review_pending_merges'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='review_queue',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.edge_review_queue', kind='attribute', target='review_queue'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='root_dir',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='root_dir'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='run_conflict_resolution_job',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='run_conflict_resolution_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='run_merge_review_job',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.review_all_unified_kg_merges', kind='attribute', target='run_merge_review_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='run_notebook_relink_job',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.relink_kg', kind='attribute', target='run_notebook_relink_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='run_unified_kg_rebuild_job',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_unified_kg', kind='attribute', target='run_unified_kg_rebuild_job'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='scale_index_status',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.scale_index_status', kind='attribute', target='scale_index_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='search_notebook',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/memory_context.py', scope='<module>.register_memory_context_tools.search_notebook_context.load', kind='attribute', target='search_notebook'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='search_notebook'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='search_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_conflict_status',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='set_conflict_status'),
            ConsumerSite(path='backend/tests/test_knowledge_governance_delegation.py', scope='<module>.test_compound_conflict_status_rides_the_facade_wrapper', kind='patch', target='set_conflict_status'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_knowledge_governance_delegation.py', scope='<module>.test_compound_conflict_status_rides_the_facade_wrapper', kind='patch', target='set_conflict_status'),
        ),
    ),
    SurfaceMember(
        name='set_edge_review',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.review_relation', kind='attribute', target='set_edge_review'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_indexing_pipeline',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/notebook_routes.py', scope='<module>.set_indexing_pipeline', kind='attribute', target='set_indexing_pipeline'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_knowhow_anchor_column',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_table', kind='attribute', target='set_knowhow_anchor_column'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_knowhow_column_kind',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_column', kind='attribute', target='set_knowhow_column_kind'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_notebook_personal',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='set_notebook_personal'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='set_request_user',
        owner='RequestContext',
        kind='method',
        consumers=(
            ConsumerSite(path='app.services.sqlite_repository', scope='<module>', kind='compatibility', target='set_request_user'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>', kind='import', target='app.services.sqlite_repository:set_request_user'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='settings',
        owner='QueryStore',
        kind='instance_attribute',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.update_report_outline', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_kg', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_question_index', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_reparse', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.build_projector', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.complete_row', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>._make_persist_image', kind='attribute', target='settings'),
            ConsumerSite(path='backend/app/services/sqlite_repository.py', scope='<module>.SQLiteRepository.__init__', kind='attribute', target='settings'),
            ConsumerSite(path='scripts/replay_retrieval.py', scope='<module>._run_full', kind='attribute', target='settings'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='share_conversation',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.share_conversation_route', kind='attribute', target='share_conversation'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='share_report',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.share_report_route', kind='attribute', target='share_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='source_elements',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__.<lambda>', kind='attribute', target='source_elements'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>._source_evidence', kind='attribute', target='source_elements'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='source_elements'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='source_id_by_hash',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>._upload_agent_source', kind='attribute', target='source_id_by_hash'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='source_metadata',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/citations.py', scope='<module>.register_citation_tools.get_cited_element.load', kind='attribute', target='source_metadata'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='source_parse_busy',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>.register_source_tools.reparse_source.run', kind='attribute', target='source_parse_busy'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='sources_missing_paper_meta',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.backfill_paper_metadata', kind='attribute', target='sources_missing_paper_meta'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='start_ask_stream',
        owner='AskExecutionCoordinator',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._stream_ask_events', kind='attribute', target='start_ask_stream'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='start_conflict_resolution',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.resolve_conflicts', kind='attribute', target='start_conflict_resolution'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='start_notebook_relink',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.relink_kg', kind='attribute', target='start_notebook_relink'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='start_unified_kg_rebuild',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_unified_kg', kind='attribute', target='start_unified_kg_rebuild'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='storage_dir',
        owner='RepositoryRuntime.storage_dir',
        kind='mutable_property',
        consumers=(
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>._dispatch_main', kind='attribute', target='storage_dir'),
            ConsumerSite(path='backend/app/services/knowhow/assets.py', scope='<module>.AssetService.__init__', kind='attribute', target='storage_dir'),
            ConsumerSite(path='backend/app/services/knowhow/transfer.py', scope='<module>.copy_table', kind='attribute', target='storage_dir'),
            ConsumerSite(path='backend/app/services/repository_facade.py', scope='<module>.RepositoryFacade.__init__', kind='attribute', target='storage_dir'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='store_kg',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/bench_sqlite_writes.py', scope='<module>._proc_work', kind='attribute', target='store_kg'),
            ConsumerSite(path='scripts/bench_sqlite_writes.py', scope='<module>._run_threads.work', kind='attribute', target='store_kg'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='store_kg'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='store_kg'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='submit_feedback',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.submit_feedback', kind='attribute', target='submit_feedback'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='submit_feedback'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='submit_feedback'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='trigger_scale_index_rebuild',
        owner='ScaleArtifactRuntime',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.rebuild_scale_index', kind='attribute', target='trigger_scale_index_rebuild'),
            ConsumerSite(path='backend/app/api/mcp_tools/maintenance.py', scope='<module>.register_maintenance_tools.build_retrieval_index.run', kind='attribute', target='trigger_scale_index_rebuild'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='unified_graph',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.get_unified_kg', kind='attribute', target='unified_graph'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='unified_kg_rebuild_status',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.unified_kg_rebuild_status', kind='attribute', target='unified_kg_rebuild_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='unified_kg_status',
        owner='KnowledgeLifecycleService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/kg_routes.py', scope='<module>.unified_kg_status', kind='attribute', target='unified_kg_status'),
            ConsumerSite(path='backend/app/api/mcp_tools/session.py', scope='<module>.register_session_tools.select_notebook.load', kind='attribute', target='unified_kg_status'),
            ConsumerSite(path='scripts/verify_repository_snapshot.py', scope='<module>.exercise_reads', kind='attribute', target='unified_kg_status'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='unshare_conversation',
        owner='AskStateStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>.unshare_conversation_route', kind='attribute', target='unshare_conversation'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='unshare_report',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.unshare_report_route', kind='attribute', target='unshare_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowhow_cell',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cell', kind='attribute', target='update_knowhow_cell'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowhow_cells',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cells_batch', kind='attribute', target='update_knowhow_cells'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowhow_cells_bulk_guarded',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/backfill_knowhow_md.py', scope='<module>.apply_reviewed_plan', kind='attribute', target='update_knowhow_cells_bulk_guarded'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowhow_cells_guarded_atomic',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cell', kind='attribute', target='update_knowhow_cells_guarded_atomic'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cells_batch', kind='attribute', target='update_knowhow_cells_guarded_atomic'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowhow_table_meta',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_table', kind='attribute', target='update_knowhow_table_meta'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_knowledge',
        owner='KnowledgeGovernanceService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.update_knowledge', kind='attribute', target='update_knowledge'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='update_knowledge'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='update_knowledge'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_notebook',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='update_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_notebook_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.update_notebook_object_schema', kind='attribute', target='update_notebook_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_object_schema',
        owner='SchemaRegistryService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowledge_routes.py', scope='<module>.update_object_schema', kind='attribute', target='update_object_schema'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_object_schemas', kind='attribute', target='update_object_schema'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='update_report',
        owner='ReportStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.cancel_report_endpoint', kind='attribute', target='update_report'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.create_report', kind='attribute', target='update_report'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.update_report_outline', kind='attribute', target='update_report'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='upload_sources',
        owner='SourceIngestionService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/sources.py', scope='<module>._upload_agent_source', kind='attribute', target='upload_sources'),
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>.upload_sources', kind='attribute', target='upload_sources'),
            ConsumerSite(path='backend/app/eval/speed.py', scope='<module>._insert_source', kind='attribute', target='upload_sources'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_all', kind='attribute', target='upload_sources'),
            ConsumerSite(path='backend/app/services/batch_ingest.py', scope='<module>.run_ingest._one', kind='attribute', target='upload_sources'),
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_ingest_repeated_interrupt_cannot_escape_worker_drain', kind='patch', target='upload_sources'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_kg_store_ask_and_conversations', kind='attribute', target='upload_sources'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.check_pipeline_event_logging', kind='attribute', target='upload_sources'),
            ConsumerSite(path='scripts/smoke_backend.py', scope='<module>.main', kind='attribute', target='upload_sources'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_batch_ingest.py', scope='<module>.test_run_ingest_repeated_interrupt_cannot_escape_worker_drain', kind='patch', target='upload_sources'),
        ),
    ),
    SurfaceMember(
        name='upsert_knowhow_cell_code',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.put_cell_code', kind='attribute', target='upsert_knowhow_cell_code'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='user_can_access_notebook',
        owner='NotebookSharingService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/mcp_tools/_shared.py', scope='<module>._writable_notebook', kind='attribute', target='user_can_access_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='user_can_read_notebook',
        owner='NotebookSharingService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._public_conversation_or_404', kind='attribute', target='user_can_read_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/profiles.py', scope='<module>.register_profile_tools.add_observation.run', kind='attribute', target='user_can_read_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/session.py', scope='<module>.register_session_tools.list_notebooks.load', kind='attribute', target='user_can_read_notebook'),
            ConsumerSite(path='backend/app/api/mcp_tools/session.py', scope='<module>.register_session_tools.select_notebook.load', kind='attribute', target='user_can_read_notebook'),
            ConsumerSite(path='backend/app/api/report_routes.py', scope='<module>.public_report_route', kind='attribute', target='user_can_read_notebook'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='validate_cell_target',
        owner='KnowhowStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.optimize_knowhow_cell', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cell', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.patch_knowhow_cells_batch', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/api/knowhow_routes.py', scope='<module>.reformat_knowhow_cell', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.delete_cell_code', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.get_cell_code', kind='attribute', target='validate_cell_target'),
            ConsumerSite(path='backend/app/services/knowhow/api.py', scope='<module>.put_cell_code', kind='attribute', target='validate_cell_target'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='visible_document_count',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/source_routes.py', scope='<module>._document_capacity', kind='attribute', target='visible_document_count'),
            ConsumerSite(path='backend/tests/test_document_limit.py', scope='<module>.test_document_capacity_limit_resolves_without_counting', kind='patch', target='visible_document_count'),
        ),
        patches=(
            ConsumerSite(path='backend/tests/test_document_limit.py', scope='<module>.test_document_capacity_limit_resolves_without_counting', kind='patch', target='visible_document_count'),
        ),
    ),
    SurfaceMember(
        name='visible_source_scope_snapshot',
        owner='SourceStore',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/api/ask_routes.py', scope='<module>._validate_source_scope', kind='attribute', target='visible_source_scope_snapshot'),
        ),
        patches=(
        ),
    ),
    SurfaceMember(
        name='warm_open_path_caches',
        owner='NotebookCatalogService',
        kind='method',
        consumers=(
            ConsumerSite(path='backend/app/services/startup_warmup.py', scope='<module>.run_startup', kind='attribute', target='warm_open_path_caches'),
        ),
        patches=(
        ),
    ),
)
def _unique_nonempty_owners(
    members: tuple[SurfaceMember, ...],
) -> dict[str, str]:
    owners: dict[str, str] = {}
    for member in members:
        owner = DELEGATE_OWNER_OVERRIDES.get(member.name, member.owner)
        owner = _RUNTIME_COMPONENT_OWNER_ALIASES.get(owner, owner)
        if not member.name or not owner:
            raise ValueError("surface members require a non-empty name and owner")
        if member.name in owners:
            raise ValueError(f"duplicate ownership entry for {member.name!r}")
        owners[member.name] = owner
    return owners


_RUNTIME_COMPONENT_OWNER_ALIASES: Mapping[str, str] = {
    "RepositoryRuntime.ask_component": "AskService",
    "RepositoryRuntime.evidence_context_component": "EvidenceContextService",
    "RepositoryRuntime.knowledge_query": "KnowledgeQueryService",
    "RepositoryRuntime.memory_service": "MemoryService",
    "RepositoryRuntime.pending_actions_service": "PendingActionsService",
    "RepositoryRuntime.retrieval_component": "RetrievalService",
}


DELEGATE_OWNER_OVERRIDES: Mapping[str, str] = {
    'confirm_memory': 'MemoryService',
    'create_memory_candidate': 'MemoryService',
    'create_memory_from_answer': 'MemoryService',
    'answer_memory_links': 'MemoryService',
    'deprecate_memory': 'MemoryService',
    'delete_memory': 'MemoryService',
    'bulk_delete_memories': 'MemoryService',
    'get_memory': 'MemoryService',
    'list_memories': 'MemoryService',
    'memory_revisions': 'MemoryService',
    'propose_memory_promotion': 'MemoryService',
    'reject_memory': 'MemoryService',
    'update_memory': 'MemoryService',
    'transfer_memories': 'MemoryService',
    '_NOTEBOOK_COUNT_TYPES': 'NotebookSummaryQuery',
    '_active_kg_delta': 'RetrievalService',
    '_add_column_if_missing': 'SqliteDatabase',
    '_annotate_edge_support': 'RepositoryRuntime.knowledge_query',
    '_answer_chunks': 'RepositoryRuntime.ask_component',
    '_answer_context': 'RepositoryRuntime.evidence_context_component',
    '_answer_mix': 'RepositoryRuntime.ask_component',
    '_answer_reasoning': 'RepositoryRuntime.ask_component',
    '_answer_with_retry': 'RepositoryRuntime.ask_component',
    '_any_base_notebook_has_kg': 'KnowledgeStore',
    '_as_retrieved': 'KnowledgeQueryService',
    '_ask_cancel_events': 'AskCancellationRegistry',
    '_ask_cancel_lock': 'AskCancellationRegistry',
    '_augment_notebook_meta': 'SourceIngestionService',
    '_backfill_knowledge_embeddings': 'SourceEmbeddingService',
    '_backfill_relation_embeddings': 'SQLiteMaintenanceAdapter',
    '_mounted_bases': 'NotebookSummaryQuery',
    '_build_chunk_retrieval_plan': 'RetrievalService',
    '_build_chunks_for_source': 'SourceChunkingService',
    '_build_viz_graph_arrays': 'ScaleArtifactRuntime',
    '_bump_cluster_mutation_seq': 'KgMutationCoordinator',
    '_chunk_and_embed_source': 'SourceChunkingService',
    '_chunk_answer_context': 'RepositoryRuntime.evidence_context_component',
    '_chunk_kg_overlay': 'RetrievalService',
    '_citations_from': 'RepositoryRuntime.evidence_context_component',
    '_cleanup_empty_conversation': 'AskStateStore',
    '_clear_source_extraction_state': 'KnowledgeStore',
    '_cluster_input_version': 'KnowledgeLifecycleService',
    '_compute_scale_version_cold': 'IndexProjectionStore',
    '_concept_cluster_id': 'RetrievalService',
    '_conversation_history': 'AskStateStore',
    '_count': 'NotebookSummaryQuery',
    '_count_knowledge': 'KnowledgeStore',
    '_count_pending_kg_sources': 'NotebookSummaryQuery',
    '_delete_file': 'SourceFileStore',
    '_delete_knowledge_object_sources': 'KnowledgeStore',
    '_delete_relations_for_source': 'KnowledgeStore',
    '_delta_vector_matrix': 'RetrievalService',
    '_dequeue_scale_idle': 'ScaleArtifactRuntime',
    '_derive_object_graph_lite': 'ScaleIndexBuilder',
    '_edge_centrality_map': 'RepositoryRuntime.knowledge_query',
    '_edge_support_map': 'RetrievalService',
    '_elem_chunk_map': 'RetrievalService',
    '_element_texts': 'KnowledgeStore',
    '_embed_chunks_batch': 'SourceEmbeddingService',
    '_embed_chunks_for_source': 'SourceEmbeddingService',
    '_embed_knowledge': 'SourceEmbeddingService',
    '_embed_objects_batch': 'SourceEmbeddingService',
    '_embed_query': 'RetrievalService',
    '_embed_relations_batch': 'SourceEmbeddingService',
    '_embed_source': 'SourceEmbeddingService',
    '_enrich_evidence': 'KnowledgeStore',
    '_ensure_conversation': 'RepositoryRuntime.ask_component',
    '_ensure_scale_scheduler': 'ScaleArtifactRuntime',
    '_ent_chunk_map': 'RetrievalService',
    '_extraction_warning': 'SourceStore',
    '_federated_graph_is_large': 'RetrievalService',
    '_federated_rx_graph': 'RetrievalService',
    '_find_base_dedup_match': 'GovernanceStore',
    '_find_stale_knowledge_ids_for_source': 'KnowledgeStore',
    '_fold_hits_to_canonical': 'RepositoryRuntime.knowledge_query',
    '_follow_chain': 'RetrievalService',
    '_gather_chunks': 'RetrievalService',
    '_gather_elements': 'RetrievalService',
    '_gather_kg_graph': 'ScaleIndexBuilder',
    '_gather_vector_chunks': 'RetrievalService',
    '_graph_seed_fusion': 'RetrievalService',
    '_has_kg': 'NotebookSummaryQuery',
    '_has_pending_merges': 'KnowledgeGovernanceService',
    '_hydrate_chunk_candidates': 'RetrievalService',
    '_hydrate_search_hits': 'RepositoryRuntime.knowledge_query',
    '_in_batches': 'RetrievalService',
    '_index_delta': 'ScaleIndexBuilder',
    '_insert_row': 'SharingStore',
    '_invalidate_unified_cache': 'KgMutationCoordinator',
    '_keyword_chunk_candidates': 'RetrievalService',
    '_keyword_token_sets': 'RetrievalService',
    '_kg_headline': 'RepositoryRuntime.knowledge_query',
    '_kg_llm_client': 'ModelProvider',
    '_kg_neighbors_db': 'KnowledgeLifecycleService',
    '_kg_object_candidates': 'RetrievalService',
    '_kg_source_chunks': 'RetrievalService',
    '_knowledge_headline': 'KnowledgeGovernanceService',
    '_knowledge_objects': 'KnowledgeStore',
    '_knowledge_record': 'RepositoryRuntime.knowledge_query',
    '_knowledge_ref': 'KnowledgeGovernanceService',
    '_knowledge_similarity': 'KnowledgeGovernanceService',
    '_knowledge_type_counts': 'NotebookSummaryQuery',
    '_llm_for_role': 'ModelProvider',
    '_mark_source_index_backfilled': 'KnowledgeStore',
    '_mark_unified_kg_dirty': 'KgMutationCoordinator',
    '_maybe_enqueue_scale_fold': 'ScaleArtifactRuntime',
    '_mention_extra_edges': 'RetrievalService',
    '_merge_evidence_lists': 'GovernanceStore',
    '_migrate': 'SqliteDatabase',
    '_mix_retrieve': 'RetrievalService',
    '_mmr_select_chunks': 'RetrievalService',
    '_needs_index': 'RepositoryRuntime.ask_component',
    '_note_model_error': 'ModelProvider',
    '_notebook_from_row': 'NotebookSummaryQuery',
    '_notebook_has_kg': 'KnowledgeStore',
    '_notebook_langs': 'RetrievalService',
    '_notebook_langs_cache': 'RepositoryRuntime.notebook_languages',
    '_notebook_name': 'ScaleArtifactRuntime',
    '_notify_index_done': 'ScaleArtifactRuntime',
    '_object_meta': 'KnowledgeLifecycleService',
    '_object_schema_from_row': 'SchemaRegistryService',
    '_open_scale_ann': 'ScaleArtifactRuntime',
    '_parse_answer_anchors': 'RepositoryRuntime.evidence_context_component',
    '_parse_url_via_local': 'SourceIngestionService',
    '_payload_join': 'KnowledgeGovernanceService',
    '_pending_merges_batch': 'KnowledgeGovernanceService',
    '_ppr_fact_rerank': 'RetrievalService',
    '_ppr_graph': 'RetrievalService',
    '_ppr_reset_vector': 'RetrievalService',
    '_ppr_retrieve': 'RetrievalService',
    '_probe_scale_version_signal': 'IndexProjectionStore',
    '_process_idle_queue': 'ScaleArtifactRuntime',
    '_promotion_row_to_dict': 'KnowledgeGovernanceService',
    '_read_ask_trace': 'AskStateStore',
    '_read_manifest_version': 'ScaleArtifactStore',
    '_reasoning_llm_client': 'ModelProvider',
    '_recover_interrupted_jobs': 'SqliteDatabase',
    '_refine_context': 'RepositoryRuntime.ask_component',
    '_relation_ann_candidates': 'RetrievalService',
    '_relations_with_names': 'RetrievalService',
    '_relink_extra_relations': 'SourceIngestionService',
    '_report_row_to_dict': 'ReportStore',
    '_resolve_index_owner': 'ScaleArtifactRuntime',
    '_resolve_path': 'SqliteDatabase',
    '_resolve_scale_mode': 'ScaleArtifactRuntime',
    '_retrieve_chunks': 'RetrievalService',
    '_retrieve_chunks_ann': 'RetrievalService',
    '_retrieve_chunks_fts_degraded': 'RetrievalService',
    '_retrieve_chunks_multi': 'RetrievalService',
    '_retrieve_elements': 'RetrievalService',
    '_retrieve_neighbors': 'RetrievalService',
    '_retrieve_relations_scored': 'RetrievalService',
    '_retrieve_scored': 'RetrievalService',
    '_rewrite_followup_query': 'RepositoryRuntime.ask_component',
    '_rewrite_llm_client': 'ModelProvider',
    '_rrf_scored': 'RetrievalService',
    '_rule_card': 'RepositoryRuntime.knowledge_query',
    '_run_extraction': 'SourceIngestionService',
    '_run_scale_op': 'ScaleArtifactRuntime',
    '_runtime_dim': 'RetrievalService',
    '_save_answer': 'RepositoryRuntime.ask_component',
    '_scale_building': 'ScaleArtifactRuntime',
    '_scale_building_lock': 'ScaleArtifactRuntime',
    '_scale_combined_graph': 'RetrievalService',
    '_scale_idle_queue': 'ScaleArtifactRuntime',
    '_scale_idx_cache': 'ScaleArtifactRuntime',
    '_scale_idx_load_lock': 'ScaleArtifactRuntime',
    '_scale_idx_load_locks': 'ScaleArtifactRuntime',
    '_scale_index': 'ScaleArtifactRuntime',
    '_scale_index_eligible': 'ScaleArtifactRuntime',
    '_scale_index_version': 'ScaleArtifactRuntime',
    '_scale_scheduler_started': 'ScaleArtifactRuntime',
    '_scale_ver_cache': 'ScaleArtifactRuntime',
    '_scale_ver_lock': 'ScaleArtifactRuntime',
    '_scale_ver_locks': 'ScaleArtifactRuntime',
    '_scale_xlayer_bridge_edges': 'RetrievalService',
    '_seed': 'SqliteDatabase',
    '_seed_fn_for': 'GovernanceStore',
    '_semantic_search': 'RepositoryRuntime.knowledge_query',
    '_set_source_status': 'SourceIngestionService',
    '_should_extract_kg': 'SourceIngestionService',
    '_source_from_row': 'SourceStore',
    '_source_has_kg': 'KnowledgeStore',
    '_source_ids_from_evidence': 'KnowledgeStore',
    '_source_index_backfilled': 'KnowledgeStore',
    '_source_raw_text': 'SourceFileStore',
    '_source_type_from_name': 'SourceIngestionService',
    '_sources_from_rows': 'SourceStore',
    '_spawn_viz_build': 'ScaleArtifactRuntime',
    '_stream_seed_reps': 'KnowledgeLifecycleService',
    '_summarize_source': 'SourceIngestionService',
    '_sweep_stuck_copies': 'NotebookSharingService',
    '_system_llm_client': 'ModelProvider',
    '_system_llm_for': 'ModelProvider',
    '_system_rerank_client': 'ModelProvider',
    '_test_insert_object': 'RepositoryRuntime.knowledge_query',
    '_tier2_bridge_candidates_ann': 'KnowledgeLifecycleService',
    '_tier_map_for': 'RepositoryRuntime.evidence_context_component',
    '_truncate_kg_block': 'RepositoryRuntime.evidence_context_component',
    '_unconfigured_model_response': 'RepositoryRuntime.ask_component',
    '_unified_graph_bounded': 'KnowledgeLifecycleService',
    '_unified_graph_full': 'KnowledgeLifecycleService',
    '_union_chunk_candidates': 'RetrievalService',
    '_upsert_knowledge_object_sources': 'KnowledgeStore',
    '_user_llm_cached': 'ModelProvider',
    '_user_llm_clients': 'ModelProvider',
    '_user_profile': 'IdentityStore',
    '_user_rerank_clients': 'ModelProvider',
    '_vector_cache': 'RetrievalSnapshotCache',
    '_vector_matrix': 'RetrievalService',
    '_vector_matrix_version': 'RetrievalService',
    '_vector_matrix_warm': 'RetrievalService',
    '_viz_arrays_from_graph': 'ScaleArtifactRuntime',
    '_viz_building': 'ScaleArtifactRuntime',
    '_viz_building_lock': 'ScaleArtifactRuntime',
    '_viz_dict': 'KnowledgeLifecycleService',
    '_viz_idx_cache': 'ScaleArtifactRuntime',
    '_viz_index': 'ScaleArtifactRuntime',
    '_viz_index_dir': 'ScaleArtifactStore',
    '_viz_index_probe': 'ScaleArtifactRuntime',
    '_viz_node': 'KnowledgeLifecycleService',
    '_write_cluster_map_streamed': 'KnowledgeLifecycleService',
    '_write_lock': 'SqliteDatabase',
    'add_member': 'NotebookSharingService',
    'add_relations': 'KnowledgeStore',
    'add_url_sources': 'SourceIngestionService',
    'append_ask_trace': 'RepositoryRuntime.ask_component',
    'append_clusters': 'KnowledgeLifecycleService',
    'apply_conflict_resolution': 'KnowledgeGovernanceService',
    'approve_promotion': 'KnowledgeGovernanceService',
    'ask': 'RepositoryRuntime.ask_component',
    'ask_chunk': 'RepositoryRuntime.ask_component',
    'ask_graph': 'RepositoryRuntime.ask_component',
    'ask_job_detail': 'AskStateStore',
    'ask_job_status': 'AskStateStore',
    'ask_reasoning': 'RepositoryRuntime.ask_component',
    'authenticate_user': 'IdentityStore',
    'backfill_chunk_fts': 'RepositoryRuntime.knowledge_query',
    'backfill_kg_fts': 'RepositoryRuntime.knowledge_query',
    'begin_ask_job': 'RepositoryRuntime.ask_component',
    'build_notebook_kg': 'KnowledgeLifecycleService',
    'build_scale_index': 'ScaleArtifactRuntime',
    'build_viz_index': 'ScaleArtifactRuntime',
    'bulk_delete_conversations': 'RepositoryRuntime.ask_component',
    'cancel_ask_job': 'RepositoryRuntime.ask_component',
    'cancel_scale_index': 'ScaleArtifactRuntime',
    'cluster_map': 'RetrievalService',
    'concept_detail': 'RepositoryRuntime.knowledge_query',
    'concept_whitelist_add': 'KnowledgeGovernanceService',
    'concept_whitelist_list': 'KnowledgeGovernanceService',
    'concept_whitelist_remove': 'KnowledgeGovernanceService',
    'concept_whitelist_terms': 'KnowledgeGovernanceService',
    'confirm_conflict': 'KnowledgeGovernanceService',
    'confirm_merge': 'KnowledgeGovernanceService',
    'conversation_owner': 'NotebookSharingService',
    'copy_notebook': 'NotebookSharingService',
    'create_notebook': 'NotebookCatalogService',
    'create_object_schema': 'SchemaRegistryService',
    'create_report': 'ReportApplicationService',
    'create_session': 'IdentityStore',
    'create_user': 'IdentityStore',
    'current_user': 'IdentityStore',
    'db_path': 'SqliteDatabase',
    'decided_pairs': 'KnowledgeGovernanceService',
    'decided_seed_pairs': 'KnowledgeGovernanceService',
    'delete_conversation': 'AskStateStore',
    'delete_notebook': 'NotebookCatalogService',
    'delete_notebook_kg': 'KnowledgeLifecycleService',
    'delete_object_schema': 'SchemaRegistryService',
    'delete_report': 'ReportStore',
    'delete_session': 'IdentityStore',
    'delete_source': 'SourceIngestionService',
    'effective_schemas': 'SchemaRegistryService',
    'embedder': 'RepositoryRuntime.embedder',
    'eval_insert_source_for_test': 'SQLiteMaintenanceAdapter',
    'export_reports': 'ReportStore',
    'extract_source': 'SourceIngestionService',
    'federated_retrieve': 'RetrievalService',
    'federated_retrieve_relations': 'RetrievalService',
    'find_duplicates': 'KnowledgeGovernanceService',
    'find_notebook_by_share_token': 'NotebookSharingService',
    'finish_ask_job': 'RepositoryRuntime.ask_component',
    'fold_scale_index_delta': 'ScaleArtifactRuntime',
    'get_community_reports': 'KnowledgeLifecycleService',
    'get_conflict_candidate': 'KnowledgeGovernanceService',
    'get_conversation': 'AskStateStore',
    'get_notebook': 'NotebookCatalogService',
    'get_report': 'ReportStore',
    'get_source': 'SourceStore',
    'get_user_model_settings': 'IdentityStore',
    'import_sources': 'SourceIngestionService',
    'incremental_fuse_source': 'KnowledgeLifecycleService',
    'index_status': 'ScaleArtifactRuntime',
    'is_member': 'NotebookSharingService',
    'join_shared': 'NotebookSharingService',
    'kg_llm_client': 'ModelProvider',
    'kg_neighbors': 'KnowledgeLifecycleService',
    'kg_search': 'RepositoryRuntime.knowledge_query',
    'kick_all_members': 'NotebookSharingService',
    'knowledge_graph': 'RepositoryRuntime.knowledge_query',
    'knowledge_types': 'RepositoryRuntime.knowledge_query',
    'leave_notebook': 'NotebookSharingService',
    'list_communities': 'KnowledgeLifecycleService',
    'list_conversations': 'RepositoryRuntime.ask_component',
    'list_knowledge': 'RepositoryRuntime.knowledge_query',
    'list_members': 'NotebookSharingService',
    'list_notebook_templates': 'NotebookCatalogService',
    'list_notebooks': 'NotebookCatalogService',
    'list_object_schemas': 'SchemaRegistryService',
    'list_promotion_queue': 'KnowledgeGovernanceService',
    'list_reports': 'ReportStore',
    'list_sources': 'SourceIngestionService',
    'list_sources_page': 'SourceIngestionService',
    'list_user_notebooks': 'QueryStore',
    'list_user_usage': 'QueryStore',
    'llm_client': 'ModelProvider',
    'mark_notebook_base': 'NotebookCatalogService',
    'maybe_auto_index': 'ScaleArtifactRuntime',
    'merge_knowledge': 'KnowledgeGovernanceService',
    'merge_review_job_status': 'KnowledgeGovernanceService',
    'node_context': 'KnowledgeQueryService',
    'notebook_analytics': 'NotebookCatalogService',
    'notebook_copy_stats': 'ScaleArtifactRuntime',
    'parse_source': 'SourceIngestionService',
    'pending_actions': 'RepositoryRuntime.pending_actions_service',
    'pending_conflicts': 'KnowledgeGovernanceService',
    'pending_merges': 'KnowledgeGovernanceService',
    'process_source': 'SourceIngestionService',
    'propose_promotion': 'KnowledgeGovernanceService',
    'propose_schemas': 'SchemaRegistryService',
    'reasoning_llm_client': 'ModelProvider',
    'rebuild_canonical_relations': 'KnowledgeLifecycleService',
    'rebuild_communities': 'KnowledgeLifecycleService',
    'rebuild_mention_bridge': 'KnowledgeLifecycleService',
    'rebuild_notebook_kg': 'KnowledgeLifecycleService',
    'rebuild_unified_kg': 'KnowledgeLifecycleService',
    'reject_conflict': 'KnowledgeGovernanceService',
    'reject_merge': 'KnowledgeGovernanceService',
    'reject_promotion': 'KnowledgeGovernanceService',
    'relations_for_notebook': 'RepositoryRuntime.knowledge_query',
    'relink_notebook_kg': 'KnowledgeLifecycleService',
    'remove_member': 'NotebookSharingService',
    'rename_conversation': 'AskStateStore',
    'rerank_client': 'ModelProvider',
    'resolve_model_config': 'IdentityStore',
    'resolve_notebook_conflicts': 'KnowledgeGovernanceService',
    'resolve_session': 'IdentityStore',
    'retrieval': 'RepositoryRuntime.retrieval_component',
    'review_pending_merges': 'KnowledgeGovernanceService',
    'review_queue': 'KnowledgeGovernanceService',
    'rewrite_llm_client': 'ModelProvider',
    'run_merge_review_job': 'KnowledgeGovernanceService',
    'scale_index_status': 'ScaleArtifactRuntime',
    'scale_ppr': 'RetrievalService',
    'search_notebook': 'NotebookCatalogService',
    'set_conflict_status': 'KnowledgeGovernanceService',
    'set_edge_review': 'KnowledgeGovernanceService',
    'set_merge_decision': 'KnowledgeGovernanceService',
    'set_notebook_personal': 'NotebookCatalogService',
    'set_user_model_settings': 'IdentityStore',
    'share_notebook': 'NotebookSharingService',
    'shared_by_me': 'NotebookSharingService',
    'shared_preview': 'NotebookSharingService',
    'source_elements': 'SourceStore',
    'source_owner': 'NotebookSharingService',
    'storage_dir': 'RepositoryRuntime.storage_dir',
    'store_kg': 'KnowledgeLifecycleService',
    'submit_feedback': 'AskStateStore',
    'summarize_communities': 'KnowledgeLifecycleService',
    'trigger_scale_index_rebuild': 'ScaleArtifactRuntime',
    'unified_graph': 'KnowledgeLifecycleService',
    'unified_kg_status': 'KnowledgeLifecycleService',
    'unshare_notebook': 'NotebookSharingService',
    'update_knowledge': 'KnowledgeGovernanceService',
    'update_notebook': 'NotebookCatalogService',
    'update_object_schema': 'SchemaRegistryService',
    'update_report': 'ReportStore',
    'upload_sources': 'SourceIngestionService',
    'user_can_access_notebook': 'NotebookSharingService',
    'user_can_read_answer': 'NotebookSharingService',
    'user_can_read_notebook': 'NotebookSharingService',
    'user_can_read_source': 'NotebookSharingService',
    'write_clusters': 'KnowledgeLifecycleService',
    'write_conflict_candidate': 'KnowledgeGovernanceService',
    'write_merge_candidate': 'KnowledgeGovernanceService',
}
DELEGATE_OWNER_OVERRIDES["_unified_cache"] = "RetrievalSnapshotCache"
DELEGATE_OWNER_OVERRIDES["_CJK_RE"] = "CandidateRetrievalService"
DELEGATE_OWNER_OVERRIDES["_LATIN_RE"] = "CandidateRetrievalService"
DELEGATE_OWNER_OVERRIDES["_REVIEW_STATUSES"] = "KnowledgeGovernanceService"


def validate_ownership_manifest(
    members: tuple[SurfaceMember, ...] = SURFACE_MEMBERS,
    delegates: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return canonical owners and optionally validate derived delegate evidence."""
    owners = _unique_nonempty_owners(members)
    if delegates is not None:
        mismatches = {
            name: (owners[name], owner)
            for name, owner in delegates.items()
            if name in owners and owners[name] != owner
        }
        if mismatches:
            raise ValueError(f"ownership/delegate mismatch: {mismatches}")
    return owners


OWNER_BY_MEMBER = validate_ownership_manifest()


@dataclass(frozen=True)
class LateBoundSeam:
    """An intentionally late-bound production compatibility seam (Task 26).

    ``scope`` is ``"module"`` for the ``sqlite_repository`` module-namespace
    seams the runtime resolves through ``RepositoryCompatibilitySeams``, or
    ``"facade"`` for the per-call facade seats the runtime wiring lambdas
    resolve.  ``resolved_through`` names the wiring-visible member that proves
    the late binding — the member itself, or the facade helper that reads it
    per call (``_IN_CHUNK`` is read by the wired ``_in_batches`` seat).

    Tests may keep monkeypatching exactly these members on the facade/module
    (the patches stay authoritative for production flows because every
    consumer resolves them at call time).  Every other private facade member
    is component-owned: probes belong on the canonical store/service/runtime
    seam, never on a facade wrapper kept only for compatibility.
    """

    name: str
    scope: str
    resolved_through: str


LATE_BOUND_COMPATIBILITY_SEAMS: Mapping[str, LateBoundSeam] = {
    seam.name: seam
    for seam in (
        # RepositoryCompatibilitySeams module seams (id/clock/copy-chunk/remap).
        LateBoundSeam("_now", "module", "_now"),
        LateBoundSeam("_new_id", "module", "_new_id"),
        LateBoundSeam("_COPY_CHUNK", "module", "_COPY_CHUNK"),
        LateBoundSeam("_remap_json_ids", "module", "_remap_json_ids"),
        # Facade seats the runtime wiring resolves per call.
        LateBoundSeam("_connect", "facade", "_connect"),
        LateBoundSeam("_write", "facade", "_write"),
        LateBoundSeam("_insert_row", "facade", "_insert_row"),
        LateBoundSeam("_mark_unified_kg_dirty", "facade", "_mark_unified_kg_dirty"),
        LateBoundSeam("_invalidate_unified_cache", "facade", "_invalidate_unified_cache"),
        LateBoundSeam("_source_ids_from_evidence", "facade", "_source_ids_from_evidence"),
        LateBoundSeam("_summarize_source", "facade", "_summarize_source"),
        LateBoundSeam("_run_extraction", "facade", "_run_extraction"),
        LateBoundSeam("_IN_CHUNK", "facade", "_IN_CHUNK"),
    )
}
