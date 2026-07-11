def test_request_identity_exports_are_the_same_objects():
    from app.core import request_context
    from app.services import sqlite_identity, sqlite_repository

    assert sqlite_identity._REQUEST_USER is request_context._REQUEST_USER
    assert sqlite_repository._REQUEST_USER is request_context._REQUEST_USER
    assert sqlite_repository.set_request_user is request_context.set_request_user
    assert sqlite_repository.reset_request_user is request_context.reset_request_user


def test_request_user_id_round_trip():
    from app.core.request_context import get_request_user, request_user_id, reset_request_user, set_request_user
    from app.models.schemas import UserProfile

    user = UserProfile(id="u-test", email="u@test", display_name="U", role="user", username="u00123456")
    token = set_request_user(user)
    try:
        assert get_request_user() is user
        assert request_user_id() == "u-test"
    finally:
        reset_request_user(token)
