from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_history_store as history
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


@pytest.fixture
def table_id(repo, notebook_id) -> str:
    return repo._runtime.knowhow_store.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}]
    )


@pytest.fixture
def store(repo) -> history.KnowhowHistoryStore:
    return repo._runtime.knowhow_history_store


def _record(repo, table_id, **kwargs):
    runtime = repo._runtime
    with runtime.database.write() as db:
        return history.record_change(
            db,
            new_id=runtime.knowhow_store.new_id,
            now=runtime.knowhow_store.now,
            table_id=table_id,
            **kwargs,
        )


def test_seq_continues_from_the_existing_head_and_increments(repo, table_id, store):
    """record_change 的 seq 用 COALESCE(MAX(seq),0)+1 现算。``table_id`` 由
    ``create_knowhow_table`` 建出，knowhow-table-version-control Task 6 之后
    它自己会先写一条创世 ``table_create`` 流水，所以这里断言"续接当前
    head"而不是硬编码从 1 开始——真正的"表从零流水开始，第一条是
    seq==1"边界由 ``test_head_seq_is_zero_for_a_table_with_no_history``
    （裸表，不经过 ``create_knowhow_table``）和
    ``test_knowhow_history_hooks.py::test_create_table_records_genesis_change``
    两处覆盖。"""
    head = store.head_seq(table_id)
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == head + 1
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == head + 2


def test_records_fingerprint_of_state_after_the_change(repo, table_id):
    """指纹必须反映"变更之后"的表状态——不是随便存哪次算的都行。

    构造一个变更前指纹 != 变更后指纹的场景（真的加一行），断言存下来的
    指纹等于变更后的、且不等于变更前的。用同一状态前后各测一次算不出
    区别，锁不住"何时算指纹"这条约束。
    """
    from app.repositories.sqlite import knowhow_fingerprint

    runtime = repo._runtime
    with runtime.database.connect() as db:
        before_fp = knowhow_fingerprint.fingerprint_on(db, table_id)

    runtime.knowhow_store.add_knowhow_row(table_id, {})

    with runtime.database.connect() as db:
        after_fp = knowhow_fingerprint.fingerprint_on(db, table_id)
    assert after_fp != before_fp  # sanity: 变更真的挪动了指纹

    seq = _record(repo, table_id, kind="row_add", payload={"rows": []})

    with runtime.database.connect() as db:
        stored = db.execute(
            "SELECT fingerprint FROM knowhow_changes WHERE table_id=? AND seq=?",
            (table_id, seq),
        ).fetchone()["fingerprint"]
    assert stored == after_fp
    assert stored != before_fp


def test_actor_origin_note_round_trip(repo, table_id, store):
    # seq 取 _record 的返回值而非硬编码 1——table_id 已带一条创世流水
    # （create_knowhow_table，Task 6），这条手工记录接在它后面。
    seq = _record(
        repo, table_id,
        kind="cell_update", payload={"cells": []},
        actor="user-abc", origin="llm_reformat", note="批量规整",
    )
    change = store.get_change(table_id, seq)
    assert change["actor"] == "user-abc"
    assert change["origin"] == "llm_reformat"
    assert change["note"] == "批量规整"
    assert change["payload"] == {"cells": []}


def test_list_changes_is_newest_first_and_paginates(repo, table_id, store):
    # table_id 自带一条创世流水（Task 6），后面 5 条手工记录接在 head 之后。
    head = store.head_seq(table_id)
    for _ in range(5):
        _record(repo, table_id, kind="cell_update", payload={"cells": []})

    newest = store.list_changes(table_id, limit=2)
    assert [c["seq"] for c in newest] == [head + 5, head + 4]

    older = store.list_changes(table_id, limit=2, before_seq=head + 4)
    assert [c["seq"] for c in older] == [head + 3, head + 2]


def test_head_seq_is_zero_for_a_table_with_no_history(repo, notebook_id, store):
    """真正零流水的表——不用 ``table_id`` fixture，因为它经
    ``create_knowhow_table`` 建出，Task 6 之后自己就会先写一条创世流水
    （``head_seq`` 会是 1，不是 0）。这里直接裸插一行 ``knowhow_tables``，
    保住"knowhow_changes 里一行都没有时 head_seq 返回 0"这条真实边界。"""
    runtime = repo._runtime
    bare_table_id = runtime.knowhow_store.new_id("khtbl")
    now = runtime.knowhow_store.now()
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables "
            "(id, notebook_id, title, description, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bare_table_id, notebook_id, "裸表", "", "", now, now),
        )
    assert store.head_seq(bare_table_id) == 0


def test_changes_between_is_left_open_right_closed_ascending(repo, table_id, store):
    """区间 (from_seq, to_seq]：排除 from_seq 本身，包含 to_seq；升序供 diff 折叠。"""
    for _ in range(5):
        _record(repo, table_id, kind="cell_update", payload={"cells": []})

    entries = store.changes_between(table_id, 2, 4)
    assert [c["seq"] for c in entries] == [3, 4]


def test_cell_history_filters_to_one_cell_newest_first(repo, table_id, store):
    # seq 取返回值而非硬编码 1/3——table_id 自带一条创世流水（Task 6）。
    seq1 = _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": None, "after": "一"}]
    })
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r2", "column_id": "c1", "before": None, "after": "别的行"}]
    })
    seq3 = _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": "一", "after": "二"}]
    })

    entries = store.cell_history(table_id, "r1", "c1")
    assert [e["seq"] for e in entries] == [seq3, seq1]
    assert entries[0]["after"] == "二"
    assert entries[0]["before"] == "一"


def test_cell_history_finds_the_cell_inside_a_multi_cell_batch(repo, table_id, store):
    """合并格批量写是一条流水里多个 cells 条目——不能只看第一个。"""
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [
            {"row_id": "rA", "column_id": "c1", "before": "旧A", "after": "新"},
            {"row_id": "rB", "column_id": "c1", "before": "旧B", "after": "新"},
        ]
    })
    entries = store.cell_history(table_id, "rB", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] == "旧B"


def test_cell_history_finds_the_cell_born_via_row_add(repo, table_id, store):
    """row_add 顶层键是 rows，格子内容嵌在 rows[i]['cells'] 字典里，不是顶层 cells 列表。"""
    _record(repo, table_id, kind="row_add", payload={
        "rows": [{
            "row_id": "r1", "position": 0,
            "cells": {"c1": "新行初始值"},
            "code": [],
        }]
    })
    entries = store.cell_history(table_id, "r1", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] is None
    assert entries[0]["after"] == "新行初始值"


def test_cell_history_finds_the_cell_born_via_import_append(repo, table_id, store):
    """import_append 与 row_add 同形状——批量导入产生的格子同样要能查到诞生记录。"""
    _record(repo, table_id, kind="import_append", payload={
        "rows": [{
            "row_id": "r1", "position": 0,
            "cells": {"c1": "导入值"},
            "code": [],
        }]
    })
    entries = store.cell_history(table_id, "r1", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] is None
    assert entries[0]["after"] == "导入值"


def test_cell_history_finds_the_cell_that_died_via_row_delete(repo, table_id, store):
    """row_delete 存整行快照（同 row_add 形状）；格子消失，before=旧值，after=None。"""
    _record(repo, table_id, kind="row_delete", payload={
        "rows": [{
            "row_id": "r1", "position": 0,
            "cells": {"c1": "被删前的值"},
            "code": [],
        }]
    })
    entries = store.cell_history(table_id, "r1", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] == "被删前的值"
    assert entries[0]["after"] is None


def test_cell_history_finds_the_cell_that_died_via_column_delete(repo, table_id, store):
    """column_delete 顶层 cells 列表的条目形状是 {row_id, content_md}，没有 column_id；
    整个 payload 只对应一列，需先核对 payload['column']['id'] == 请求的 column_id。"""
    _record(repo, table_id, kind="column_delete", payload={
        "column": {"id": "c1", "name": "概念", "role": "attribute", "position": 1},
        "cells": [{"row_id": "r1", "content_md": "列删前的值"}],
        "code": [],
    })
    entries = store.cell_history(table_id, "r1", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] == "列删前的值"
    assert entries[0]["after"] is None


def test_cell_history_column_delete_does_not_leak_into_a_different_column(repo, table_id, store):
    """column_delete 的 cells 条目没有 column_id 字段本身——防回归：错误实现如果忽略
    payload['column']['id'] 校验，任何列被删都会污染所有列的格子历史。"""
    _record(repo, table_id, kind="column_delete", payload={
        "column": {"id": "c1", "name": "概念", "role": "attribute", "position": 1},
        "cells": [{"row_id": "r1", "content_md": "列删前的值"}],
        "code": [],
    })
    entries = store.cell_history(table_id, "r1", "c2")
    assert entries == []


# --- history_page：head/changes/milestones 同一读快照（codex 第 4 轮 P1）-----


def _trace_next_connection(store, monkeypatch) -> list[str]:
    """给 SqliteDatabase 之后新建的连接挂语句 tracer，返回它 append 的共享列表。
    调用方须先 ``close_local()`` 丢掉线程复用读连接，``history_page`` 的
    ``connect()`` 才会新建一条被 trace 的连接。callback 在 ``_new_connection``
    跑完 PRAGMA 之后才挂，列表从方法自身发出的第一条语句起。"""
    statements: list[str] = []
    original = store.database._new_connection

    def traced():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store.database, "_new_connection", traced)
    return statements


def test_history_page_reads_head_changes_and_milestones_in_one_snapshot(
    repo, table_id, store, monkeypatch
):
    """结构证据：一条 ``BEGIN`` 先于全部三条 SELECT、其间不夹 COMMIT——head_seq、
    changes、milestones 因此取自同一读快照。删掉 ``history_page`` 里那句
    ``db.execute("BEGIN")``，三读退回各自 autocommit 快照，本测试立刻转红——
    这正是 codex 第 4 轮 P1 修的 bug（head 领先于本页最新 change，用户没看见
    的并发编辑被陈旧守卫放行、连带回退抹掉）。"""
    _record(repo, table_id, kind="cell_update", payload={"cells": []})
    _record(repo, table_id, kind="cell_update", payload={"cells": []})

    store.database.close_local()  # 丢线程复用读连接，下次 connect() 才被 trace
    statements = _trace_next_connection(store, monkeypatch)

    page = store.history_page(table_id, limit=50)

    upper = [s.strip().upper() for s in statements]
    begins = [i for i, s in enumerate(upper) if s.startswith("BEGIN")]
    selects = [i for i, s in enumerate(upper) if s.startswith("SELECT")]
    assert len(begins) == 1, f"期望恰好一条 BEGIN 包住三读，trace={statements}"
    assert len(selects) >= 3, f"head/changes/milestones 三条 SELECT 都应在，trace={statements}"
    assert begins[0] < selects[0], f"BEGIN 必须先于首条 SELECT，trace={statements}"
    commit_idx = next(
        (i for i, s in enumerate(upper) if s.startswith("COMMIT")), len(upper)
    )
    assert selects[2] < commit_idx, f"三读必须在 COMMIT 之前全部完成，trace={statements}"


def test_history_page_returns_consistent_head_page_and_milestones(repo, table_id, store):
    """功能面：head_seq 就是本页最新一条 change 的 seq；changes 按 seq 倒序；
    milestones 带 stale 标记；before_seq 严格小于翻页。"""
    s1 = _record(repo, table_id, kind="cell_update", payload={"cells": []})
    s2 = _record(repo, table_id, kind="cell_update", payload={"cells": []})
    store.create_milestone(table_id, s1, "里程碑A", "", "user-1")

    page = store.history_page(table_id, limit=50)
    assert page["head_seq"] == s2
    assert page["head_seq"] == page["changes"][0]["seq"]  # 自洽：head==最新change
    assert [c["seq"] for c in page["changes"]][:2] == [s2, s1]  # 倒序
    assert [m["seq"] for m in page["milestones"]] == [s1]
    assert page["milestones"][0]["stale"] is False

    older = store.history_page(table_id, limit=50, before_seq=s2)
    seqs = [c["seq"] for c in older["changes"]]
    assert s2 not in seqs and s1 in seqs  # before_seq 严格小于
