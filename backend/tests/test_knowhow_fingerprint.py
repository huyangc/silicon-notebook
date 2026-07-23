from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
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


BASE_COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "修复方法", "role": "attribute"},
]


def test_transfer_store_alias_is_the_shared_module_object(repo):
    """搬迁不能改变 KnowhowTransferStore 的既有表面：别名必须是同一个对象。"""
    assert KnowhowTransferStore._FINGERPRINT_SQL is knowhow_fingerprint.FINGERPRINT_SQL
    assert KnowhowTransferStore._GROUP_SEP is knowhow_fingerprint.GROUP_SEP


def test_shared_helper_and_transfer_store_agree(repo, notebook_id):
    store = repo._runtime.knowhow_store
    table_id = store.create_knowhow_table(notebook_id, "t", "", BASE_COLUMNS)
    transfer = repo._runtime.knowhow_transfer_store

    with repo._runtime.database.connect() as db:
        shared = knowhow_fingerprint.fingerprint_on(db, table_id)

    assert shared == transfer.table_fingerprint(table_id)
    assert isinstance(shared, str) and len(shared) == 64


def test_missing_table_returns_none(repo):
    with repo._runtime.database.connect() as db:
        assert knowhow_fingerprint.fingerprint_on(db, "nope") is None


def test_fingerprint_no_collision_when_free_text_contains_separator_chars(repo, notebook_id):
    """codex 第 3 轮 P2：自由文本含分隔控制字符时，两个「拼起来一模一样」但语义
    不同的表状态必须有不同指纹。旧编码下 title=`a\x1db`+desc=`c` 与 title=`a`+
    desc=`b\x1dc` 序列化后完全相同 → 碰撞 → 回退/并发守卫会把不同状态判成相同。
    同一张表在两种状态间切（列 id 不变，只 title/desc 变），精确隔离该碰撞。"""
    store = repo._runtime.knowhow_store
    gs = knowhow_fingerprint.GROUP_SEP  # '\x1d'
    table_id = store.create_knowhow_table(notebook_id, "t", "", BASE_COLUMNS)

    store.update_knowhow_table_meta(table_id, title=f"a{gs}b", description="c")
    with repo._runtime.database.connect() as db:
        fp1 = knowhow_fingerprint.fingerprint_on(db, table_id)

    store.update_knowhow_table_meta(table_id, title="a", description=f"b{gs}c")
    with repo._runtime.database.connect() as db:
        fp2 = knowhow_fingerprint.fingerprint_on(db, table_id)

    assert fp1 != fp2, (
        "含分隔符的两个不同表状态指纹相同——回退前后置守卫会误判一致、放过被篡改的表"
    )


def test_fingerprint_no_collision_when_cell_content_contains_separator_chars(repo, notebook_id):
    """同上，但碰撞点在格子内容里的 char(31)（US，字段内分隔符）。"""
    store = repo._runtime.knowhow_store
    us = "\x1f"  # char(31)，signal 里字段间的分隔符
    table_id = store.create_knowhow_table(notebook_id, "t", "", BASE_COLUMNS)
    detail = store.get_knowhow_table(table_id)
    col = detail["columns"][1]["id"]
    row = store.add_knowhow_row(table_id, {})

    store.update_knowhow_cell(row, col, f"x{us}y")
    with repo._runtime.database.connect() as db:
        fp1 = knowhow_fingerprint.fingerprint_on(db, table_id)
    store.update_knowhow_cell(row, col, "x")
    with repo._runtime.database.connect() as db:
        fp2 = knowhow_fingerprint.fingerprint_on(db, table_id)

    assert fp1 != fp2, "格子内容 `x\\x1fy` 与 `x` 必须有不同指纹"
