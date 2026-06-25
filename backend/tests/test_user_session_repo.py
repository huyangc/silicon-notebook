import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_create_user_and_authenticate(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("z00123456", "pw")
    assert user.id != "user-local"
    assert user.username == "z00123456"
    assert user.role == "user"
    assert repo.authenticate_user("z00123456", "pw").id == user.id
    assert repo.authenticate_user("Z00123456", "pw").id == user.id  # 登录大小写不敏感
    assert repo.authenticate_user("z00123456", "wrong") is None
    assert repo.authenticate_user("n00111111", "pw") is None


def test_duplicate_username_rejected(tmp_path):
    repo = _repo(tmp_path)
    repo.create_user("z00123456", "pw")
    with pytest.raises(ValueError):
        repo.create_user("z00123456", "pw2")


def test_session_lifecycle(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("z00123456", "pw")
    token = repo.create_session(user.id)
    assert token
    assert repo.resolve_session(token).id == user.id
    repo.delete_session(token)
    assert repo.resolve_session(token) is None
    assert repo.resolve_session("bogus") is None


def test_create_user_makes_profile(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("z00123456", "pw")
    with repo._connect() as db:
        prof = db.execute("SELECT * FROM user_profiles WHERE user_id=?", (user.id,)).fetchone()
    assert prof is not None
