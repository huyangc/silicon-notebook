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


def test_backfill_chunk_embeddings_missing_only(repo, tmp_path):
    """missing_only=True 只补缺向量的 chunk:返回值==被删数、补全后全有向量、
    未删的行 created_at 不变(证明没重嵌已有的)。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)  # 全量嵌入

    with repo._connect() as db:
        rows = db.execute(
            "SELECT chunk_id, created_at FROM chunk_embeddings WHERE notebook_id=? "
            "ORDER BY chunk_id", (nb_id,)).fetchall()
    all_ids = [r["chunk_id"] for r in rows]
    assert len(all_ids) >= 2
    before_created = {r["chunk_id"]: r["created_at"] for r in rows}

    k = 2
    deleted = all_ids[:k]
    kept = all_ids[k:]
    with repo._write() as db:
        db.executemany("DELETE FROM chunk_embeddings WHERE chunk_id=?",
                       [(cid,) for cid in deleted])
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"] == len(kept)

    n = bi.backfill_chunk_embeddings(repo, nb_id, conc=2, missing_only=True)

    assert n == k                                    # 只处理缺的
    with repo._connect() as db:
        nemb = db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
        nch = db.execute("SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
                         (nb_id,)).fetchone()["c"]
        after = {r["chunk_id"]: r["created_at"] for r in db.execute(
            "SELECT chunk_id, created_at FROM chunk_embeddings WHERE notebook_id=?",
            (nb_id,)).fetchall()}
    assert nemb == nch                               # 补全后所有 chunk 都有向量
    for cid in kept:                                 # 未删的没被重嵌(created_at 不变)
        assert after[cid] == before_created[cid]


def test_backfill_chunk_embeddings_missing_only_noop_when_complete(repo, tmp_path):
    """全有向量时 missing_only=True 返回 0(无缺失则跳过)。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1, conc=2)
    assert bi.backfill_chunk_embeddings(repo, nb_id, conc=2, missing_only=True) == 0


def test_backfill_chunk_embeddings_default_full_reembed(repo, tmp_path):
    """missing_only 默认 False:仍走全量(遍历 source),返回处理的 source 数。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert bi.backfill_chunk_embeddings(repo, nb_id, conc=2) == nsrc   # 默认=全量按 source


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

    def fake_rebuild(nb, progress=None, force=False):
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
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg", lambda self, nb, progress=None, force=False: 0)
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
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda nb, progress=None, force=False: 0)

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


# ── Task 2: embed 子命令 + run_embed ─────────────────────────────────────────

def _seed_node(repo, nb_id, oid):
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "active",
             json.dumps({"name": f"name-{oid}", "definition": "definition text " * 5}),
             "src-x", now, now))


def test_run_embed_fills_missing_chunk_and_node_vectors(repo, tmp_path):
    """run_embed:盘点 → 补缺失 chunk(missing_only)+ 节点向量 → 归零。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    _seed_node(repo, nb_id, "ko-1")
    _seed_node(repo, nb_id, "ko-2")

    with repo._connect() as db:                      # 制造缺失:删 1 个 chunk 向量
        cid = db.execute("SELECT chunk_id FROM chunk_embeddings WHERE notebook_id=? LIMIT 1",
                         (nb_id,)).fetchone()["chunk_id"]
    with repo._write() as db:
        db.execute("DELETE FROM chunk_embeddings WHERE chunk_id=?", (cid,))

    out = bi.run_embed(repo, nb_id, conc=2)

    assert out["chunk_missing_before"] == 1
    assert out["node_missing_before"] == 2           # 两个 node 都还没向量
    assert out["chunks_embedded"] == 1
    assert out["nodes_embedded"] >= 2
    with repo._connect() as db:                      # 缺失归零
        chunk_missing = db.execute(
            "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=? AND NOT EXISTS "
            "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)", (nb_id,)).fetchone()["c"]
        node_missing = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects o WHERE o.notebook_id=? "
            "AND o.status!='deprecated' AND NOT EXISTS "
            "(SELECT 1 FROM knowledge_embeddings e WHERE e.object_id=o.id)",
            (nb_id,)).fetchone()["c"]
    assert chunk_missing == 0 and node_missing == 0


def test_main_embed_end_to_end_zeroes_missing(repo, tmp_path, capsys, monkeypatch):
    """main(['embed','--notebook-id',id]) 端到端:跑完缺失归零,打印 phase=embed。
    main 自建 repo → 用 FakeEmbedder 替 make_embedder,与 fixture repo 同库。"""
    monkeypatch.setattr("app.services.embedding.make_embedder",
                        lambda settings: FakeEmbedder(dim=16))
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1, conc=2)
    with repo._connect() as db:                      # 制造缺失
        cids = [r["chunk_id"] for r in db.execute(
            "SELECT chunk_id FROM chunk_embeddings WHERE notebook_id=?", (nb_id,)).fetchall()]
    with repo._write() as db:
        db.execute("DELETE FROM chunk_embeddings WHERE chunk_id=?", (cids[0],))

    rc = bi.main(["embed", "--notebook-id", nb_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "phase=embed" in out
    with repo._connect() as db:
        chunk_missing = db.execute(
            "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=? AND NOT EXISTS "
            "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)", (nb_id,)).fetchone()["c"]
    assert chunk_missing == 0


def test_main_embed_requires_notebook_id(repo, capsys):
    rc = bi.main(["embed"])
    assert rc == 2
    assert "notebook-id" in capsys.readouterr().err


def test_main_embed_requires_embed_even_with_allow_no_embed(repo, tmp_path, monkeypatch, capsys):
    """embed 子命令就是补向量:EMBED 未配 → return 2,且忽略 --allow-no-embed。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")     # 用配好 EMBED 的 repo 先建库
    monkeypatch.setenv("EMBED_PROVIDER", "")         # main 自建 repo → embedder 未配
    rc = bi.main(["embed", "--notebook-id", nb_id, "--allow-no-embed"])
    assert rc == 2
    assert "EMBED" in capsys.readouterr().err


def test_arg_parser_embed_phase():
    args = bi.build_arg_parser().parse_args(["embed", "--notebook-id", "nb-x"])
    assert args.phase == "embed"


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
                        lambda nb, progress=None, force=False: (rebuild_calls.append(nb), 5)[1])
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


def test_run_all_configures_job_pool_and_restores_embed_conc(repo, tmp_path, monkeypatch):
    """Task 3:run_all 用 scheduler.configure(job_workers=workers) 覆盖 KG_JOB_CONCURRENCY,
    并在 try 内把 repo.settings.embed_concurrency 设为 conc、finally 恢复原值。"""
    from app.services.kg import scheduler as _sched

    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-flags")
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: None)
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda nb, progress=None, force=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    configure_calls = []
    monkeypatch.setattr(_sched, "configure",
                        lambda **kw: configure_calls.append(kw))
    seen_embed_conc = {}
    real_rebuild = repo.rebuild_unified_kg

    def _spy_rebuild(nb, progress=None, force=False):  # rebuild 在 try 内 → 此刻应已被覆盖为 conc
        seen_embed_conc["during"] = repo.settings.embed_concurrency
        return real_rebuild(nb, progress=progress, force=force)
    monkeypatch.setattr(repo, "rebuild_unified_kg", _spy_rebuild)

    orig_embed_conc = repo.settings.embed_concurrency
    try:
        bi.run_all(repo, nb_id, bi.iter_files(d), workers=3, conc=7)
        assert any(c.get("job_workers") == 3 for c in configure_calls)  # 以 job_workers==workers 调过
        assert seen_embed_conc["during"] == 7        # try 内 embed_concurrency 被设为 conc
        assert repo.settings.embed_concurrency == orig_embed_conc       # finally 恢复
    finally:
        _sched.reset()                               # 避免污染全局池


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
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda nb, progress=None, force=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, files, workers=2, conc=2)

    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 3                              # 不重复新建
    assert res["new"] == 0 and res["resumed"] == 3
    assert sorted(extracted) == sorted(sids)      # 已存在的全部走 extract_source 补抽
    assert res["extracted"] == 3 and res["failed"] == 0


# --- _rebuild_progress banner rendering (n==0 => stage banner) ---------------

def test_rebuild_progress_banner_prints_phase_alone(capsys):
    bi._rebuild_progress("concept: streamed 10 objs → 3 seeds", 0, 0)
    out = capsys.readouterr().out
    assert "concept: streamed 10 objs → 3 seeds" in out
    assert "/" not in out          # no i/n rendering for a banner


def test_rebuild_progress_item_prints_ratio(capsys):
    bi._rebuild_progress("concept_desc", 2, 5)
    out = capsys.readouterr().out
    assert "concept_desc" in out and "2/5" in out


# --- run_index real-time per-stage terminal output ---------------------------

def test_run_index_prints_stage_timings(repo, monkeypatch, capsys):
    """run_index must pass an on_stage callback into build_scale_index that
    prints each stage's latency to the terminal in real time — the events
    logger alone doesn't surface progress on a CLI run that can take tens of
    minutes on a large (490k-object) library."""
    nb_id = bi.ensure_notebook(repo, None, "nb")

    def fake_build_scale_index(notebook_id, on_stage=None):
        assert notebook_id == nb_id
        assert on_stage is not None
        for stage, ms in [("gather", 12), ("transition", 3), ("kg_matrix", 5),
                           ("chunk_matrix", 4), ("viz_arrays", 7), ("persist", 9),
                           ("total", 40)]:
            on_stage(stage, ms)
        return {"n_nodes": 2}

    monkeypatch.setattr(repo, "build_scale_index", fake_build_scale_index)
    res = bi.run_index(repo, nb_id)
    out = capsys.readouterr().out
    assert res["indexed_nodes"] == 2
    assert "  [index] gather: 12ms" in out
    assert "  [index] total: 40ms" in out


# --- vectors-to-blob backfill CLI --------------------------------------------

def _seed_json_vector(repo, table, id_col, vid, nb_id, dim=16, created_at="2026-01-01T00:00:00"):
    """Insert a legacy JSON-text vector row directly (bypasses encode_vector,
    simulating a pre-migration row)."""
    vec = [float(i) for i in range(dim)]
    with repo._write() as db:
        db.execute(
            f"INSERT INTO {table} ({id_col},notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (vid, nb_id, json.dumps(vec), created_at),
        )
    return vec


def test_backfill_table_to_blob_converts_json_rows(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    vec = _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    out = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id")
    assert out == {"table": "knowledge_embeddings", "total": 1, "converted": 1, "skipped_bad": 0}

    with repo._connect() as db:
        row = db.execute(
            "SELECT vector, typeof(vector) AS ty, created_at FROM knowledge_embeddings "
            "WHERE object_id=?", ("ko-1",)).fetchone()
    assert row["ty"] == "blob"
    from app.services.vector_index import decode_vector
    assert decode_vector(row["vector"]).tolist() == vec
    assert row["created_at"] == "2026-01-01T00:00:00"  # backfill must not touch created_at


def test_backfill_table_to_blob_idempotent_second_run_noop(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    out1 = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id")
    assert out1["converted"] == 1

    # Re-run: typeof(vector)='text' filter finds 0 rows now (all BLOB) — idempotent.
    out2 = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id")
    assert out2 == {"table": "knowledge_embeddings", "total": 0, "converted": 0, "skipped_bad": 0}


def test_backfill_table_to_blob_batches_across_txn_boundary(repo):
    """batch_size smaller than the row count must still convert every row across
    multiple batched transactions (each _write() call is its own commit)."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(7):
        oid = f"ko-{i}"
        _seed_node(repo, nb_id, oid)
        _seed_json_vector(repo, "knowledge_embeddings", "object_id", oid, nb_id)

    out = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id", batch_size=3)
    assert out["total"] == 7
    assert out["converted"] == 7

    with repo._connect() as db:
        remaining_text = db.execute(
            "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=? "
            "AND typeof(vector)='text'", (nb_id,)).fetchone()["c"]
    assert remaining_text == 0


def test_backfill_table_to_blob_scoped_to_notebook(repo):
    """--notebook-id scopes the conversion; another notebook's JSON rows are untouched."""
    nb_a = bi.ensure_notebook(repo, None, "nb-a")
    nb_b = bi.ensure_notebook(repo, None, "nb-b")
    _seed_node(repo, nb_a, "ko-a")
    _seed_node(repo, nb_b, "ko-b")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-a", nb_a)
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-b", nb_b)

    out = bi._backfill_table_to_blob(repo, nb_a, "knowledge_embeddings", "object_id")
    assert out["converted"] == 1

    with repo._connect() as db:
        ty_a = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                          ("ko-a",)).fetchone()["t"]
        ty_b = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                          ("ko-b",)).fetchone()["t"]
    assert ty_a == "blob"
    assert ty_b == "text"  # untouched — different notebook


def test_backfill_table_to_blob_malformed_row_does_not_loop_forever(repo):
    """A row whose vector text isn't valid JSON must still be moved out of the
    typeof='text' selection set (as an empty-BLOB sentinel) — otherwise a batch
    made entirely of unparseable rows would be re-selected by the same LIMIT
    query forever (regression: an early version left bad rows untouched and
    hung). batch_size=1 forces every row into its own batch so a single bad
    row would be the *entire* batch, maximally exercising this path."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-good")
    _seed_node(repo, nb_id, "ko-bad")
    good_vec = _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-good", nb_id)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)", ("ko-bad", nb_id, "not-valid-json{{{", "2026-01-01T00:00:00"))

    out = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id", batch_size=1)
    assert out["total"] == 2
    assert out["converted"] == 2       # loop terminated after exactly 2 rows, not forever
    assert out["skipped_bad"] == 1

    with repo._connect() as db:
        rows = {r["object_id"]: (r["ty"], r["vector"]) for r in db.execute(
            "SELECT object_id, typeof(vector) AS ty, vector FROM knowledge_embeddings "
            "WHERE notebook_id=?", (nb_id,)).fetchall()}
    assert rows["ko-good"][0] == "blob"
    assert rows["ko-bad"][0] == "blob"      # sentinel moved it out of typeof='text'
    from app.services.vector_index import decode_vector
    assert decode_vector(rows["ko-good"][1]).tolist() == good_vec
    assert decode_vector(rows["ko-bad"][1]) is None   # empty-blob sentinel decodes to None


def test_run_vectors_to_blob_covers_all_embeddings_tables(repo, tmp_path, capsys):
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1, conc=1)  # chunk_embeddings via real path (BLOB already)
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    # Force one chunk_embeddings row back to legacy JSON text to exercise that table too.
    with repo._connect() as db:
        cid = db.execute("SELECT chunk_id FROM chunk_embeddings WHERE notebook_id=? LIMIT 1",
                         (nb_id,)).fetchone()["chunk_id"]
    with repo._write() as db:
        db.execute("UPDATE chunk_embeddings SET vector=? WHERE chunk_id=?",
                   (json.dumps([0.1] * 16), cid))

    out = bi.run_vectors_to_blob(repo, nb_id, all_notebooks=False)
    tables_seen = {t["table"] for t in out["tables"]}
    assert tables_seen == {"chunk_embeddings", "knowledge_embeddings",
                           "element_embeddings", "relation_embeddings"}
    assert out["converted"] >= 2  # the seeded knowledge row + the forced-JSON chunk row

    with repo._connect() as db:
        remaining_text = db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM chunk_embeddings WHERE notebook_id=? AND typeof(vector)='text') + "
            "(SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=? AND typeof(vector)='text') "
            "AS c", (nb_id, nb_id)).fetchone()["c"]
    assert remaining_text == 0
    out_str = capsys.readouterr().out
    assert "[blob] chunk_embeddings:" in out_str
    assert "[blob] knowledge_embeddings:" in out_str


def test_run_vectors_to_blob_requires_notebook_id_or_all(repo):
    with pytest.raises(ValueError):
        bi.run_vectors_to_blob(repo, None, all_notebooks=False)


def test_run_vectors_to_blob_all_notebooks_covers_every_notebook(repo):
    nb_a = bi.ensure_notebook(repo, None, "nb-a")
    nb_b = bi.ensure_notebook(repo, None, "nb-b")
    _seed_node(repo, nb_a, "ko-a")
    _seed_node(repo, nb_b, "ko-b")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-a", nb_a)
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-b", nb_b)

    out = bi.run_vectors_to_blob(repo, None, all_notebooks=True)
    assert out["converted"] >= 2

    with repo._connect() as db:
        ty_a = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                          ("ko-a",)).fetchone()["t"]
        ty_b = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                          ("ko-b",)).fetchone()["t"]
    assert ty_a == "blob" and ty_b == "blob"


def test_main_vectors_to_blob_requires_notebook_or_all(repo, capsys):
    rc = bi.main(["vectors-to-blob"])
    assert rc == 2
    assert "vectors-to-blob" in capsys.readouterr().err


def test_main_vectors_to_blob_end_to_end(repo, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    rc = bi.main(["vectors-to-blob", "--notebook-id", nb_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vectors-to-blob done" in out
    with repo._connect() as db:
        ty = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                        ("ko-1",)).fetchone()["t"]
    assert ty == "blob"


def test_main_vectors_to_blob_does_not_require_embedder_configured(repo, tmp_path, monkeypatch, capsys):
    """Pure format conversion — must work even when EMBED_* is unset (no new
    vectors are computed, only re-encoded), unlike ingest/kg/embed phases."""
    nb_id = bi.ensure_notebook(repo, None, "nb")  # build with EMBED configured
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    monkeypatch.setenv("EMBED_PROVIDER", "")  # main() builds a fresh unconfigured repo
    rc = bi.main(["vectors-to-blob", "--notebook-id", nb_id])
    assert rc == 0
    with repo._connect() as db:
        ty = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                        ("ko-1",)).fetchone()["t"]
    assert ty == "blob"


def test_arg_parser_vectors_to_blob_phase():
    args = bi.build_arg_parser().parse_args(["vectors-to-blob", "--notebook-id", "nb-x"])
    assert args.phase == "vectors-to-blob"
    assert args.all_notebooks is False

    args2 = bi.build_arg_parser().parse_args(["vectors-to-blob", "--all-notebooks"])
    assert args2.all_notebooks is True


def test_backfill_does_not_change_vector_matrix_version_key(repo):
    """_vector_matrix's cache version is (COUNT, MAX(created_at)) — backfill
    rewrites vector bytes in place without touching created_at, so the version
    tuple is unchanged after backfill. This is benign because the content is
    unchanged (same vectors, new encoding), documented in the report."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    with repo._connect() as db:
        ver_before = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
            "FROM knowledge_embeddings WHERE notebook_id=?", (nb_id,)).fetchone()
        ver_before = (ver_before["c"], ver_before["ts"])

    bi.run_vectors_to_blob(repo, nb_id, all_notebooks=False)

    with repo._connect() as db:
        ver_after = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
            "FROM knowledge_embeddings WHERE notebook_id=?", (nb_id,)).fetchone()
        ver_after = (ver_after["c"], ver_after["ts"])
    assert ver_before == ver_after


# --- vectors-to-blob: --workers parallel parse/encode ------------------------

def test_parse_encode_worker_is_module_level_pure_function():
    """_parse_encode must be a top-level function (spawn-safe: picklable, no
    closures) taking (id, vector_raw_text) and returning (id, blob_bytes)."""
    import inspect
    assert inspect.isfunction(bi._parse_encode)
    assert bi._parse_encode.__module__ == bi.__name__
    assert bi._parse_encode.__qualname__ == "_parse_encode"  # not nested/closure


def test_parse_encode_worker_valid_json():
    vid, blob = bi._parse_encode(("ko-1", json.dumps([1.0, 2.0, 3.0])))
    assert vid == "ko-1"
    import numpy as np
    assert np.frombuffer(blob, dtype=np.float32).tolist() == [1.0, 2.0, 3.0]


def test_parse_encode_worker_corrupt_json_returns_sentinel():
    vid, blob = bi._parse_encode(("ko-bad", "not-valid-json{{{"))
    assert vid == "ko-bad"
    assert blob == b""


def test_parse_encode_worker_empty_string_returns_sentinel():
    vid, blob = bi._parse_encode(("ko-empty", ""))
    assert vid == "ko-empty"
    assert blob == b""


def _seed_many_json_vectors(repo, table, id_col, nb_id, n, dim=8, bad_every=None, prefix="ko"):
    """Seed n legacy JSON-text vector rows (plus optional corrupt rows every
    `bad_every`-th index) for parallel-vs-serial comparison tests."""
    rows = []
    with repo._write() as db:
        for i in range(n):
            vid = f"{prefix}-{i}"
            if bad_every and i % bad_every == 0:
                text = "not-valid-json{{{"
            else:
                vec = [float(i), float(i + 1), float(dim - i % dim)]
                text = json.dumps(vec)
            db.execute(
                f"INSERT INTO {table} ({id_col},notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (vid, nb_id, text, "2026-01-01T00:00:00"))
            rows.append(vid)
    return rows


def test_backfill_parallel_output_byte_identical_to_serial(repo):
    """workers=2 must produce byte-identical BLOB rows to the workers=1 (default)
    serial path, on a mixed fixture of valid + corrupt rows."""
    nb_serial = bi.ensure_notebook(repo, None, "nb-serial")
    nb_par = bi.ensure_notebook(repo, None, "nb-par")
    for i in range(37):
        _seed_node(repo, nb_serial, f"kos-{i}")
        _seed_node(repo, nb_par, f"kop-{i}")
    _seed_many_json_vectors(repo, "knowledge_embeddings", "object_id", nb_serial, 37,
                            bad_every=5, prefix="kos")
    _seed_many_json_vectors(repo, "knowledge_embeddings", "object_id", nb_par, 37,
                            bad_every=5, prefix="kop")

    out_serial = bi._backfill_table_to_blob(repo, nb_serial, "knowledge_embeddings", "object_id",
                                            batch_size=10, workers=1)
    out_par = bi._backfill_table_to_blob(repo, nb_par, "knowledge_embeddings", "object_id",
                                         batch_size=10, workers=2)

    assert out_serial["converted"] == out_par["converted"] == 37
    assert out_serial["skipped_bad"] == out_par["skipped_bad"]

    with repo._connect() as db:
        # strip the nb-specific prefix ("kos-"/"kop-") so both sides compare on
        # the same logical index (both fixtures encode identical vector values
        # at each index — only the id prefix differs to avoid the global
        # knowledge_objects.id uniqueness constraint across notebooks).
        rows_serial = {r["object_id"].split("-", 1)[1]: r["vector"] for r in db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (nb_serial,)).fetchall()}
        rows_par = {r["object_id"].split("-", 1)[1]: r["vector"] for r in db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
            (nb_par,)).fetchall()}
    assert rows_serial.keys() == rows_par.keys()
    for k in rows_serial:
        assert bytes(rows_serial[k]) == bytes(rows_par[k]), f"mismatch at {k}"


def test_backfill_parallel_idempotent_second_run_converts_zero(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(20):
        _seed_node(repo, nb_id, f"ko-{i}")
    _seed_many_json_vectors(repo, "knowledge_embeddings", "object_id", nb_id, 20, bad_every=7)

    out1 = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id",
                                      batch_size=6, workers=2)
    assert out1["converted"] == 20

    out2 = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id",
                                      batch_size=6, workers=2)
    assert out2 == {"table": "knowledge_embeddings", "total": 0, "converted": 0, "skipped_bad": 0}


def test_backfill_workers_le_1_never_imports_process_pool_executor(repo, monkeypatch):
    """workers<=1 must take the exact current serial path — zero multiprocessing
    machinery. Spy on ProcessPoolExecutor to assert it's never constructed."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    calls = []
    import concurrent.futures as cf

    class _SpyExecutor:
        def __init__(self, *a, **kw):
            calls.append((a, kw))
            raise AssertionError("ProcessPoolExecutor must not be constructed when workers<=1")

    monkeypatch.setattr(cf, "ProcessPoolExecutor", _SpyExecutor)
    monkeypatch.setattr(bi, "ProcessPoolExecutor", _SpyExecutor, raising=False)

    out = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id", workers=1)
    assert out["converted"] == 1
    assert calls == []


def test_backfill_broken_process_pool_falls_back_to_serial(repo, monkeypatch, capsys):
    """If the pool dies mid-run (BrokenProcessPool), the batch must fall back to
    serial parse/encode for the remaining rows rather than losing the run."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(5):
        _seed_node(repo, nb_id, f"ko-{i}")
    _seed_many_json_vectors(repo, "knowledge_embeddings", "object_id", nb_id, 5)

    from concurrent.futures.process import BrokenProcessPool

    def _broken_map(*a, **kw):
        raise BrokenProcessPool("simulated pool crash")

    monkeypatch.setattr(
        "concurrent.futures.ProcessPoolExecutor.map", _broken_map, raising=True)

    out = bi._backfill_table_to_blob(repo, nb_id, "knowledge_embeddings", "object_id",
                                     batch_size=10, workers=2)
    assert out["converted"] == 5
    assert out["skipped_bad"] == 0
    warn = capsys.readouterr().out
    assert "fallback" in warn.lower() or "回退" in warn or "serial" in warn.lower()

    with repo._connect() as db:
        remaining_text = db.execute(
            "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=? AND typeof(vector)='text'",
            (nb_id,)).fetchone()["c"]
    assert remaining_text == 0


def test_run_vectors_to_blob_accepts_workers_param(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    out = bi.run_vectors_to_blob(repo, nb_id, all_notebooks=False, workers=2)
    assert out["converted"] >= 1


def test_arg_parser_vectors_to_blob_workers_default():
    """--workers omitted on vectors-to-blob resolves to min(8, cpu_count) at
    dispatch time (argparse default itself may be None; main() resolves it)."""
    args = bi.build_arg_parser().parse_args(["vectors-to-blob", "--notebook-id", "nb-x"])
    assert args.workers is None or isinstance(args.workers, int)

    args2 = bi.build_arg_parser().parse_args(
        ["vectors-to-blob", "--notebook-id", "nb-x", "--workers", "3"])
    assert args2.workers == 3


def test_main_vectors_to_blob_passes_workers_through(repo, monkeypatch, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, nb_id, "ko-1")
    _seed_json_vector(repo, "knowledge_embeddings", "object_id", "ko-1", nb_id)

    captured = {}
    orig = bi.run_vectors_to_blob

    def _spy(repo_, notebook_id, all_notebooks=False, workers=1):
        captured["workers"] = workers
        return orig(repo_, notebook_id, all_notebooks=all_notebooks, workers=workers)

    monkeypatch.setattr(bi, "run_vectors_to_blob", _spy)
    rc = bi.main(["vectors-to-blob", "--notebook-id", nb_id, "--workers", "2"])
    assert rc == 0
    assert captured["workers"] == 2
