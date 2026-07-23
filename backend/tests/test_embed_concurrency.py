import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.model_provider import RuntimeModelProvider
from app.services.model_registry import ModelServiceDefinition, SystemModelServiceRegistry
from app.services.sqlite_repository import SQLiteRepository, _now


class _EventLog:
    def emit(self, event):
        pass


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
            time.sleep(0.03)
            if self.fail_substr and any(self.fail_substr in text for text in texts):
                raise RuntimeError("batch failed")
            return [[1.0] * self.dim for _ in texts]
        finally:
            with self.lock:
                self.active -= 1

    def embed_query(self, text):
        return [1.0] * self.dim


def _provider(raw, maximum=2):
    service = ModelServiceDefinition(
        id="embed", display_name="向量服务", kind="embedding",
        protocol="openai", base_url="https://embed.example/v1", model="embed-model",
        api_key_env="EMBED_TEST_KEY", api_key="secret", max_concurrency=maximum,
        fingerprint="embed-fingerprint",
    )
    registry = SystemModelServiceRegistry(
        {"embed": service}, {"source_element_embedding": "embed"}
    )
    return RuntimeModelProvider(
        Settings(_env_file=None, event_log_enabled=False, llm_log_enabled=False),
        _EventLog(), registry=registry, embedding_factory=lambda _service: raw,
    )


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")

    def make(raw, maximum=2):
        return SQLiteRepository(
            Settings(model_services_config=""),
            model_provider=_provider(raw, maximum),
        )
    return make


def _insert_source_with_elements(repo, notebook_id, n):
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
               VALUES (?,?,?, 'markdown','parsed','parsed', ?, '', 0, '', '', '', ?, ?)""",
            (sid, notebook_id, "Doc", "doc.md", now, now),
        )
        for i in range(n):
            db.execute(
                """INSERT INTO source_elements (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, 'paragraph', ?, ?, '{}', ?)""",
                (f"el-{i:04d}-{sid}", sid, f"p{i}", f"element text number {i}", now),
            )
    return sid


def test_all_source_batches_share_bound_service_peak(repo_factory):
    raw = _PeakEmbedder()
    repo = repo_factory(raw, maximum=2)
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    source_ids = [_insert_source_with_elements(repo, notebook.id, 40) for _ in range(3)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(repo._embed_source, source_id) for source_id in source_ids]
        for future in futures:
            future.result()

    assert raw.maximum == 2
    with repo._connect() as db:
        persisted = db.execute("SELECT COUNT(*) FROM element_embeddings").fetchone()[0]
    # Admission is shared as well: overload may fail a best-effort batch, but
    # it can never create a second per-source capacity island.
    assert 0 < persisted <= 120


def test_source_failed_batch_isolated_under_scheduler(repo_factory):
    raw = _PeakEmbedder(fail_substr="number 15")
    repo = repo_factory(raw, maximum=3)
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    source_id = _insert_source_with_elements(repo, notebook.id, 30)

    repo._embed_source(source_id)

    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    assert count == 20
    assert raw.maximum == 3
