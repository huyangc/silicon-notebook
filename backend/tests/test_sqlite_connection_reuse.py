import sqlite3
import threading
import concurrent.futures as cf

import pytest

from app.core.config import Settings
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
