"""Task 12 — ingestion failure boundaries stay frozen (error_policies.json):

- chunk build is best-effort (never aborts extraction);
- background embedding is best-effort (never fails the pipeline);
- extraction failure records failed status + error_message (record_and_continue);
- delete commits DB cleanup before file delete (fs failure can't roll it back);
- metadata augmentation falls back deterministically when the model fails;
- URL sources with local MinerU configured NEVER silently fall back to cloud.
"""
import types
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.source_ingestion import SourceIngestionService  # noqa: F401 — Task 12 gate
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider()
    repository = SQLiteRepository(Settings(), model_provider=provider)
    repository.recording_model_provider = provider
    return repository


@pytest.fixture
def embed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    """本地 MinerU(http) 已配置、云端 token 缺失：URL 来源必须走本地。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://localhost:8888")
    return SQLiteRepository(Settings())


def _element(text):
    return types.SimpleNamespace(
        element_type="paragraph", location_label="p1", text=text, metadata={}
    )


def _seed_queued_source(repo, notebook_id):
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, "Doc", "markdown", "queued", "queued",
             "doc.md", "/tmp/s.md", 0, "", "", "academic_paper", now, now))
    return sid


def _patch_parse(monkeypatch, elements):
    import app.services.parser_chain_execution as parser_execution
    monkeypatch.setattr(
        parser_execution,
        "parse_builtin_source_file",
        lambda *a, **k: elements,
    )


def test_chunk_failure_does_not_abort_extraction(repo, monkeypatch):
    _patch_parse(monkeypatch, [_element("chunk boom body")])
    repo.settings.kg_auto_extract = True
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)

    def boom(source_id):
        raise RuntimeError("chunk build boom")

    monkeypatch.setattr(repo._runtime.source_chunking, "build_chunks_for_source", boom)
    extracted = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda sid: extracted.append(sid),
    )
    repo.process_source(sid)
    assert extracted == [sid], "chunk failure must not skip extraction"
    assert repo.get_source(sid).parse_status == "extracted"


def test_embedding_failure_does_not_fail_pipeline(embed_repo, monkeypatch):
    repo = embed_repo
    _patch_parse(monkeypatch, [_element("embed boom body")])
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)

    class _BoomEmbedder:
        def embed_texts(self, texts):
            raise RuntimeError("embed backend down")

        def embed_query(self, text):
            raise RuntimeError("embed backend down")

        def _ensure(self):
            pass

    bind_all_embedding_clients(repo, _BoomEmbedder())
    repo.process_source(sid)
    src = repo.get_source(sid)
    assert src.parse_status == "extracted"
    with repo._connect() as db:
        (n,) = db.execute(
            "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)
        ).fetchone()
    assert n == 0


def test_extraction_failure_sets_failed_and_persists_error_message(repo, monkeypatch):
    _patch_parse(monkeypatch, [_element("extract boom body")])
    repo.settings.kg_auto_extract = True
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)

    def boom(source_id):
        raise RuntimeError("extract boom")

    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", boom)
    result = repo.process_source(sid)  # record_and_continue: never raises
    assert result.parse_status == "failed"
    src = repo.get_source(sid)
    assert src.parse_status == "failed"
    assert "extract boom" in src.error_message
    assert src.summary == "Parsing failed; see source error."


def test_delete_commits_db_cleanup_before_file_delete(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    out = repo.upload_sources(
        nb.id,
        [UploadedSourceFile(
            file_name="a.md", content_type="text/markdown", content=b"# T\n\nbody",
        )],
        scheduler=lambda sid: None,   # keep it queued; pipeline not needed here
    )
    sid = out[0].id
    order = []

    def probing_delete(file_path):
        with repo._connect() as db:
            row = db.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()
        order.append(("file_delete_saw_row", row is not None))
        raise RuntimeError("fs boom")

    monkeypatch.setattr(repo._runtime.source_files, "delete", probing_delete)
    with pytest.raises(RuntimeError, match="fs boom"):
        repo.delete_source(sid)
    # DB cleanup committed BEFORE the file delete ran, and the fs failure
    # cannot roll it back.
    assert order == [("file_delete_saw_row", False)]
    with repo._connect() as db:
        assert db.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone() is None


def test_delete_locks_source_before_projection_cleanup(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    out = repo.upload_sources(
        nb.id,
        [UploadedSourceFile(
            file_name="a.md", content_type="text/markdown", content=b"# T\n\nbody",
        )],
        scheduler=lambda sid: None,
    )
    sid = out[0].id
    events: list[str] = []
    store = repo._runtime.source_store
    ingestion = repo._runtime.source_ingestion
    original_lock = store.source_exists_for_update_tx
    original_clear = ingestion.clear_source_extraction_state

    def observe_lock(db, source_id):
        events.append("lock")
        return original_lock(db, source_id)

    def observe_clear(*args, **kwargs):
        events.append("clear")
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(store, "source_exists_for_update_tx", observe_lock)
    monkeypatch.setattr(ingestion, "clear_source_extraction_state", observe_clear)

    repo.delete_source(sid)

    assert events[:2] == ["lock", "clear"]


def test_metadata_augmentation_model_failure_falls_back_deterministically(repo):
    nb = repo.create_notebook(NotebookCreate(name="未命名笔记本"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?, 'markdown','extracted','extracted', 'a.md','',0,'',"
            "'Summary text.','academic_paper',?,?)",
            (sid, nb.id, "Bandgap Reference Design", now, now))

    class _BoomLLM:
        configured = True

        def chat_json(self, *a, **k):
            raise RuntimeError("llm down")

    bind_chat_client(repo, "notebook_metadata", _BoomLLM())
    repo._augment_notebook_meta(nb.id)
    got = repo.get_notebook(nb.id)
    assert got.name == "Bandgap Reference Design"      # first title, [:40]
    assert "本笔记本收录了 1 个来源" in got.purpose
    assert ("chat", "notebook_metadata") in repo.recording_model_provider.calls


def test_source_summary_binds_source_summary_workload(repo):
    class _SummaryLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            return '{"summary":"system summary"}'

    bind_chat_client(repo, "source_summary", _SummaryLLM())

    result = repo._runtime.source_ingestion.summarize(
        "Doc", [_element("source body")]
    )

    assert result == "system summary"
    assert ("chat", "source_summary") in repo.recording_model_provider.calls


def test_source_summary_queue_failure_uses_deterministic_fallback(repo):
    from app.services.model_work import ModelQueueTimeout

    class _BusySummary:
        configured = True

        def chat_json(self, *args, **kwargs):
            raise ModelQueueTimeout(support_id="mdl-source-timeout")

    bind_chat_client(repo, "source_summary", _BusySummary())

    result = repo._runtime.source_ingestion.summarize(
        "Doc", [_element("source body")]
    )

    assert result.startswith("1 parsed text element(s).")


def test_url_local_parse_never_falls_back_to_cloud(local_repo, monkeypatch):
    repo = local_repo
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 10, "doc.pdf"),
    )
    res = repo.add_url_sources(
        nb.id, ["https://a/doc.pdf"], scheduler=lambda sid: None
    )
    sid = res.created[0].id

    def fake_download(url, dest, **kw):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with open(dest, "wb") as handle:
            writer.write(handle)

    monkeypatch.setattr(remote_sources, "download_pdf", fake_download)
    monkeypatch.setattr(
        repo.mineru_client, "parse_with_images",
        lambda path, name: (_ for _ in ()).throw(RuntimeError("mineru down")),
    )

    def cloud_forbidden(*a, **k):
        raise AssertionError("local-first URL parse must NEVER touch the cloud")

    monkeypatch.setattr(
        repo.mineru_cloud_client, "parse_url_with_images", cloud_forbidden
    )
    repo.process_source(sid)
    src = repo.get_source(sid)
    # Local MinerU failed → pypdf fallback on the downloaded file (blank page
    # → zero elements) — parsed to empty is surfaced, never a cloud fallback.
    assert src.parse_status == "extracted"
    assert "No extractable text" in src.error_message


def test_elements_land_even_if_summary_llm_fails(repo, monkeypatch):
    """摘要(LLM)在 elements 落地之后:即便 summarize 抛错(模拟 LLM 超时/hang 被打断),
    该源的 source_elements 也已写入——避免 all 导入遇端点抽风集体丢 elements、KG 无从接地。"""
    _patch_parse(monkeypatch, [_element("body text")])
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)

    def _boom(*a, **k):
        raise RuntimeError("summary llm hang")
    monkeypatch.setattr(repo._runtime.source_ingestion, "summarize_source", _boom)

    repo.process_source(sid)   # summarize 抛被 except 记 failed,不外抛

    with repo._connect() as db:
        n_el = db.execute(
            "SELECT COUNT(*) c FROM source_elements WHERE source_id=?", (sid,)
        ).fetchone()["c"]
    assert n_el > 0                                        # elements 已落地(摘要挪到写库之后)
    assert repo.get_source(sid).parse_status == "failed"  # summarize 抛 → 源 failed,但 elements 保住
