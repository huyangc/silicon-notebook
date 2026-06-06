from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _index_names(repo, table):
    with repo._connect() as db:
        return {row["name"] for row in db.execute(f"PRAGMA index_list({table})").fetchall()}


def test_notebook_scale_indexes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())

    assert "idx_sources_notebook_status" in _index_names(repo, "sources")
    assert "idx_source_elements_source" in _index_names(repo, "source_elements")
    assert "idx_knowledge_objects_nb_type_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_nb_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_source" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_relations_nb_source" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_relations_nb_target" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_embeddings_nb" in _index_names(repo, "knowledge_embeddings")
    assert "idx_element_embeddings_nb" in _index_names(repo, "element_embeddings")
