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
    assert owner_dir("a00123456") == "a00123456"
    assert owner_dir("../etc/passwd") == "_system"
    assert owner_dir("Robert'); DROP") == "_system"


def test_per_user_writes_to_owner_subdir(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("a00123456")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "a00123456" / "events.jsonl").exists()
    assert not (tmp_path / "events.jsonl").exists()
    assert _read(tmp_path / "a00123456" / "events.jsonl")[0]["kind"] == "k"


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
    tok = set_log_owner("a00123456")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "events.jsonl").exists()        # 全局,忽略 owner
    assert not (tmp_path / "a00123456").exists()
