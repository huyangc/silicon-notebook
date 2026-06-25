from app.core.config import Settings
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)
from app.models.schemas import UserProfile


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_current_user_falls_back_to_admin_when_unset(tmp_path):
    repo = _repo(tmp_path)
    assert repo.current_user().id == "user-local"  # 未设 ContextVar → 回退 admin


def test_current_user_reads_contextvar(tmp_path):
    repo = _repo(tmp_path)
    fake = UserProfile(id="u-zhang", email="z@x", display_name="z", role="user", username="zhang00123456")
    tok = set_request_user(fake)
    try:
        assert repo.current_user().id == "u-zhang"
        assert repo.current_user().username == "zhang00123456"
    finally:
        reset_request_user(tok)
    assert repo.current_user().id == "user-local"  # 复位后回退
