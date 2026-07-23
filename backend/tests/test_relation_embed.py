import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.services.retrieval import relation_embed_text
from tests.model_testkit import RecordingModelProvider


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    embedder = FakeEmbedder(dim=16)
    models = RecordingModelProvider(embedding_clients={
        "knowledge_object_embedding": embedder,
        "relation_embedding": embedder,
    })
    r = SQLiteRepository(
        Settings(model_services_config=""), model_provider=models
    )
    return r


def test_relation_embed_text_combines_fields():
    t = relation_embed_text("Regulated Cascode", "derived_from", "Cascode",
                            ["adds a gain stage to boost output resistance"])
    assert "Regulated Cascode" in t and "Cascode" in t
    assert "derived_from" in t
    assert "gain stage" in t


def test_relation_embed_text_truncates_evidence():
    t = relation_embed_text("A", "supports", "B", ["x" * 1000], max_evidence_chars=50)
    # evidence 截断到 50;头部 "A —supports→ B." 不计入截断额度
    assert t.count("x") <= 50


def test_relation_embed_text_handles_empty_evidence():
    t = relation_embed_text("A", "about", "B", [])
    assert t == "A —about→ B."


def test_relation_embeddings_table_schema(repo):
    with repo._connect() as db:
        cols = [r["name"] for r in db.execute("PRAGMA table_info(relation_embeddings)")]
    assert cols == ["relation_id", "notebook_id", "vector", "created_at"]


def test_relation_embeddings_idempotent_reinit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())
    SQLiteRepository(Settings())  # 第二次 init 同库不应抛错(CREATE TABLE IF NOT EXISTS)


def _seed_two_node_relation(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "Regulated Cascode"}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "Cascode"}, "evidence": []},
    ]
    relations = [{"source_local_id": "a", "target_local_id": "b",
                  "edge_type": "derived_from",
                  "evidence": [{"quoted_span": "regulated cascode adds a gain stage"}]}]
    repo.store_kg(nb.id, None, objects, relations)
    return nb


def test_store_kg_embeds_relations(repo):
    nb = _seed_two_node_relation(repo)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM relation_embeddings WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert n == 1  # FakeEmbedder configured → 关系被 embed


def test_backfill_relation_embeddings_fills_missing(repo):
    nb = _seed_two_node_relation(repo)
    with repo._write() as db:
        db.execute("DELETE FROM relation_embeddings WHERE notebook_id=?", (nb.id,))
    repo._runtime.models.calls.clear()
    repo._backfill_relation_embeddings(nb.id)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM relation_embeddings WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert n == 1
    assert ("embedding", "relation_embedding") in repo._runtime.models.calls
