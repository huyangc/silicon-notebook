# backend/tests/test_batch_ingest.py
import json
import inspect
import threading
import time
import pytest
from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from app.core.request_context import get_request_user, set_request_user, reset_request_user
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services.model_concurrency import (
    ConcurrencySnapshot,
    LimitedJsonChatClient,
    activate_model_concurrency,
    current_model_concurrency,
)
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
    assert counts2["reparsed"] == 0               # 有 elements = 真已摄取,不该重 parse
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb_id,)).fetchone()["c"]
    assert nsrc == 3


def test_run_ingest_reparses_existing_source_missing_elements(repo, tmp_path, monkeypatch):
    """hash 已在库、但没有 source_elements 的源(上次 parse 中途中断)必须重新 parse,
    不能按 hash 认账成 skipped——file_hash 是 INSERT 时写的(早于 parse),旧的二元判定
    会让这种源永久停在空源状态。行为证据:跑完真的有了 elements。"""
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda s: None)
    nb_id = bi.ensure_notebook(repo, None, "nb-ingest-reparse")
    p = tmp_path / "half.md"
    p.write_text("# Half\n\nBody paragraph " + "z" * 200, encoding="utf-8")
    digest = bi.sha256_bytes(p.read_bytes())
    now = "2026-01-01T00:00:00"
    with repo._write() as db:      # 预置:hash 已写、parse_status 看似前进,但 elements 为空
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-half", nb_id, "Half", "document", p.name, str(p), 0, digest,
             "", "", "parsed", now, now))

    counts = bi.run_ingest(repo, nb_id, [p], workers=1, conc=1)

    assert counts["reparsed"] == 1
    assert counts["uploaded"] == 0 and counts["skipped"] == 0 and counts["failed"] == 0
    with repo._connect() as db:
        n_el = db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id='src-half'"
                          ).fetchone()["c"]
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert n_el > 0                # 真的重新 parse 出了 elements
    assert nsrc == 1               # 复用原源,不新建


def test_run_ingest_skips_source_that_parsed_to_zero_elements(repo, tmp_path):
    """一次**成功**的 parse 可以产出零个 element(扫描版/纯图 PDF 没有文本层,
    process_source 照常走完管线置 'extracted' 并把提示写进 error_message)。

    这种源不能因为「没有 elements」就每次重跑都被重新解析——那样永远不收敛,与
    本函数要修的 bug 同类只是反了个向。判据里的 sources_with_completed_parse
    就是为这一格存在的。'metadata-only'(只导入元数据、永远没有 element)同理。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-ingest-empty-ok")
    p = tmp_path / "scanned.pdf"
    p.write_bytes(b"%PDF-1.4 fake scanned page with no text layer")
    digest = bi.sha256_bytes(p.read_bytes())
    now = "2026-01-01T00:00:00"
    with repo._write() as db:   # 预置:parse 已跑完(extracted),但零 element
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,status,parse_status,error_message,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-scan", nb_id, "Scanned", "pdf", p.name, str(p), 0, digest,
             "", "", "extracted", "extracted",
             "No extractable text — likely a scanned/image PDF.", now, now))

    counts = bi.run_ingest(repo, nb_id, [p], workers=1, conc=1)

    assert counts["skipped"] == 1
    assert counts["reparsed"] == 0, "零 element 的成功 parse 被当成中断重跑了 → 永不收敛"
    assert counts["uploaded"] == 0 and counts["failed"] == 0


def test_run_ingest_reparses_source_with_elements_but_no_chunks(repo, tmp_path, monkeypatch):
    """「有 element」不等于「管线跑完」。elements 的原子写入在管线中段,其后还有
    分块、向量、终态置位——崩在中间的源有 element 却一个 chunk 都没有。

    这种源若按「有 element 即完成」被跳过,就永远补不回来:run_ingest 收尾的
    backfill_chunk_embeddings 只嵌入**已存在**的 chunk,零 chunk 的源它救不了。
    所以判据只认管线终态,不看 element 有无。"""
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda s: None)
    nb_id = bi.ensure_notebook(repo, None, "nb-ingest-no-chunks")
    p = tmp_path / "halfway.md"
    p.write_text("# Halfway\n\nBody paragraph " + "q" * 200, encoding="utf-8")
    digest = bi.sha256_bytes(p.read_bytes())
    now = "2026-01-01T00:00:00"
    with repo._write() as db:   # 预置:elements 已落库、parse_status 停在过渡态、零 chunk
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-halfway", nb_id, "Halfway", "document", p.name, str(p), 0, digest,
             "", "", "parsed", "parsed", now, now))
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,"
            "created_at) VALUES ('el-1','src-halfway','paragraph','p1','stale body',?)",
            (now,))
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id='src-halfway'"
                          ).fetchone()["c"] == 0      # 前提:确实零 chunk

    counts = bi.run_ingest(repo, nb_id, [p], workers=1, conc=1)

    assert counts["reparsed"] == 1, "有 element 但没跑完的源被当成已完成跳过了"
    assert counts["skipped"] == 0
    with repo._connect() as db:
        n_chunks = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id='src-halfway'"
                              ).fetchone()["c"]
    assert n_chunks > 0, "重解析没有补出 chunk,这个源仍然不可检索"


def test_run_ingest_reports_failed_when_reparse_does_not_recover(repo, tmp_path, monkeypatch):
    """process_source 对解析异常是自己吞掉的:内部 except 把源置 'failed' 后照常
    return SourceSummary,**不** re-raise。所以 _one 外面的 try/except 对普通解析失败
    根本不可达——必须查返回值的真实状态,否则一次失败的重解析会被计成 reparsed,
    汇总数字说谎(「跑完了 N 个」而那 N 个其实还是空源)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-ingest-reparse-fail")
    p = tmp_path / "broken.md"
    p.write_text("# Broken\n\n" + "z" * 200, encoding="utf-8")
    digest = bi.sha256_bytes(p.read_bytes())
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-broken", nb_id, "Broken", "document", p.name, str(p), 0, digest,
             "", "", "parsed", now, now))

    # 复刻真实形态:解析抛错 → process_source 内部吞掉、置 failed、正常返回。
    ingestion = repo._runtime.source_ingestion

    def _boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(ingestion, "parse_file", _boom)

    counts = bi.run_ingest(repo, nb_id, [p], workers=1, conc=1)

    assert counts["failed"] == 1, "失败的重解析被计成了 reparsed"
    assert counts["reparsed"] == 0


def test_run_ingest_same_run_duplicate_files_skip_not_reparse(repo, tmp_path):
    """同一次运行里两个内容相同的文件:第一个 uploaded、第二个 skipped。第二个虽然
    hash 命中(第一个刚建的源)却不在进池前的 parsed 快照里,若无本次运行的认领集合
    就会被误判成 reparsed、白跑一遍 parse。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-dup")
    body = "# Same\n\nIdentical body " + "q" * 200
    a = tmp_path / "a.md"; a.write_text(body, encoding="utf-8")
    b = tmp_path / "b.md"; b.write_text(body, encoding="utf-8")

    counts = bi.run_ingest(repo, nb_id, [a, b], workers=1, conc=1)

    assert counts["uploaded"] == 1 and counts["skipped"] == 1
    assert counts["reparsed"] == 0 and counts["failed"] == 0   # 第二个不该重 parse
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 1                                # 同内容只建一个源


def test_run_kg_disables_fusion_and_rebuilds(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    calls = {}

    def fake_build(nb, *, progress=None):
        calls["fusion_flag_during"] = repo.settings.kg_incremental_fusion_enabled
        calls["build_nb"] = nb
        return {"built": ["s1", "s2"], "failed": [], "skipped": []}

    def fake_rebuild(nb, progress=None, force=False, fresh=False):
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
    monkeypatch.setattr("app.services.source_ingestion.SourceIngestionService.run_extraction", lambda self, sid: None)
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg",
                        lambda self, nb, progress=None, force=False, fresh=False: 0)
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
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: extracted_calls.append(sid))
    monkeypatch.setattr(repo._runtime.source_ingestion, "set_source_status", lambda *a, **k: None)

    def _no_build(nb):
        raise AssertionError("build_notebook_kg must not be called when limit is set")
    monkeypatch.setattr(repo, "build_notebook_kg", _no_build)
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)

    repo.settings.kg_llm_base_url = "http://kg.example"
    repo.settings.kg_llm_api_key = "k"
    repo.settings.kg_llm_model = "kg-model"
    res = bi.run_kg(repo, nb_id, limit=2, conc=2)
    assert res["extracted"] == 2
    assert len(extracted_calls) == 2          # 只抽前 2 个未抽源(targets[:limit])


class _StubLLM:
    configured = True; chat_json = lambda self, messages, response_schema_hint, **kwargs: '{"ok":true}'


def test_run_kg_limit_parallelizes_sources_with_workers(repo, monkeypatch):
    lock = threading.Lock()
    active = 0
    peak = 0
    targets = [f"src-{i}" for i in range(6)]

    monkeypatch.setattr(
        repo.maintenance, "source_ids", lambda notebook_id: targets
    )
    monkeypatch.setattr(
        repo.maintenance, "kg_covered_source_ids", lambda notebook_id: set()
    )
    monkeypatch.setattr(repo, "llm_client", _StubLLM())

    def extract(source_id):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(repo.maintenance, "run_extraction", extract)
    effective = bi.EffectiveConcurrency(
        workers=3,
        llm=8,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    with bi._batch_concurrency_scope(repo, effective):
        bi.run_kg(
            repo,
            bi.ensure_notebook(repo, None, "nb-kg-limit"),
            limit=6,
            no_rebuild=True,
        )
    assert peak == 3


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
            db.execute(   # 有 elements = 已成功 parse(build_notebook_kg 才会把它当抽取目标)
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,'paragraph','p1','body','{}',?)",
                (f"el-{sid}", sid, now))
    return sids


def test_build_notebook_kg_concurrent_reports_progress(repo, monkeypatch):
    """build_notebook_kg 跨源并发抽取(全局 job 池),逐源回调进度;全部成功。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-conc")
    sids = _seed_sources(repo, nb_id, 6, "src-c")
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid, **kwargs: None)
    monkeypatch.setattr(repo._runtime.source_ingestion, "set_source_status", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", lambda nb: None)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "relink_notebook_kg", lambda nb: 0)
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

    def _extract(sid, **kwargs):
        if sid == bad:
            raise RuntimeError("boom")
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", _extract)
    monkeypatch.setattr(repo._runtime.source_ingestion, "set_source_status", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_mark_unified_kg_dirty", lambda nb: None)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "relink_notebook_kg", lambda nb: 0)
    out = repo.build_notebook_kg(nb_id)
    assert bad in out["failed"] and len(out["built"]) == 2


def test_ensure_notebook_explicit_owner(repo):
    u = repo.create_user("a00123456", "pw123456")
    assert u.id != "user-local"
    assert bi._resolve_owner_profile(repo, "A00123456").id == u.id
    token = set_request_user(u)
    try:
        nb_id = bi.ensure_notebook(repo, None, "nb")
        nb_id2 = bi.ensure_notebook(repo, None, "nb2")
    finally:
        reset_request_user(token)
    with repo._connect() as db:
        cb = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id,)).fetchone()["created_by"]
        cb2 = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id2,)).fetchone()["created_by"]
    assert cb == u.id and cb2 == u.id


def test_ensure_notebook_unknown_owner_errors(repo):
    with pytest.raises(SystemExit):
        bi._resolve_owner_profile(repo, "a00999999")


def test_main_new_notebook_owner_context_drives_model_and_scheduler_worker(
    repo, tmp_path, monkeypatch
):
    from app.services.kg.scheduler import submit_job

    owner = repo.create_user("b00123456", "pw123456")
    repo.set_user_model_settings(
        owner.id,
        {
            "llm": {
                "base_url": "https://owner-new.example/v1",
                "api_key": "owner-key",
                "model": "owner-new-model",
            }
        },
    )
    docs = tmp_path / "owner-new-docs"
    docs.mkdir()
    seen = {}

    def fake_run_ingest(repo_, notebook_id, files, **kwargs):
        seen["user_id"] = repo_.current_user().id
        seen["model"] = repo_.kg_llm_client.model
        seen["worker_user_id"] = submit_job(
            lambda: repo_.current_user().id
        ).result(timeout=5)
        return {"uploaded": 0, "skipped": 0, "failed": 0}

    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(bi, "run_ingest", fake_run_ingest)
    assert get_request_user() is None

    rc = bi.main([
        "ingest",
        "--input-dir",
        str(docs),
        "--notebook-name",
        "Owner notebook",
        "--owner",
        owner.username,
        "--allow-no-embed",
    ])

    assert rc == 0
    assert seen == {
        "user_id": owner.id,
        "model": "owner-new-model",
        "worker_user_id": owner.id,
    }
    assert get_request_user() is None
    with repo._connect() as db:
        created_by = db.execute(
            "SELECT created_by FROM notebooks WHERE name = ?",
            ("Owner notebook",),
        ).fetchone()["created_by"]
    assert created_by == owner.id


def test_main_existing_notebook_owner_context_drives_model_and_resets_on_failure(
    repo, monkeypatch
):
    owner = repo.create_user("c00123456", "pw123456")
    repo.set_user_model_settings(
        owner.id,
        {
            "llm": {
                "base_url": "https://owner-existing.example/v1",
                "api_key": "owner-key",
                "model": "owner-existing-model",
            }
        },
    )
    token = set_request_user(owner)
    try:
        notebook_id = bi.ensure_notebook(
            repo, None, "Owner existing notebook"
        )
    finally:
        reset_request_user(token)
    seen = {}

    def fail_reparse(repo_, notebook_id_, **kwargs):
        seen["notebook_id"] = notebook_id_
        seen["user_id"] = repo_.current_user().id
        seen["model"] = repo_.kg_llm_client.model
        raise RuntimeError("phase failed")

    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(bi, "run_reparse", fail_reparse)
    assert get_request_user() is None

    with pytest.raises(RuntimeError, match="phase failed"):
        bi.main([
            "reparse",
            "--notebook-id",
            notebook_id,
            "--owner",
            owner.username,
            "--allow-no-embed",
            "--no-rebuild",
        ])

    assert seen == {
        "notebook_id": notebook_id,
        "user_id": owner.id,
        "model": "owner-existing-model",
    }
    assert get_request_user() is None


def test_main_owner_context_resets_when_concurrency_setup_fails(
    repo, monkeypatch
):
    owner = repo.create_user("d00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook_id = bi.ensure_notebook(
            repo, None, "Owner setup failure"
        )
    finally:
        reset_request_user(token)

    class _FailingScope:
        def __enter__(self):
            assert get_request_user().id == owner.id
            raise RuntimeError("scope setup failed")

        def __exit__(self, *exc):
            return False

    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(
        bi,
        "_batch_concurrency_scope",
        lambda *_args, **_kwargs: _FailingScope(),
    )

    with pytest.raises(RuntimeError, match="scope setup failed"):
        bi.main([
            "reparse",
            "--notebook-id",
            notebook_id,
            "--owner",
            owner.username,
            "--allow-no-embed",
            "--no-rebuild",
        ])

    assert get_request_user() is None


def test_main_omitted_owner_preserves_active_user_context(
    repo, monkeypatch
):
    owner = repo.create_user("e00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook_id = bi.ensure_notebook(
            repo, None, "Active owner notebook"
        )
        seen = []
        monkeypatch.setattr(
            bi,
            "run_index",
            lambda repo_, notebook_id_: (
                seen.append((repo_.current_user().id, notebook_id_))
                or {"indexed_nodes": 0}
            ),
        )

        rc = bi.main(["index", "--notebook-id", notebook_id])

        assert rc == 0
        assert seen == [(owner.id, notebook_id)]
        assert get_request_user() is owner
    finally:
        reset_request_user(token)
    assert get_request_user() is None


def test_arg_parser_has_owner():
    args = bi.build_arg_parser().parse_args(["ingest", "--input-dir", "x", "--owner", "a00123456"])
    assert args.owner == "a00123456"


def test_arg_parser_concurrency_help_matches_positive_contract():
    help_text = bi.build_arg_parser().format_help()
    normalized = " ".join(help_text.split())
    assert "<=1" not in normalized
    assert "1 走原串行路径" in normalized
    assert "all/kg/reparse 阶段生效" in normalized


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


# ── element 向量的缺失查询与补齐 ──────────────────────────────────────────────
# 注意 ingest 阶段刻意零嵌入(run_ingest 把 embed_provider 置空),收尾只补 chunk 向量,
# 所以刚 ingest 完的库里 element 向量是**全缺**的——正好是补齐路径的真实输入。

class _TextRecordingEmbedder(FakeEmbedder):
    """FakeEmbedder + 记录每次真正送给 embedder 的文本(断言截断规则/只补缺失用)。"""

    def __init__(self, dim, seen):
        super().__init__(dim=dim)
        self.seen = seen

    def embed_texts(self, texts):
        self.seen.extend(texts)
        return super().embed_texts(texts)


def _element_rows(repo, source_id):
    with repo._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, text FROM source_elements WHERE source_id=? ORDER BY id",
            (source_id,)).fetchall()]


def _source_ids(repo, notebook_id):
    with repo._connect() as db:
        return [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=? ORDER BY id",
            (notebook_id,)).fetchall()]


def _insert_element(repo, source_id, element_id, text):
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            (element_id, source_id, "paragraph", "p1", text, "{}", now))


def test_py_whitespace_constant_covers_every_python_strip_char():
    """PY_WHITESPACE 必须就是 str.strip() 的字符全集——缺失查询拿它当 TRIM 字符集,
    少一个字符就有一类元素「算缺失但永远补不上」,补齐命令不收敛。"""
    from app.repositories.sqlite.maintenance import PY_WHITESPACE

    assert set(PY_WHITESPACE) == {
        chr(c) for c in range(0x110000) if chr(c).isspace()
    }


def test_missing_element_rows_exclude_blank_text(repo, tmp_path):
    """空白文本元素不算缺失:embed_source 会跳过它们(text.strip() 为空),它们永远不会
    有向量。若算进缺失,补齐命令每次都报「还有 N 个缺失」、每次都试着嵌入空串 → 永不
    收敛的脏状态。含纯制表符/换行/全角空格——SQLite 裸 TRIM() 只去半角空格,挡不住。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-blank")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1, conc=1)
    sid = _source_ids(repo, nb_id)[0]
    bi.backfill_element_embeddings(repo, nb_id, conc=1)      # 先把真实元素补齐
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    for i, blank in enumerate(["", "   ", "\t\n", "　　", "\xa0"]):
        _insert_element(repo, sid, f"el-blank-{i}", blank)   # 全部无向量

    rows = repo.maintenance.missing_element_embedding_rows(nb_id)

    assert [r["id"] for r in rows] == []                     # 一个都不算缺失
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.backfill_element_embeddings(repo, nb_id, conc=1) == 0   # 仍然 0 项可补
    _insert_element(repo, sid, "el-real", " 有内容的段落 ")    # 对照:非空白才算缺失
    assert [r["id"] for r in repo.maintenance.missing_element_embedding_rows(nb_id)] == [
        "el-real"]
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 1


def test_missing_element_rows_are_scoped_to_the_notebook(repo, tmp_path):
    """缺失查询按 notebook 限定(source_elements 表本身没有 notebook_id,靠 JOIN sources)。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_a = bi.ensure_notebook(repo, None, "nb-a")
    nb_b = bi.ensure_notebook(repo, None, "nb-b")
    bi.run_ingest(repo, nb_a, bi.iter_files(d), workers=1, conc=1)
    n_a = repo.maintenance.count_missing_element_vectors(nb_a)

    assert n_a > 0                                            # A 有缺
    assert repo.maintenance.count_missing_element_vectors(nb_b) == 0   # B 不受影响
    assert all(r["source_id"] in _source_ids(repo, nb_a)
               for r in repo.maintenance.missing_element_embedding_rows(nb_a))
    assert repo.maintenance.missing_element_embedding_rows(nb_b) == []


def test_backfill_element_embeddings_fills_missing_and_is_idempotent(repo, tmp_path):
    """补齐后全部有向量、第二遍 0 项可补(幂等);再删掉一部分重跑时,只有被删的那些
    重新送进 embedder(INSERT OR REPLACE upsert 只碰缺的,不整源重嵌)。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb-el")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    total = repo.maintenance.count_missing_element_vectors(nb_id)
    assert total >= 3                                         # 多源、多 element

    assert bi.backfill_element_embeddings(repo, nb_id, conc=2) == total
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.backfill_element_embeddings(repo, nb_id, conc=2) == 0   # 幂等

    with repo._connect() as db:                               # 制造部分缺失
        eids = [r["element_id"] for r in db.execute(
            "SELECT element_id FROM element_embeddings WHERE notebook_id=? "
            "ORDER BY element_id", (nb_id,)).fetchall()]
    deleted = eids[:2]
    with repo._write() as db:
        db.executemany("DELETE FROM element_embeddings WHERE element_id=?",
                       [(eid,) for eid in deleted])
    seen = []
    repo.embedder = _TextRecordingEmbedder(dim=16, seen=seen)

    assert bi.backfill_element_embeddings(repo, nb_id, conc=2) == len(deleted)

    assert len(seen) == len(deleted)                          # 只重嵌了被删的那两条
    with repo._connect() as db:
        want = {r["text"] for r in db.execute(
            "SELECT text FROM source_elements WHERE id IN (?,?)", tuple(deleted)
        ).fetchall()}
    assert set(seen) == want
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0


def test_backfill_element_embeddings_groups_rows_by_source(repo, tmp_path):
    """待补行跨多个源:replace_element_vectors 是 per-source 签名(source_id, notebook_id,
    rows),必须按 source_id 分组逐组落库——落错组会让 element_embeddings.source_id 指向
    别的源(按源删向量/按源重嵌都会走错)。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb-group")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    sids = _source_ids(repo, nb_id)
    assert len(sids) >= 2
    missing = repo.maintenance.count_missing_element_vectors(nb_id)
    assert len({r["source_id"] for r in
                repo.maintenance.missing_element_embedding_rows(nb_id)}) == len(sids)

    assert bi.backfill_element_embeddings(repo, nb_id, conc=2) == missing

    with repo._connect() as db:                               # 每行的 source_id 都对得上
        mismatched = db.execute(
            "SELECT COUNT(*) c FROM element_embeddings v JOIN source_elements e "
            "ON e.id = v.element_id WHERE v.notebook_id=? AND v.source_id != e.source_id",
            (nb_id,)).fetchone()["c"]
        per_source = {r["source_id"]: r["c"] for r in db.execute(
            "SELECT source_id, COUNT(*) c FROM element_embeddings WHERE notebook_id=? "
            "GROUP BY source_id", (nb_id,)).fetchall()}
    assert mismatched == 0
    assert set(per_source) == set(sids)                       # 每个源都写到了
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0


def test_backfill_element_embeddings_truncates_by_settings_not_hardcoded_2000(repo, tmp_path):
    """补齐路径的截断必须用 settings.embed_truncate_chars(与 embed_source 同构),不是
    embed_chunks_batch 里硬编码的 2000。默认值恰好也是 2000 → 必须把配置调成非 2000
    才有检出力,这里用 137。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-trunc")
    p = tmp_path / "long.md"
    p.write_text("# Long\n\n" + "词" * 5000, encoding="utf-8")
    bi.run_ingest(repo, nb_id, [p], workers=1, conc=1)
    sid = _source_ids(repo, nb_id)[0]
    assert any(len(r["text"]) > 2000 for r in _element_rows(repo, sid))   # 有超长元素
    seen = []
    repo.embedder = _TextRecordingEmbedder(dim=16, seen=seen)
    repo.settings.embed_truncate_chars = 137                  # 非默认值,否则本断言零检出力

    assert bi.backfill_element_embeddings(repo, nb_id, conc=1) > 0

    assert seen
    assert max(len(t) for t in seen) == 137                   # 真按 137 截,不是 2000


def test_run_embed_fills_missing_element_vectors(repo, tmp_path):
    """run_embed 三侧并列:chunk / element / 节点都盘点 → 补齐 → 复盘归零。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-embed3")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1, conc=2)
    element_missing = repo.maintenance.count_missing_element_vectors(nb_id)
    assert element_missing > 0

    out = bi.run_embed(repo, nb_id, conc=2)

    assert out["element_missing_before"] == element_missing
    assert out["elements_embedded"] == element_missing
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.run_embed(repo, nb_id, conc=2)["element_missing_before"] == 0   # 幂等


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


def test_model_concurrency_cli_omission_inherits_settings(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "13")
    monkeypatch.setenv("EMBED_CONCURRENCY", "3")
    args = bi.build_arg_parser().parse_args(["reparse", "--notebook-id", "nb-x"])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert (effective.workers, effective.llm, effective.embedding) == (11, 13, 3)
    assert (
        effective.workers_source,
        effective.llm_source,
        effective.embedding_source,
    ) == ("env", "env", "env")


def test_model_concurrency_cli_overrides_settings(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    monkeypatch.setenv("KG_EXTRACT_WORKERS", "13")
    monkeypatch.setenv("EMBED_CONCURRENCY", "3")
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x",
        "--workers", "20", "--llm-conc", "16", "--embed-conc", "2",
    ])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert (effective.workers, effective.llm, effective.embedding) == (20, 16, 2)
    assert effective.workers_source == "cli"
    assert effective.llm_source == "cli"
    assert effective.embedding_source == "cli"


@pytest.mark.parametrize(
    "flag", ["--workers", "--llm-conc", "--embed-conc"]
)
def test_model_concurrency_rejects_non_positive_values(flag):
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x", flag, "0",
    ])
    with pytest.raises(ValueError, match="positive"):
        bi._resolve_effective_concurrency(args, Settings(), "reparse")


def test_main_reports_non_positive_model_concurrency(repo, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb-invalid-concurrency")
    rc = bi.main([
        "reparse",
        "--notebook-id",
        nb_id,
        "--embed-conc",
        "0",
        "--allow-no-embed",
    ])
    assert rc == 2
    assert "positive integer" in capsys.readouterr().err


def test_batch_concurrency_scope_configures_and_restores(repo, monkeypatch):
    from app.services.kg import scheduler
    from app.services.model_concurrency import current_model_concurrency

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    real_configure = scheduler.configure
    configure_calls = []

    def tracked_configure(**kwargs):
        configure_calls.append(kwargs)
        return real_configure(**kwargs)

    monkeypatch.setattr(scheduler, "configure", tracked_configure)
    effective = bi.EffectiveConcurrency(
        workers=7, llm=5, embedding=2,
        workers_source="cli", llm_source="cli", embedding_source="cli",
    )

    with bi._batch_concurrency_scope(repo, effective):
        assert scheduler.job_concurrency() == 7
        assert scheduler.max_workers() == 5
        assert repo.settings.embed_concurrency == 2
        assert current_model_concurrency() is not None

    assert current_model_concurrency() is None
    assert scheduler.job_concurrency() == old_job
    assert scheduler.max_workers() == old_window
    assert (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    ) == old_settings
    assert configure_calls == [
        {"window_workers": 5, "job_workers": 7},
        {"window_workers": old_window, "job_workers": old_job},
    ]


def test_run_reparse_does_not_reconfigure_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
    monkeypatch.setattr(
        repo.maintenance, "source_ids", lambda notebook_id: []
    )
    monkeypatch.setattr(
        repo.maintenance, "sources_with_elements", lambda notebook_id: set()
    )

    bi.run_reparse(
        repo,
        bi.ensure_notebook(repo, None, "nb-reparse-owner"),
        conc=2,
        no_rebuild=True,
    )

    assert configure_calls == []


def test_run_all_does_not_reconfigure_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
    monkeypatch.setattr(
        repo,
        "rebuild_unified_kg",
        lambda notebook_id, progress=None, force=False, fresh=False: 0,
    )
    monkeypatch.setattr(
        bi, "backfill_node_embeddings", lambda repo, notebook_id, conc: 0
    )

    bi.run_all(
        repo,
        bi.ensure_notebook(repo, None, "nb-all-owner"),
        [],
        conc=2,
        report_interval=0,
    )

    assert configure_calls == []


def test_run_all_has_no_phase_local_workers_parameter():
    assert "workers" not in inspect.signature(bi.run_all).parameters


class _PeakRecorder:
    configured = True
    model = "peak-recorder"

    def __init__(self, delay=0.05):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def _call(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.active -= 1

    def chat_json(self, messages, schema="", **kwargs):
        self._call()
        return "{}"

    def embed(self):
        self._call()
        return [0.0]


class _FakeMaintenance:
    def __init__(self, source_ids):
        self._source_ids = source_ids

    def sources_with_elements(self, notebook_id):
        return set()

    def source_ids(self, notebook_id):
        return list(self._source_ids)


class _ReparseConcurrencyRepo:
    def __init__(self, llm, embed):
        self.settings = SimpleNamespace(
            kg_auto_extract=False,
            kg_incremental_fusion_enabled=True,
            kg_job_concurrency=8,
            kg_extract_workers=6,
            embed_concurrency=8,
        )
        self.maintenance = _FakeMaintenance(
            [f"src-peak-{i}" for i in range(12)]
        )
        self._llm = llm
        self._embed = embed

    def process_source(self, source_id):
        state = current_model_concurrency()
        assert state is not None
        LimitedJsonChatClient(self._llm, state.llm).chat_json([], "{}")
        state.embedding.run(
            self._embed.embed,
            task_prefix="emb-el",
        )
        return SimpleNamespace(id=source_id)


def test_reparse_llm_and_embedding_peaks_are_independent():
    from app.services.kg import scheduler as kg_scheduler

    llm = _PeakRecorder()
    embed = _PeakRecorder()
    repo = _ReparseConcurrencyRepo(llm, embed)

    try:
        kg_scheduler.configure(window_workers=6, job_workers=8)
        with activate_model_concurrency(llm_max=6, embed_max=2) as state:
            result = bi.run_reparse(
                repo,
                "nb-peak",
                conc=2,
                no_rebuild=True,
                report_interval=0,
            )
            llm_snapshot = state.llm.snapshot()
            embed_snapshot = state.embedding.snapshot()
    finally:
        kg_scheduler.reset()

    assert result["reparsed"] == 12
    assert result["failed"] == 0
    assert 4 <= llm.peak <= 6
    assert embed.peak == 2
    assert llm_snapshot.active == llm_snapshot.waiting == 0
    assert embed_snapshot.active == embed_snapshot.waiting == 0


class _RunAllMaintenance:
    def __init__(self, hash_to_source, resumed):
        self._hash_to_source = hash_to_source
        self._resumed = resumed

    def source_id_by_hash(self, notebook_id, digest):
        return self._hash_to_source.get(digest)

    def kg_covered_source_ids(self, notebook_id):
        return set()

    def sources_with_elements(self, notebook_id):
        return set(self._resumed)

    def has_scale_index(self, notebook_id):
        return False


class _RunAllConcurrencyRepo:
    def __init__(self, llm, embed, hash_to_source, resumed, source_max):
        self.settings = SimpleNamespace(
            kg_auto_extract=False,
            kg_incremental_fusion_enabled=True,
            kg_job_concurrency=3,
            kg_extract_workers=3,
            embed_concurrency=3,
        )
        self.maintenance = _RunAllMaintenance(hash_to_source, resumed)
        self._llm = llm
        self._embed = embed
        self._source_max = source_max
        self._source_lock = threading.Lock()
        self._source_release = threading.Event()
        self.source_active = 0
        self.source_peak = 0

    def _model_work(self, source_id):
        with self._source_lock:
            self.source_active += 1
            self.source_peak = max(self.source_peak, self.source_active)
            if self.source_active == self._source_max:
                self._source_release.set()
        try:
            if not self._source_release.wait(timeout=1):
                raise AssertionError("source job pool did not reach configured maximum")
            state = current_model_concurrency()
            assert state is not None
            LimitedJsonChatClient(self._llm, state.llm).chat_json([], "{}")
            state.embedding.run(self._embed.embed, task_prefix="emb-el")
            return SimpleNamespace(id=source_id)
        finally:
            with self._source_lock:
                self.source_active -= 1

    def process_source(self, source_id):
        return self._model_work(source_id)

    def extract_source(self, source_id):
        return self._model_work(source_id)

    def upload_sources(self, notebook_id, files, scheduler=None):
        assert scheduler is not None
        for index, _file in enumerate(files):
            scheduler(f"src-new-{index}")
        return []

    def rebuild_unified_kg(
        self, notebook_id, progress=None, force=False, fresh=False
    ):
        return 0

    def get_notebook(self, notebook_id):
        return SimpleNamespace(tier="personal")


def test_run_all_scope_keeps_source_llm_and_embedding_peaks_independent(
    tmp_path, monkeypatch
):
    files = []
    resumed = set()
    hash_to_source = {}
    for index in range(16):
        path = tmp_path / f"source-{index}.md"
        path.write_text(f"# Source {index}\n\nunique body {index}", encoding="utf-8")
        files.append(path)
        if index < 4:
            source_id = f"src-resume-{index}"
            resumed.add(source_id)
            hash_to_source[bi.sha256_bytes(path.read_bytes())] = source_id

    llm = _PeakRecorder()
    embed = _PeakRecorder()
    repo = _RunAllConcurrencyRepo(
        llm, embed, hash_to_source, resumed, source_max=8
    )
    effective = bi.EffectiveConcurrency(
        workers=8,
        llm=6,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    monkeypatch.setattr(
        bi, "backfill_node_embeddings", lambda repo, notebook_id, conc: 0
    )

    with bi._batch_concurrency_scope(repo, effective) as state:
        result = bi.run_all(
            repo,
            "nb-run-all-peak",
            files,
            conc=effective.embedding,
            report_interval=0,
        )
        llm_snapshot = state.llm.snapshot()
        embed_snapshot = state.embedding.snapshot()
        from app.services.kg import scheduler as kg_scheduler
        assert kg_scheduler.job_concurrency() == 8
        assert kg_scheduler.max_workers() == 6

    assert result["new"] == 12
    assert result["resumed"] == 4
    assert result["extracted"] == 16
    assert result["failed"] == 0
    assert repo.source_peak == 8
    assert llm.peak == 6
    assert embed.peak == 2
    assert llm_snapshot.active == llm_snapshot.waiting == 0
    assert embed_snapshot.active == embed_snapshot.waiting == 0


def test_batch_scope_restore_failure_does_not_mask_phase_error(
    repo, monkeypatch, capsys
):
    from app.services.kg import scheduler

    effective = bi.EffectiveConcurrency(
        workers=2,
        llm=2,
        embedding=1,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    real_configure = scheduler.configure
    calls = 0

    def flaky_configure(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("restore failed")
        return real_configure(**kwargs)

    monkeypatch.setattr(scheduler, "configure", flaky_configure)
    try:
        with pytest.raises(ValueError, match="phase failed"):
            with bi._batch_concurrency_scope(repo, effective):
                raise ValueError("phase failed")
        assert "failed to restore KG scheduler" in capsys.readouterr().err
    finally:
        scheduler.reset()


def test_batch_scope_install_failure_restores_settings_and_scheduler(
    repo, monkeypatch
):
    from app.services.kg import scheduler
    from app.services.model_concurrency import current_model_concurrency

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    effective = bi.EffectiveConcurrency(
        workers=7,
        llm=5,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    real_configure = scheduler.configure
    calls = 0

    def fail_install_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("install failed")
        return real_configure(**kwargs)

    monkeypatch.setattr(scheduler, "configure", fail_install_once)

    with pytest.raises(RuntimeError, match="install failed"):
        with bi._batch_concurrency_scope(repo, effective):
            pytest.fail("phase must not start when scheduler installation fails")

    assert calls == 2
    assert current_model_concurrency() is None
    assert scheduler.max_workers() == old_window
    assert scheduler.job_concurrency() == old_job
    assert (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    ) == old_settings


def test_batch_scope_activation_failure_restores_settings_and_scheduler(
    repo, monkeypatch
):
    from app.services.kg import scheduler
    from app.services.model_concurrency import current_model_concurrency

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    effective = bi.EffectiveConcurrency(
        workers=7,
        llm=5,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    real_configure = scheduler.configure
    configure_calls = []

    class _FailingActivation:
        def __enter__(self):
            raise RuntimeError("activation failed")

        def __exit__(self, *exc):
            return False

    def tracked_configure(**kwargs):
        configure_calls.append(kwargs)
        return real_configure(**kwargs)

    monkeypatch.setattr(scheduler, "configure", tracked_configure)
    monkeypatch.setattr(
        bi,
        "activate_model_concurrency",
        lambda **_kwargs: _FailingActivation(),
    )

    with pytest.raises(RuntimeError, match="activation failed"):
        with bi._batch_concurrency_scope(repo, effective):
            pytest.fail("phase must not start when model activation fails")

    assert current_model_concurrency() is None
    assert configure_calls == [
        {"window_workers": 5, "job_workers": 7},
        {"window_workers": old_window, "job_workers": old_job},
    ]
    assert (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    ) == old_settings


def test_batch_scope_settings_restore_failure_does_not_mask_phase_error(
    repo, monkeypatch, capsys
):
    from app.services.kg import scheduler

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    effective = bi.EffectiveConcurrency(
        workers=7,
        llm=5,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    settings_type = type(repo.settings)
    real_setattr = settings_type.__setattr__
    real_configure = scheduler.configure
    configure_calls = []

    def fail_job_restore(settings, name, value):
        if (
            settings is repo.settings
            and name == "kg_job_concurrency"
            and value == old_settings[0]
        ):
            raise RuntimeError("settings restore failed")
        return real_setattr(settings, name, value)

    def tracked_configure(**kwargs):
        configure_calls.append(kwargs)
        return real_configure(**kwargs)

    monkeypatch.setattr(settings_type, "__setattr__", fail_job_restore)
    monkeypatch.setattr(scheduler, "configure", tracked_configure)
    try:
        with pytest.raises(ValueError, match="phase failed"):
            with bi._batch_concurrency_scope(repo, effective):
                raise ValueError("phase failed")

        assert len(configure_calls) == 2
        assert repo.settings.kg_extract_workers == old_settings[1]
        assert repo.settings.embed_concurrency == old_settings[2]
        assert "failed to restore batch setting kg_job_concurrency" in (
            capsys.readouterr().err
        )
    finally:
        real_setattr(repo.settings, "kg_job_concurrency", old_settings[0])
        scheduler.reset()


def test_batch_scope_settings_restore_failure_surfaces_after_success(
    repo, monkeypatch
):
    from app.services.kg import scheduler

    old_workers = repo.settings.kg_job_concurrency
    effective = bi.EffectiveConcurrency(
        workers=7,
        llm=5,
        embedding=2,
        workers_source="cli",
        llm_source="cli",
        embedding_source="cli",
    )
    settings_type = type(repo.settings)
    real_setattr = settings_type.__setattr__
    real_configure = scheduler.configure
    configure_calls = []

    def fail_job_restore(settings, name, value):
        if (
            settings is repo.settings
            and name == "kg_job_concurrency"
            and value == old_workers
        ):
            raise RuntimeError("settings restore failed")
        return real_setattr(settings, name, value)

    def tracked_configure(**kwargs):
        configure_calls.append(kwargs)
        return real_configure(**kwargs)

    monkeypatch.setattr(settings_type, "__setattr__", fail_job_restore)
    monkeypatch.setattr(scheduler, "configure", tracked_configure)
    try:
        with pytest.raises(RuntimeError, match="settings restore failed"):
            with bi._batch_concurrency_scope(repo, effective):
                pass

        assert len(configure_calls) == 2
    finally:
        real_setattr(repo.settings, "kg_job_concurrency", old_workers)
        scheduler.reset()


def test_main_prints_effective_concurrency(
    repo, monkeypatch, capsys
):
    nb_id = bi.ensure_notebook(repo, None, "nb-effective-concurrency")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    monkeypatch.setattr(
        bi,
        "run_reparse",
        lambda repo, notebook_id, **kwargs: {
            "targets": 0,
            "reparsed": 0,
            "failed": 0,
            "clusters": 0,
            "nodes_embedded": 0,
        },
    )

    rc = bi.main([
        "reparse",
        "--notebook-id",
        nb_id,
        "--workers",
        "32",
        "--llm-conc",
        "24",
        "--embed-conc",
        "4",
        "--allow-no-embed",
        "--no-rebuild",
    ])

    assert rc == 0
    assert (
        "concurrency: source=32(cli) llm=24(cli) embedding=4(cli)"
        in capsys.readouterr().out
    )
    manifest = Path(repo.storage_dir) / "batch_ingest" / f"{nb_id}.jsonl"
    event = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    event.pop("ts")
    assert event == {
        "phase": "concurrency",
        "workers": 32,
        "llm": 24,
        "embedding": 4,
        "workers_source": "cli",
        "llm_source": "cli",
        "embedding_source": "cli",
    }


def test_pool_snapshot_reports_gate_truth():
    line = bi._format_pool_snapshot(
        "17:52:33",
        {
            "window_active": 7,
            "window_max": 24,
            "job_active": 29,
            "job_max": 32,
        },
        llm=ConcurrencySnapshot(active=23, maximum=24, waiting=5),
        embedding=ConcurrencySnapshot(active=4, maximum=4, waiting=18),
        done=5,
        total=40,
    )
    assert "LLM 23/24 waiting=5" in line
    assert "embedding 4/4 waiting=18" in line
    assert "source 29/32" in line
    assert "源完成 5/40" in line


def test_pool_reporter_emits_gate_truth(monkeypatch):
    from app.services.kg import scheduler

    llm_snapshot = ConcurrencySnapshot(active=3, maximum=5, waiting=7)
    embedding_snapshot = ConcurrencySnapshot(active=2, maximum=2, waiting=11)

    class _Gate:
        def __init__(self, snapshot):
            self._snapshot = snapshot

        def snapshot(self):
            return self._snapshot

    class _State:
        llm = _Gate(llm_snapshot)
        embedding = _Gate(embedding_snapshot)

    events = []
    reporter = bi._PoolReporter(interval=1, total=9, log=events.append)
    waits = iter([False, True])
    monkeypatch.setattr(reporter._stop, "wait", lambda _interval: next(waits))
    monkeypatch.setattr(bi, "current_model_concurrency", lambda: _State())
    monkeypatch.setattr(
        scheduler,
        "stats",
        lambda: {
            "window_active": 4,
            "window_max": 5,
            "job_active": 6,
            "job_max": 8,
        },
    )

    reporter._loop()

    assert events == [{
        "phase": "pool",
        "window_active": 4,
        "window_max": 5,
        "job_active": 6,
        "job_max": 8,
        "llm_active": 3,
        "llm_max": 5,
        "llm_waiting": 7,
        "embed_active": 2,
        "embed_max": 2,
        "embed_waiting": 11,
        "done": 0,
        "total": 9,
        "label": "",
    }]


def test_run_all_pipelines_new_sources(repo, tmp_path, monkeypatch):
    """run_all per-source 流水线:每个新文件建 source + 走 process_source 抽取(extracted=N),
    末尾一次 rebuild_unified_kg。强制 kg_auto_extract 让 process_source 走到 extract。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())   # configured=True → 走 extract 分支
    d = _make_md_dir(tmp_path, n=2)                        # 2 个 docN.md + 1 个 nested.md = 3
    nb_id = bi.ensure_notebook(repo, None, "nb-all")
    extracted = []
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: extracted.append(sid))
    rebuild_calls = []
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: (rebuild_calls.append(nb), 5)[1])
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, bi.iter_files(d), conc=2)

    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 3                              # 每个文件都建了 source
    assert res["new"] == 3 and res["resumed"] == 0
    assert res["extracted"] == 3 and res["failed"] == 0   # 每个都被抽取(process_source→_run_extraction)
    assert len(extracted) == 3
    assert res["clusters"] == 5
    assert rebuild_calls == [nb_id]               # 末尾恰好一次 rebuild


def test_run_all_leaves_scheduler_to_controller_and_restores_embed_conc(
    repo, tmp_path, monkeypatch
):
    """run_all 不自行重配 scheduler；批次 controller 统一安装 source/LLM 上限。
    run_all 自己临时覆盖的 embed_concurrency 仍须 finally 恢复。"""
    from app.services.kg import scheduler as _sched

    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-flags")
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: None)
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    configure_calls = []
    monkeypatch.setattr(_sched, "configure",
                        lambda **kw: configure_calls.append(kw))
    seen_embed_conc = {}
    real_rebuild = repo.rebuild_unified_kg

    def _spy_rebuild(nb, progress=None, force=False, fresh=False):  # rebuild 在 try 内 → 此刻应已被覆盖为 conc
        seen_embed_conc["during"] = repo.settings.embed_concurrency
        return real_rebuild(nb, progress=progress, force=force, fresh=fresh)
    monkeypatch.setattr(repo, "rebuild_unified_kg", _spy_rebuild)

    orig_embed_conc = repo.settings.embed_concurrency
    try:
        bi.run_all(repo, nb_id, bi.iter_files(d), conc=7)
        assert configure_calls == []
        assert seen_embed_conc["during"] == 7        # try 内 embed_concurrency 被设为 conc
        assert repo.settings.embed_concurrency == orig_embed_conc       # finally 恢复
    finally:
        _sched.reset()                               # 避免污染全局池


def test_run_all_resumes_existing_without_kg(repo, tmp_path, monkeypatch):
    """已 parse(有 source_elements)、无 KG 的 source 走 extract_source 补抽,不重复新建。
    对照 test_run_all_reparses_existing_source_missing_elements:无 elements 的源才 reparse。"""
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
        with repo._write() as db:               # 预置 parsed source(带 elements),同 hash → already_ingested
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, f"S{i}", "document", p.name, str(p), 0, digest,
                 "", "", "parsed", now, now))
            db.execute(                          # 有 elements = 真正已 parse → 走 resume 补抽
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-0", sid, "paragraph", "p1", body, "{}", now))
    extracted = []
    monkeypatch.setattr(repo, "extract_source", lambda sid: extracted.append(sid))
    # 有 elements → 全部走 resume(extract_source);process_source 不应被调用
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, files, conc=2)

    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 3                              # 不重复新建
    assert res["new"] == 0 and res["resumed"] == 3 and res["reparsed"] == 0
    assert sorted(extracted) == sorted(sids)      # 有 elements → 全部走 extract_source 补抽
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


def test_main_index_stays_outside_model_concurrency_scope(
    repo, monkeypatch
):
    nb_id = bi.ensure_notebook(repo, None, "nb-index-no-model-scope")
    seen = []

    def forbidden_scope(*_args, **_kwargs):
        raise AssertionError("index must not activate model concurrency")

    monkeypatch.setattr(bi, "_batch_concurrency_scope", forbidden_scope)
    monkeypatch.setattr(
        bi,
        "run_index",
        lambda repo_, notebook_id: (
            seen.append((repo_.current_user().id, notebook_id))
            or {"indexed_nodes": 0}
        ),
    )

    rc = bi.main(["index", "--notebook-id", nb_id])

    assert rc == 0
    assert seen == [("user-local", nb_id)]
    assert current_model_concurrency() is None


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
# --- backfill-source-index CLI (P0-4 proactive reverse-index backfill) -------

def _seed_node_with_evidence(repo, nb_id, oid, source_ids):
    now = "2026-01-01T00:00:00"
    evidence = [
        {"source_id": sid, "source_title": "Doc", "element_id": f"el-{sid}",
         "element_type": "paragraph", "location_label": "p1",
         "quoted_span": "span", "confidence": 1.0}
        for sid in source_ids
    ]
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "active",
             json.dumps({"name": f"name-{oid}"}),
             json.dumps(evidence), source_ids[0] if source_ids else "", now, now))


def test_run_backfill_source_index_populates_reverse_index_and_marks(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a", "src-b"])  # merged object
    _seed_node_with_evidence(repo, nb_id, "ko-2", ["src-b"])
    _seed_node_with_evidence(repo, nb_id, "ko-3", [])                  # no evidence

    out = bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    assert out["objects"] == 3
    assert out["rows"] == 3   # ko-1 x2 + ko-2 x1

    with repo._connect() as db:
        rows = {(r["object_id"], r["source_id"]) for r in db.execute(
            "SELECT object_id, source_id FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,)).fetchall()}
        assert repo._source_index_backfilled(db, nb_id)
    assert rows == {("ko-1", "src-a"), ("ko-1", "src-b"), ("ko-2", "src-b")}


def test_run_backfill_source_index_is_idempotent(repo):
    """Re-running does not duplicate rows — each run clears-then-rebuilds this
    notebook's slice of knowledge_object_sources."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])

    bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)

    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,)).fetchone()["c"]
    assert count == 1


def test_run_backfill_source_index_requires_notebook_id_or_all(repo):
    with pytest.raises(ValueError):
        bi.run_backfill_source_index(repo, None, all_notebooks=False)


def test_run_backfill_source_index_all_notebooks_covers_every_notebook(repo):
    nb_a = bi.ensure_notebook(repo, None, "nb-a")
    nb_b = bi.ensure_notebook(repo, None, "nb-b")
    _seed_node_with_evidence(repo, nb_a, "ko-a", ["src-a"])
    _seed_node_with_evidence(repo, nb_b, "ko-b", ["src-b"])

    out = bi.run_backfill_source_index(repo, None, all_notebooks=True)
    assert out["objects"] == 2
    assert out["rows"] == 2

    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb_a)
        assert repo._source_index_backfilled(db, nb_b)


def test_run_backfill_source_index_paginates_in_batches(repo, monkeypatch):
    """Bounded-memory backfill: with a tiny batch size, a notebook with more
    objects than one batch is still fully covered (pagination via id > last_id
    doesn't skip or duplicate rows)."""
    monkeypatch.setattr(bi, "_KOS_BACKFILL_BATCH_SIZE", 2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(5):
        _seed_node_with_evidence(repo, nb_id, f"ko-{i}", [f"src-{i}"])

    out = bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    assert out["objects"] == 5
    assert out["rows"] == 5
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,)).fetchone()["c"]
    assert count == 5


def test_main_backfill_source_index_requires_notebook_or_all(repo, capsys):
    rc = bi.main(["backfill-source-index"])
    assert rc == 2
    assert "backfill-source-index" in capsys.readouterr().err


def test_main_backfill_source_index_end_to_end(repo, capsys):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])

    rc = bi.main(["backfill-source-index", "--notebook-id", nb_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backfill-source-index done" in out
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb_id)


def test_main_backfill_source_index_does_not_require_embedder_configured(repo, monkeypatch, capsys):
    """Pure SQL derivation from existing evidence — must work even when
    EMBED_* is unset, like vectors-to-blob."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])

    monkeypatch.setenv("EMBED_PROVIDER", "")  # main() builds a fresh unconfigured repo
    rc = bi.main(["backfill-source-index", "--notebook-id", nb_id])
    assert rc == 0
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb_id)


def test_arg_parser_backfill_source_index_phase():
    args = bi.build_arg_parser().parse_args(["backfill-source-index", "--notebook-id", "nb-x"])
    assert args.phase == "backfill-source-index"
    assert args.all_notebooks is False

    args2 = bi.build_arg_parser().parse_args(["backfill-source-index", "--all-notebooks"])
    assert args2.all_notebooks is True


# ── Task 6: CLI --fresh 贯通 ──────────────────────────────────────────────────

def test_kg_fresh_flag_clears_checkpoint(repo, monkeypatch):
    """run_kg(fresh=True) 把 fresh 透传给 rebuild_unified_kg;rebuild_only=True 时
    force 本就该是 True(两个显式入口都成立)。"""
    from app.services import batch_ingest
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    seen = {}
    def _fake_rebuild(notebook_id, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _fake_rebuild)
    batch_ingest.run_kg(repo, nb.id, rebuild_only=True, fresh=True)
    assert seen == {"force": True, "fresh": True}


def test_kg_fresh_alone_forces_rebuild(repo, monkeypatch):
    """run_kg(fresh=True) 即便 rebuild_only=False,也必须让 rebuild 调用带 force=True——
    否则 fresh 清空 checkpoint 后,仍会被 rebuild_unified_kg 里 force=False 的跳过门挡回
    缓存簇数,merge 审查/概念描述根本没有重跑,--fresh 变成假 no-op(Task 3 review 揪出的
    整合缺口,这里钉死回归)。"""
    from app.services import batch_ingest
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    monkeypatch.setattr(
        repo, "build_notebook_kg",
        lambda notebook_id, *, progress=None: {"built": [], "failed": [], "skipped": []})
    seen = {}
    def _fake_rebuild(notebook_id, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _fake_rebuild)
    batch_ingest.run_kg(repo, nb.id, rebuild_only=False, fresh=True)
    assert seen == {"force": True, "fresh": True}


def test_run_all_fresh_flag_forces_rebuild(repo, monkeypatch, tmp_path):
    """同款漏洞回归,但对 run_all:其末尾 rebuild 调用原先硬编码 force=False,加 fresh 后
    必须变成 force=fresh,否则 --fresh 对 all 阶段也是假 no-op。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-all-fresh")
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: None)
    seen = {}
    def _fake_rebuild(nb, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _fake_rebuild)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    bi.run_all(repo, nb_id, bi.iter_files(d), conc=1, fresh=True)

    assert seen == {"force": True, "fresh": True}


def test_arg_parser_has_fresh_flag():
    args = bi.build_arg_parser().parse_args(["kg"])
    assert args.fresh is False
    args2 = bi.build_arg_parser().parse_args(["kg", "--fresh"])
    assert args2.fresh is True


def test_main_kg_fresh_dispatches_force_and_fresh(repo, monkeypatch):
    """端到端:CLI argv → main() → run_kg → repo.rebuild_unified_kg 收到 force=True、
    fresh=True。main() 内部自建新 repo 实例,故按 test_main_all_ingests_then_runs_kg 的
    惯例在 SQLiteRepository 类级打桩(而非 fixture 的 repo 实例)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-cli-fresh")
    monkeypatch.setenv("EMBED_PROVIDER", "")   # main 自建 repo → embedder 未配,--allow-no-embed 绕过
    seen = {}
    def _fake_rebuild(self, nb, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg", _fake_rebuild)

    rc = bi.main(["kg", "--notebook-id", nb_id, "--rebuild-only", "--fresh", "--allow-no-embed"])

    assert rc == 0
    assert seen == {"force": True, "fresh": True}


# ── Task 27: batch ingest owns no SQLite plumbing ────────────────────────────

def test_batch_ingest_module_reaches_no_private_facade_member():
    """batch_ingest 是 CLI 组合根:构造 SQLiteRepository 合法,但所有 SQL/私有面
    必须经 repo.maintenance(SQLiteMaintenanceAdapter)——模块内不得再出现
    repo._x 私有属性访问(_connect/_write/_run_extraction/_embed_* 等)。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bi))
    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and not node.attr.startswith("__")
            and isinstance(node.value, ast.Name)
            and node.value.id == "repo"
        ):
            offenders.append((node.lineno, node.attr))
    assert not offenders, f"batch_ingest still reaches private facade members: {offenders}"


def test_repo_maintenance_is_cached_port_implementation(repo):
    from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter

    mnt = repo.maintenance
    assert isinstance(mnt, SQLiteMaintenanceAdapter)
    assert repo.maintenance is mnt                      # 同实例(缓存,非每次新建)
    for name in ("delete_notebook_kg", "backfill_kg_fts", "backfill_chunk_fts",
                 "build_scale_index", "fold_scale_index_delta"):
        assert callable(getattr(mnt, name)), name


def test_maintenance_extraction_routes_through_ingestion_service(repo, monkeypatch):
    """adapter.run_extraction/set_source_status 必须动态经 runtime 组件转发——
    组件级 monkeypatch(既有测试的座)对经 adapter 的调用同样可见。"""
    calls = []
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction",
                        lambda sid: calls.append(("run", sid)))
    monkeypatch.setattr(repo._runtime.source_ingestion, "set_source_status",
                        lambda sid, status, **k: calls.append(("status", sid, status)))
    repo.maintenance.run_extraction("src-1")
    repo.maintenance.set_source_status("src-1", "extracting")
    assert calls == [("run", "src-1"), ("status", "src-1", "extracting")]


# ── Task 6: metadata phase (offline paper-metadata bulk backfill) ───────────

from app.repositories.sqlite.source_store import SourceElementWrite

_META_HEAD_TEXT = ("Systolic Arrays Revisited\nJane Doe\nMIT\nISCA 2025\n"
                    "Abstract: a survey of dataflow variants ...")
_META_PAYLOAD = {
    "is_paper": True, "title": "Systolic Arrays Revisited",
    "authors": [{"name": "Jane Doe", "affiliations": ["MIT"]}],
    "venue": "ISCA", "year": 2025, "doi": "", "keywords": [],
}


class _FakeMetaLLM:
    """chat_json 返回同一 payload 的 JSON 序列化;calls 加锁(backfill 用线程池
    并发调用同一实例,镜像 test_paper_meta_service.py::_FakeKgLLM)。构造后即
    configured=True,签名上不依赖 OpenAICompatibleClient 的真实构造参数——见
    _patch_fake_llm 的工厂 lambda 如何把它套进去。"""

    def __init__(self):
        self.configured = True
        self.model = "fake-meta"
        self.calls = 0
        self._lock = threading.Lock()

    def chat_json(self, messages, schema_hint, **kwargs):
        with self._lock:
            self.calls += 1
        return json.dumps(_META_PAYLOAD)


def _patch_fake_llm(monkeypatch) -> _FakeMetaLLM:
    """main() 内部自建新 SQLiteRepository 实例,其 RuntimeModelProvider 在
    __init__ 里无条件构造 system 主 client = OpenAICompatibleClient(settings)
    (kg/reasoning/rewrite 各自的专属 client 视 *_configured 决定是否再构造一个,
    否则回落到这个 system 主 client)。故在工厂类级打桩,而非 fixture 的 repo
    实例属性——镜像 test_main_embed_end_to_end_zeroes_missing 对 make_embedder
    的做法(instance 级属性对 main() 自建的实例不可见)。同一 fake 会同时顶替
    repo.llm_client 与 repo.kg_llm_client 的解析结果(system 回落链),两个都
    configured=True,足以通过 run_metadata 的门控并支撑实际抽取调用。"""
    fake = _FakeMetaLLM()
    monkeypatch.setattr("app.services.model_provider.OpenAICompatibleClient",
                        lambda settings, **kw: fake)
    return fake


def _insert_meta_source(repo, notebook_id, source_id):
    """建一个 parsed 状态的 academic_paper 源(doc_type='' 落 academic_paper 默认
    分支),文本走 source_elements(file_path 用 .pdf 扩展名,故 read_source_text
    走 element 拼接分支,不依赖真实磁盘文件)。镜像 test_paper_meta_service.py
    ::_insert_source。"""
    store = repo._runtime.source_store
    store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=f"Doc {source_id}",
        source_type="document", status="parsed", parse_status="parsed",
        file_name=f"{source_id}.pdf", file_path=f"/tmp/{source_id}.pdf",
        file_size=0, file_hash=f"h-{source_id}", summary="", doc_type="",
    )
    with repo._write() as db:
        store.replace_elements(
            db, source_id,
            [SourceElementWrite(id=f"el-{source_id}-0001", element_type="text",
                                 location_label="", text=_META_HEAD_TEXT, metadata={})],
            created_at="2026-01-01T00:00:00",
        )


def test_paper_metadata_backfill_uses_source_job_concurrency(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb-meta-workers")
    service = repo._runtime.source_ingestion
    source_ids = [f"src-meta-{i}" for i in range(12)]
    repo.settings.kg_job_concurrency = 12
    repo.settings.kg_extract_workers = 3
    lock = threading.Lock()
    active = 0
    peak = 0
    # 结构性判据,不是墙钟判据:12 个 worker 必须全部到齐 barrier 才有人能继续。
    # 每个 worker 在 wait() 之前就已经 active += 1,所以「最后一个到齐」这一刻
    # active 必然等于 12 —— peak == 12 由 barrier 保证,而不是靠「40ms 窗口内恰好
    # 都在跑」。后者在本仓库默认的 -n 12 并行下会被兄弟 worker 进程抢 CPU 打散,
    # 实测偶发 peak == 11。参见 test_bulk_write_fairness.py 里同类问题的处理:
    # 负载相关的 flake 不能靠放宽阈值消除,只能换成结构性断言。
    barrier = threading.Barrier(12, timeout=10)
    barrier_broke = threading.Event()

    monkeypatch.setattr(
        service.sources,
        "sources_missing_paper_meta",
        lambda notebook_id, include_existing=False: source_ids,
    )
    monkeypatch.setattr(
        service.sources,
        "get_source",
        lambda source_id: SimpleNamespace(
            id=source_id,
            notebook_id=nb_id,
            type="document",
        ),
    )
    monkeypatch.setattr(service, "_publish_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        service, "_notify_paper_meta_done", lambda *args, **kwargs: None
    )

    def ensure(source, force=False):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # 并发度不足 12(或第一个到齐者已超时把 barrier 打破)。记标志后
                # 正常返回,让剩下的 worker 立即穿过已破的 barrier —— 测试干净地
                # 红在下面的断言上,而不是挂住。
                barrier_broke.set()
            return "stored"
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(service, "ensure_paper_metadata", ensure)
    counts = service.backfill_paper_metadata(nb_id)

    assert not barrier_broke.is_set(), (
        "12 个 worker 没能同时在途 —— backfill 的并发度低于 "
        f"kg_job_concurrency=12(最高只观察到 {peak} 路同时活动)"
    )
    assert counts["total"] == 12
    assert counts["stored"] == 12
    assert peak == 12


def test_metadata_phase_requires_llm(repo, capsys):
    """kg_llm 与 llm 均未配置(fixture 默认环境,未打桩)→ main 返回 2,stderr 含
    「LLM 未配置」,且门控先于 --notebook-id 检查(即便给了合法 notebook 也拦)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-meta")

    rc = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc == 2
    assert "LLM 未配置" in capsys.readouterr().err


def test_metadata_phase_requires_notebook(repo, monkeypatch, capsys):
    """LLM 已配但未给 --notebook-id → 返回 2,且绝不新建 notebook。"""
    _patch_fake_llm(monkeypatch)
    with repo._connect() as db:
        before = db.execute("SELECT COUNT(*) c FROM notebooks").fetchone()["c"]

    rc = bi.main(["metadata"])

    assert rc == 2
    assert "--notebook-id" in capsys.readouterr().err
    with repo._connect() as db:
        after = db.execute("SELECT COUNT(*) c FROM notebooks").fetchone()["c"]
    assert after == before


def test_metadata_phase_backfills(repo, monkeypatch, capsys):
    """FakeLLM + 2 个缺 meta 源 → 返回 0;两源 get_paper_meta 非 None;再跑一次
    (幂等续跑)输出 total 0。"""
    _patch_fake_llm(monkeypatch)
    nb_id = bi.ensure_notebook(repo, None, "nb-meta")
    _insert_meta_source(repo, nb_id, "src-1")
    _insert_meta_source(repo, nb_id, "src-2")

    rc = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "[meta done]" in out
    assert repo.get_paper_meta("src-1") is not None
    assert repo.get_paper_meta("src-2") is not None

    rc2 = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert '"total": 0' in out2


def test_metadata_phase_does_not_require_embedding_provider(
    repo, monkeypatch, capsys
):
    _patch_fake_llm(monkeypatch)
    monkeypatch.setenv("EMBED_PROVIDER", "")
    nb_id = bi.ensure_notebook(repo, None, "nb-meta-no-embed")
    _insert_meta_source(repo, nb_id, "src-no-embed")

    rc = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc == 0
    assert "[meta done]" in capsys.readouterr().out


def test_metadata_phase_force(repo, monkeypatch, capsys):
    """--force → 已有元数据行的源也重抽(fake.calls 增加)。"""
    fake = _patch_fake_llm(monkeypatch)
    nb_id = bi.ensure_notebook(repo, None, "nb-meta")
    _insert_meta_source(repo, nb_id, "src-1")
    rc = bi.main(["metadata", "--notebook-id", nb_id])
    assert rc == 0
    calls_after_first = fake.calls
    assert calls_after_first >= 1

    rc2 = bi.main(["metadata", "--notebook-id", nb_id, "--force"])

    assert rc2 == 0
    assert fake.calls > calls_after_first


def test_run_all_reparses_existing_source_missing_elements(repo, tmp_path, monkeypatch):
    """已存在但无 source_elements 的 source(上次 parse 未落 elements)必须走 process_source
    重新 parse 补 elements,而不是 extract_source 空抽。否则 build_records 的接地校验没有
    elements 可对照 → 每个 LLM 抽出的节点被丢弃 → objects=0(kg-ingest-count 根因)。
    行为证据:跑完后该源真的有了 elements(reparse 执行了 parse),而非停留在 0。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-reparse")
    d = tmp_path / "docs"
    d.mkdir()
    p = d / "doc0.md"
    p.write_text("# Title\n\nBody paragraph " + "z" * 200, encoding="utf-8")
    digest = bi.sha256_bytes(p.read_bytes())
    sid = "src-noel-0"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:   # 预置已存在源,但不插 source_elements(elements 空)
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, nb_id, "S", "document", p.name, str(p), 0, digest,
             "", "", "parsed", now, now))
    # patch 掉真实抽取/rebuild(与既有 run_all 测试同款),让 process_source 只跑到 parse
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda s: None)
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_all(repo, nb_id, [p], conc=1)

    assert res["new"] == 0 and res["resumed"] == 0 and res["reparsed"] == 1
    with repo._connect() as db:  # reparse 真的 parse 了 .md → 有 elements(不是空抽)
        n_el = db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id=?",
                          (sid,)).fetchone()["c"]
    assert n_el > 0


def test_run_reparse_only_targets_sources_missing_elements(repo, tmp_path, monkeypatch):
    """reparse 子命令:只对 n_el=0(未成功 parse)的存量源重跑 process_source 补 elements,
    已有 elements 的源跳过。修复历史 run_all 分流把无-elements 源空抽(objects=0)的存量数据。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-reparse-cmd")
    now = "2026-01-01T00:00:00"
    d = tmp_path / "docs"
    d.mkdir()
    pa = d / "a.md"; pa.write_text("# A\n\nBody paragraph " + "z" * 200, encoding="utf-8")
    pb = d / "b.md"; pb.write_text("# B\n\nBody paragraph " + "y" * 200, encoding="utf-8")
    with repo._write() as db:
        for sid, p in (("src-a", pa), ("src-b", pb)):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, sid, "document", p.name, str(p), 0, sid, "", "", "parsed", now, now))
        db.execute(  # src-b 已有 elements → reparse 应跳过它
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-b-0", "src-b", "paragraph", "p1", "existing", "{}", now))
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda s: None)
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)

    res = bi.run_reparse(repo, nb_id, conc=1)

    assert res["reparsed"] == 1                    # 只有 src-a(无 elements)被 reparse
    with repo._connect() as db:
        na = db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id='src-a'").fetchone()["c"]
        n_b = db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id='src-b'").fetchone()["c"]
    assert na > 0                                  # src-a 被 reparse 补出 elements
    assert n_b == 1                                # src-b 未动(仍是预置的 1 条)


def test_main_reparse_requires_notebook_id(repo, capsys):
    rc = bi.main(["reparse"])
    assert rc == 2
    assert "notebook-id" in capsys.readouterr().err


def test_main_reparse_backfills_missing_elements(repo, tmp_path, monkeypatch):
    """reparse 子命令端到端:对无 elements 的存量源重新 parse 补 elements(LLM 未配 →
    抽取走 no-llm no-op,但 parse 真跑)。--no-rebuild 跳过收尾聚类。"""
    monkeypatch.setenv("EMBED_PROVIDER", "")       # main 自建 repo → embedder 未配
    nb_id = bi.ensure_notebook(repo, None, "nb-rp")
    now = "2026-01-01T00:00:00"
    p = tmp_path / "a.md"
    p.write_text("# A\n\nBody paragraph " + "z" * 200, encoding="utf-8")
    with repo._write() as db:   # 预置无 elements 的存量源
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-x", nb_id, "X", "document", p.name, str(p), 0, "hx", "", "", "parsed", now, now))

    rc = bi.main(["reparse", "--notebook-id", nb_id, "--allow-no-embed", "--no-rebuild"])

    assert rc == 0
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        n_el = db.execute(
            "SELECT COUNT(*) c FROM source_elements WHERE source_id='src-x'").fetchone()["c"]
    assert n_el > 0                                 # reparse 真的 parse 了 → 有 elements


def test_run_reparse_disables_incremental_fusion_during_run(repo, tmp_path, monkeypatch):
    """run_reparse 批量期必须关 per-source 增量融合。否则每源抽完都触发
    incremental_fuse_source(加载整库 cluster_map + 写 concept_clusters),几万源的库
    O(N²) 卡死;收尾的 rebuild_unified_kg 已做一次全量融合(同 run_all/run_kg)。结束恢复原值。"""
    monkeypatch.setattr(repo, "llm_client", _StubLLM())
    nb_id = bi.ensure_notebook(repo, None, "nb-fuse")
    now = "2026-01-01T00:00:00"
    p = tmp_path / "a.md"
    p.write_text("# A\n\nBody paragraph " + "z" * 200, encoding="utf-8")
    with repo._write() as db:   # 无 elements 源 → reparse target → 走到末尾 rebuild
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-f", nb_id, "F", "document", p.name, str(p), 0, "hf", "", "", "parsed", now, now))
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda s: None)
    seen = {}

    def _spy_rebuild(nb, progress=None, force=False, fresh=False):
        seen["fusion_during"] = repo.settings.kg_incremental_fusion_enabled
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _spy_rebuild)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb, conc: 0)
    orig = repo.settings.kg_incremental_fusion_enabled

    bi.run_reparse(repo, nb_id, conc=1)

    assert seen["fusion_during"] is False                        # 批量期关了 per-source 融合
    assert repo.settings.kg_incremental_fusion_enabled == orig   # 结束恢复原值
