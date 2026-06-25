from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.auth_utils import verify_password


def _repo(tmp_path, password="admin"):
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/t.db",
        SILICON_NOTEBOOK_ADMIN_PASSWORD=password,
    )
    return SQLiteRepository(s)


def test_seed_upgrades_local_user_to_admin(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        row = db.execute("SELECT * FROM users WHERE id='user-local'").fetchone()
    assert row["role"] == "admin"
    assert row["username"] == "admin"
    assert verify_password(
        "admin", row["password_hash"], row["password_salt"], row["password_iterations"])


def test_seed_admin_password_from_settings(tmp_path):
    repo = _repo(tmp_path, password="s3cret")
    with repo._connect() as db:
        row = db.execute("SELECT * FROM users WHERE id='user-local'").fetchone()
    assert verify_password("s3cret", row["password_hash"], row["password_salt"], row["password_iterations"])


def test_admin_id_stays_user_local(tmp_path):
    """关键不变量：admin 的 id 不变，现有 created_by='user-local' 数据零迁移。"""
    repo = _repo(tmp_path)
    assert repo.current_user().id == "user-local"
    assert repo.current_user().role == "admin"
