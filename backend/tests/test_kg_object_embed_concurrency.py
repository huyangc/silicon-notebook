import threading
import time

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider


class _PeakEmbedder:
    configured = True
    dim = 8

    def __init__(self, fail_substr=None):
        self.fail_substr = fail_substr
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def embed_texts(self, texts):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            time.sleep(0.02)
            if self.fail_substr and any(self.fail_substr in text for text in texts):
                raise RuntimeError("batch failed")
            return [[1.0] * self.dim for _ in texts]
        finally:
            with self.lock:
                self.active -= 1

    def embed_query(self, text):
        return [1.0] * self.dim


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    provider = RecordingModelProvider(
        parallelism_by_workload={"knowledge_object_embedding": 2}
    )
    repository = SQLiteRepository(
        Settings(model_services_config=""), model_provider=provider
    )
    repository.recording_model_provider = provider
    return repository


def _items(repo, notebook_id, count):
    rows = []
    for index in range(count):
        name = f"Concept number {index}"
        object_id = repo._test_insert_object(notebook_id, "concept", {"name": name})
        rows.append({"_oid": object_id, "payload": {"name": name}})
    return rows


def test_object_producer_parallelism_comes_from_bound_workload(repo):
    raw = _PeakEmbedder()
    repo.recording_model_provider.embedding_clients = {
        "knowledge_object_embedding": raw
    }
    notebook = repo.create_notebook(NotebookCreate(name="nb"))

    repo._embed_objects_batch(notebook.id, _items(repo, notebook.id, 35))

    assert raw.maximum == 2
    assert ("embedding", "knowledge_object_embedding") in repo.recording_model_provider.calls
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?", (notebook.id,)
        ).fetchone()[0] == 35


def test_object_failed_batch_isolated(repo):
    raw = _PeakEmbedder(fail_substr="number 15")
    repo.recording_model_provider.embedding_clients = {
        "knowledge_object_embedding": raw
    }
    notebook = repo.create_notebook(NotebookCreate(name="nb"))

    repo._embed_objects_batch(notebook.id, _items(repo, notebook.id, 30))

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?", (notebook.id,)
        ).fetchone()[0] == 20
