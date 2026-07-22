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


def test_seq_starts_at_one_and_increments(repo, table_id):
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == 1
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == 2


def test_records_fingerprint_of_state_after_the_change(repo, table_id):
    from app.repositories.sqlite import knowhow_fingerprint

    _record(repo, table_id, kind="table_create", payload={})
    with repo._runtime.database.connect() as db:
        expected = knowhow_fingerprint.fingerprint_on(db, table_id)
        stored = db.execute(
            "SELECT fingerprint FROM knowhow_changes WHERE table_id=? AND seq=1",
            (table_id,),
        ).fetchone()["fingerprint"]
    assert stored == expected


def test_actor_origin_note_round_trip(repo, table_id, store):
    _record(
        repo, table_id,
        kind="cell_update", payload={"cells": []},
        actor="user-abc", origin="llm_reformat", note="批量规整",
    )
    change = store.get_change(table_id, 1)
    assert change["actor"] == "user-abc"
    assert change["origin"] == "llm_reformat"
    assert change["note"] == "批量规整"
    assert change["payload"] == {"cells": []}


def test_list_changes_is_newest_first_and_paginates(repo, table_id, store):
    for _ in range(5):
        _record(repo, table_id, kind="cell_update", payload={"cells": []})

    newest = store.list_changes(table_id, limit=2)
    assert [c["seq"] for c in newest] == [5, 4]

    older = store.list_changes(table_id, limit=2, before_seq=4)
    assert [c["seq"] for c in older] == [3, 2]


def test_head_seq_is_zero_for_a_table_with_no_history(repo, table_id, store):
    assert store.head_seq(table_id) == 0


def test_cell_history_filters_to_one_cell_newest_first(repo, table_id, store):
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": None, "after": "一"}]
    })
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r2", "column_id": "c1", "before": None, "after": "别的行"}]
    })
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": "一", "after": "二"}]
    })

    entries = store.cell_history(table_id, "r1", "c1")
    assert [e["seq"] for e in entries] == [3, 1]
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
