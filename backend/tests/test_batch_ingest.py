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


def test_ensure_notebook_default_owner_is_admin(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")   # owner defaults to the admin user
    with repo._connect() as db:
        cb = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id,)).fetchone()["created_by"]
        role = db.execute("SELECT role FROM users WHERE id=?", (cb,)).fetchone()["role"]
    assert role == "admin"          # 归属 admin 用户(语义),不依赖 created_by 是否字面 user-local


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

    def fake_build(nb, *, progress=None):
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


def test_main_requires_notebook_name_when_creating(repo, tmp_path, capsys):
    """新建库(ingest 无 --notebook-id)必须显式 --notebook-name,不再默认用目录名。"""
    d = _make_md_dir(tmp_path, n=1)
    rc = bi.main(["ingest", "--input-dir", str(d)])
    assert rc == 2
    assert "notebook-name" in capsys.readouterr().err


def test_main_all_ingests_then_runs_kg(repo, tmp_path, monkeypatch):
    """`all` 现在走 run_all(process_source/extract_source + rebuild_unified_kg),
    不再走 build_notebook_kg。无向量模式下抽取 no-op,但 parse 流程跑通,3 个 source 建成。"""
    d = _make_md_dir(tmp_path, n=2)
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(SQLiteRepository, "_run_extraction", lambda self, sid: None)
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg", lambda self, nb: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)
    rc = bi.main(["all", "--input-dir", str(d), "--notebook-name", "X", "--workers", "1",
                  "--allow-no-embed"])
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

    repo.settings.kg_llm_base_url = "http://kg.example"
    repo.settings.kg_llm_api_key = "k"
    repo.settings.kg_llm_model = "kg-model"
    res = bi.run_kg(repo, nb_id, limit=2, conc=2)
    assert res["extracted"] == 2
    assert len(extracted_calls) == 2          # 只抽前 2 个未抽源(targets[:limit])


class _StubLLM:
    configured = True


def _seed_sources(repo, nb_id, n, prefix):
    now = "2026-01-01T00:00:00"
    sids = [f"{prefix}-{i}" for i in range(n)]
    with repo._write() as db:
        for i, sid in enumerate(sids):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, f"S{i}", "document", f"s{i}.md", f"/tmp/s{i}.md",
                 0, f"h{i}", "", "", "parsed", now, now))
    return sids


def test_build_notebook_kg_concurrent_reports_progress(repo, monkeypatch):
    """build_notebook_kg 跨源并发抽取(全局 job 池),逐源回调进度;全部成功。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-conc")
    sids = _seed_sources(repo, nb_id, 6, "src-c")
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: None)
    monkeypatch.setattr(repo, "_set_source_status", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", lambda nb: None)
    monkeypatch.setattr(repo, "relink_notebook_kg", lambda nb: 0)
    seen = []
    out = repo.build_notebook_kg(nb_id, progress=lambda i, n, sid, ok: seen.append((i, n, sid, ok)))
    assert sorted(out["built"]) == sorted(sids) and out["failed"] == []
    assert len(seen) == len(sids)
    assert {n for _, n, _, _ in seen} == {len(sids)}              # 总数稳定 = N
    assert all(ok for *_, ok in seen)
    assert sorted(i for i, *_ in seen) == list(range(1, len(sids) + 1))  # 进度 i 覆盖 1..N


def test_build_notebook_kg_isolates_source_failure(repo, monkeypatch):
    """单源抽取异常被隔离:计入 failed,其余照常 built。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-iso")
    sids = _seed_sources(repo, nb_id, 3, "src-i")
    bad = sids[1]

    def _extract(sid):
        if sid == bad:
            raise RuntimeError("boom")
    monkeypatch.setattr(repo, "_run_extraction", _extract)
    monkeypatch.setattr(repo, "_set_source_status", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", lambda nb: None)
    monkeypatch.setattr(repo, "relink_notebook_kg", lambda nb: 0)
    out = repo.build_notebook_kg(nb_id)
    assert bad in out["failed"] and len(out["built"]) == 2


def test_ensure_notebook_explicit_owner(repo):
    u = repo.create_user("a00123456", "pw123456")
    assert u.id != "user-local"
    nb_id = bi.ensure_notebook(repo, None, "nb", owner="a00123456")
    nb_id2 = bi.ensure_notebook(repo, None, "nb2", owner="A00123456")  # 大小写不敏感
    with repo._connect() as db:
        cb = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id,)).fetchone()["created_by"]
        cb2 = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id2,)).fetchone()["created_by"]
    assert cb == u.id and cb2 == u.id


def test_ensure_notebook_unknown_owner_errors(repo):
    with pytest.raises(SystemExit):
        bi.ensure_notebook(repo, None, "nb", owner="a00999999")


def test_arg_parser_has_owner():
    args = bi.build_arg_parser().parse_args(["ingest", "--input-dir", "x", "--owner", "a00123456"])
    assert args.owner == "a00123456"


def test_main_refuses_silent_no_embed(repo, tmp_path, monkeypatch, capsys):
    d = _make_md_dir(tmp_path, n=1)
    monkeypatch.setenv("EMBED_PROVIDER", "")     # main 自建 repo → embedder 未配
    rc = bi.main(["ingest", "--input-dir", str(d), "--notebook-name", "X"])
    assert rc == 2
    assert "allow-no-embed" in capsys.readouterr().err


def test_main_allows_no_embed_with_flag(repo, tmp_path, monkeypatch):
    d = _make_md_dir(tmp_path, n=1)
    monkeypatch.setenv("EMBED_PROVIDER", "")
    rc = bi.main(["ingest", "--input-dir", str(d), "--notebook-name", "X", "--allow-no-embed"])
    assert rc == 0
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
        nemb = db.execute("SELECT COUNT(*) c FROM chunk_embeddings").fetchone()["c"]
    assert nsrc >= 1 and nemb == 0               # 显式确认的无向量导入


def test_run_kg_limit_requires_llm(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    with pytest.raises(RuntimeError):            # 无 KG/主 LLM → --limit 直接报错,不静默
        bi.run_kg(repo, nb_id, limit=1, conc=2)


def test_run_all_pipelines_new_sources(repo, tmp_path, monkeypatch):
    """run_all per-source 流水线:每个新文件建 source + 走 process_source 抽取(extracted=N),
    末尾一次 rebuild_unified_kg。强制 kg_auto_extract 让 process_source 走到 extract。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())   # configured=True → 走 extract 分支
    d = _make_md_dir(tmp_path, n=2)                        # 2 个 docN.md + 1 个 nested.md = 3
    nb_id = bi.ensure_notebook(repo, None, "nb-all")
    extracted = []
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: extracted.append(sid))
    rebuild_calls = []
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb: (rebuild_calls.append(nb), 5)[1])
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, bi.iter_files(d), workers=2, conc=2)

    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 3                              # 每个文件都建了 source
    assert res["new"] == 3 and res["resumed"] == 0
    assert res["extracted"] == 3 and res["failed"] == 0   # 每个都被抽取(process_source→_run_extraction)
    assert len(extracted) == 3
    assert res["clusters"] == 5
    assert rebuild_calls == [nb_id]               # 末尾恰好一次 rebuild


def test_run_all_resumes_existing_without_kg(repo, tmp_path, monkeypatch):
    """已 parse、无 KG 的 source(同 hash 已摄取过)走 extract_source 补抽,不重复新建 source。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-resume")
    d = tmp_path / "docs"
    d.mkdir()
    files = []
    sids = []
    now = "2026-01-01T00:00:00"
    for i in range(3):
        p = d / f"doc{i}.md"
        body = f"# Title {i}\n\nBody {i} " + "z" * 50
        p.write_text(body, encoding="utf-8")
        files.append(p)
        digest = bi.sha256_bytes(p.read_bytes())
        sid = f"src-r-{i}"
        sids.append(sid)
        with repo._write() as db:               # 预置 parsed source,同 hash → already_ingested
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, f"S{i}", "document", p.name, str(p), 0, digest,
                 "", "", "parsed", now, now))
    extracted = []
    monkeypatch.setattr(repo, "extract_source", lambda sid: extracted.append(sid))
    # process_source 不应被调用(全部走 resume 路径);若被调用会因无 elements 抛错并计 failed
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda nb: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, files, workers=2, conc=2)

    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 3                              # 不重复新建
    assert res["new"] == 0 and res["resumed"] == 3
    assert sorted(extracted) == sorted(sids)      # 已存在的全部走 extract_source 补抽
    assert res["extracted"] == 3 and res["failed"] == 0
