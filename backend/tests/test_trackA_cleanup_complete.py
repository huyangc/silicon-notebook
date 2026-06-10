"""Track A: public notebook-delete path (used by eval `_cleanup`) must leave
ZERO orphan rows, including the non-cascading `knowledge_embeddings` table.

Regression: the refactor of `app/eval/speed.py:_cleanup` replaced an explicit
`DELETE FROM knowledge_embeddings WHERE notebook_id=?` with a single
`repo.delete_notebook(nb_id)` call and assumed FK cascade clears every child
table. But `knowledge_embeddings` has no foreign key (see DDL ~line 300), so
deleting the `notebooks` row leaves orphan embedding rows behind.
"""
import uuid

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_notebook_with_artifacts(repo) -> str:
    """Create a notebook with one source/element plus a knowledge_object,
    knowledge_relation, knowledge_embeddings row and an element_embeddings row.
    Returns the notebook id."""
    nb = repo.create_notebook(NotebookCreate(name=f"cleanup-{uuid.uuid4().hex[:6]}"))
    nb_id = nb.id
    now = _now()
    sid = f"src-{uuid.uuid4().hex[:8]}"
    eid = f"el-{uuid.uuid4().hex[:8]}"
    obj_id = f"obj-{uuid.uuid4().hex[:8]}"
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary,
                created_at, updated_at)
               VALUES (?, ?, 'seed', 'markdown', 'extracted', 'parsed',
                       'seed.md', '', 0, '', '', ?, ?)""",
            (sid, nb_id, now, now),
        )
        db.execute(
            """INSERT INTO source_elements
               (id, source_id, element_type, location_label, text, metadata, created_at)
               VALUES (?, ?, 'paragraph', 'L1-L1', 'hello', '{}', ?)""",
            (eid, sid, now),
        )
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_id, created_at, updated_at)
               VALUES (?, ?, 'concept', 'approved', '', '{}', '[]', ?, ?, ?)""",
            (obj_id, nb_id, sid, now, now),
        )
        db.execute(
            """INSERT INTO knowledge_relations
               (id, notebook_id, source_id, source_object_id, target_object_id,
                edge_type, evidence, created_at)
               VALUES (?, ?, ?, ?, ?, 'relates_to', '[]', ?)""",
            (f"rel-{uuid.uuid4().hex[:8]}", nb_id, sid, obj_id, obj_id, now),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id, notebook_id, vector, created_at)"
            " VALUES (?, ?, '[0.1]', ?)",
            (obj_id, nb_id, now),
        )
        db.execute(
            "INSERT INTO element_embeddings (element_id, source_id, notebook_id, vector, created_at)"
            " VALUES (?, ?, ?, '[0.1]', ?)",
            (eid, sid, nb_id, now),
        )
    return nb_id


def _counts(repo, nb_id: str) -> dict:
    tables = (
        "knowledge_objects",
        "knowledge_relations",
        "knowledge_embeddings",
        "element_embeddings",
        "source_elements",
        "sources",
    )
    out = {}
    with repo._connect() as db:
        for t in tables:
            if t == "source_elements":
                row = db.execute(
                    "SELECT COUNT(*) c FROM source_elements e "
                    "JOIN sources s ON s.id = e.source_id WHERE s.notebook_id = ?",
                    (nb_id,),
                ).fetchone()
            else:
                row = db.execute(
                    f"SELECT COUNT(*) c FROM {t} WHERE notebook_id = ?", (nb_id,)
                ).fetchone()
            out[t] = row["c"]
    return out


def test_seed_actually_inserts_rows(repo):
    """Guard: the seed must really populate every table, else the cleanup
    assertion below would be vacuously true."""
    nb_id = _seed_notebook_with_artifacts(repo)
    before = _counts(repo, nb_id)
    assert before == {
        "knowledge_objects": 1,
        "knowledge_relations": 1,
        "knowledge_embeddings": 1,
        "element_embeddings": 1,
        "source_elements": 1,
        "sources": 1,
    }, before


def test_delete_notebook_leaves_zero_orphans(repo):
    """The public cleanup path used by eval `_cleanup` must remove every
    notebook-scoped row, including non-cascading embedding tables."""
    nb_id = _seed_notebook_with_artifacts(repo)
    # This is exactly the call app/eval/speed.py:_cleanup makes.
    repo.delete_notebook(nb_id)
    after = _counts(repo, nb_id)
    assert all(v == 0 for v in after.values()), f"orphan rows remain: {after}"
