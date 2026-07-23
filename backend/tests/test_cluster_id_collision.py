"""TDD regression for the production crash: concept_clusters.id (and other
surrogate row ids) generated as f"{prefix}-{uuid4().hex[:10]}" (40 bits).
_write_cluster_map_streamed inserts ONE ROW PER CLUSTER MEMBER; on a giant
notebook (millions of member rows) the birthday paradox on a 40-bit space
(~50% collision at ~1.3M rows, ~certain at 5M) causes
`sqlite3.IntegrityError: UNIQUE constraint failed: concept_clusters.id`.

Fix: all short random surrogate ids in sqlite_repository.py now go through
_new_id(prefix) = f"{prefix}-{uuid4().hex}" (full 128-bit uuid hex),
collision-free at any realistic scale.
"""
import re

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _new_id
from tests.model_testkit import bind_all_embedding_clients

_FULL_ID_RE = re.compile(r"^cc-[0-9a-f]{32}$")


def test_new_id_is_full_128bit_uuid_not_truncated():
    """RED (format/entropy): guards the root cause directly. The old
    f"cc-{uuid4().hex[:10]}" pattern produces only 10 hex chars (40 bits);
    _new_id must produce the full 32 hex chars (128 bits)."""
    new_id = _new_id("cc")
    assert _FULL_ID_RE.match(new_id), f"expected cc-<32 hex chars>, got {new_id!r}"


def test_new_id_is_unique_across_many_calls():
    ids = {_new_id("cc") for _ in range(2000)}
    assert len(ids) == 2000
    assert all(re.match(r"^cc-[0-9a-f]{32}$", i) for i in ids)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_rebuild_unified_kg_writes_full_length_concept_cluster_ids(repo):
    """RED (functional — the real crash path): concept_clusters rows written by
    rebuild_unified_kg -> _write_cluster_map_streamed must carry full-length,
    collision-proof ids. Two sources sharing a concept name (different casing)
    is enough to make the cross-doc clusterer actually persist concept_clusters
    rows (mirrors test_cross_doc_merge.test_rebuild_keeps_concept_behavior)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "cascode", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "Cascode", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id, force=True)

    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM concept_clusters WHERE notebook_id=?", (nb.id,)
        ).fetchall()
    ids = [r["id"] for r in rows]
    assert ids, "expected at least one concept_clusters row to be written"
    assert all(_FULL_ID_RE.match(i) for i in ids), ids
    assert len(ids) == len(set(ids))  # no collisions among the fresh ids
