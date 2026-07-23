"""NotebookSummary.ask_available —— 该库能否在任一可用问答模式下产出有据回答。

前端据此禁用"空库"的问答输入框(codex PR#334 评审:判定所需的隐藏 knowhow chunk、
confirmed memory、base+overlay 配置前端都看不到,故由后端权威计算)。这里钉住每条
证据线索各自都能让 ask_available 为真,尤其 P1-1:无可见来源、零 knowledge_objects
但有可检索 chunk 的 knowhow 表 —— 它可对话,绝不能被判为不可用。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-07-20T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ask_available.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _add_source(db, notebook_id, source_id, source_type):
    db.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (source_id, notebook_id, source_id, source_type, "ready", NOW, NOW),
    )


def _add_chunk(db, notebook_id, source_id, chunk_id):
    db.execute(
        "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
        "VALUES (?,?,?,?,?)",
        (chunk_id, notebook_id, source_id, "some retrievable text", NOW),
    )


def _add_kg_object(db, notebook_id, object_id):
    db.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (object_id, notebook_id, "concept", "approved", NOW, NOW),
    )


def _add_memory(db, notebook_id, user_id, memory_id, status):
    db.execute(
        "INSERT INTO memory_items "
        "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, notebook_id, user_id, "external_agent", status,
         "t", "c", NOW, NOW),
    )


def test_empty_notebook_is_not_ask_available(repo):
    """报告的 bug 本体:全空的新库 —— 无来源/无 chunk/无 KG/无参考库/无 memory —— 该禁。"""
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.get_notebook(nb.id).ask_available is False


def test_visible_source_with_chunk_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="doc"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-doc", "document")
        _add_chunk(db, nb.id, "s-doc", "c-doc")
    assert repo.get_notebook(nb.id).ask_available is True


def test_knowhow_only_chunks_are_ask_available(repo):
    """P1-1 反向护栏:无锚点列的 knowhow 表产出可检索 chunk 但零 knowledge_objects,
    且其源是 source_type='knowhow' 的隐藏合成源(被 visible_source_count 排除)。
    visible_sources=0 且 kg_ready=False,但 chunk 模式能答 —— 必须 ask_available=True。"""
    nb = repo.create_notebook(NotebookCreate(name="knowhow-only"))
    with repo._write() as db:
        _add_source(db, nb.id, "s-knowhow", "knowhow")
        _add_chunk(db, nb.id, "s-knowhow", "c-knowhow")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["sources"] == 0   # 可见来源确实为 0
    assert summary.kg_ready is False        # 确实无 knowledge_objects
    assert summary.ask_available is True     # 但仍可对话


def test_kg_ready_notebook_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="kg"))
    with repo._write() as db:
        _add_kg_object(db, nb.id, "ko-1")
    summary = repo.get_notebook(nb.id)
    assert summary.kg_ready is True
    assert summary.ask_available is True


def test_mounted_base_with_kg_is_ask_available(repo):
    """本库无自有内容,但挂载了一个有 KG 的参考库 —— 严格模式可借用,故可对话。"""
    base = repo.create_notebook(NotebookCreate(name="ref"))
    with repo._write() as db:
        _add_kg_object(db, base.id, "ko-base")
    repo.mark_notebook_base(base.id)
    nb = repo.create_notebook(NotebookCreate(name="mounts-base"))
    repo.replace_notebook_bases(nb.id, [base.id], "user-local")
    summary = repo.get_notebook(nb.id)
    assert summary.base_kg_available is True
    assert summary.ask_available is True


def test_confirmed_memory_is_ask_available(repo):
    nb = repo.create_notebook(NotebookCreate(name="mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-1", "confirmed")
    assert repo.get_notebook(nb.id).ask_available is True


def test_candidate_only_memory_is_not_ask_available(repo):
    """只有 candidate memory(无来源/KG/参考库)—— candidate 不作证据,该禁。
    钉住 ask_available 用的是 confirmed-only 判定,而非 counts["memories"](含候选)。"""
    nb = repo.create_notebook(NotebookCreate(name="candidate-mem"))
    with repo._write() as db:
        _add_memory(db, nb.id, repo.current_user().id, "m-cand", "candidate")
    summary = repo.get_notebook(nb.id)
    assert summary.counts["memories"] == 1   # 计数看得到候选
    assert summary.ask_available is False     # 但候选不让对话可用
