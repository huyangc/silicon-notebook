from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
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
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store):
    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    return {
        "id": table_id,
        "anchor": detail["columns"][0]["id"],
        "plain": detail["columns"][1]["id"],
    }


def test_milestone_names_must_be_unique_per_table(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    seq = hist.head_seq(table["id"])
    hist.create_milestone(table["id"], seq, "评审前", "", "user-1")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        hist.create_milestone(table["id"], seq, "评审前", "", "user-1")


def test_prune_deletes_only_the_oldest_prefix(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    for i in range(5):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")
    head = hist.head_seq(table["id"])

    # 人为把前 3 条的时间戳改老
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq <= 3",
            (table["id"],),
        )

    hist.prune(table["id"], "2001-01-01T00:00:00")

    remaining = [c["seq"] for c in hist.list_changes(table["id"], limit=100)]
    assert remaining == sorted(remaining, reverse=True)
    assert min(remaining) == 4, "只能删最老的连续前缀"
    assert max(remaining) == head


def test_prune_never_removes_the_head(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    head = hist.head_seq(table["id"])
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ?",
            (table["id"],),
        )

    hist.prune(table["id"], "2099-01-01T00:00:00")

    remaining = [c["seq"] for c in hist.list_changes(table["id"], limit=100)]
    assert remaining == [head], (
        "head 必须留着——前置指纹守卫拿它当参照，删了整表回退就不可用了"
    )


def test_prune_uses_seq_not_timestamp_so_clock_skew_cannot_punch_holes(hist, store, table):
    """时钟回拨会让 created_at 局部乱序；按 seq 执行删除才保证删的是前缀。"""
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    for i in range(4):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")

    with hist.database.write() as db:
        # seq 1,2,4 老，seq 3 却是新的（时钟回拨）
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq IN (1,2,4)",
            (table["id"],),
        )
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2050-01-01T00:00:00' "
            "WHERE table_id = ? AND seq = 3",
            (table["id"],),
        )

    hist.prune(table["id"], "2001-01-01T00:00:00")

    remaining = sorted(c["seq"] for c in hist.list_changes(table["id"], limit=100))
    assert remaining == list(range(remaining[0], remaining[-1] + 1)), (
        f"流水链出现空洞：{remaining}"
    )


def test_prune_keeps_a_recent_entry_wedged_between_old_ones(hist, store, table):
    """codex 第 3 轮 P2：时钟回拨让 created_at 非单调时，只能删「从最老起、每条
    都够老的最长连续前缀」。一条较新的记录（created_at ≥ cutoff）即使夹在老
    记录之间，也必须保留——旧实现取 MAX(seq WHERE old) 会跳过它、连带删掉。"""
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    for i in range(4):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")
    head = hist.head_seq(table["id"])

    with hist.database.write() as db:
        # 除 head 外：seq 1、2 老；seq 3 是较新的（时钟未回拨）；seq 4 又是老的
        # （被回拨到 cutoff 之前）。连续前缀应止于 seq 2——seq 3（新）必须留下。
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq IN (1, 2, 4)",
            (table["id"],),
        )
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2050-01-01T00:00:00' "
            "WHERE table_id = ? AND seq = 3",
            (table["id"],),
        )

    hist.prune(table["id"], "2001-01-01T00:00:00")

    remaining = sorted(c["seq"] for c in hist.list_changes(table["id"], limit=100))
    assert 3 in remaining, (
        f"较新的 seq 3 夹在老记录之间，绝不能被删；实际剩余={remaining}"
    )
    assert remaining == list(range(3, head + 1)), (
        f"应只删连续前缀 seq 1、2，剩 3..{head}；实际={remaining}"
    )


def test_milestone_pointing_at_a_pruned_seq_survives_as_stale(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    old_seq = hist.head_seq(table["id"])
    hist.create_milestone(table["id"], old_seq, "很久以前", "", "user-1")
    for i in range(3):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")

    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq <= ?",
            (table["id"], old_seq),
        )
    hist.prune(table["id"], "2001-01-01T00:00:00")

    milestones = hist.list_milestones(table["id"])
    assert len(milestones) == 1
    assert milestones[0]["stale"] is True, "指向已删流水的里程碑要标记失效，但不能删"


def test_delete_milestone_requires_table_id_isolation(hist, store, repo, table):
    """delete_milestone 必须同时检查 table_id 和 milestone_id，隔离不同表。"""
    # 创建表 B（第二张表）
    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_b_id = store.create_knowhow_table(
        notebook_id, "表B", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )

    # 在表 A 上建一个里程碑
    row_a = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    seq_a = hist.head_seq(table["id"])
    milestone = hist.create_milestone(table["id"], seq_a, "里程碑A", "", "user-1")
    milestone_id = milestone["id"]

    # 用表 B 的 table_id + 表 A 的 milestone_id 试图删除 → 应该是 no-op
    hist.delete_milestone(table_b_id, milestone_id)

    # 表 A 的里程碑仍然存在
    milestones_a = hist.list_milestones(table["id"])
    assert len(milestones_a) == 1
    assert milestones_a[0]["id"] == milestone_id

    # 用表 A 的正确 table_id 再删一次 → 才真的删掉
    hist.delete_milestone(table["id"], milestone_id)

    milestones_a = hist.list_milestones(table["id"])
    assert len(milestones_a) == 0


def test_revert_after_prune_preserves_full_replayability(hist, store, table):
    """prune 删掉最老流水后，剩余链仍能完整回退到任意中间点。"""
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    seq_after_row = hist.head_seq(table["id"])

    # 对同一格子做 5 次更新，记下中间的内容状态
    for i in range(5):
        store.update_knowhow_cell(row, table["plain"], f"版本{i}")

    head_before_prune = hist.head_seq(table["id"])
    all_changes = hist.list_changes(table["id"], limit=100)

    # 确认有足够的流水
    assert len(all_changes) >= 6, f"应该至少有 6 条流水（row_add + 5 updates），但只有 {len(all_changes)}"

    # 把最早的 3 条流水（除了最新的）的时间戳改成很老
    seqs_to_age = sorted([c["seq"] for c in all_changes])[:3]
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq IN (?, ?, ?)",
            (table["id"], seqs_to_age[0], seqs_to_age[1], seqs_to_age[2]),
        )

    # prune 删掉那 3 条流水
    result = hist.prune(table["id"], "2001-01-01T00:00:00")
    assert result["removed"] == 3

    # 断言剩余流水 seq 连续无空洞
    remaining = sorted(c["seq"] for c in hist.list_changes(table["id"], limit=100))
    assert remaining == list(range(remaining[0], remaining[-1] + 1)), (
        f"prune 后出现 seq 空洞：{remaining}"
    )
    assert len(remaining) >= 3, "prune 后应该至少剩 3 条流水"

    # 回退到剩余最早的那条 seq，应该成功
    min_remaining_seq = remaining[0]
    current_head_seq = hist.head_seq(table["id"])
    result = hist.revert_to(
        table["id"], target_seq=min_remaining_seq, expected_head_seq=current_head_seq
    )
    assert result["target_seq"] == min_remaining_seq

    # 验证内容确实回退到了那一刻（格子应该有对应的值，不是 None）
    with store.database.connect() as db:
        cells = db.execute(
            "SELECT content_md FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
            (row, table["plain"]),
        ).fetchone()
    assert cells is not None
    # 回退到 min_remaining_seq，应该有一个格子内容值
    assert cells["content_md"] is not None, "回退后格子应该有内容"
