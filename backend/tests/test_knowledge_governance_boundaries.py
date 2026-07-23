import json
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import KnowledgeUpdate, MergeRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def _insert_source_with_element(repo, notebook_id: str) -> str:
    source_id = f"src-{uuid4().hex[:10]}"
    element_id = f"el-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, 'Doc', 'markdown', 'extracted', 'parsed',
                       'doc.md', '', 0, '', '', 'academic_paper', ?, ?)""",
            (source_id, notebook_id, now, now),
        )
        db.execute(
            """INSERT INTO source_elements
               (id, source_id, element_type, location_label, text, metadata, created_at)
               VALUES (?, ?, 'paragraph', 'p1', 'Engram improves recall.', '{}', ?)""",
            (element_id, source_id, now),
        )
    return source_id


def test_update_knowledge_requires_matching_notebook(repo):
    nb_a = repo.create_notebook(NotebookCreate(name="A"))
    nb_b = repo.create_notebook(NotebookCreate(name="B"))
    obj_b = repo._test_insert_object(nb_b.id, "claim", {"name": "B-only claim"})

    with pytest.raises(KeyError):
        repo.update_knowledge(nb_a.id, obj_b, KnowledgeUpdate(status="deprecated"))

    updated = repo.update_knowledge(nb_b.id, obj_b, KnowledgeUpdate(status="deprecated"))
    assert updated.status == "deprecated"


def test_merge_knowledge_requires_same_notebook(repo):
    nb_a = repo.create_notebook(NotebookCreate(name="A"))
    nb_b = repo.create_notebook(NotebookCreate(name="B"))
    obj_a = repo._test_insert_object(nb_a.id, "claim", {"name": "A claim"})
    obj_b = repo._test_insert_object(nb_b.id, "claim", {"name": "B claim"})

    with pytest.raises(KeyError):
        repo.merge_knowledge(nb_a.id, obj_a, MergeRequest(into_id=obj_b))

    obj_a_2 = repo._test_insert_object(nb_a.id, "claim", {"name": "A claim duplicate"})
    merged = repo.merge_knowledge(nb_a.id, obj_a_2, MergeRequest(into_id=obj_a))
    assert merged.id == obj_a


def test_delete_source_removes_source_knowledge_embeddings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "8")
    repo = SQLiteRepository(Settings())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=8))

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    source_id = _insert_source_with_element(repo, nb.id)
    repo.store_kg(
        nb.id,
        source_id,
        [
            {
                "local_id": "C1",
                "object_type": "concept",
                "payload": {"name": "Engram"},
                "evidence": [{"source_id": source_id, "quoted_span": "Engram"}],
            }
        ],
        [],
    )
    with repo._connect() as db:
        (before,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id = ?",
            (nb.id,),
        ).fetchone()
    assert before == 1

    repo.delete_source(source_id)

    with repo._connect() as db:
        (after,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id = ?",
            (nb.id,),
        ).fetchone()
    assert after == 0
