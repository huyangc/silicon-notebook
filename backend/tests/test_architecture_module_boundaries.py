from app.services import sqlite_repository
from app.services.sqlite_identity import SQLiteIdentityMixin
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
