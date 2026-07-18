import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def store(repo):
    rt = repo._runtime
    return KnowhowTransferStore(rt.database)

def _table(repo) -> str:
    nb = repo.create_notebook(NotebookCreate(name="KH", purpose="p", primary_domain="d")).id
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def test_snapshot_returns_business_rows(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    assert snap["table"]["id"] == tid
    assert {c["name"] for c in snap["columns"]} == {"违例类型", "现象识别"}
    assert len(snap["rows"]) == 1
    assert len(snap["cells"]) == 2
    # 未投影表没有派生产物
    assert snap["elements"] == [] and snap["chunks"] == []

def test_snapshot_missing_table_raises(store):
    with pytest.raises(KeyError):
        store.snapshot_table("khtbl-nope")

def test_insert_transfer_rejects_count_mismatch(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    # 只插一个全新表行、零 cell，但 expected_counts 声称 cells=1 → 校验必须拒绝并回滚
    payload = {
        "table": {**snap["table"], "id": "khtbl-x"},
        "columns": [], "rows": [], "cells": [], "cell_code": [],
        "assets": [], "source": None, "elements": [], "chunks": [], "chunk_embeddings": [],
    }
    with pytest.raises(RuntimeError):
        store.insert_transfer(payload, {"columns": 0, "rows": 0, "cells": 1, "cell_code": 0})
    # 回滚生效：khtbl-x 未落库
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowhow_tables WHERE id='khtbl-x'").fetchone()[0] == 0


# --- PR review round 3 P1-3 (fingerprint gap, data loss precursor):
# upsert_knowhow_cell_code is an INSERT-ON-CONFLICT(row_id,column_id) upsert —
# an in-place code edit on an already-attached cell keeps the same row/column
# pair, so none of the four count-based fingerprint fields move (col_count/
# row_count/cell_count/cell_code_count are all unchanged: same number of
# rows). It also does NOT bump knowhow_tables.mutation_seq — only
# update_knowhow_cell/update_knowhow_cells do (see KnowhowStore's own
# docstring; upsert_knowhow_cell_code is conspicuously absent from that list).
# So a fingerprint built only from mutation_seq + the four counts is blind to
# this edit: move_table's snapshot-vs-delete concurrent-edit guard would sail
# straight past a concurrent code edit and delete the source table, silently
# discarding the newer code forever. table_fingerprint must therefore include
# a CONTENT signal over the code rows themselves, not just their count.
def test_fingerprint_changes_when_cell_code_is_edited_in_place(repo, store):
    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    before = store.table_fingerprint(tid)
    # 仅仅原地编辑代码内容——不加行、不加列、不碰 knowhow_cells，
    # row/column 都是同一对，UNIQUE(row_id, column_id) 走 UPDATE 分支。
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(2)", "python", "user-x", "hash-v2")
    after = store.table_fingerprint(tid)

    assert before != after, (
        "cell_code 内容原地编辑后 table_fingerprint 必须变化——否则 move_table 的"
        "并发编辑防护对代码编辑视而不见，编辑会随源表一起被删掉、永久丢失"
    )


def test_fingerprint_stable_when_nothing_changes(repo, store):
    """Companion happy-path guard: calling table_fingerprint twice with no
    intervening write must be stable (no spurious churn from e.g. dict key
    ordering or non-deterministic aggregate ordering)."""
    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    first = store.table_fingerprint(tid)
    second = store.table_fingerprint(tid)

    assert first == second


# --- PR review round 4 P1 (copy correctness, not only move's): snapshot_table
# used to run its SELECTs on a plain `with self.database.connect() as db:`
# block. `connect()` returns the THREAD-LOCAL, REUSED connection (see
# SqliteDatabase.connect's own docstring — 233+ call sites share it — and
# _Conn's class docstring, which explicitly warns "生产中复用连接实际只读,
# 所有写经 write()") with no explicit BEGIN: under WAL, a bare SELECT with no
# open transaction observes the latest state committed AS OF THE MOMENT THAT
# STATEMENT runs, not a snapshot pinned at the start of the method. A writer
# committing BETWEEN two of snapshot_table's SELECTs therefore makes them see
# two different committed states.
#
# This test injects a concurrent INSERT (a new row + its cell), not a
# delete: a delete landing in this same window can only ever make a LATER
# query (cells) a subset of what a fully-consistent read would show — still
# stale, but not self-contradictory. A concurrent INSERT is what produces a
# snapshot no single point in time could have produced: `cells` (read AFTER
# the insert) contains a cell whose row_id is absent from `rows` (read
# BEFORE the insert) — exactly what makes `_remap` hard-crash with a KeyError
# on `khrow_map[cell["row_id"]]` (transfer.py), an ID-remap failure on a
# plain copy, no move/delete involved.
#
# The interleaving is a REAL background thread whose write goes through the
# actual `SqliteDatabase.write()` + process-wide `write_lock` — deliberately
# not a same-thread call. `write_lock` is a `threading.RLock`: a same-thread
# reentrant acquire always succeeds regardless of whether the fix is
# applied, so a same-thread hook could never distinguish "fixed" from
# "buggy" here. Local imports (not module-level) so this test's own
# threading/contextlib helpers don't shift the line-pinned `from
# app.services.sqlite_repository import SQLiteRepository` at line 4
# (test_repository_surface_manifest.py's KNOWHOW_TRANSFER_STORE_ALLOWED_
# IMPORTS) — appended at EOF for the same zero-line-shift reason documented
# throughout the sibling transfer test files.
def test_snapshot_table_is_internally_consistent_under_concurrent_insert(
    repo, store, monkeypatch
):
    import threading
    from contextlib import contextmanager

    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    col_id = detail["columns"][0]["id"]
    main_thread_id = threading.get_ident()

    hook_fired = threading.Event()
    writer_started = threading.Event()
    writer_committed = threading.Event()
    spawned: list = []

    def _concurrent_writer() -> None:
        writer_started.set()
        # 真正经过 SqliteDatabase.write() + 进程级 write_lock 的并发写者——
        # 不是同线程再入（RLock 同线程可重入，会让这条测试测不出锁到底生不
        # 生效）。
        repo.add_knowhow_row(tid, {col_id: "并发新增的行"})
        writer_committed.set()

    def _hook() -> None:
        if hook_fired.is_set():
            return
        hook_fired.set()
        thread = threading.Thread(target=_concurrent_writer, daemon=True)
        spawned.append(thread)
        thread.start()
        writer_started.wait(timeout=2.0)
        # 有界等待「插入已提交」。修复前 connect() 没有锁竞争，一次单行插入
        # 远快于这个超时，可靠地在 cells 查询之前提交——确定性 RED。修复后
        # write() 全程占着 write_lock，这次等待会超时（不是死锁：这里只是
        # 有界等待，不是无限等待——真正的插入被推迟到 snapshot_table 自己
        # 的 write() 块退出、释放锁之后才能提交；测试代码在下面会再等一次，
        # 确认它最终真的完成了）。
        writer_committed.wait(timeout=1.0)

    class _HookConnProxy:
        def __init__(self, real):
            self._real = real

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._real.__exit__(exc_type, exc, tb)

        def execute(self, sql, params=()):
            result = self._real.execute(sql, params)
            if "FROM knowhow_rows WHERE table_id" in sql:
                _hook()
            return result

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = store.database.connect
    real_write = store.database.write

    def _patched_connect():
        conn = real_connect()
        if threading.get_ident() != main_thread_id:
            return conn
        return _HookConnProxy(conn)

    @contextmanager
    def _patched_write():
        with real_write() as conn:
            if threading.get_ident() != main_thread_id:
                yield conn
            else:
                yield _HookConnProxy(conn)

    monkeypatch.setattr(store.database, "connect", _patched_connect)
    monkeypatch.setattr(store.database, "write", _patched_write)

    snap = store.snapshot_table(tid)

    assert hook_fired.is_set(), "hook 从未触发——rows 查询没有真的跑过，测试前置条件不成立"
    assert spawned, "并发写者线程从未启动"
    spawned[0].join(timeout=5.0)
    assert writer_committed.is_set(), "并发写者最终必须成功提交（只是被推迟，不是永久卡死）"

    row_ids = {r["id"] for r in snap["rows"]}
    orphan_cells = [c for c in snap["cells"] if c["row_id"] not in row_ids]
    assert not orphan_cells, (
        f"快照内部不一致：{len(orphan_cells)} 个 cell 引用的 row_id 不在 rows 快照里——"
        "rows 和 cells 两条 SELECT 在窗口内观察到了两个不同的已提交状态"
    )
