from app.services import sqlite_repository
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
    "list_user_usage",
    "list_user_notebooks",
    "create_session",
    "resolve_session",
    "delete_session",
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


def test_sqlite_identity_domain_is_inherited_not_reimplemented():
    assert issubclass(SQLiteRepository, SQLiteIdentityMixin)
    for method_name in IDENTITY_METHODS:
        assert method_name not in SQLiteRepository.__dict__
        assert getattr(SQLiteRepository, method_name) is getattr(SQLiteIdentityMixin, method_name)


def test_request_identity_exports_remain_backwards_compatible():
    from app.services import sqlite_identity

    assert sqlite_repository._REQUEST_USER is sqlite_identity._REQUEST_USER
    assert sqlite_repository.set_request_user is sqlite_identity.set_request_user
    assert sqlite_repository.reset_request_user is sqlite_identity.reset_request_user


def test_sqlite_sharing_domain_is_inherited_not_reimplemented():
    assert issubclass(SQLiteRepository, SQLiteNotebookSharingMixin)
    for method_name in SHARING_METHODS:
        assert method_name not in SQLiteRepository.__dict__
        assert getattr(SQLiteRepository, method_name) is getattr(
            SQLiteNotebookSharingMixin, method_name
        )


def test_sharing_helpers_remain_backwards_compatible():
    from app.services import sqlite_notebook_sharing

    assert sqlite_repository._remap_json_ids is sqlite_notebook_sharing._remap_json_ids
    assert sqlite_repository._COPY_CHUNK == 1000
