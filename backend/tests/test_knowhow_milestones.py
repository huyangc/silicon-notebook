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
