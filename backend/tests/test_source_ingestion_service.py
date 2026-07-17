"""Task 12 — SourceIngestionService: upload/URL/process/delete orchestration
moves behind fresh compatibility hooks (SourcePipelineHooks built per call).

RED-first contracts frozen here:
- upload with a scheduler commits the queued source row BEFORE the callback
  fires and never processes inline; without a scheduler processing is inline;
- parsed source/elements commit before the best-effort chunk build;
- background element embedding overlaps extraction and 'extracted' gates on
  extraction only;
- hooks are built fresh on every call, so post-construction facade
  monkeypatches (_run_extraction) stay observed by the pipeline;
- extraction relink ordering and stale re-extraction cleanup match master;
- pipeline status/event order equals the frozen transaction_phases.json.
"""
import json
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.source_ingestion import SourceIngestionService, SourcePipelineHooks
from app.services.sqlite_repository import SQLiteRepository, _now

ROOT = Path(__file__).resolve().parents[2]
PHASES = (
    ROOT / "backend" / "tests" / "fixtures" / "repository_contract"
    / "transaction_phases.json"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def embed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    return SQLiteRepository(Settings())


class _FakeLLM:
    configured = True

    def __init__(self, payload):
        self._p = payload

    def chat_json(self, messages, response_schema_hint):
        return self._p

    def embed(self, text):
        return [0.0, 0.0]


def _element(text):
    return types.SimpleNamespace(
        element_type="paragraph", location_label="p1", text=text, metadata={}
    )


def _seed_queued_source(repo, notebook_id, file_path="/tmp/s.md"):
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, "Doc", "markdown", "queued", "queued",
             "doc.md", file_path, 0, "", "", "academic_paper", now, now))
    return sid


def test_runtime_wires_ingestion_service_and_builds_fresh_hooks(repo):
    service = repo._runtime.source_ingestion
    assert isinstance(service, SourceIngestionService)
    hooks = repo._source_pipeline_hooks()
    assert isinstance(hooks, SourcePipelineHooks)
    # Hooks are minted per call and never stored on the runtime.
    assert repo._source_pipeline_hooks() is not hooks
    assert not hasattr(repo._runtime, "source_pipeline_hooks")


def test_upload_with_scheduler_commits_queued_before_callback_and_skips_inline(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    inline = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "process_source",
        lambda sid, hooks: inline.append(sid),
    )
    seen = []

    def scheduler(source_id):
        # A fresh connection only sees COMMITTED rows: the queued source must
        # already be durable when the scheduler callback fires.
        with repo._connect() as db:
            row = db.execute(
                "SELECT status, parse_status FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
        seen.append((source_id, row["status"], row["parse_status"]))

    out = repo.upload_sources(
        nb.id,
        [UploadedSourceFile(
            file_name="a.md", content_type="text/markdown", content=b"# T\n\nbody",
        )],
        scheduler=scheduler,
    )
    assert [s.id for s in out] == [seen[0][0]]
    assert seen[0][1:] == ("queued", "queued")
    assert inline == []
    assert out[0].parse_status == "queued"


def test_upload_without_scheduler_processes_inline(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "process_source",
        lambda sid, hooks: calls.append(sid),
    )
    out = repo.upload_sources(
        nb.id,
        [UploadedSourceFile(
            file_name="a.md", content_type="text/markdown", content=b"# T\n\nbody",
        )],
    )
    assert calls == [out[0].id]


def test_parsed_source_and_elements_commit_before_chunk_build(repo, monkeypatch):
    import app.services.sqlite_repository as facade_mod
    monkeypatch.setattr(
        facade_mod, "parse_source_file", lambda *a, **k: [_element("chunk body " * 40)]
    )
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)
    observed = {}

    def probe(source_id):
        with repo._connect() as db:
            n = db.execute(
                "SELECT COUNT(*) c FROM source_elements WHERE source_id=?",
                (source_id,),
            ).fetchone()["c"]
            status = db.execute(
                "SELECT parse_status FROM sources WHERE id=?", (source_id,)
            ).fetchone()["parse_status"]
        observed["at_chunk_time"] = (n, status)

    monkeypatch.setattr(
        repo._runtime.source_chunking, "build_chunks_for_source", probe
    )
    repo.process_source(sid)
    assert observed["at_chunk_time"] == (1, "parsed")


class _ElementBlockingEmbedder:
    """Blocks ONLY element-embedding worker threads (named 'emb-el*'), so KG
    object embedding inside extraction proceeds while element embedding is
    held — proving 'extracted' waits for extraction only."""

    def __init__(self, dim=8):
        self.dim = dim
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_texts(self, texts):
        if threading.current_thread().name.startswith("emb-el"):
            self.entered.set()
            self.release.wait(15)
        return [[0.1] * self.dim for _ in texts]

    def embed_query(self, text):
        return [0.0] * self.dim

    def _ensure(self):
        pass


def test_background_embedding_overlaps_extraction_and_extracted_gates_on_extraction(
    embed_repo, tmp_path
):
    repo = embed_repo
    md = tmp_path / "doc.md"
    md.write_text("# Doc\n\nEngram is a memory architecture.\n", encoding="utf-8")
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id, file_path=str(md))
    repo.settings.kg_auto_extract = True
    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []}))
    emb = _ElementBlockingEmbedder()
    repo.embedder = emb

    done = threading.Event()

    def run():
        try:
            repo.process_source(sid)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    assert emb.entered.wait(10), "background element-embedding never started"

    deadline = time.time() + 10
    reached = False
    while time.time() < deadline:
        if repo.get_source(sid).parse_status == "extracted":
            reached = True
            break
        time.sleep(0.05)
    assert not emb.release.is_set(), "precondition: embedding still blocked"
    assert reached, "'extracted' must not wait for the background element embed"

    emb.release.set()
    assert done.wait(15), "process_source did not finish after releasing embedder"
    with repo._connect() as db:
        (n,) = db.execute(
            "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)
        ).fetchone()
    assert n >= 1, "element embeddings must persist once the pipeline completes"


def test_fresh_hooks_preserve_post_construction_component_monkeypatch(
    repo, monkeypatch
):
    import app.services.sqlite_repository as facade_mod
    monkeypatch.setattr(
        facade_mod, "parse_source_file", lambda *a, **k: [_element("body text")]
    )
    repo.settings.kg_auto_extract = True
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)
    calls = []
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: calls.append(sid))
    repo.process_source(sid)
    assert calls == [sid], "hooks must re-resolve the component seam on every call"
    assert repo.get_source(sid).parse_status == "extracted"


def test_extraction_relink_ordering_and_stale_source_cleanup_match_master(repo):
    """Relink edges are proposed BEFORE store_kg (same review_status/source_id
    remap as LLM edges) and a re-extraction replaces — never duplicates —
    the source's prior objects."""
    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [
            {"local_id": "a", "type": "Concept", "name": "Engram",
             "evidence": "Engram is a memory architecture"},
            {"local_id": "b", "type": "Claim", "name": "Engram improves perplexity",
             "evidence": "Engram improves perplexity"},
        ],
        "edges": []}))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?, 'markdown','extracted','parsed', 'doc.md','',0,'','',"
            "'academic_paper',?,?)",
            (sid, nb.id, "Doc", now, now))
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"el-{sid}-0001", sid, "paragraph", "p1",
             "Engram is a memory architecture. Engram improves perplexity.",
             "{}", now))
    repo._run_extraction(sid)
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1, f"deterministic relink must reconnect degree-0 nodes: {rels}"
    with repo._connect() as db:
        obj_ids = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchall()}
        (n1,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (sid,)
        ).fetchone()
    assert rels[0]["source_object_id"] in obj_ids
    assert rels[0]["target_object_id"] in obj_ids

    repo._run_extraction(sid)  # stale-source cleanup: replaced, not duplicated
    with repo._connect() as db:
        (n2,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (sid,)
        ).fetchone()
    assert n2 == n1
    assert len(repo.relations_for_notebook(nb.id)) == 1


def test_pipeline_status_and_event_order_equals_transaction_phases(repo, monkeypatch):
    frozen = json.loads(PHASES.read_text(encoding="utf-8"))["process_source"]
    assert frozen["sequence"] == [
        "set parsing",
        "parse outside transaction",
        "replace elements and source-derived state in one write",
        "set parsed",
        "best-effort chunk build",
        "background embedding",
        "foreground extraction",
        "set extracted",
        "join embedding thread",
        "enqueue existing-index fold",
    ]

    import app.services.sqlite_repository as facade_mod
    monkeypatch.setattr(
        facade_mod, "parse_source_file", lambda *a, **k: [_element("event body")]
    )
    repo.settings.kg_auto_extract = True
    monkeypatch.setattr(
        repo._runtime.source_ingestion, "run_extraction", lambda sid: None
    )
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _seed_queued_source(repo, nb.id)

    events = []
    original_emit = repo.event_log.emit
    monkeypatch.setattr(
        repo.event_log,
        "emit",
        lambda payload, **kw: (events.append(dict(payload)), original_emit(payload, **kw))[0],
    )
    repo.process_source(sid)

    labels = []
    for event in events:
        if event.get("kind") == "status":
            labels.append(f"status:{event['status']}")
        elif event.get("kind") == "pipeline":
            labels.append(f"pipeline:{event['stage']}:{event['status']}")

    def index(label):
        assert label in labels, (label, labels)
        return labels.index(label)

    # Frozen order: parsing → parse → parsed → embed start (background) →
    # extracting → extract → extracted → embed joined → pipeline done last.
    assert index("status:parsing") < index("pipeline:parse:start")
    assert index("pipeline:parse:start") < index("pipeline:parse:done")
    assert index("pipeline:parse:done") < index("status:parsed")
    assert index("status:parsed") < index("pipeline:embed:start")
    assert index("pipeline:embed:start") < index("status:extracting")
    assert index("status:extracting") < index("pipeline:extract:start")
    assert index("pipeline:extract:start") < index("pipeline:extract:done")
    assert index("pipeline:extract:done") < index("status:extracted")
    assert index("pipeline:embed:done") < index("pipeline:pipeline:done")
    assert labels[-1] == "pipeline:pipeline:done"


def test_make_persist_image_enforces_guardrails(monkeypatch, tmp_path):
    # facade 工厂：超尺寸/超张数被挡；关配置时整体返回 None。
    from app.core.config import Settings
    from app.services.knowhow.assets import AssetService

    class _Repo:
        storage_dir = tmp_path
        def __init__(self): self.saved = []
        def insert_notebook_asset(self, nb, fn, mime, size, by, source_id=None):
            aid = f"a{len(self.saved)}"; self.saved.append(aid); return aid
        def get_notebook_asset(self, aid): return {"id": aid, "notebook_id": "nb", "mime": "image/png"}

    # 直接测工厂逻辑：构造与 facade._make_persist_image 等价的闭包工厂
    from app.services.source_image_persist import make_persist_image_factory  # Task 引入的纯函数
    repo = _Repo()
    settings = Settings(MINERU_RETURN_IMAGES="1", MINERU_MAX_IMAGE_BYTES="10",
                        MINERU_MAX_IMAGES_PER_SOURCE="1")
    factory = make_persist_image_factory(settings, lambda: AssetService(repo))
    persist = factory("nb", "src-1", "u")
    assert persist(b"x" * 5, "a.png") == "a0"      # 通过
    assert persist(b"x" * 50, "b.png") is None      # 超尺寸被挡
    assert persist(b"x" * 5, "c.png") is None        # 超张数(上限1)被挡

    off = Settings(MINERU_RETURN_IMAGES="0")
    assert make_persist_image_factory(off, lambda: AssetService(repo))("nb", "s", "u") is None


def test_process_source_persists_mineru_local_image_to_source_assets(tmp_path, monkeypatch):
    """Task 8 end-to-end: an image block returned by (fake) local MinerU,
    flowing through the REAL facade (process_source -> make_persist_image ->
    parse_file -> parse_pdf -> mineru_content_list_to_elements ->
    AssetService.save_source_image), actually lands as a notebook_assets row
    tagged with this source's id AND a real file on disk with matching
    bytes — the behavior this whole task exists to wire up, beyond the
    guardrail-only unit test above."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://localhost:8888")
    repo = SQLiteRepository(Settings())

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "pdf", "queued", "queued",
             "doc.pdf", "/tmp/doc.pdf", 0, "", "", "academic_paper", now, now))

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    monkeypatch.setattr(
        repo.mineru_client, "parse_with_images",
        lambda path, name: (
            [{"type": "image", "img_path": "fig1.png",
              "image_caption": ["Figure 1."], "page_idx": 0}],
            {"fig1.png": png_bytes},
        ),
    )
    repo.process_source(sid)

    detail = repo.get_source(sid)
    assert detail.parse_status == "extracted"
    asset_ids = repo.source_asset_ids(sid)
    assert len(asset_ids) == 1
    asset = repo.get_notebook_asset(asset_ids[0])
    assert asset["notebook_id"] == nb.id

    from app.services.knowhow.assets import AssetService
    on_disk = AssetService(repo).path_for(asset)
    assert on_disk.is_file()
    assert on_disk.read_bytes() == png_bytes

    elements = repo.source_elements(sid)
    image_el = next(e for e in elements if e.element_type == "image")
    assert image_el.metadata.get("asset_id") == asset_ids[0]


def _seed_source_with_mineru_image(tmp_path, monkeypatch):
    """Shared setup (Task 8's e2e style): a real facade + a queued pdf source
    whose (fake) local MinerU parse yields one image block, so process_source
    persists exactly one notebook_assets row + on-disk file. Used by both
    Task 9 tests below (re-parse cleanup / delete cleanup) to get to a
    just-parsed source with a real image asset in one shared call."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_MODE", "http")
    monkeypatch.setenv("MINERU_API_URL", "http://localhost:8888")
    repo = SQLiteRepository(Settings())

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "pdf", "queued", "queued",
             "doc.pdf", "/tmp/doc.pdf", 0, "", "", "academic_paper", now, now))

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    monkeypatch.setattr(
        repo.mineru_client, "parse_with_images",
        lambda path, name: (
            [{"type": "image", "img_path": "fig1.png",
              "image_caption": ["Figure 1."], "page_idx": 0}],
            {"fig1.png": png_bytes},
        ),
    )
    return repo, sid


def test_process_source_reparse_clears_stale_source_images(tmp_path, monkeypatch):
    """Task 9: re-parsing the SAME source (parse_status already 'extracted')
    must not accumulate MinerU image assets across runs. process_source now
    calls delete_source_images(source_id) before the new parse persists its
    own images, so a second process_source call leaves exactly ONE asset row
    (the fresh one) — never two (stale + fresh) — and the stale row/file are
    actually gone, not merely uncounted."""
    repo, sid = _seed_source_with_mineru_image(tmp_path, monkeypatch)

    repo.process_source(sid)
    first_ids = repo.source_asset_ids(sid)
    assert len(first_ids) == 1

    from app.services.knowhow.assets import AssetService
    asset_svc = AssetService(repo)
    first_asset = repo.get_notebook_asset(first_ids[0])
    first_path = asset_svc.path_for(first_asset)
    assert first_path.is_file()

    # Re-parse the same source (e.g. user clicks "重新解析"). Without the
    # Task 9 cascade-delete this would append a second row/file and double
    # the count.
    repo.process_source(sid)
    second_ids = repo.source_asset_ids(sid)
    assert len(second_ids) == 1
    assert second_ids != first_ids  # old row replaced, not appended to

    assert repo.get_notebook_asset(first_ids[0]) is None  # stale row gone
    assert not first_path.is_file()  # stale file gone, not just orphaned


def test_delete_source_cleans_images(tmp_path, monkeypatch):
    """Task 9: deleting a source must cascade-delete its MinerU image assets
    (notebook_assets rows + on-disk files) alongside its elements/file,
    otherwise every deleted source with embedded images leaves orphaned
    asset rows and files behind forever."""
    repo, sid = _seed_source_with_mineru_image(tmp_path, monkeypatch)
    repo.process_source(sid)
    asset_ids = repo.source_asset_ids(sid)
    assert len(asset_ids) == 1

    from app.services.knowhow.assets import AssetService
    asset = repo.get_notebook_asset(asset_ids[0])
    on_disk = AssetService(repo).path_for(asset)
    assert on_disk.is_file()

    repo.delete_source(sid)

    assert repo.source_asset_ids(sid) == []
    assert repo.get_notebook_asset(asset_ids[0]) is None
    assert not on_disk.is_file()


def _seed_queued_pdf(repo, tmp_path):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb.id, "Doc", "pdf", "queued", "queued",
             "doc.pdf", "/tmp/doc.pdf", 0, "", "", "academic_paper", now, now))
    return nb, sid


def test_upload_file_uses_cloud_when_only_cloud_configured(tmp_path, monkeypatch):
    """本地 MinerU 未配置 + 云端已配 → 上传文件走云端 parse_file_with_images，
    图片作为 notebook_assets 落地(与本地路径同一持久化闭包)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_MODE", raising=False)          # 本地 off → not configured
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")             # 云端 configured
    repo = SQLiteRepository(Settings())
    nb, sid = _seed_queued_pdf(repo, tmp_path)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    monkeypatch.setattr(
        repo.mineru_cloud_client, "parse_file_with_images",
        lambda path, data_id="": (
            [{"type": "image", "img_path": "fig1.png",
              "image_caption": ["Figure 1."], "page_idx": 0}],
            {"fig1.png": png},
        ),
    )
    repo.process_source(sid)

    assert repo.get_source(sid).parse_status == "extracted"
    asset_ids = repo.source_asset_ids(sid)
    assert len(asset_ids) == 1
    elements = repo.source_elements(sid)
    assert any(e.element_type == "image" for e in elements)


def test_upload_file_cloud_error_falls_back_to_pypdf(tmp_path, monkeypatch):
    """云端上传解析抛错 → 回落 pypdf 纯文本，摄取不中断(仍产出文本 elements)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_MODE", raising=False)
    monkeypatch.delenv("MINERU_API_URL", raising=False)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")
    repo = SQLiteRepository(Settings())
    nb, sid = _seed_queued_pdf(repo, tmp_path)

    def _boom(path, data_id=""):
        raise RuntimeError("云端 500")
    monkeypatch.setattr(repo.mineru_cloud_client, "parse_file_with_images", _boom)
    # pypdf 回落读取真实文件：写一个最小 PDF 到 source.file_path
    import pypdf, io as _io
    writer = pypdf.PdfWriter(); writer.add_blank_page(width=200, height=200)
    real_pdf = tmp_path / "real.pdf"
    with real_pdf.open("wb") as fh: writer.write(fh)
    with repo._write() as db:
        db.execute("UPDATE sources SET file_path=? WHERE id=?", (str(real_pdf), sid))

    repo.process_source(sid)   # 不抛：云端错 → pypdf 兜底
    assert repo.get_source(sid).parse_status == "extracted"
    assert repo.source_asset_ids(sid) == []   # 空白页无图片资产
