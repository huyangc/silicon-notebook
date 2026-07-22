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
