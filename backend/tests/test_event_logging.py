import json

from app.core.config import Settings
from app.core.event_logging import (
    EventLogger, set_log_owner, reset_log_owner, get_log_owner, owner_dir,
)


def _read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_owner_dir_mapping():
    assert owner_dir(None) == "user-local"
    assert owner_dir("") == "user-local"
    assert owner_dir("user-local") == "user-local"
    assert owner_dir("user-3a8f9c2b1d") == "user-3a8f9c2b1d"
    assert owner_dir("a00123456") == "_system"   # username 不是合法 owner 键（owner=user.id）
    assert owner_dir("../etc/passwd") == "_system"
    assert owner_dir("Robert'); DROP") == "_system"


def test_per_user_writes_to_owner_subdir(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("user-3a8f9c2b1d")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "user-3a8f9c2b1d" / "events.jsonl").exists()
    assert not (tmp_path / "events.jsonl").exists()
    assert _read(tmp_path / "user-3a8f9c2b1d" / "events.jsonl")[0]["kind"] == "k"


def test_per_user_no_owner_falls_back_to_user_local(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    log.emit({"kind": "k", "status": "ok"})  # ContextVar 未设
    assert (tmp_path / "user-local" / "events.jsonl").exists()


def test_per_user_illegal_owner_falls_back_to_system(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("../escape")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "_system" / "events.jsonl").exists()


def test_non_per_user_writes_global(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events")  # per_user=False
    tok = set_log_owner("user-3a8f9c2b1d")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "events.jsonl").exists()        # 全局,忽略 owner
    assert not (tmp_path / "user-3a8f9c2b1d").exists()


def test_set_request_user_syncs_log_owner(tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import (
        SQLiteRepository, set_request_user, reset_request_user,
    )
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))
    user = repo.create_user("a00123456", "pw")
    assert get_log_owner() is None
    tok = set_request_user(user)
    try:
        assert get_log_owner() == user.id          # owner = user.id（UUID user-<hex>）
        assert user.id.startswith("user-")          # 防回归：键是 id 不是 username
    finally:
        reset_request_user(tok)
    assert get_log_owner() is None


def test_llm_logger_per_user(tmp_path):
    from app.core.config import Settings
    from app.core.llm_logging import LLMInteractionLogger
    s = Settings(
        llm_log_path=str(tmp_path / "logs" / "llm.jsonl"),
        llm_log_enabled=True,
    )
    logger = LLMInteractionLogger(s)
    tok = set_log_owner("user-3a8f9c2b1d")
    try:
        logger.log({"kind": "chat", "model": "m", "status": "ok", "latency_ms": 1})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "logs" / "user-3a8f9c2b1d" / "llm.jsonl").exists()
    assert not (tmp_path / "logs" / "llm.jsonl").exists()


def test_repo_event_log_is_per_user(tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import (
        SQLiteRepository, set_request_user, reset_request_user,
    )
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/t.db",
        event_log_dir=str(tmp_path / "logs"),
    ))
    user = repo.create_user("a00123456", "pw")
    tok = set_request_user(user)
    try:
        repo.event_log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_request_user(tok)
    assert (tmp_path / "logs" / user.id / "events.jsonl").exists()
