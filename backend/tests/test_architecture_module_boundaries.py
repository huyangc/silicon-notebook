from app.services import sqlite_repository
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.query_store import QueryStore
from app.services.sqlite_identity import SQLiteIdentityMixin
from app.services.sqlite_notebook_sharing import SQLiteNotebookSharingMixin
from app.services.sqlite_repository import SQLiteRepository


IDENTITY_METHODS = (
    "current_user",
    "get_user_model_settings",
    "set_user_model_settings",
    "resolve_model_config",
    "create_user",
    "authenticate_user",
    "create_session",
    "resolve_session",
    "delete_session",
)

QUERY_METHODS = (
    "list_user_usage",
    "list_user_notebooks",
    "notebook_analytics",
    "search_notebook",
    "load_notebook_scale_facts",
)

SHARING_METHODS = (
    "share_notebook",
    "unshare_notebook",
    "find_notebook_by_share_token",
    "notebook_copy_stats",
    "shared_preview",
    "shared_by_me",
    "_insert_row",
    "_sweep_stuck_copies",
    "copy_notebook",
    "user_can_access_notebook",
    "is_member",
    "user_can_read_notebook",
    "user_can_read_source",
    "add_member",
    "remove_member",
    "kick_all_members",
    "list_members",
    "join_shared",
    "leave_notebook",
    "source_owner",
    "conversation_owner",
    "answer_owner",
    "user_can_read_answer",
)


def test_sqlite_identity_domain_is_composed_with_explicit_delegates():
    assert not issubclass(SQLiteRepository, SQLiteIdentityMixin)
    for method_name in IDENTITY_METHODS:
        assert method_name in IdentityStore.__dict__
        assert method_name in SQLiteRepository.__dict__


def test_sqlite_query_domain_is_composed_with_explicit_delegates():
    for method_name in QUERY_METHODS:
        assert method_name in QueryStore.__dict__
        assert method_name in SQLiteRepository.__dict__


def test_request_identity_exports_remain_backwards_compatible():
    from app.services import sqlite_identity

    assert sqlite_repository._REQUEST_USER is sqlite_identity._REQUEST_USER
    assert sqlite_repository.set_request_user is sqlite_identity.set_request_user
    assert sqlite_repository.reset_request_user is sqlite_identity.reset_request_user


def test_sqlite_sharing_domain_is_composed_with_explicit_delegates():
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.services.notebook_sharing import (
        NotebookCopyService,
        NotebookSharingService,
    )

    # Task 9: the facade no longer inherits the sharing mixin — every frozen
    # member is an explicit facade delegate onto the composed service layer.
    assert not issubclass(SQLiteRepository, SQLiteNotebookSharingMixin)
    for method_name in SHARING_METHODS:
        assert method_name in SQLiteRepository.__dict__, method_name

    service_methods = (
        "share_notebook", "unshare_notebook", "find_notebook_by_share_token",
        "notebook_copy_stats", "shared_preview", "shared_by_me",
        "copy_notebook", "sweep_stuck_copies",
        "user_can_access_notebook", "is_member", "user_can_read_notebook",
        "user_can_read_source", "user_can_read_answer",
        "add_member", "remove_member", "kick_all_members", "list_members",
        "join_shared", "leave_notebook",
        "source_owner", "conversation_owner", "answer_owner",
    )
    for method_name in service_methods:
        assert method_name in NotebookSharingService.__dict__, method_name

    for method_name in ("copy_notebook", "sweep_stuck_copies"):
        assert method_name in NotebookCopyService.__dict__, method_name

    store_methods = (
        "set_share_token", "clear_share", "find_by_token",
        "list_shared_by_owner", "user_can_access_notebook", "is_member",
        "add_member", "remove_member", "kick_all_members", "list_members",
        "source_owner", "conversation_owner", "answer_owner",
        "snapshot_copy_rows", "insert_copy_rows", "compensate_copy",
        "sweep_stale_copies",
    )
    for method_name in store_methods:
        assert method_name in SharingStore.__dict__, method_name


def test_sharing_helpers_remain_backwards_compatible():
    from app.services import sqlite_notebook_sharing

    assert sqlite_repository._remap_json_ids is sqlite_notebook_sharing._remap_json_ids
    assert sqlite_repository._COPY_CHUNK == 1000


def test_remaining_sql_bodies_are_composed_with_explicit_delegates():
    """Task 26: the last SQL bodies leave the facade for their canonical
    connection-taking store/service primitives; the facade keeps
    frozen-signature delegates (and every `_connect`/`_write` boundary)."""
    from app.repositories.sqlite.chunk_store import ChunkStore
    from app.repositories.sqlite.embedding_store import EmbeddingStore
    from app.repositories.sqlite.knowledge_store import KnowledgeStore
    from app.repositories.sqlite.notebook_store import NotebookStore
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.repositories.sqlite.source_store import SourceStore
    from app.services.source_embedding import SourceEmbeddingService

    component_methods = {
        KnowledgeStore: (
            "source_has_kg", "any_base_has_kg_on", "retrieval_objects",
            "edge_centrality_source_rows", "concept_cluster_detail_rows",
            "concept_neighbor_rows", "insert_test_object",
        ),
        ChunkStore: ("backfill_fts",),
        NotebookStore: ("meta_row", "apply_meta", "tier_on", "participant_ids"),
        SourceStore: ("meta_source_rows",),
        EmbeddingStore: ("embedded_object_ids", "embedded_relation_ids"),
        SharingStore: ("insert_row_values",),
        SourceEmbeddingService: ("backfill_knowledge_embeddings",),
    }
    for component, method_names in component_methods.items():
        for method_name in method_names:
            assert method_name in component.__dict__, (component.__name__, method_name)

    facade_delegates = (
        "_source_has_kg", "_any_base_notebook_has_kg", "_knowledge_objects",
        "_insert_row", "backfill_chunk_fts", "_notebook_meta_row",
        "_notebook_meta_sources", "_apply_notebook_meta", "_notebook_tier",
        "_backfill_knowledge_embeddings", "_backfill_relation_embeddings",
        "_edge_centrality_map", "concept_detail", "_test_insert_object",
        "_participant_notebook_ids",
    )
    for method_name in facade_delegates:
        assert method_name in SQLiteRepository.__dict__, method_name
