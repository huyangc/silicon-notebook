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
# not a same-thread call. Nested `write()` on ONE thread never blocks (the
# lock is a plain `threading.Lock`, and re-entry is accounted for by
# `write_depth`, not by lock reentrancy), so it always succeeds regardless of
# whether the fix is applied — a same-thread hook could never distinguish
# "fixed" from "buggy" here. Local imports (not module-level) so this test's own
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


# --- PR review round 5 P1-1 (ROOT FIX, fingerprint gap #3, data loss): rounds
# 2/3 patched table_fingerprint by ENUMERATING specific signals one at a time
# (mutation_seq, then four counts, then a cell_code content signal) — each
# added only after a reviewer found one more field a move could silently
# lose. That pattern just missed a THIRD class of edit: knowhow_tables.title/
# description and per-column name/role/position. None of these touch row/
# cell/code cardinality OR content, so none of the old signals move — see
# the pre-fix table_fingerprint docstring's own "Known, accepted scope
# boundary" paragraph, which explicitly named this exact gap and called it
# "acceptable" (round 5 disagrees: role alone encodes BOTH the anchor
# designation and the content kind, which changes KG projection semantics,
# not just a display label).
#
# The six tests below pin the fix's actual requirement — "the fingerprint
# changes if and only if some field the copy reproduces changed" — one edit
# at a time, so a future partial reimplementation can't silently reintroduce
# a narrower gap. Each edit is the SAME shape already established by this
# file's own test_fingerprint_changes_when_cell_code_is_edited_in_place
# above: capture, edit via the real repo/store API (never write raw SQL —
# these are the exact editing methods a real user/route would call), capture
# again, assert the two differ. Appended at EOF, same zero-line-shift
# convention as every other addition in this file.
def test_fingerprint_changes_when_table_title_is_edited(repo, store):
    tid = _table(repo)
    before = store.table_fingerprint(tid)

    repo.update_knowhow_table_meta(tid, title="改名后的时序修复")

    after = store.table_fingerprint(tid)
    assert before != after, (
        "表标题原地编辑后 table_fingerprint 必须变化——旧指纹只看 mutation_seq/"
        "行列格计数，标题编辑两者都不碰，move_table 的并发编辑防护会对它视而"
        "不见，编辑随源表一起被删掉、永久丢失"
    )


def test_fingerprint_changes_when_table_description_is_edited(repo, store):
    tid = _table(repo)
    before = store.table_fingerprint(tid)

    repo.update_knowhow_table_meta(tid, description="补充说明")

    after = store.table_fingerprint(tid)
    assert before != after, "表描述原地编辑后 table_fingerprint 必须变化，理由同标题"


def test_fingerprint_changes_when_column_is_renamed(repo, store):
    tid = _table(repo)
    column_id = repo.get_knowhow_table(tid)["columns"][0]["id"]
    before = store.table_fingerprint(tid)

    repo.rename_knowhow_column(column_id, "新列名")

    after = store.table_fingerprint(tid)
    assert before != after, (
        "列重命名后 table_fingerprint 必须变化——重命名不改行列格计数、不碰"
        "mutation_seq（KnowhowStore 自己的类文档字符串明确把 rename 列进"
        "「结构性编辑方法故意不 bump」清单），旧指纹对它完全失明"
    )


def test_fingerprint_changes_when_column_kind_changes(repo, store):
    """纯内容 kind 变化（非 anchor 迁移）：现象识别列 procedure → entity。"""
    tid = _table(repo)
    column_id = repo.get_knowhow_table(tid)["columns"][1]["id"]  # "现象识别"，role='procedure'
    before = store.table_fingerprint(tid)

    repo.set_knowhow_column_kind(column_id, "entity")

    after = store.table_fingerprint(tid)
    assert before != after, (
        "列 kind 原地变化后 table_fingerprint 必须变化——role 决定 KG 投影语义"
        "（哪些列参与实体/关系抽取），旧指纹的四个计数和 cell_code 信号都不会"
        "因为纯 kind 变化而移动"
    )


def test_fingerprint_changes_when_anchor_designation_moves(repo, store):
    """role 的另一半语义：anchor 指定本身迁移（这次改动会把「现象识别」提升为
    anchor，同时把原 anchor「违例类型」降级为 attribute——单次操作改了两列的
    role，任务描述里 "attribute→anchor" 这个例子在这条用例里精确对应「违例
    类型」这一侧的落点）。"""
    tid = _table(repo)
    cols = repo.get_knowhow_table(tid)["columns"]
    new_anchor_id = next(c["id"] for c in cols if c["name"] == "现象识别")
    before = store.table_fingerprint(tid)

    repo.set_knowhow_anchor_column(tid, new_anchor_id)

    after = store.table_fingerprint(tid)
    assert before != after, (
        "anchor 指定迁移后 table_fingerprint 必须变化——这是「行标题列是哪一"
        "列」的表级语义变化，行列格计数和 mutation_seq 都不会因此移动"
    )


def test_fingerprint_changes_when_a_column_is_swapped_for_a_different_one(repo, store):
    """净列数不变的换列：删一列、加一列不同的，且被删的列刻意选一个从未写过
    任何 cell 的空列（占位列），使级联删除也不会碰 cell_count——前后两次快照
    在旧指纹的全部字段（mutation_seq/col_count/row_count/cell_count/
    cell_code_count/cell_code_signal）上逐一比较都相等，因为它们只看「数量」,
    从根本上无法分辨「同样 3 列」和「3 列但其中一列换了身份」。只有把列的
    id 本身哈希进去才能抓住这种编辑——这正是「结构完备」这个提法要堵的那类
    永远也数不完的漏洞，不是又一个可以枚举补上的第四个信号。"""
    tid = _table(repo)
    extra_col_id = repo.add_knowhow_column(tid, "占位列", "attribute")
    before_detail = repo.get_knowhow_table(tid)
    before = store.table_fingerprint(tid)

    repo.add_knowhow_column(tid, "替换列", "entity")
    repo.delete_knowhow_column(extra_col_id)

    after_detail = repo.get_knowhow_table(tid)
    assert len(after_detail["columns"]) == len(before_detail["columns"]), (
        "前置条件：净列数必须不变（占位列换成替换列），否则这条用例测的是"
        "列数变化，不是列换身份"
    )
    after = store.table_fingerprint(tid)
    assert before != after, (
        "净列数不变的换列后 table_fingerprint 必须变化——纯计数式指纹在这种"
        "编辑下无论加多少个计数字段都测不出来，move_table 会把换掉的那一列"
        "连同源表一起删掉，永久丢失"
    )


# --- PR review round 7 P2-B: cell_code_signal hashed row_id/column_id/
# code_text/language/cell_content_hash, but NOT updated_by — an agent
# rewriting an attachment with IDENTICAL code/language/content-hash but a
# DIFFERENT updated_by left every one of those four fields unchanged, so the
# fingerprint was blind to it. _remap (transfer.py) copies the FULL cell_code
# row into the target (`code = dict(code)`; only id/row_id/column_id get
# overwritten — updated_by rides along untouched), so the fingerprint must
# cover every field the copy reproduces, per this file's own structural-
# completeness rule (see table_fingerprint's docstring) — updated_by is no
# exception just because it happens to carry no display content. Without
# this, a concurrent updated_by-only rewrite racing a move sails straight
# past the recheck: the source row gets deleted, and the target is left
# holding the OLDER attribution forever. Appended at EOF, same zero-line-
# shift convention as every other addition in this file.
def test_fingerprint_changes_when_cell_code_updated_by_changes(repo, store):
    """同 test_fingerprint_changes_when_cell_code_is_edited_in_place 的编辑
    手法，但反过来：code_text/language/cell_content_hash 三者原样不变，只换
    updated_by——旧指纹的四个字段一个都不移动，必须新增第五个才能抓住它。"""
    tid = _table(repo)
    detail = repo.get_knowhow_table(tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    before = store.table_fingerprint(tid)
    # 代码/语言/内容哈希三者原样不变，只换 updated_by（同一次 UPSERT 的
    # ON CONFLICT(row_id, column_id) DO UPDATE 分支——同 test_fingerprint_
    # changes_when_cell_code_is_edited_in_place 走的同一条更新路径）。
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-y", "hash-v1")
    after = store.table_fingerprint(tid)

    assert before != after, (
        "仅 updated_by 变化时 table_fingerprint 必须变化——_remap 会把整行"
        "（含 updated_by）原样搬进副本，指纹若对它失明，并发 move 会把刚刚"
        "变化的属主信息连同源表一并删掉，目标副本永久留着旧的属主"
    )
