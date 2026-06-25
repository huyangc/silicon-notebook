import sqlite3
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    return SQLiteRepository(s)


def test_users_table_has_auth_columns(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    assert {"username", "password_hash", "password_salt", "password_iterations"} <= cols


def test_auth_sessions_table_exists(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(auth_sessions)").fetchall()}
    assert {"token", "user_id", "created_at", "expires_at", "last_seen_at"} <= cols


def test_username_unique_index(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        idx = {r["name"] for r in db.execute("PRAGMA index_list(users)").fetchall()}
    assert "idx_users_username" in idx
