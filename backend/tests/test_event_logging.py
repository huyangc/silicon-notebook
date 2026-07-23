import json
from datetime import datetime

from app.core.config import Settings
from app.core.event_logging import (
    EventLogger, set_log_owner, reset_log_owner, get_log_owner, owner_dir,
)

_TODAY = datetime.now().strftime("%Y-%m-%d")


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
    assert (tmp_path / "user-3a8f9c2b1d" / f"events-{_TODAY}.jsonl").exists()
    assert not (tmp_path / "events.jsonl").exists()
    assert _read(tmp_path / "user-3a8f9c2b1d" / f"events-{_TODAY}.jsonl")[0]["kind"] == "k"


def test_per_user_no_owner_falls_back_to_user_local(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    log.emit({"kind": "k", "status": "ok"})  # ContextVar 未设
    assert (tmp_path / "user-local" / f"events-{_TODAY}.jsonl").exists()


def test_per_user_illegal_owner_falls_back_to_system(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("../escape")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "_system" / f"events-{_TODAY}.jsonl").exists()


def test_non_per_user_writes_global(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events")  # per_user=False
    tok = set_log_owner("user-3a8f9c2b1d")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / f"events-{_TODAY}.jsonl").exists()        # 全局,忽略 owner
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
    assert (tmp_path / "logs" / "user-3a8f9c2b1d" / f"llm-{_TODAY}.jsonl").exists()
    assert not (tmp_path / "logs" / "llm.jsonl").exists()


def test_llm_interaction_support_scope_adds_the_opaque_support_id(tmp_path):
    from app.core.llm_logging import LLMInteractionLogger, interaction_support_scope

    logger = LLMInteractionLogger(Settings(
        llm_log_path=str(tmp_path / "logs" / "llm.jsonl"),
        llm_log_enabled=True,
    ))
    with interaction_support_scope("mdl-safe-correlation"):
        logger.log({"kind": "chat", "model": "m", "status": "ok", "latency_ms": 1})

    path = tmp_path / "logs" / "user-local" / f"llm-{_TODAY}.jsonl"
    assert _read(path)[0]["support_id"] == "mdl-safe-correlation"


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
    assert (tmp_path / "logs" / user.id / f"events-{_TODAY}.jsonl").exists()


def test_expand_channel_paths(tmp_path):
    from app.services.log_reader import expand_channel_paths
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text("legacy\n", encoding="utf-8")           # 旧全局
    (logs / "user-3a8f9c2b1d").mkdir()
    (logs / "user-3a8f9c2b1d" / "events.jsonl").write_text("u1\n", encoding="utf-8")
    (logs / "user-local").mkdir()
    (logs / "user-local" / "events.jsonl").write_text("u2\n", encoding="utf-8")
    (logs / "user-3a8f9c2b1d" / "llm.jsonl").write_text("x\n", encoding="utf-8")  # 不应混入

    paths = expand_channel_paths(logs / "events.jsonl")
    rels = [str(p.relative_to(logs)) for p in paths]
    assert rels == ["events.jsonl", "user-3a8f9c2b1d/events.jsonl", "user-local/events.jsonl"]


def test_expand_channel_paths_reads_dated_file(tmp_path):
    """EventLogger.emit 实际写入的是按天文件(`events-YYYY-MM-DD.jsonl`,见
    _target_path_for_day),expand_channel_paths 必须发现它,不能只认无日期名。"""
    from app.services.log_reader import expand_channel_paths
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events-2026-07-20.jsonl").write_text("d1\n", encoding="utf-8")

    paths = expand_channel_paths(logs / "events.jsonl")
    assert logs / "events-2026-07-20.jsonl" in paths


def test_expand_channel_paths_reads_gzipped_dated_file(tmp_path):
    """归档器(_gzip_day_file)压缩后的按天文件(`.jsonl.gz`)也必须被发现。"""
    import gzip
    from app.services.log_reader import expand_channel_paths
    logs = tmp_path / "logs"
    logs.mkdir()
    with gzip.open(logs / "events-2026-07-19.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write("d0\n")

    paths = expand_channel_paths(logs / "events.jsonl")
    assert logs / "events-2026-07-19.jsonl.gz" in paths


def test_expand_channel_paths_reads_dated_file_in_per_user_subdir(tmp_path):
    """per-user 子目录里的按天文件同样要并入(EventLogger(per_user=True) 写的
    正是这个形状)。"""
    from app.services.log_reader import expand_channel_paths
    logs = tmp_path / "logs"
    logs.mkdir()
    sub = logs / "user-local"
    sub.mkdir()
    (sub / "events-2026-07-20.jsonl").write_text("u1\n", encoding="utf-8")

    paths = expand_channel_paths(logs / "events.jsonl")
    assert sub / "events-2026-07-20.jsonl" in paths


def test_expand_channel_paths_rejects_lookalike_stem(tmp_path):
    """`events-foo-2026-07-21.jsonl` 的 stem 是 `events-foo`,不是 `events`——
    channel `events` 的按天发现不能把它收进来。此前 _dated_paths_in_dir 只用
    `search()` 在完整文件名里找日期子串(不锚定、不核对 stem),这种"channel 前缀
    恰好也是另一个真 channel 前缀"的文件会被误吞进错误的 channel(scripts/diag.py
    一直是严格全等匹配,这条测试让 log_reader 与之保持一致)。"""
    from app.services.log_reader import expand_channel_paths, available_days
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events-2026-07-20.jsonl").write_text("real\n", encoding="utf-8")
    (logs / "events-foo-2026-07-21.jsonl").write_text("lookalike\n", encoding="utf-8")

    paths = expand_channel_paths(logs / "events.jsonl")
    assert logs / "events-2026-07-20.jsonl" in paths
    assert logs / "events-foo-2026-07-21.jsonl" not in paths

    days = available_days(logs, "events")
    assert "2026-07-20" in days
    assert "2026-07-21" not in days


def test_llm_log_dir_aligned():
    from app.core.event_logging import llm_log_dir_aligned, _ROOT_DIR
    # 默认：llm_log_path 的父目录与 event_log_dir 都相对、锚定到同一处 → 对齐
    assert llm_log_dir_aligned(".local/logs/llm.jsonl", ".local/logs") is True
    # 自定义 LLM_LOG_PATH 到别处而 event_log_dir 仍默认 → 不对齐
    assert llm_log_dir_aligned("/custom/dir/llm.jsonl", ".local/logs") is False
    # 都绝对、同目录 → 对齐
    assert llm_log_dir_aligned("/x/y/llm.jsonl", "/x/y") is True
    # 相对 vs 绝对但指向同一处：必须锚定后比较（直接字符串比较会误判为不对齐）
    assert llm_log_dir_aligned(str(_ROOT_DIR / ".local/logs/llm.jsonl"), ".local/logs") is True


def test_repo_warns_on_log_dir_mismatch(tmp_path, caplog):
    import logging
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    with caplog.at_level(logging.WARNING):
        SQLiteRepository(Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            event_log_dir=str(tmp_path / "logs"),
            llm_log_path=str(tmp_path / "other" / "llm.jsonl"),  # 不同目录
        ))
    assert any("EVENT_LOG_DIR" in r.getMessage() for r in caplog.records)


def test_repo_no_warn_when_log_dirs_aligned(tmp_path, caplog):
    import logging
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    with caplog.at_level(logging.WARNING):
        SQLiteRepository(Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            event_log_dir=str(tmp_path / "logs"),
            llm_log_path=str(tmp_path / "logs" / "llm.jsonl"),  # 对齐
        ))
    assert not any("EVENT_LOG_DIR" in r.getMessage() for r in caplog.records)


def test_ingestion_service_shares_the_per_user_event_logger(tmp_path):
    """Task 12: the ingestion orchestration moved into SourceIngestionService
    must keep logging through the repository's per-user EventLogger — the
    same object, not a second channel."""
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/t.db",
        event_log_dir=str(tmp_path / "logs"),
    ))
    assert repo._runtime.source_ingestion.event_log is repo.event_log


def test_auto_console_translates_source_status_to_plain_chinese():
    """源处理状态事件的 console 摘要(给人看的日志行)用中文阶段名,不暴露 parsing/
    parsed/extracting 等内部状态值;jsonl 里写入的原始 status 值不受影响。"""
    cases = [("queued", "排队中"), ("parsing", "解析文档中"), ("parsed", "解析完成"),
             ("extracting", "抽取知识图谱中"), ("extracted", "处理完成"), ("failed", "失败")]
    for status, expect in cases:
        line = EventLogger._auto_console({"kind": "status", "status": status, "source_id": "s1"})
        assert expect in line, (status, line)
        assert status not in line          # 英文状态值不出现在给人看的摘要
    # kind="status" 这个技术词本身也不该进摘要(翻译后的中文已自解释)
    assert "status" not in EventLogger._auto_console({"kind": "status", "status": "parsing"})


def test_auto_console_keeps_non_source_status_verbatim():
    """非源状态事件(如 pipeline stage)的 console 摘要保持原样,只翻源状态词。"""
    line = EventLogger._auto_console({"kind": "pipeline", "stage": "parse", "status": "start",
                                      "latency_ms": 12})
    assert "pipeline" in line and "parse" in line and "start" in line
