# backend/tests/test_batch_ingest.py
import json
import inspect
import signal
import threading
import time
import pytest
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from app.core.config import Settings
from app.core.request_context import get_request_user, set_request_user, reset_request_user
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services import batch_ingest as bi
from tests.model_testkit import (
    RecordingModelProvider,
    bind_chat_client,
    bind_embedding_client,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repository with explicit system workload bindings."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    embedder = FakeEmbedder(dim=16)
    provider = RecordingModelProvider(
        embedding_clients={
            workload: embedder
            for workload in (
                "retrieval_query_embedding",
                "source_element_embedding",
                "chunk_embedding",
                "knowledge_object_embedding",
                "relation_embedding",
                "memory_embedding",
                "knowhow_embedding",
            )
        },
        parallelism_by_workload={
            "source_element_embedding": 2,
            "chunk_embedding": 2,
            "knowledge_object_embedding": 2,
            "relation_embedding": 2,
        },
    )
    r = SQLiteRepository(Settings(model_services_config=""), model_provider=provider)

    @contextmanager
    def _open_test_repository(_settings, **_kwargs):
        yield r

    monkeypatch.setattr(
        bi, "open_maintenance_cli_repository", _open_test_repository
    )
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


def test_discover_files_matches_iter_files_and_groups_excluded(tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    (d / "sub" / "raw.docx").write_bytes(b"docx")
    (d / "noext").write_text("no extension", encoding="utf-8")
    files, excluded = bi.discover_files(d)
    assert files == bi.iter_files(d)          # 受支持文件列表与 iter_files 逐位相同
    assert set(excluded) == {".txt", ".docx", "(无扩展名)"}
    assert excluded[".txt"] == {"count": 1, "examples": ["ignore.txt"]}
    assert excluded[".docx"]["count"] == 1
    assert excluded[".docx"]["examples"] == [str(Path("sub") / "raw.docx")]
    assert excluded["(无扩展名)"]["examples"] == ["noext"]


def test_discover_files_bounds_examples_but_counts_all(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    n = bi._EXCLUDED_EXAMPLES_PER_EXT + 3
    for i in range(n):
        (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    _, excluded = bi.discover_files(d)
    assert excluded[".txt"]["count"] == n
    assert excluded[".txt"]["examples"] == [
        f"f{i:02d}.txt" for i in range(bi._EXCLUDED_EXAMPLES_PER_EXT)
    ]


def test_report_excluded_files_prints_and_logs(capsys):
    entries = []
    bi.report_excluded_files(
        {
            ".txt": {"count": 7, "examples": ["a.txt", "b.txt"]},
            ".docx": {"count": 1, "examples": ["c.docx"]},
        },
        log=entries.append,
    )
    out = capsys.readouterr().out
    assert "[warn]" in out and "8 个文件" in out
    assert ".txt: 7" in out and "(+5 more)" in out
    assert ".docx: 1" in out and "(+0 more)" not in out
    assert entries == [{
        "phase": "excluded_files",
        "total": 8,
        "by_ext": {".docx": 1, ".txt": 7},
    }]


def test_report_excluded_files_silent_when_empty(capsys):
    bi.report_excluded_files({}, log=None)
    assert capsys.readouterr().out == ""


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
    counts = bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)
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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)  # 全量嵌入

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

    n = bi.backfill_chunk_embeddings(repo, nb_id, missing_only=True)

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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)
    assert bi.backfill_chunk_embeddings(repo, nb_id, missing_only=True) == 0


def test_backfill_chunk_embeddings_default_full_reembed(repo, tmp_path):
    """missing_only 默认 False:仍走全量(遍历 source),返回处理的 source 数。"""
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert bi.backfill_chunk_embeddings(repo, nb_id) == nsrc   # 默认=全量按 source


def test_run_ingest_dedup_skips_on_rerun(repo, tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    files = bi.iter_files(d)
    bi.run_ingest(repo, nb_id, files, workers=1)
    counts2 = bi.run_ingest(repo, nb_id, files, workers=1)
    assert counts2["uploaded"] == 0 and counts2["skipped"] == 3
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb_id,)).fetchone()["c"]
    assert nsrc == 3


def test_run_ingest_releases_the_claim_when_the_claimant_fails(repo, tmp_path, monkeypatch):
    """认领只代表「这个内容由我这一份来做」,不代表已摄取。认领者失败必须交回认领,
    否则后面同内容的副本会看到 digest 已被认领而直接 skipped —— 一次瞬时失败
    (存储/SQLite 抖动)就让这份内容整轮都进不来,而在有认领集合之前它本可以由下一个
    副本重试成功。workers=1 保证副本按序处理,让「先失败、后重试」可确定性复现。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-claim-release")
    body = "# Same\n\nIdentical body " + "r" * 200
    a = tmp_path / "a.md"; a.write_text(body, encoding="utf-8")
    b = tmp_path / "b.md"; b.write_text(body, encoding="utf-8")

    # 打桩点选在 repo.maintenance 上(认领之后、upload 之前的那次查库),刻意**不**给
    # facade 的 repo.upload_sources 打桩:那会往冻结的 facade 契约里塞 test-only patch 记录。
    real_lookup = repo.maintenance.source_id_by_hash
    calls = {"n": 0}

    def _flaky(notebook_id_, digest_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sqlite blip")
        return real_lookup(notebook_id_, digest_)

    monkeypatch.setattr(repo.maintenance, "source_id_by_hash", _flaky)

    counts = bi.run_ingest(repo, nb_id, [a, b], workers=1)

    assert counts["failed"] == 1
    assert counts["uploaded"] == 1, "认领没被交回,第二个副本失去了重试机会"
    assert counts["skipped"] == 0
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"] == 1


def test_run_ingest_same_run_duplicate_files_skip_not_reparse(repo, tmp_path):
    """同一次运行里两个内容相同的文件:第一个 uploaded、第二个 skipped。

    没有本次运行的认领集合时,第二个要多跑一次 source_id_by_hash 查库才能判出重复;
    认领集合让它在做任何事之前就短路。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-dup")
    body = "# Same\n\nIdentical body " + "q" * 200
    a = tmp_path / "a.md"; a.write_text(body, encoding="utf-8")
    b = tmp_path / "b.md"; b.write_text(body, encoding="utf-8")

    counts = bi.run_ingest(repo, nb_id, [a, b], workers=1)

    assert counts["uploaded"] == 1 and counts["skipped"] == 1
    assert counts["failed"] == 0
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (nb_id,)).fetchone()["c"]
    assert nsrc == 1                                # 同内容只建一个源


def test_run_kg_disables_fusion_and_rebuilds(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubLLM())
    calls = {}

    def fake_build(
        nb, *, progress=None, target_limit=None, retry_partial=False,
        finalize=None, skip_policy=None,
    ):
        calls["fusion_flag_during"] = repo.settings.kg_incremental_fusion_enabled
        calls["build_nb"] = nb
        result = {"built": ["s1", "s2"], "failed": [], "skipped": []}
        result.update(finalize(result) or {})
        return result

    def fake_rebuild(nb, progress=None, force=False, fresh=False):
        calls.setdefault("finalize_order", []).append("rebuild")
        calls["rebuild_nb"] = nb
        return 7

    monkeypatch.setattr(repo, "build_notebook_kg", fake_build)
    monkeypatch.setattr(repo, "rebuild_unified_kg", fake_rebuild)
    monkeypatch.setattr(
        bi,
        "backfill_node_embeddings",
        lambda *_args: calls.setdefault("finalize_order", []).append("embed") or 0,
    )
    res = bi.run_kg(repo, nb_id, limit=None)
    assert res["extracted"] == 2 and res["failed"] == 0
    assert res["clusters"] == 7
    assert calls["fusion_flag_during"] is False
    assert calls["build_nb"] == nb_id and calls["rebuild_nb"] == nb_id
    assert calls["finalize_order"] == ["embed", "rebuild"]


class _StubKgChatClient:
    configured = True
    model = "test-kg"

    def chat_json(self, messages, response_schema_hint, **kwargs):  # pragma: no cover
        raise AssertionError("单飞守卫应在任何模型调用之前挡回")


def test_main_reports_already_running_kg_job_without_a_traceback(
    repo, monkeypatch, capsys
):
    """单飞守卫挡回时 CLI 必须给出可操作说明并以 2 退出,而不是把 sqlite 的
    UNIQUE 约束 traceback 糊到运维脸上(该异常正是从 build_notebook_kg 冒出来的)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubKgChatClient())
    repo._runtime.kg_build_jobs.create_job(nb_id, "u", "incremental", 812)
    with repo._write() as db:
        db.execute(
            "UPDATE kg_build_jobs SET stage='extracting', completed_sources=3, "
            "updated_at='2026-07-20T00:00:00+00:00' WHERE notebook_id=?",
            (nb_id,),
        )

    rc = bi.main(["kg", "--notebook-id", nb_id, "--allow-no-embed"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "进行中" in err
    assert nb_id in err
    assert "extracting" in err and "3/812" in err
    assert "重启后端服务" in err
    assert "Traceback" not in err


def test_report_already_running_still_prints_paths_when_details_unreadable(
    capsys,
):
    """诊断信息读不到也必须打印出路——不能因为取现存任务失败就把异常再抛出去,
    否则运维拿到的仍是 traceback。"""
    class _Unreadable:
        def get_notebook(self, _notebook_id):
            raise RuntimeError("catalog unavailable")

    bi._report_kg_build_already_running(_Unreadable(), "nb-xyz")

    err = capsys.readouterr().err
    assert "nb-xyz" in err
    assert "重启后端服务" in err
    assert "现存任务" not in err


def test_main_interrupt_exits_cleanly_with_shell_convention(
    repo, monkeypatch, capsys
):
    """Ctrl-C 不该以 traceback 收场;收尾在被中断的那层完成,这里只给 130。
    顺带钉住会排空的那条路径(build_notebook_kg)确实被终止信号转换包住了,并在
    返回时还原(没装/装完不还原都算回归)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubLLM())
    during = {}
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def _interrupt(nb, **_kwargs):
        during["sigterm"] = signal.getsignal(signal.SIGTERM)
        raise KeyboardInterrupt("ctrl-c")

    monkeypatch.setattr(repo, "build_notebook_kg", _interrupt)

    rc = bi.main(["kg", "--notebook-id", nb_id, "--allow-no-embed"])

    assert rc == 130
    assert "已中断" in capsys.readouterr().err
    assert callable(during["sigterm"])
    assert during["sigterm"] is not signal.SIG_DFL
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_limited_kg_uses_durable_termination_signals(
    repo, monkeypatch, capsys
):
    """limit 分支也必须进入 durable job，因而使用同一终止信号协议。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubLLM())
    bind_chat_client(repo, "kg_extract", _StubKgChatClient())
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    observed = {}

    def _build(_notebook_id, **_kwargs):
        observed["sigterm"] = signal.getsignal(signal.SIGTERM)
        observed["sighup"] = signal.getsignal(signal.SIGHUP)
        return {"built": [], "failed": []}

    monkeypatch.setattr(repo, "build_notebook_kg", _build)

    rc = bi.main(
        ["kg", "--notebook-id", nb_id, "--limit", "5", "--allow-no-embed"]
    )

    assert rc == 0
    assert callable(observed["sigterm"])
    assert callable(observed["sighup"])
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_limited_kg_interrupt_cancels_and_drains_accepted_workers(
    repo, monkeypatch
):
    """limit 路径委托 durable lifecycle，由它负责取消并排空 worker。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubKgChatClient())
    seen = {}

    def _interrupt(_notebook_id, **kwargs):
        seen.update(kwargs)
        raise KeyboardInterrupt("ctrl-c")
    monkeypatch.setattr(repo, "build_notebook_kg", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        bi.run_kg(repo, nb_id, limit=1, no_rebuild=True)

    assert seen["target_limit"] == 1
    assert seen["retry_partial"] is False
    assert seen["finalize"] is not None


def test_cancel_and_drain_absorbs_repeated_interrupt_during_cancellation():
    """A second Ctrl-C inside the cleanup window must not release DB ownership."""
    class InterruptOnceFuture:
        def __init__(self):
            self.calls = 0

        def cancel(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt("second ctrl-c")
            return True

    future = InterruptOnceFuture()
    bi._cancel_and_drain_futures([future])
    assert future.calls == 2


def test_tracked_submission_is_drainable_if_submit_interrupts_after_acceptance():
    accepted: set[Future[object]] = set()
    started = threading.Event()
    release = threading.Event()
    threads = []

    def _work():
        started.set()
        assert release.wait(timeout=5)
        return "done"

    def _submit(wrapper, *args):
        worker = threading.Thread(target=wrapper, args=args)
        threads.append(worker)
        worker.start()
        assert started.wait(timeout=5)
        raise KeyboardInterrupt("accepted before submit returned")

    with pytest.raises(KeyboardInterrupt):
        bi._submit_tracked_future(_submit, accepted, _work)

    assert len(accepted) == 1
    completion = next(iter(accepted))
    assert completion.running()
    release.set()
    bi._cancel_and_drain_futures(accepted)
    for worker in threads:
        worker.join(timeout=5)
    assert completion.result() == "done"


def test_run_ingest_repeated_interrupt_cannot_escape_worker_drain(
    repo, tmp_path, monkeypatch
):
    path = tmp_path / "blocked.md"
    path.write_text("# blocked", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    real_submit_tracked = bi._submit_tracked_future
    real_wait = bi.wait
    waits = 0

    def _upload(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=5)
        returned.set()
        return []

    class _InterruptingResult:
        def result(self):
            assert started.wait(timeout=5)
            raise KeyboardInterrupt("first ctrl-c while ingest waits")

    def _submit(*args, **kwargs):
        real_submit_tracked(*args, **kwargs)
        return _InterruptingResult()

    def _wait(futures, *args, **kwargs):
        nonlocal waits
        waits += 1
        if waits == 1:
            release.set()
            raise KeyboardInterrupt("second ctrl-c during ingest drain")
        return real_wait(futures, *args, **kwargs)

    monkeypatch.setattr(repo, "upload_sources", _upload)
    monkeypatch.setattr(bi, "_submit_tracked_future", _submit)
    monkeypatch.setattr(bi, "wait", _wait)

    with pytest.raises(KeyboardInterrupt):
        bi.run_ingest(repo, "nb-unused", [path], workers=1)

    assert returned.is_set()
    assert waits >= 2


def test_limited_kg_interrupt_during_submission_drains_already_accepted_worker(
    repo, monkeypatch
):
    """limit 的提交窗口也由 durable lifecycle 单点负责。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-submit-interrupt")
    bind_chat_client(repo, "kg_extract", _StubKgChatClient())
    seen = {}

    def _build(_notebook_id, **kwargs):
        seen.update(kwargs)
        raise KeyboardInterrupt("ctrl-c while submitting")
    monkeypatch.setattr(repo, "build_notebook_kg", _build)

    with pytest.raises(KeyboardInterrupt):
        bi.run_kg(repo, nb_id, limit=2, no_rebuild=True)

    assert seen["target_limit"] == 2


def test_reparse_interrupt_during_submission_drains_already_accepted_worker(
    repo, monkeypatch
):
    nb_id = bi.ensure_notebook(repo, None, "nb-reparse-submit-interrupt")
    accepted: Future[object] = Future()
    tracked = []
    calls = 0
    monkeypatch.setattr(
        repo.maintenance,
        "source_ids_missing_elements_page",
        lambda *_args, **_kwargs: ["src-one", "src-two"],
    )

    def _submit(_wrapper, completion, *_args, **_kwargs):
        nonlocal calls
        tracked.append(completion)
        calls += 1
        if calls == 1:
            return accepted
        raise KeyboardInterrupt("ctrl-c while submitting")

    monkeypatch.setattr("app.services.kg.scheduler.submit_job", _submit)

    with pytest.raises(KeyboardInterrupt):
        bi.run_reparse(repo, nb_id, limit=2, no_rebuild=True)

    assert len(tracked) == 2 and all(future.cancelled() for future in tracked)


def test_main_interrupt_after_a_committed_build_still_reports_130(
    repo, monkeypatch, capsys
):
    """反向护栏(codex 评审 P2 的后半条被驳回,见 PR 说明):即使抽取任务本身已成功
    提交,信号打断的仍是**整条批处理**——run_kg 后面的聚类重建与补向量都没跑完,
    所以必须给 130 + 「已中断」,不能因为 job 行是 succeeded 就报成功退出 0。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bind_chat_client(repo, "kg_extract", _StubLLM())

    def _succeed_then_interrupt(nb, **_kwargs):
        # 模拟「分析已提交,信号在返回前落下」
        raise KeyboardInterrupt("ctrl-c right after commit")

    monkeypatch.setattr(repo, "build_notebook_kg", _succeed_then_interrupt)

    rc = bi.main(["kg", "--notebook-id", nb_id, "--allow-no-embed"])

    assert rc == 130
    assert "已中断" in capsys.readouterr().err


def test_install_termination_signals_converts_once_absorbs_repeats_and_restores():
    previous_sigint = signal.getsignal(signal.SIGINT)
    # pytest-xdist workers may inherit SIGINT=SIG_IGN from their controller.
    # Establish the callable precondition this test exercises, then restore the
    # worker's original state so ignored-signal preservation remains intact.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        saved = bi._install_termination_signals()
        try:
            assert signal.SIGTERM in [signum for signum, _ in saved]
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
            sigint_handler = signal.getsignal(signal.SIGINT)
            assert callable(sigint_handler)
            sigint_handler(signal.SIGINT, None)
        finally:
            bi._restore_signals(saved)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    assert signal.getsignal(signal.SIGINT) is previous_sigint


def test_install_termination_signals_keeps_ignored_sighup_ignored():
    """`nohup` 把 SIGHUP 置为 SIG_IGN;抢回来会让本来能扛住 SSH 掉线的长跑批处理
    在断连时被杀掉,所以已忽略的信号必须保持忽略。"""
    previous = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        saved = bi._install_termination_signals()
        try:
            assert signal.SIGHUP not in [signum for signum, _ in saved]
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        finally:
            bi._restore_signals(saved)
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_install_termination_signals_noop_off_the_main_thread():
    """signal.signal 只能在主线程调用;工作线程里必须原样退回,不能抛。"""
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(saved=bi._install_termination_signals())
    )
    worker.start()
    worker.join()
    assert result["saved"] == []


def test_main_dry_run_lists_files(repo, tmp_path, capsys):
    d = _make_md_dir(tmp_path, n=2)
    rc = bi.main(["ingest", "--input-dir", str(d), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out and "3 files" in out


def test_main_postgres_mutation_requires_stopped_service_confirmation_before_open(
    monkeypatch, capsys
):
    from app.services import maintenance_cli

    secret_url = (
        "postgresql://batch-user:do-not-leak@db.example.test:5432/"
        "silicon_notebook?sslmode=require"
    )
    monkeypatch.setenv("DATABASE_URL", secret_url)
    constructed = False

    def _unexpected_repository(_settings):
        nonlocal constructed
        constructed = True
        raise AssertionError("PostgreSQL mutation must not open a repository")

    monkeypatch.setattr(
        maintenance_cli, "create_repository", _unexpected_repository
    )

    rc = bi.main(["index", "--notebook-id", "nb-existing"])

    captured = capsys.readouterr()
    assert rc == 2
    assert constructed is False
    assert "--confirm-service-stopped" in captured.err
    assert "PostgreSQL" in captured.err
    combined_output = captured.out + captured.err
    assert secret_url not in combined_output
    assert "do-not-leak" not in combined_output


def test_arg_parser_help_documents_dual_backend_safety_gate(capsys):
    with pytest.raises(SystemExit) as exc_info:
        bi.main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "SQLite/PostgreSQL" in output
    assert "--confirm-service-stopped" in output


def test_main_postgres_dry_run_remains_filesystem_only(
    monkeypatch, tmp_path, capsys
):
    d = _make_md_dir(tmp_path, n=2)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://batch-user:do-not-leak@db.example.test:5432/silicon_notebook",
    )

    @contextmanager
    def _unexpected_repository(_settings, **_kwargs):
        raise AssertionError("dry-run must not open a repository")
        yield  # pragma: no cover

    monkeypatch.setattr(
        bi, "open_maintenance_cli_repository", _unexpected_repository
    )

    rc = bi.main(["ingest", "--input-dir", str(d), "--dry-run"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "dry-run" in captured.out and "3 files" in captured.out
    assert "do-not-leak" not in captured.out + captured.err


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
    monkeypatch.setattr("app.services.source_ingestion.SourceIngestionService.run_extraction", lambda self, sid, **_kw: None)
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg",
                        lambda self, nb, progress=None, force=False, fresh=False: 0)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)
    rc = bi.main(["all", "--input-dir", str(d), "--notebook-name", "X", "--workers", "1",
                  "--allow-no-embed"])
    assert rc == 0
    r2 = repo
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
            source_id = f"src-lim-{i}"
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, nb_id, f"S{i}", "document", f"s{i}.md", f"/tmp/s{i}.md",
                 0, f"h{i}", "", "", "parsed", now, now))
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-lim-{i}", source_id, "paragraph", "L1", "text", "{}", now),
            )
    extracted_calls = []
    monkeypatch.setattr(
        repo.maintenance,
        "kg_target_source_rows_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy eligible-target query must not run")
        ),
    )
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda sid, **_kwargs: extracted_calls.append(sid),
    )
    monkeypatch.setattr(repo._runtime.source_ingestion, "set_source_status", lambda *a, **k: None)

    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: 0)

    repo._runtime.models.chat_clients = {
        **repo._runtime.models.chat_clients,
        "kg_extract": _StubLLM(),
    }
    res = bi.run_kg(repo, nb_id, limit=2)
    assert res["extracted"] == 2
    assert len(extracted_calls) == 2          # 只抽前 2 个未抽源(targets[:limit])
    latest = repo._runtime.kg_build_jobs.latest(nb_id)
    assert latest["status"] == "succeeded" and latest["total_sources"] == 2


class _StubLLM:
    configured = True; chat_json = lambda self, messages, response_schema_hint, **kwargs: '{"ok":true}'


def _bind_chat(repo, workload_id, client):
    repo._runtime.models.chat_clients = {
        **repo._runtime.models.chat_clients,
        workload_id: client,
    }


def test_run_kg_limit_uses_initialized_business_job_pool(repo, monkeypatch):
    """run_kg 走的是业务作业池(并发跑),不是串行。

    并发**用栅栏证明,不用睡眠赌**:原来的写法让每个 extract 睡 40ms、再断言峰值恰好
    等于池宽,赌的是"这么多线程都能在 40ms 的窗口里被调度起来"。整套测试的负载一涨
    (本仓库每加几个用例就会),这一赌就会输,而输的表现是一条与被测特性毫不相干的红
    ——已经实测复现过。栅栏把它变成确定的:前 `expected` 次调用各自登记完就在栅栏上
    等,峰值只有在它们**真的同时在跑**时才够得着 `expected`;池子若比声称的窄,栅栏
    永远凑不齐、超时抛错,测试照样红,而且红得就是它本来要抓的那件事。

    栅栏只拦前 `expected` 次:`threading.Barrier` 放行后会自动重置,让剩下的作业也去
    等,它们会凑不齐人数而白白超时。
    """
    from app.services.kg import scheduler
    lock = threading.Lock()
    active = 0
    peak = 0
    targets = [f"src-{i}" for i in range(6)]
    expected = min(len(targets), scheduler.job_concurrency())
    # 超时只在"池子没那么宽"这种真失败上才付,正常路径瞬间放行。
    gate = threading.Barrier(expected, timeout=30)
    arrived = 0

    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(
        lifecycle,
        "_kg_target_batches",
        lambda *_args, **_kwargs: iter([(
            [(sid, False) for sid in targets], [], []
        )]),
    )
    monkeypatch.setattr(
        lifecycle,
        "_reconcile_extracted_terminal",
        lambda sid, extract_one: extract_one(sid),
    )
    repo._runtime.models.chat_clients = {
        **repo._runtime.models.chat_clients,
        "kg_extract": _StubLLM(),
    }

    def extract(source_id, **_kwargs):
        nonlocal active, peak, arrived
        with lock:
            active += 1
            peak = max(peak, active)
            wait_at_gate = arrived < expected
            if wait_at_gate:
                arrived += 1
        try:
            if wait_at_gate:
                gate.wait()
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(lifecycle, "_run_extraction", extract)
    bi.run_kg(
        repo,
        bi.ensure_notebook(repo, None, "nb-kg-limit"),
        limit=6,
        no_rebuild=True,
    )
    assert peak == expected


def test_run_kg_retry_partial_adds_safe_repair_targets(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb-retry-partial")
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(
        lifecycle,
        "_kg_target_batches",
        lambda *_args, **_kwargs: iter([(
            [("src-missing", False), ("src-partial", True)], [], []
        )]),
    )
    monkeypatch.setattr(lifecycle, "_set_source_status", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle,
        "_reconcile_extracted_terminal",
        lambda sid, extract_one: extract_one(sid),
    )
    seen = []

    def extract(
        source_id, *, preserve_existing_until_complete=False, **_kwargs
    ):
        seen.append((source_id, preserve_existing_until_complete))

    monkeypatch.setattr(lifecycle, "_run_extraction", extract)
    repo._runtime.models.chat_clients = {
        **repo._runtime.models.chat_clients,
        "kg_extract": _StubLLM(),
    }

    res = bi.run_kg(
        repo,
        nb_id,
        no_rebuild=True,
        retry_partial=True,
    )

    assert set(seen) == {
        ("src-missing", False),
        ("src-partial", True),
    }
    assert res["extracted"] == 2
    assert res["partial_retried"] == 1
    assert res["partial_failed_preserved"] == 0


def test_run_kg_finalizer_failure_marks_durable_job_failed(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb-finalizer-failure")
    _seed_sources(repo, nb_id, 1, "src-finalizer")
    _bind_chat(repo, "kg_extract", _StubLLM())
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(
        lifecycle, "_run_extraction", lambda _sid, **_kwargs: None
    )
    monkeypatch.setattr(
        repo,
        "rebuild_unified_kg",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("finalizer failed")
        ),
    )

    with pytest.raises(RuntimeError, match="finalizer failed"):
        bi.run_kg(repo, nb_id)

    latest = repo._runtime.kg_build_jobs.latest(nb_id)
    assert latest["status"] == "failed"
    assert latest["error_code"] == "internal_error"


def test_partial_kg_source_ids_uses_latest_run_and_requires_existing_graph(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb-partial-inventory")
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        for sid in ("src-partial", "src-clean", "src-empty", "src-retry-empty"):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, sid, "document", f"{sid}.md", f"/tmp/{sid}.md",
                 0, f"hash-{sid}", "", "", "parsed", now, now),
            )
        for sid, message in (
            ("src-partial", "kg objects=1 windows_failed=2/5"),
            ("src-clean", "kg objects=1 windows_failed=0/5"),
            ("src-empty", "kg objects=0 windows_failed=2/5"),
            ("src-retry-empty", "retry_incomplete=1 windows_failed=0/5 empty_result=1"),
        ):
            db.execute(
                "INSERT INTO extraction_runs "
                "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"run-{sid}", nb_id, sid, "kg", "completed", message, now, now),
            )
        for sid in ("src-partial", "src-clean", "src-retry-empty"):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"ko-{sid}", nb_id, "concept", "approved", "{}", "[]", sid, now, now),
            )

    assert repo.maintenance.partial_kg_source_ids(nb_id) == {
        "src-partial",
        "src-retry-empty",
    }


def test_bounded_source_inventories_exclude_hidden_projection_sources(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb-hidden-inventory")
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        for sid, source_type, has_elements in (
            ("src-doc-empty", "document", False),
            ("src-doc-ready", "document", True),
            ("src-knowhow", "knowhow", True),
            ("src-memory", "memory", True),
        ):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, nb_id, sid, source_type, f"{sid}.md", "", 0, f"hash-{sid}",
                 "", "", "parsed", now, now),
            )
            if has_elements:
                db.execute(
                    "INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) "
                    "VALUES (?,?,'paragraph','L1','text','{}',?)",
                    (f"el-{sid}", sid, now),
                )

    assert repo.maintenance.user_source_ids_page(nb_id) == [
        "src-doc-empty",
        "src-doc-ready",
    ]
    assert repo.maintenance.source_ids_missing_elements_page(nb_id) == [
        "src-doc-empty"
    ]
    assert repo.maintenance.kg_target_source_rows_page(nb_id) == [
        {"source_id": "src-doc-ready", "is_partial": False}
    ]
    other_nb = bi.ensure_notebook(repo, None, "nb-other")
    assert repo.maintenance.source_is_user_visible(nb_id, "src-doc-ready")
    assert not repo.maintenance.source_is_user_visible(other_nb, "src-doc-ready")
    assert not repo.maintenance.source_is_user_visible(nb_id, "src-knowhow")


def test_run_kg_rejects_retry_partial_with_rebuild_only(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb-retry-conflict")
    with pytest.raises(ValueError, match="retry_partial"):
        bi.run_kg(repo, nb_id, retry_partial=True, rebuild_only=True)


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
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    _bind_chat(repo, "kg_extract", _StubLLM())
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


def test_main_new_notebook_owner_context_reaches_scheduler_worker(
    repo, tmp_path, monkeypatch
):
    from app.services.kg.scheduler import submit_job

    owner = repo.create_user("b00123456", "pw123456")
    docs = tmp_path / "owner-new-docs"
    docs.mkdir()
    seen = {}

    def fake_run_ingest(repo_, notebook_id, files, **kwargs):
        seen["user_id"] = repo_.current_user().id
        seen["worker_user_id"] = submit_job(
            lambda: repo_.current_user().id
        ).result(timeout=5)
        return {"uploaded": 0, "skipped": 0, "failed": 0}

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
        "worker_user_id": owner.id,
    }
    assert get_request_user() is None
    with repo._connect() as db:
        created_by = db.execute(
            "SELECT created_by FROM notebooks WHERE name = ?",
            ("Owner notebook",),
        ).fetchone()["created_by"]
    assert created_by == owner.id


def test_main_existing_notebook_owner_context_resets_on_failure(
    repo, monkeypatch
):
    owner = repo.create_user("c00123456", "pw123456")
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
        raise RuntimeError("phase failed")

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
    }
    assert get_request_user() is None


def test_main_rejects_explicit_wrong_notebook_owner(repo, capsys):
    owner = repo.create_user("f00123456", "pw123456")
    wrong = repo.create_user("g00123456", "pw123456")
    token = set_request_user(owner)
    try:
        notebook_id = bi.ensure_notebook(repo, None, "Owned by F")
    finally:
        reset_request_user(token)

    rc = bi.main([
        "reparse",
        "--notebook-id",
        notebook_id,
        "--owner",
        wrong.username,
        "--allow-no-embed",
        "--no-rebuild",
    ])

    assert rc == 2
    assert "notebook owner mismatch" in capsys.readouterr().err


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
    assert "--workers" in normalized
    assert "--llm-conc" not in normalized
    assert "--embed-conc" not in normalized


def test_main_refuses_silent_no_embed(repo, tmp_path, monkeypatch, capsys):
    d = _make_md_dir(tmp_path, n=1)
    repo._runtime.models.embedding_clients = {}
    rc = bi.main(["ingest", "--input-dir", str(d), "--notebook-name", "X"])
    assert rc == 2
    assert "allow-no-embed" in capsys.readouterr().err


def test_main_allows_no_embed_with_flag(repo, tmp_path, monkeypatch):
    d = _make_md_dir(tmp_path, n=1)
    repo._runtime.models.embedding_clients = {}
    rc = bi.main(["ingest", "--input-dir", str(d), "--notebook-name", "X", "--allow-no-embed"])
    assert rc == 0
    r2 = repo
    with r2._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
        nemb = db.execute("SELECT COUNT(*) c FROM chunk_embeddings").fetchone()["c"]
    assert nsrc >= 1 and nemb == 0               # 显式确认的无向量导入


def test_run_kg_limit_requires_llm(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    with pytest.raises(
        RuntimeError,
        match="kg_extract workload 未绑定系统模型服务",
    ):                                           # 未绑定 workload → 直接报错,不静默
        bi.run_kg(repo, nb_id, limit=1)


def test_run_kg_plain_no_rebuild_without_llm_is_noop(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    monkeypatch.setattr(
        repo,
        "build_notebook_kg",
        lambda *_args, **_kwargs: pytest.fail("durable KG job must not start"),
    )
    monkeypatch.setattr(
        repo,
        "rebuild_unified_kg",
        lambda *_args, **_kwargs: pytest.fail("clustering must not start"),
    )

    assert bi.run_kg(repo, nb_id, no_rebuild=True) == {
        "extracted": 0,
        "failed": 0,
        "model_skipped": 0,
        "partial_retried": 0,
        "partial_failed_preserved": 0,
        "clusters": 0,
        "nodes_embedded": 0,
    }


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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)
    _seed_node(repo, nb_id, "ko-1")
    _seed_node(repo, nb_id, "ko-2")

    with repo._connect() as db:                      # 制造缺失:删 1 个 chunk 向量
        cid = db.execute("SELECT chunk_id FROM chunk_embeddings WHERE notebook_id=? LIMIT 1",
                         (nb_id,)).fetchone()["chunk_id"]
    with repo._write() as db:
        db.execute("DELETE FROM chunk_embeddings WHERE chunk_id=?", (cid,))

    out = bi.run_embed(repo, nb_id)

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


def test_node_backfill_reports_inserted_rows_and_only_dirties_on_change(
    repo, monkeypatch
):
    nb_id = bi.ensure_notebook(repo, None, "nb-node-backfill-count")
    _seed_node(repo, nb_id, "ko-a")
    _seed_node(repo, nb_id, "ko-b")
    dirty = []
    monkeypatch.setattr(
        repo.maintenance,
        "mark_unified_kg_dirty",
        lambda notebook_id: dirty.append(notebook_id),
    )

    assert bi.backfill_node_embeddings(repo, nb_id) == 2
    assert bi.backfill_node_embeddings(repo, nb_id) == 0
    assert dirty == [nb_id]


def test_node_backfill_does_not_dirty_when_best_effort_embedding_writes_nothing(
    repo, monkeypatch
):
    nb_id = bi.ensure_notebook(repo, None, "nb-node-backfill-failed")
    _seed_node(repo, nb_id, "ko-a")
    dirty = []
    monkeypatch.setattr(
        repo._runtime.source_embedding,
        "embed_objects_batch",
        lambda _notebook_id, _items: 0,
    )
    monkeypatch.setattr(
        repo.maintenance,
        "mark_unified_kg_dirty",
        lambda notebook_id: dirty.append(notebook_id),
    )

    assert bi.backfill_node_embeddings(repo, nb_id) == 0
    assert dirty == []


def test_main_embed_end_to_end_zeroes_missing(repo, tmp_path, capsys, monkeypatch):
    """main(['embed','--notebook-id',id]) 端到端:跑完缺失归零,打印 phase=embed。
    main 自建 repo → 用 FakeEmbedder 替 make_embedder,与 fixture repo 同库。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)
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
# run_ingest 现在通过系统模型调度同时生成 chunk / element 向量。因此补齐用例
# 显式删除 element 向量来模拟旧数据、中断任务或人工清理后的待回填状态。

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


def _delete_element_vectors(repo, notebook_id):
    with repo._write() as db:
        db.execute(
            "DELETE FROM element_embeddings WHERE notebook_id=?",
            (notebook_id,),
        )


def _insert_element(repo, source_id, element_id, text):
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            (element_id, source_id, "paragraph", "p1", text, "{}", now))


def test_missing_element_rows_exclude_blank_text(repo, tmp_path):
    """空白文本元素不算缺失:embed_source 会跳过它们(text.strip() 为空),它们永远不会
    有向量。若算进缺失,补齐命令每次都报「还有 N 个缺失」、每次都试着嵌入空串 → 永不
    收敛的脏状态。含纯制表符/换行/全角空格——SQLite 裸 TRIM() 只去半角空格,挡不住。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-blank")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)
    sid = _source_ids(repo, nb_id)[0]
    bi.backfill_element_embeddings(repo, nb_id)      # 先把真实元素补齐
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    for i, blank in enumerate(["", "   ", "\t\n", "　　", "\xa0"]):
        _insert_element(repo, sid, f"el-blank-{i}", blank)   # 全部无向量

    rows = repo.maintenance.missing_element_embedding_rows(nb_id)

    assert [r["id"] for r in rows] == []                     # 一个都不算缺失
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.backfill_element_embeddings(repo, nb_id) == 0   # 仍然 0 项可补
    _insert_element(repo, sid, "el-real", " 有内容的段落 ")    # 对照:非空白才算缺失
    assert [r["id"] for r in repo.maintenance.missing_element_embedding_rows(nb_id)] == [
        "el-real"]
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 1


def test_missing_element_rows_are_scoped_to_the_notebook(repo, tmp_path):
    """缺失查询按 notebook 限定(source_elements 表本身没有 notebook_id,靠 JOIN sources)。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_a = bi.ensure_notebook(repo, None, "nb-a")
    nb_b = bi.ensure_notebook(repo, None, "nb-b")
    bi.run_ingest(repo, nb_a, bi.iter_files(d), workers=1)
    _delete_element_vectors(repo, nb_a)
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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)
    _delete_element_vectors(repo, nb_id)
    total = repo.maintenance.count_missing_element_vectors(nb_id)
    assert total >= 3                                         # 多源、多 element

    assert bi.backfill_element_embeddings(repo, nb_id) == total
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.backfill_element_embeddings(repo, nb_id) == 0   # 幂等

    with repo._connect() as db:                               # 制造部分缺失
        eids = [r["element_id"] for r in db.execute(
            "SELECT element_id FROM element_embeddings WHERE notebook_id=? "
            "ORDER BY element_id", (nb_id,)).fetchall()]
    deleted = eids[:2]
    with repo._write() as db:
        db.executemany("DELETE FROM element_embeddings WHERE element_id=?",
                       [(eid,) for eid in deleted])
    seen = []
    bind_embedding_client(
        repo,
        _TextRecordingEmbedder(dim=16, seen=seen),
        workload_ids=("source_element_embedding",),
    )

    assert bi.backfill_element_embeddings(repo, nb_id) == len(deleted)

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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2)
    _delete_element_vectors(repo, nb_id)
    sids = _source_ids(repo, nb_id)
    assert len(sids) >= 2
    missing = repo.maintenance.count_missing_element_vectors(nb_id)
    assert len({r["source_id"] for r in
                repo.maintenance.missing_element_embedding_rows(nb_id)}) == len(sids)

    assert bi.backfill_element_embeddings(repo, nb_id) == missing

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


def test_backfill_element_embeddings_persists_before_all_embedding_finishes(repo, tmp_path, monkeypatch):
    """向量必须**分页**推进:每页算完立刻落库,而不是全算完再统一写。

    直接测内存不现实,但分页有个等价的可观测性质:落库会与嵌入**交错**发生。
    不分页时必然是「所有 embed → 才开始 write」。这条断言正是钉住那个差别——
    注意光把落库循环写成「逐批」是**无效**的:_map_embedding_batches 返回
    list[list](内部等全部 future 完成再收集),遍历它时向量早已全在内存里;
    必须在喂进去之前分页。"""
    d = _make_md_dir(tmp_path, n=3)
    nb_id = bi.ensure_notebook(repo, None, "nb-stream")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)
    _delete_element_vectors(repo, nb_id)
    missing = repo.maintenance.count_missing_element_vectors(nb_id)
    assert missing >= 3, "样本太小,分不出多页 → 本用例无检出力"

    svc = repo._runtime.source_embedding
    # 页大小 = 模型服务并行度 * embed_batch_size,都压到 1 → 每行一页。
    monkeypatch.setattr(repo.settings, "embed_batch_size", 1)
    monkeypatch.setitem(
        repo._runtime.models.parallelism_by_workload,
        "source_element_embedding",
        1,
    )

    events: list[str] = []
    real_embed = svc.embedder

    def _recording_embedder(workload_id):
        inner = real_embed(workload_id)

        class _Rec:
            def embed_texts(self, texts):
                events.append("embed")
                return inner.embed_texts(texts)

            def __getattr__(self, name):
                return getattr(inner, name)

        return _Rec()

    real_write = svc.vectors.replace_element_vectors

    def _recording_write(*args, **kwargs):
        events.append("write")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(svc, "embedder", _recording_embedder)
    monkeypatch.setattr(svc.vectors, "replace_element_vectors", _recording_write)

    assert bi.backfill_element_embeddings(repo, nb_id) == missing

    assert "write" in events and "embed" in events
    last_embed = len(events) - 1 - events[::-1].index("embed")
    first_write = events.index("write")
    assert first_write < last_embed, (
        "所有 embed 都跑完才开始 write —— 向量没有分页落库,大库上会 OOM", events)


def test_backfill_element_embeddings_truncates_by_settings_not_hardcoded_2000(repo, tmp_path):
    """补齐路径的截断必须用 settings.embed_truncate_chars(与 embed_source 同构),不是
    embed_chunks_batch 里硬编码的 2000。默认值恰好也是 2000 → 必须把配置调成非 2000
    才有检出力,这里用 137。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-trunc")
    p = tmp_path / "long.md"
    p.write_text("# Long\n\n" + "词" * 5000, encoding="utf-8")
    bi.run_ingest(repo, nb_id, [p], workers=1)
    sid = _source_ids(repo, nb_id)[0]
    assert any(len(r["text"]) > 2000 for r in _element_rows(repo, sid))   # 有超长元素
    _delete_element_vectors(repo, nb_id)
    seen = []
    bind_embedding_client(
        repo,
        _TextRecordingEmbedder(dim=16, seen=seen),
        workload_ids=("source_element_embedding",),
    )
    repo.settings.embed_truncate_chars = 137                  # 非默认值,否则本断言零检出力

    assert bi.backfill_element_embeddings(repo, nb_id) > 0

    assert seen
    assert max(len(t) for t in seen) == 137                   # 真按 137 截,不是 2000


def test_run_embed_fills_missing_element_vectors(repo, tmp_path):
    """run_embed 三侧并列:chunk / element / 节点都盘点 → 补齐 → 复盘归零。"""
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-embed3")
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)
    _delete_element_vectors(repo, nb_id)
    element_missing = repo.maintenance.count_missing_element_vectors(nb_id)
    assert element_missing > 0

    out = bi.run_embed(repo, nb_id)

    assert out["element_missing_before"] == element_missing
    assert out["elements_embedded"] == element_missing
    assert repo.maintenance.count_missing_element_vectors(nb_id) == 0
    assert bi.run_embed(repo, nb_id)["element_missing_before"] == 0   # 幂等


def test_main_embed_requires_embed_even_with_allow_no_embed(repo, tmp_path, monkeypatch, capsys):
    """embed 子命令就是补向量:EMBED 未配 → return 2,且忽略 --allow-no-embed。"""
    nb_id = bi.ensure_notebook(repo, None, "nb")     # 用配好 EMBED 的 repo 先建库
    repo._runtime.models.embedding_clients = {}
    rc = bi.main(["embed", "--notebook-id", nb_id, "--allow-no-embed"])
    assert rc == 2
    assert "chunk_embedding 未绑定系统模型服务" in capsys.readouterr().err


def test_arg_parser_embed_phase():
    args = bi.build_arg_parser().parse_args(["embed", "--notebook-id", "nb-x"])
    assert args.phase == "embed"


def test_workers_cli_omission_inherits_business_orchestration_setting(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    args = bi.build_arg_parser().parse_args(["reparse", "--notebook-id", "nb-x"])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert effective == bi.EffectiveConcurrency(workers=11, workers_source="env")


def test_workers_cli_overrides_business_orchestration_setting(monkeypatch):
    monkeypatch.setenv("KG_JOB_CONCURRENCY", "11")
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x", "--workers", "20",
    ])
    effective = bi._resolve_effective_concurrency(args, Settings(), "reparse")
    assert effective == bi.EffectiveConcurrency(workers=20, workers_source="cli")


def test_workers_rejects_non_positive_value():
    args = bi.build_arg_parser().parse_args([
        "reparse", "--notebook-id", "nb-x", "--workers", "0",
    ])
    with pytest.raises(ValueError, match="positive"):
        bi._resolve_effective_concurrency(args, Settings(), "reparse")


def test_run_reparse_and_run_all_do_not_reconfigure_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
    monkeypatch.setattr(repo.maintenance, "source_ids", lambda notebook_id: [])
    monkeypatch.setattr(
        repo.maintenance, "sources_with_elements", lambda notebook_id: set()
    )
    monkeypatch.setattr(
        repo, "rebuild_unified_kg",
        lambda notebook_id, progress=None, force=False, fresh=False: 0,
    )
    monkeypatch.setattr(
        bi, "backfill_node_embeddings", lambda repo, notebook_id: 0
    )

    bi.run_reparse(
        repo,
        bi.ensure_notebook(repo, None, "nb-reparse-owner"),
        no_rebuild=True,
    )
    bi.run_all(
        repo,
        bi.ensure_notebook(repo, None, "nb-all-owner"),
        [],
        report_interval=0,
    )

    assert configure_calls == []


def test_run_all_has_no_phase_local_workers_parameter():
    assert "workers" not in inspect.signature(bi.run_all).parameters


def test_main_prints_only_business_orchestration_concurrency(
    repo, monkeypatch, capsys
):
    nb_id = bi.ensure_notebook(repo, None, "nb-effective-concurrency")
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
        "--no-rebuild",
    ])

    assert rc == 0
    assert "concurrency: source=32(cli)" in capsys.readouterr().out
    manifest = Path(repo.storage_dir) / "batch_ingest" / f"{nb_id}.jsonl"
    event = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    event.pop("ts")
    assert event == {
        "phase": "concurrency",
        "workers": 32,
        "workers_source": "cli",
    }


def test_main_applies_workers_override_before_repository_construction(
    repo, monkeypatch
):
    nb_id = bi.ensure_notebook(repo, None, "nb-workers-runtime")
    opened_with = {}

    @contextmanager
    def _capture(settings, **_kwargs):
        opened_with["kg_job_concurrency"] = settings.kg_job_concurrency
        yield repo

    monkeypatch.setattr(bi, "open_maintenance_cli_repository", _capture)
    monkeypatch.setattr(
        bi,
        "run_reparse",
        lambda *_args, **_kwargs: {
            "targets": 0,
            "reparsed": 0,
            "failed": 0,
            "clusters": 0,
            "nodes_embedded": 0,
        },
    )

    assert bi.main([
        "reparse",
        "--notebook-id",
        nb_id,
        "--workers",
        "19",
        "--no-rebuild",
    ]) == 0
    assert opened_with == {"kg_job_concurrency": 19}


def test_run_all_pipelines_new_sources(repo, tmp_path, monkeypatch):
    """run_all per-source 流水线:每个新文件建 source + 走 process_source 抽取(extracted=N),
    末尾一次 rebuild_unified_kg。强制 kg_auto_extract 让 process_source 走到 extract。"""
    _bind_chat(repo, "kg_extract", _StubLLM())
    d = _make_md_dir(tmp_path, n=2)                        # 2 个 docN.md + 1 个 nested.md = 3
    nb_id = bi.ensure_notebook(repo, None, "nb-all")
    extracted = []
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid, **_kw: extracted.append(sid))
    rebuild_calls = []
    monkeypatch.setattr(repo, "rebuild_unified_kg",
                        lambda nb, progress=None, force=False, fresh=False: (rebuild_calls.append(nb), 5)[1])
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)

    res = bi.run_all(repo, nb_id, bi.iter_files(d))

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
    """已 parse(有 source_elements)、无 KG 的 source 走 extract_source 补抽,不重复新建。
    对照 test_run_all_reparses_existing_source_missing_elements:无 elements 的源才 reparse。"""
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)

    res = bi.run_all(repo, nb_id, files)

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


def test_main_index_does_not_reconfigure_kg_scheduler(repo, monkeypatch):
    from app.services.kg import scheduler

    nb_id = bi.ensure_notebook(repo, None, "nb-index-no-model-scope")
    seen = []
    configure_calls = []
    monkeypatch.setattr(
        scheduler, "configure", lambda **kwargs: configure_calls.append(kwargs)
    )
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
    assert configure_calls == []


def test_main_index_reports_a_busy_scale_build_claim(repo, monkeypatch, capsys):
    """W-CLI T-W1: the stopped-service gate is a database-wide maintenance lock;
    it does not exclude the offline scale-build CLI, which takes a per-notebook
    claim instead. Losing that claim must read as "someone else is building",
    not as a traceback — and must say the earlier phases are already durable so
    the operator knows a re-run only redoes the index step."""
    from app.repositories.scale_build_lock import ScaleBuildBusy

    nb_id = bi.ensure_notebook(repo, None, "nb-index-busy")

    def busy(_repo, notebook_id):
        raise ScaleBuildBusy(
            f"another process is building the scale index for {notebook_id}"
        )

    monkeypatch.setattr(bi, "run_index", busy)

    rc = bi.main(["index", "--notebook-id", nb_id])

    captured = capsys.readouterr()
    assert rc == 2
    assert "another process is building the scale index" in captured.err
    assert "已落库" in captured.err


def test_main_index_reports_a_lost_scale_build_claim(repo, monkeypatch, capsys):
    """codex W-CLI R1 P2-7: the sibling of the busy path. A claim that
    evaporated mid-build abandons the swap — nothing published, the previous
    generation still serving, a ``.tmp`` left on disk. Bare, that is a traceback
    ending in a path; the operator needs to know what survived and what to
    investigate before re-running."""
    from app.repositories.scale_build_lock import ScaleBuildLockLost

    nb_id = bi.ensure_notebook(repo, None, "nb-index-lost-claim")

    def lost(_repo, notebook_id):
        raise ScaleBuildLockLost(
            "scale build lock was lost before the artifact swap for "
            f"/data/kg_index/{notebook_id}; nothing was published and the "
            f"staged build remains at /data/kg_index/{notebook_id}.tmp"
        )

    monkeypatch.setattr(bi, "run_index", lost)

    rc = bi.main(["index", "--notebook-id", nb_id])

    captured = capsys.readouterr()
    assert rc == 1
    assert "lock was lost before the artifact swap" in captured.err
    assert ".tmp" in captured.err
    assert "锁会话" in captured.err


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
    bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=1)  # chunk_embeddings via real path (BLOB already)
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


def test_vector_blob_encoder_receives_neutral_mapping_rows_and_returns_updates(repo):
    notebook_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node(repo, notebook_id, "ko-vector-contract")
    _seed_json_vector(
        repo,
        "knowledge_embeddings",
        "object_id",
        "ko-vector-contract",
        notebook_id,
    )
    observed = []

    def encode(rows):
        observed.append(rows)
        assert len(rows) == 1
        assert isinstance(rows[0], Mapping)
        row = rows[0]
        return [(b"", row["notebook_id"], row["vid"])]

    processed, bad = repo.maintenance.convert_text_vector_batch(
        "knowledge_embeddings", "object_id", notebook_id, 10, encode
    )

    assert processed == 1
    assert bad == 1
    assert len(observed) == 1
    with repo._connect() as db:
        row = db.execute(
            "SELECT typeof(vector) AS kind FROM knowledge_embeddings "
            "WHERE object_id=?",
            ("ko-vector-contract",),
        ).fetchone()
    assert row["kind"] == "blob"


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
        # This helper deliberately bypasses the online KG writer and models a
        # historical row.  New notebooks are born with a certified empty
        # reverse index, so the raw legacy fixture must explicitly invalidate
        # that certificate before exercising the offline backfill.
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 "
            "WHERE notebook_id=?",
            (nb_id,),
        )


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
    """Re-running skips a completed generation without rewriting its rows."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])

    first = bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    second = bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)

    assert first["notebooks"][0]["already_complete"] is False
    assert second["notebooks"][0]["already_complete"] is True

    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,)).fetchone()["c"]
    assert count == 1


def test_run_backfill_source_index_force_repairs_completed_generation(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])
    bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    with repo._write() as db:
        db.execute(
            "DELETE FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,),
        )

    result = bi.run_backfill_source_index(
        repo, nb_id, all_notebooks=False, force=True
    )

    assert result["notebooks"][0]["already_complete"] is False
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()["c"] == 1


def test_run_backfill_source_index_resumes_committed_cursor(repo, monkeypatch):
    monkeypatch.setattr(bi, "_KOS_BACKFILL_BATCH_SIZE", 2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(5):
        _seed_node_with_evidence(repo, nb_id, f"ko-{i}", [f"src-{i}"])

    started = repo.maintenance.begin_source_index_backfill(nb_id)
    assert started["objects_scanned"] == 0
    page = repo.maintenance.resume_source_index_backfill_batch(nb_id, batch_size=2)
    assert page["objects_scanned"] == 2
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()["c"] == 2

    resumed = repo.maintenance.begin_source_index_backfill(nb_id)
    assert resumed["resumed"] is True
    assert resumed["objects_scanned"] == 2
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()["c"] == 2

    result = bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)
    assert result["notebooks"][0]["resumed"] is True
    assert result["objects"] == 5
    assert result["rows"] == 5


def test_source_index_backfill_generation_drift_fails_closed(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(3):
        _seed_node_with_evidence(repo, nb_id, f"ko-{i}", [f"src-{i}"])

    repo.maintenance.begin_source_index_backfill(nb_id)
    repo.maintenance.resume_source_index_backfill_batch(nb_id, batch_size=1)
    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,kg_mutation_seq,source_index_backfilled,updated_at) "
            "VALUES (?,1,1,0,?) ON CONFLICT(notebook_id) DO UPDATE SET "
            "kg_mutation_seq=kg_mutation_seq+1,source_index_backfilled=0,updated_at=excluded.updated_at",
            (nb_id, "2026-01-02T00:00:00"),
        )

    failed = repo.maintenance.resume_source_index_backfill_batch(nb_id, batch_size=1)
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "kg_generation_changed"
    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, nb_id)

    restarted = repo.maintenance.begin_source_index_backfill(nb_id)
    assert restarted["status"] == "running"
    assert restarted["objects_scanned"] == 0
    assert restarted["resumed"] is False


def test_clear_source_index_unpublishes_fast_path_until_backfill_finishes(repo):
    """A crash after clearing must leave the reverse index explicitly unready."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_node_with_evidence(repo, nb_id, "ko-1", ["src-a"])
    bi.run_backfill_source_index(repo, nb_id, all_notebooks=False)

    assert repo.maintenance.clear_source_index(nb_id) == 1

    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, nb_id)
        assert db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()["c"] == 0


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

    args3 = bi.build_arg_parser().parse_args(
        ["backfill-source-index", "--notebook-id", "nb-x", "--force"]
    )
    assert args3.force is True


# --- backfill-chunk-elements CLI (批 5 element→chunk 反查索引) --------------


def _seed_chunk_with_elements(repo, notebook_id, chunk_id, element_ids,
                              source_id="src-ce"):
    now = "2026-08-10T00:00:00+00:00"
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, "t", "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            (chunk_id, notebook_id, source_id, chunk_id, "",
             json.dumps(list(element_ids)), now),
        )


def test_run_backfill_chunk_elements_paginates_in_batches(repo, monkeypatch):
    """Bounded-memory backfill: with a tiny batch size a notebook with more
    chunks than one page is still fully covered (keyset id > cursor neither
    skips nor duplicates rows)."""
    monkeypatch.setattr(bi, "_CHUNK_ELEMENT_BACKFILL_BATCH_SIZE", 2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    for i in range(5):
        _seed_chunk_with_elements(repo, nb_id, f"c-{i}", [f"e-{i}"])

    out = bi.run_backfill_chunk_elements(repo, nb_id, all_notebooks=False)
    assert out["chunks"] == 5
    assert out["rows"] == 5
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM chunk_elements WHERE notebook_id=?",
            (nb_id,)).fetchone()["c"]
    assert count == 5


def test_main_backfill_chunk_elements_requires_notebook_or_all(repo, capsys):
    rc = bi.main(["backfill-chunk-elements"])
    assert rc == 2
    assert "backfill-chunk-elements" in capsys.readouterr().err


def test_main_backfill_chunk_elements_end_to_end(repo, capsys):
    """Pure SQL derivation from existing chunk rows — must work even when
    EMBED_* is unset, like backfill-source-index."""
    nb_id = bi.ensure_notebook(repo, None, "nb")
    _seed_chunk_with_elements(repo, nb_id, "c-1", ["e-1"])

    rc = bi.main(["backfill-chunk-elements", "--notebook-id", nb_id])
    assert rc == 0
    assert "backfill-chunk-elements done" in capsys.readouterr().out
    assert repo.maintenance.chunk_elements_indexed(nb_id) is True


def test_arg_parser_backfill_chunk_elements_phase():
    args = bi.build_arg_parser().parse_args(
        ["backfill-chunk-elements", "--notebook-id", "nb-x"]
    )
    assert args.phase == "backfill-chunk-elements"
    assert args.all_notebooks is False

    args2 = bi.build_arg_parser().parse_args(
        ["backfill-chunk-elements", "--all-notebooks"]
    )
    assert args2.all_notebooks is True

    args3 = bi.build_arg_parser().parse_args(
        ["backfill-chunk-elements", "--notebook-id", "nb-x", "--force"]
    )
    assert args3.force is True


# ── Task 6: CLI --fresh 贯通 ──────────────────────────────────────────────────

def test_kg_fresh_flag_clears_checkpoint(repo, monkeypatch):
    """run_kg(fresh=True) 把 fresh 透传给 rebuild_unified_kg;rebuild_only=True 时
    force 本就该是 True(两个显式入口都成立)。"""
    from app.services import batch_ingest
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    _bind_chat(repo, "kg_extract", _StubLLM())
    monkeypatch.setattr(
        repo, "build_notebook_kg",
        lambda notebook_id, *, finalize=None, **_kwargs: {
            "built": [],
            "failed": [],
            "skipped": [],
            **(finalize({}) or {}),
        })
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
    _bind_chat(repo, "kg_extract", _StubLLM())
    d = _make_md_dir(tmp_path, n=1)
    nb_id = bi.ensure_notebook(repo, None, "nb-all-fresh")
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: None)
    seen = {}
    def _fake_rebuild(nb, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _fake_rebuild)
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)

    bi.run_all(repo, nb_id, bi.iter_files(d), fresh=True)

    assert seen == {"force": True, "fresh": True}


def test_arg_parser_has_fresh_flag():
    args = bi.build_arg_parser().parse_args(["kg"])
    assert args.fresh is False
    args2 = bi.build_arg_parser().parse_args(["kg", "--fresh"])
    assert args2.fresh is True


def test_arg_parser_has_retry_partial_flag():
    args = bi.build_arg_parser().parse_args(["kg"])
    assert args.retry_partial is False
    args2 = bi.build_arg_parser().parse_args(["kg", "--retry-partial"])
    assert args2.retry_partial is True


def test_main_kg_fresh_dispatches_force_and_fresh(repo, monkeypatch):
    """端到端:CLI argv → main() → run_kg → repo.rebuild_unified_kg 收到 force=True、
    fresh=True。main() 内部自建新 repo 实例,故按 test_main_all_ingests_then_runs_kg 的
    惯例在 SQLiteRepository 类级打桩(而非 fixture 的 repo 实例)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-cli-fresh")
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


def _patch_fake_llm(repo) -> _FakeMetaLLM:
    """Bind the exact system workload used by paper metadata backfill."""
    fake = _FakeMetaLLM()
    _bind_chat(repo, "paper_metadata", fake)
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
    repo._runtime.models.parallelism_by_workload = {
        **repo._runtime.models.parallelism_by_workload,
        "paper_metadata": 3,
    }
    lock = threading.Lock()
    active = 0
    peak = 0
    # 结构性判据：模型 workload 绑定的服务并行度为 3，因此同时只能有
    # 3 个 metadata producer 到达 barrier；容量只来自服务配置。
    barrier = threading.Barrier(3, timeout=10)
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

    def ensure(source, force=False, client=None):
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
        "3 个 worker 没能同时在途 —— paper_metadata 未使用服务并行度 "
        f"3（最高只观察到 {peak} 路同时活动）"
    )
    assert counts["total"] == 12
    assert counts["stored"] == 12
    assert peak == 3


def test_metadata_phase_requires_llm(repo, capsys):
    """kg_llm 与 llm 均未配置(fixture 默认环境,未打桩)→ main 返回 2,stderr 含
    「LLM 未配置」,且门控先于 --notebook-id 检查(即便给了合法 notebook 也拦)。"""
    nb_id = bi.ensure_notebook(repo, None, "nb-meta")

    rc = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc == 2
    assert "paper_metadata workload 未绑定模型服务" in capsys.readouterr().err


def test_metadata_phase_requires_notebook(repo, monkeypatch, capsys):
    """LLM 已配但未给 --notebook-id → 返回 2,且绝不新建 notebook。"""
    _patch_fake_llm(repo)
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
    _patch_fake_llm(repo)
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


def test_metadata_dispatch_passes_resolved_default_concurrency(repo, monkeypatch):
    seen = {}

    def _run_metadata(_repo, _args, workers=None):
        seen["workers"] = workers
        return 0

    monkeypatch.setattr(bi, "run_metadata", _run_metadata)
    args = SimpleNamespace(phase="metadata")
    effective = bi.EffectiveConcurrency(workers=11, workers_source="env")

    assert bi._dispatch_main(args, repo, effective) == 0
    assert seen == {"workers": 11}

def test_metadata_interrupt_during_submission_drains_accepted_worker(
    monkeypatch,
):
    accepted: Future[object] = Future()
    tracked = []
    calls = 0

    class _Pool:
        def __init__(self, max_workers):
            assert max_workers == 2

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def submit(self, _wrapper, completion, *_args, **_kwargs):
            nonlocal calls
            tracked.append(completion)
            calls += 1
            if calls == 1:
                return accepted
            raise KeyboardInterrupt("ctrl-c while submitting metadata")

    maintenance = SimpleNamespace(
        paper_metadata_source_ids_page=lambda *_args, **_kwargs: ["src-one", "src-two"],
        ensure_paper_metadata_source=lambda *_args, **_kwargs: "stored",
    )
    fake_repo = SimpleNamespace(
        configured=lambda workload: workload == "paper_metadata",
        maintenance=maintenance,
    )
    args = SimpleNamespace(notebook_id="nb", force=False, workers=None)
    monkeypatch.setattr(bi, "ThreadPoolExecutor", _Pool)

    with pytest.raises(KeyboardInterrupt):
        bi.run_metadata(fake_repo, args, workers=2)

    assert len(tracked) == 2 and all(future.cancelled() for future in tracked)


def test_metadata_phase_does_not_require_embedding_provider(
    repo, monkeypatch, capsys
):
    _patch_fake_llm(repo)
    nb_id = bi.ensure_notebook(repo, None, "nb-meta-no-embed")
    _insert_meta_source(repo, nb_id, "src-no-embed")

    rc = bi.main(["metadata", "--notebook-id", nb_id])

    assert rc == 0
    assert "[meta done]" in capsys.readouterr().out


def test_metadata_phase_force(repo, monkeypatch, capsys):
    """--force → 已有元数据行的源也重抽(fake.calls 增加)。"""
    fake = _patch_fake_llm(repo)
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
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)

    res = bi.run_all(repo, nb_id, [p])

    assert res["new"] == 0 and res["resumed"] == 0 and res["reparsed"] == 1
    with repo._connect() as db:  # reparse 真的 parse 了 .md → 有 elements(不是空抽)
        n_el = db.execute("SELECT COUNT(*) c FROM source_elements WHERE source_id=?",
                          (sid,)).fetchone()["c"]
    assert n_el > 0


def test_run_reparse_only_targets_sources_missing_elements(repo, tmp_path, monkeypatch):
    """reparse 子命令:只对 n_el=0(未成功 parse)的存量源重跑 process_source 补 elements,
    已有 elements 的源跳过。修复历史 run_all 分流把无-elements 源空抽(objects=0)的存量数据。"""
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)

    res = bi.run_reparse(repo, nb_id)

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
    _bind_chat(repo, "kg_extract", _StubLLM())
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
    monkeypatch.setattr(bi, "backfill_node_embeddings", lambda repo, nb: 0)
    orig = repo.settings.kg_incremental_fusion_enabled

    bi.run_reparse(repo, nb_id)

    assert seen["fusion_during"] is False                        # 批量期关了 per-source 融合
    assert repo.settings.kg_incremental_fusion_enabled == orig   # 结束恢复原值


@pytest.mark.parametrize(
    "workers,requested,expected",
    [
        (8, None, 32),      # 并发度小 → 用固定下限
        (32, None, 64),     # 并发度追平下限 → 按并发度派生,始终高于它
        (64, None, 128),
        (8, 9, 9),          # 显式取值只要高于并发度就原样采用
        (8, 100, 100),
    ],
)
def test_max_consecutive_model_failures_stays_above_source_concurrency(
    workers, requested, expected
):
    resolved = bi._resolve_max_consecutive_model_failures(requested, workers)
    assert resolved == expected
    assert resolved > workers


@pytest.mark.parametrize("workers,requested", [(32, 32), (8, 8), (8, 1)])
def test_max_consecutive_model_failures_rejects_values_at_or_below_workers(
    workers, requested
):
    """不满足关系就报错,不静默抬高——用户写下的数字被悄悄改掉比报错更坏。

    阈值不高于并发度时,一次瞬时抖动会同时打中所有在飞来源、直接凑满"连续失败",
    跳过模式等于没开(codex 第 1 轮 P2)。
    """
    with pytest.raises(ValueError, match="并发度"):
        bi._resolve_max_consecutive_model_failures(requested, workers)


def test_question_index_cli_requires_explicit_opt_in(capsys):
    fake = SimpleNamespace(
        settings=Settings(generated_question_index_mode="off"),
    )
    args = SimpleNamespace(notebook_id="nb", force=False)

    assert bi.run_question_index(fake, args, workers=1) == 2
    assert "GENERATED_QUESTION_INDEX_MODE=off" in capsys.readouterr().err


def test_question_index_cli_uses_maintenance_port(capsys):
    calls = []
    maintenance = SimpleNamespace(
        build_chunk_question_index=lambda notebook_id, **kwargs: (
            calls.append((notebook_id, kwargs))
            or {"chunks_seen": 2, "questions_stored": 4, "failed": 0}
        )
    )
    fake = SimpleNamespace(
        settings=Settings(generated_question_index_mode="shadow"),
        configured=lambda workload: workload in {
            "chunk_question_generation", "chunk_embedding"
        },
        get_notebook=lambda notebook_id: SimpleNamespace(id=notebook_id),
        maintenance=maintenance,
    )
    args = SimpleNamespace(notebook_id="nb", force=True)

    assert bi.run_question_index(fake, args, workers=3) == 0
    assert calls[0][0] == "nb"
    assert calls[0][1]["workers"] == 3
    assert calls[0][1]["force"] is True
    assert "question-index done" in capsys.readouterr().out
