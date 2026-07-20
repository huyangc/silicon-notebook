import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


def test_kg_llm_configured_default_false(monkeypatch):
    for k in ("KG_LLM_BASE_URL", "KG_LLM_API_KEY", "KG_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).kg_llm_configured is False
    monkeypatch.setenv("KG_LLM_BASE_URL", "https://kg.example")
    monkeypatch.setenv("KG_LLM_API_KEY", "k")
    monkeypatch.setenv("KG_LLM_MODEL", "kg-extract-fast")
    assert Settings(_env_file=None).kg_llm_configured is True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None)); r.embedder = FakeEmbedder(dim=16); return r


def test_kg_llm_client_falls_back_to_main_when_unset(repo):
    sentinel = object()
    repo.llm_client = sentinel
    assert repo.kg_llm_client is sentinel


def test_extraction_passes_kg_llm_client_to_extract_graph(repo, monkeypatch):
    import app.services.kg_ingest as kg_ingest
    captured = {}
    def _fake(client, *a, **k):
        captured["client"] = client
        return type("G", (), {
            "objects": [], "relations": [],
            "total_windows": 0, "failed_windows": 0,
            "windows_skipped": 0, "concepts_dropped": 0, "claims_dropped": 0,
        })()
    monkeypatch.setattr(kg_ingest, "extract_graph", _fake)
    monkeypatch.setattr(kg_ingest, "build_records", lambda *a, **k: ([], []))
    kg_stub = type("KG", (), {"configured": True})()
    repo._kg_llm_client = kg_stub
    repo.llm_client = type("Main", (), {"configured": True})()
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", ("src-x", nb.id, "T", "md", "extracted", "extracted", now, now))
    monkeypatch.setattr(repo, "source_elements", lambda sid: [])
    monkeypatch.setattr(repo._runtime.source_files, "read_source_text", lambda path, elements: "MoE 是一种架构。")
    repo._run_extraction("src-x")
    assert captured.get("client") is kg_stub


def test_extraction_accepts_explicit_task_scoped_kg_client(repo, monkeypatch):
    import app.services.kg_ingest as kg_ingest
    captured = {}

    def _fake(client, *args, **kwargs):
        captured["client"] = client
        return type("G", (), {
            "objects": [], "relations": [],
            "total_windows": 0, "failed_windows": 0,
            "windows_skipped": 0, "concepts_dropped": 0, "claims_dropped": 0,
        })()

    monkeypatch.setattr(kg_ingest, "extract_graph", _fake)
    monkeypatch.setattr(kg_ingest, "build_records", lambda *a, **k: ([], []))
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "kg_llm",
        lambda: (_ for _ in ()).throw(AssertionError("default resolver used")),
    )
    explicit_client = type("TaskKg", (), {"configured": True})()
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("src-explicit", nb.id, "T", "md", "extracted", "extracted", now, now),
        )
    monkeypatch.setattr(repo, "source_elements", lambda sid: [])
    monkeypatch.setattr(
        repo._runtime.source_files,
        "read_source_text",
        lambda path, elements: "MoE 是一种架构。",
    )

    repo._runtime.source_ingestion.run_extraction(
        "src-explicit", kg_client=explicit_client
    )

    assert captured.get("client") is explicit_client
