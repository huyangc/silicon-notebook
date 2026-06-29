# backend/tests/test_batch_ingest.py
import json
import pytest
from pathlib import Path

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services import batch_ingest as bi


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo + FakeEmbedder(embedder_configured=True). 镜像 test_chunk_embed.py。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _make_md_dir(tmp_path, n=2):
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    for i in range(n):
        (d / f"doc{i}.md").write_text(
            f"# Title {i}\n\nBody paragraph {i} " + "x" * 200, encoding="utf-8")
    (d / "sub" / "nested.md").write_text("# Nested\n\nNested body " + "y" * 200, encoding="utf-8")
    (d / "ignore.txt").write_text("ignore me", encoding="utf-8")
    return d


def test_iter_files_filters_and_sorts(tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    files = bi.iter_files(d)
    names = [p.name for p in files]
    assert names == sorted(names)
    assert "ignore.txt" not in names
    assert "nested.md" in names
    assert len([n for n in names if n.endswith(".md")]) == 3


def test_ensure_notebook_owner_defaults_user_local(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    with repo._connect() as db:
        row = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id,)).fetchone()
    assert nb_id.startswith("nb-")
    assert row["created_by"] == "user-local"


def test_ensure_notebook_existing_id_passthrough(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    same = bi.ensure_notebook(repo, nb_id, "ignored-name")
    assert same == nb_id


def test_already_ingested_detects_hash(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    assert bi.already_ingested(repo, nb_id, "deadbeef") is False
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-x", nb_id, "S", "document", "s.md", "/tmp/s.md", 0, "cafe", "", "", "parsed", now, now))
    assert bi.already_ingested(repo, nb_id, "cafe") is True


def test_run_ingest_creates_sources_chunks_embeddings_no_kg(repo, tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    counts = bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    assert counts["uploaded"] == 3 and counts["skipped"] == 0 and counts["failed"] == 0
    with repo._connect() as db:
        def c(sql, *a): return db.execute(sql, a).fetchone()["c"]
        nsrc = c("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", nb_id)
        nel = c("SELECT COUNT(*) c FROM source_elements", )
        nch = c("SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", nb_id)
        nemb = c("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", nb_id)
        nko = c("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", nb_id)
    assert nsrc == 3
    assert nel > 0 and nch > 0
    assert nemb == nch
    assert nko == 0


def test_run_ingest_dedup_skips_on_rerun(repo, tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    files = bi.iter_files(d)
    bi.run_ingest(repo, nb_id, files, workers=1, conc=2)
    counts2 = bi.run_ingest(repo, nb_id, files, workers=1, conc=2)
    assert counts2["uploaded"] == 0 and counts2["skipped"] == 3
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb_id,)).fetchone()["c"]
    assert nsrc == 3


def test_run_kg_disables_fusion_and_rebuilds(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    calls = {}

    def fake_build(nb):
        calls["fusion_flag_during"] = repo.settings.kg_incremental_fusion_enabled
        calls["build_nb"] = nb
        return {"built": ["s1", "s2"], "failed": [], "skipped": []}

    def fake_rebuild(nb):
        calls["rebuild_nb"] = nb
        return 7

    monkeypatch.setattr(repo, "build_notebook_kg", fake_build)
    monkeypatch.setattr(repo, "rebuild_unified_kg", fake_rebuild)
    res = bi.run_kg(repo, nb_id, limit=None, conc=2)
    assert res["extracted"] == 2 and res["failed"] == 0
    assert res["clusters"] == 7
    assert calls["fusion_flag_during"] is False
    assert calls["build_nb"] == nb_id and calls["rebuild_nb"] == nb_id


def test_main_dry_run_lists_files(repo, tmp_path, capsys):
    d = _make_md_dir(tmp_path, n=2)
    rc = bi.main(["ingest", "--input-dir", str(d), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out and "3 files" in out


def test_main_requires_input_dir_for_ingest(repo, capsys):
    rc = bi.main(["ingest"])
    assert rc == 2
    assert "input-dir" in capsys.readouterr().err


def test_main_all_ingests_then_runs_kg(repo, tmp_path, monkeypatch):
    d = _make_md_dir(tmp_path, n=2)
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(SQLiteRepository, "build_notebook_kg",
                        lambda self, nb: {"built": [], "failed": [], "skipped": []})
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg", lambda self, nb: 0)
    rc = bi.main(["all", "--input-dir", str(d), "--notebook-name", "X", "--workers", "1"])
    assert rc == 0
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        row = db.execute("SELECT id FROM notebooks WHERE name='X'").fetchone()
        assert row is not None
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (row["id"],)).fetchone()["c"]
    assert nsrc == 3


def test_run_kg_limit_extracts_subset(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"src-lim-{i}", nb_id, f"S{i}", "document", f"s{i}.md", f"/tmp/s{i}.md",
                 0, f"h{i}", "", "", "parsed", now, now))
    extracted_calls = []
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: extracted_calls.append(sid))
    monkeypatch.setattr(repo, "_set_source_status", lambda *a, **k: None)

    def _no_build(nb):
        raise AssertionError("build_notebook_kg must not be called when limit is set")
    monkeypatch.setattr(repo, "build_notebook_kg", _no_build)
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda nb: 0)

    res = bi.run_kg(repo, nb_id, limit=2, conc=2)
    assert res["extracted"] == 2
    assert len(extracted_calls) == 2          # 只抽前 2 个未抽源(targets[:limit])
