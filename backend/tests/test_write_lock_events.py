import json

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def test_slow_write_emits_a_db_write_lock_slow_event(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # 0 而非brief原稿的 1:实测本机开新连接(WAL+多条PRAGMA)+CREATE TABLE的
    # hold_ms 常在 0.65~0.95ms 徘徊,阈值=1ms在快机器上是掷硬币式偶发失败
    # (同一命令连跑5次4败1过,非本任务wiring引入,该test自己手动挂sink不吃
    # Step4接线)。wait_ms/hold_ms 恒为 perf_counter() 差值故 >=0 永真,阈值
    # 设0才是把注释里「任何写都算慢」落到确定性上,而非概率性。
    monkeypatch.setenv("DB_WRITE_LOCK_WARN_MS", "0")
    repo = SQLiteRepository(Settings())

    seen = []
    repo._runtime.database.stats.sink = seen.append
    with repo._runtime.database.write() as conn:
        conn.execute("CREATE TABLE probe (a INTEGER)")

    kinds = [e["kind"] for e in seen]
    assert "db_write_lock_slow" in kinds, seen
    slow = next(e for e in seen if e["kind"] == "db_write_lock_slow")
    assert "site" in slow and "wait_ms" in slow and "hold_ms" in slow
    # 脱敏:事件里不得出现 SQL、参数值或文件绝对路径
    blob = json.dumps(slow)
    assert "CREATE TABLE" not in blob
    assert str(tmp_path) not in blob


def test_runtime_wires_the_event_log_as_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    assert repo._runtime.database.stats is not None
    assert repo._runtime.database.stats.sink is not None
