import sqlite3
import threading
import concurrent.futures as cf
import inspect
import json
import time

import pytest

from app.core.config import Settings
from app.core import diagnostics_runtime as diagnostics
from app.repositories.sqlite.database import SqliteDatabase


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'reuse.db'}")
    return SqliteDatabase(Settings(), tmp_path)


def _rows_from_other_thread(db, sql):
    """从另一条连接(另一线程)读,验证对其它连接的可见性(WAL 快照)。"""
    out = {}
    def run():
        out["rows"] = db.connect().execute(sql).fetchall()
    t = threading.Thread(target=run)
    t.start()
    t.join()
    return out["rows"]


def test_reuse_same_thread_returns_same_connection(db):  # INV-1
    c1 = db.connect()
    c2 = db.connect()
    assert c1 is c2


def test_new_connection_called_once_per_thread(db, monkeypatch):  # INV-1
    calls = []
    orig = db._new_connection
    monkeypatch.setattr(db, "_new_connection", lambda: (calls.append(1), orig())[1])
    db.connect()
    db.connect()
    with db.connect() as c:
        c.execute("SELECT 1")
    assert len(calls) == 1


def test_isolation_across_threads(db):  # INV-2
    grabbed = {}
    def grab(name):
        grabbed[name] = db.connect()
    for name in ("a", "b"):
        t = threading.Thread(target=grab, args=(name,))
        t.start()
        t.join()
    main = db.connect()
    assert grabbed["a"] is not grabbed["b"]
    assert grabbed["a"] is not main


def test_with_commits_on_success(db):  # INV-4
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
    rows = _rows_from_other_thread(db, "SELECT x FROM t")
    assert [r["x"] for r in rows] == [1]


def test_with_rolls_back_on_exception(db):  # INV-4
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute("INSERT INTO t VALUES (99)")
            raise RuntimeError("boom")
    rows = _rows_from_other_thread(db, "SELECT x FROM t")
    assert rows == []


def test_nested_inner_does_not_commit_early(db):  # INV-5
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with db.connect() as outer:
        outer.execute("INSERT INTO t VALUES (1)")
        with db.connect() as inner:
            assert inner is outer
            inner.execute("INSERT INTO t VALUES (2)")
        # 内层退出但外层未退出 → 另一连接尚看不到(未提交)
        assert _rows_from_other_thread(db, "SELECT x FROM t") == []
    # 外层退出后才提交
    rows = _rows_from_other_thread(db, "SELECT x FROM t ORDER BY x")
    assert [r["x"] for r in rows] == [1, 2]


def test_nested_inner_failure_rolls_back_all(db):  # INV-5
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with pytest.raises(RuntimeError):
        with db.connect() as outer:
            outer.execute("INSERT INTO t VALUES (1)")
            with db.connect():
                outer.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("boom")
    assert _rows_from_other_thread(db, "SELECT x FROM t") == []


def test_close_local_releases_and_rebuilds(db):  # INV-6
    c1 = db.connect()
    db.close_local()
    with pytest.raises(sqlite3.ProgrammingError):
        c1.execute("SELECT 1")           # 原连接已关
    c2 = db.connect()
    assert c2 is not c1
    c2.execute("SELECT 1")               # 新连接可用
    db.close_local()
    db.close_local()                     # 幂等,不抛


def test_connection_count_bounded_under_concurrency(db, monkeypatch):  # INV-3
    lock = threading.Lock()
    calls = []
    orig = db._new_connection
    def counting():
        with lock:
            calls.append(1)
        return orig()
    monkeypatch.setattr(db, "_new_connection", counting)
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    K = 4
    def op(i):
        for _ in range(20):
            with db.connect() as c:
                c.execute("INSERT INTO t VALUES (?)", (i,))
    with cf.ThreadPoolExecutor(max_workers=K) as ex:
        list(ex.map(op, range(K * 5)))
    # 连接数 = 用到的线程数(≤ K 个 worker + 1 主线程),不随 400 次操作增长
    assert len(calls) <= K + 1


def test_write_lock_serialization_preserved(db):  # 回归 write() 语义
    with db.write() as d:
        d.execute("CREATE TABLE t(x INTEGER)")
    def worker(i):
        with db.write() as d:
            d.execute("INSERT INTO t VALUES (?)", (i,))
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(200)))
    rows = _rows_from_other_thread(db, "SELECT COUNT(*) AS n FROM t")
    assert rows[0]["n"] == 200


def test_write_uses_independent_connection(db):  # INV-8
    with db.connect() as c:
        c.execute("CREATE TABLE t(x INTEGER)")
    with db.connect() as outer:                 # outer read connection (reused)
        with db.write() as w:                   # inner write: independent conn
            assert w is not outer
            w.execute("INSERT INTO t VALUES (1)")
        # inner write already committed independently → visible from a 3rd
        # connection while the outer read `with` is still open
        assert [r["x"] for r in _rows_from_other_thread(db, "SELECT x FROM t")] == [1]


def test_facade_close_local(tmp_path, monkeypatch):  # facade delegate
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facade.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    c1 = repo._connect()
    repo.close_local()
    c2 = repo._connect()
    assert c2 is not c1


def test_no_bare_close_on_reused_conn_in_knowledge_lifecycle():  # INV-7
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "app" / "services" / "knowledge_lifecycle.py").read_text(encoding="utf-8")
    assert "scan_db.close()" not in src, "复用连接不得裸 close;用 self._close_local()"


def test_fetchall_remains_observable_after_execute_returns(db):
    with db.write() as conn:
        conn.execute("CREATE TABLE fetch_probe (value TEXT)")
        conn.executemany(
            "INSERT INTO fetch_probe VALUES (?)",
            (("first",), ("second",)),
        )

    runtime = diagnostics.DiagnosticsRuntime(
        db.root_dir / "diagnostics",
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        enable_signal=False,
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    execute_finished = threading.Event()
    failures = []

    def run_query():
        calls = 0
        try:
            conn = db.connect()

            def diag_fetch(value):
                nonlocal calls
                calls += 1
                if calls == 2:
                    fetch_started.set()
                    release_fetch.wait(timeout=2)
                return value

            conn.create_function("diag_fetch", 1, diag_fetch)
            cursor = conn.execute("SELECT diag_fetch(value) FROM fetch_probe")
            execute_finished.set()
            cursor.fetchall()
        except BaseException as exc:  # preserve worker failures for the assertion thread
            failures.append(exc)
        finally:
            db.close_local()

    with diagnostics.install_runtime(runtime):
        worker = threading.Thread(target=run_query)
        worker.start()
        assert execute_finished.wait(timeout=2)
        assert fetch_started.wait(timeout=2)
        try:
            active = runtime.snapshot()["active_sql"]
            assert len(active) == 1
            assert active[0]["verb"] == "SELECT"
            assert active[0]["table"] == "fetch_probe"
            encoded = json.dumps(active)
            assert "diag_fetch" not in encoded
            assert "first" not in encoded
            assert "second" not in encoded
        finally:
            release_fetch.set()
            worker.join(timeout=2)

    assert not worker.is_alive()
    assert failures == []
    assert runtime.snapshot()["active_sql"] == []


def test_diagnostic_cursor_preserves_native_argument_contract(db):
    cursor = db.connect().cursor()
    for name in ("execute", "executemany", "executescript"):
        native = inspect.signature(getattr(sqlite3.Cursor, name))
        wrapped = inspect.signature(getattr(type(cursor), name))
        assert tuple(item.kind for item in wrapped.parameters.values()) == tuple(
            item.kind for item in native.parameters.values()
        )
    with pytest.raises(TypeError):
        cursor.execute(sql="SELECT 1")
    cursor.execute("SELECT 1")
    with pytest.raises(TypeError, match="integer"):
        cursor.fetchmany(None)


@pytest.mark.parametrize(
    "operation",
    ("execute", "executemany", "executescript", "fetchone", "fetchmany", "fetchall", "iteration"),
)
def test_every_blocking_cursor_operation_is_observable_and_cleans_up(db, operation):
    runtime = diagnostics.DiagnosticsRuntime(
        db.root_dir / "diagnostics",
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        enable_signal=False,
    )
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    with db.write() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS cursor_probe(value INTEGER)")
        connection.execute("DELETE FROM cursor_probe")
        connection.executemany("INSERT INTO cursor_probe VALUES (?)", ((1,), (2,), (3,)))

    def worker() -> None:
        try:
            connection = db.connect()
            calls = 0

            def diag_block(value):
                nonlocal calls
                calls += 1
                if operation in {"execute", "executemany", "executescript"} or calls >= 2:
                    entered.set()
                    release.wait(timeout=2)
                return value

            connection.create_function("diag_block", 1, diag_block)
            cursor = connection.cursor()
            if operation == "execute":
                cursor.execute("SELECT diag_block(1) FROM cursor_probe")
            elif operation == "executemany":
                cursor.executemany(
                    "INSERT INTO cursor_probe(value) VALUES (diag_block(?))",
                    ((1,),),
                )
            elif operation == "executescript":
                cursor.executescript("SELECT diag_block(1) FROM cursor_probe;")
            else:
                cursor.execute(
                    "SELECT diag_block(value) FROM cursor_probe ORDER BY value"
                )
                if operation == "fetchone":
                    cursor.fetchone()
                elif operation == "fetchmany":
                    cursor.fetchmany(2)
                elif operation == "fetchall":
                    cursor.fetchall()
                else:
                    next(cursor)
        except BaseException as exc:
            failures.append(exc)
        finally:
            db.close_local()

    with diagnostics.install_runtime(runtime):
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(timeout=2)
        active = runtime.snapshot()["active_sql"]
        assert len(active) == 1
        assert active[0]["table"] == "cursor_probe"
        release.set()
        thread.join(timeout=2)

    assert thread.is_alive() is False
    assert failures == []
    assert runtime.snapshot()["active_sql"] == []
