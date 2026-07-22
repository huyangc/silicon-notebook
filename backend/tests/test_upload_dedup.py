"""UI 上传的同 notebook 去重——对齐 batch_ingest 既有行为。

跨 notebook 刻意不去重：用户通常确实想在自己库里拥有这份文件，且跨用户共享
source 行会引爆权限、删除级联与归属问题。
"""
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    # 不传 scheduler 时 upload_sources 会同步跑完整 parse→extract 流水线；
    # 本测试只关心去重短路，把它打桩掉。
    monkeypatch.setattr(
        r._runtime.source_ingestion, "process_source", lambda sid, hooks: None
    )
    return r


@pytest.fixture
def notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def other_notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb2")).id


def _upload(repo, notebook_id, content=b"hello world", name="a.txt"):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(
            file_name=name, content_type="text/plain", content=content)],
    )


def test_same_content_twice_in_one_notebook_creates_one_source(repo, notebook_id):
    first = _upload(repo, notebook_id)
    second = _upload(repo, notebook_id)
    assert first[0].id == second[0].id, "同 notebook 内相同内容应复用既有源"
    assert len(repo.list_sources(notebook_id)) == 1


def test_different_content_still_creates_a_new_source(repo, notebook_id):
    _upload(repo, notebook_id, content=b"one")
    _upload(repo, notebook_id, content=b"two")
    assert len(repo.list_sources(notebook_id)) == 2


def test_same_content_in_another_notebook_is_not_deduped(
    repo, notebook_id, other_notebook_id
):
    a = _upload(repo, notebook_id)
    b = _upload(repo, other_notebook_id)
    assert a[0].id != b[0].id, "跨 notebook 刻意不去重"
