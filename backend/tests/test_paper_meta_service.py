"""SourceIngestionService 论文元数据抽取服务集成测试(paper-metadata Task 4)。

覆盖 ensure_paper_metadata 的幂等/门控矩阵、run_extraction 的历史源 catch-up
挂载、backfill_paper_metadata 的计数/进度回调,以及 controller 复核后追加的
非-dict JSON 顶层防护(见 ensure_paper_metadata 内 parsed 形状校验)。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.source_store import SourceElementWrite
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


class _FakeKgLLM:
    """chat_json 返回同一 payload 的 JSON 序列化,不区分 schema_hint(paper-meta
    与 KG 抽取共用同一 fake 时按需在测试里另行 monkeypatch kg_ingest)。calls
    加锁:backfill 测试并发调用同一实例。"""

    def __init__(self, payload):
        self.configured = True
        self.model = "fake-kg"
        self.payload = payload
        self.calls = 0
        self._lock = threading.Lock()

    def chat_json(self, messages, schema_hint, **kwargs):
        with self._lock:
            self.calls += 1
        return json.dumps(self.payload)


class _RawJsonLLM:
    """chat_json 原样返回给定字符串(绕开 json.dumps),用于构造畸形顶层 JSON。"""

    def __init__(self, raw):
        self.configured = True
        self.model = "raw-llm"
        self.raw = raw
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        return self.raw


class _RaisingLLM:
    """chat_json 直接抛异常,验证 ensure_paper_metadata 吞异常返回 failed。"""

    configured = True
    model = "raising-llm"

    def chat_json(self, messages, schema_hint, **kwargs):
        raise RuntimeError("boom")


PAYLOAD = {
    "is_paper": True, "title": "Gate Sizing Under Variability",
    "authors": [{"name": "Chen Hao", "affiliations": ["Fudan University"]}],
    "venue": "DAC", "year": 2025, "doi": "", "keywords": [],
}
HEAD_TEXT = ("Gate Sizing Under Variability\nChen Hao\nFudan University\nDAC 2025\n"
             "Abstract: ...")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo + FakeEmbedder(embedder_configured=True). 镜像 test_batch_ingest.py。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    provider = RecordingModelProvider()
    r = SQLiteRepository(Settings(), model_provider=provider)
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    r.recording_model_provider = provider
    return r


@pytest.fixture
def service(repo):
    return repo._runtime.source_ingestion


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="nb")).id


def _insert_source(repo, notebook_id, source_id, *, text=HEAD_TEXT,
                    doc_type="", source_type="document"):
    """建一个 parsed 状态的源,element 文本=text,供 ensure_paper_metadata 的
    source_elements()/read_source_text() 路径水合(file_path 用 .pdf 扩展名,
    故 read_source_text 走 element 拼接分支,不依赖真实磁盘文件)。返回水合后
    的 SourceDetail(供调用方直接喂给 ensure_paper_metadata)。"""
    store = repo._runtime.source_store
    store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=f"Doc {source_id}",
        source_type=source_type, status="parsed", parse_status="parsed",
        file_name=f"{source_id}.pdf", file_path=f"/tmp/{source_id}.pdf",
        file_size=0, file_hash=f"h-{source_id}", summary="", doc_type=doc_type,
    )
    with repo._write() as db:
        store.replace_elements(
            db, source_id,
            [SourceElementWrite(id=f"el-{source_id}-0001", element_type="text",
                                 location_label="", text=text, metadata={})],
            created_at="2026-01-01T00:00:00",
        )
    return store.get_source(source_id)


# ---------------------------------------------------------------------------
# ensure_paper_metadata: happy path + idempotency
# ---------------------------------------------------------------------------


def test_ensure_stores_verified_meta(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    repo.recording_model_provider.calls.clear()
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "stored"
    assert fake.calls == 1
    assert ("chat", "paper_metadata") in repo.recording_model_provider.calls
    meta = repo.get_paper_meta("src-1")
    assert meta is not None
    assert meta["is_paper"] is True
    assert meta["paper_title"] == "Gate Sizing Under Variability"
    assert meta["venue"] == "DAC"
    assert meta["pub_year"] == 2025
    assert [a["name"] for a in meta["authors"]] == ["Chen Hao"]
    assert meta["authors"][0]["affiliation"] == "Fudan University"


def test_ensure_idempotent_skip(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")
    assert service.ensure_paper_metadata(source) == "stored"

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 1


def test_ensure_force_reextracts(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")
    assert service.ensure_paper_metadata(source) == "stored"

    status = service.ensure_paper_metadata(source, force=True)

    assert status == "stored"
    assert fake.calls == 2


def test_not_paper_marker_prevents_retry(repo, notebook_id, service):
    fake = _FakeKgLLM({"is_paper": False})
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "not_paper"
    meta = repo.get_paper_meta("src-1")
    assert meta is not None
    assert meta["is_paper"] is False

    status2 = service.ensure_paper_metadata(source)
    assert status2 == "skipped"
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# ensure_paper_metadata: gates (order matters — memory/knowhow and doc_type
# must short-circuit BEFORE the LLM is ever touched)
# ---------------------------------------------------------------------------


def test_no_llm_returns_no_llm_without_row(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    fake.configured = False
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "no_llm"
    assert repo.get_paper_meta("src-1") is None
    assert fake.calls == 0


def test_memory_source_gated(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1", source_type="memory")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0
    assert repo.get_paper_meta("src-1") is None


def test_knowhow_source_gated(repo, notebook_id, service):
    """姊妹场景:source.type 门控是单个 in ("memory", "knowhow") 检查,两个成员
    都要各自覆盖,免得元组打错字只有一半被测试兜住。"""
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1", source_type="knowhow")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0
    assert repo.get_paper_meta("src-1") is None


def test_textbook_doc_type_gated(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1", doc_type="textbook")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0


def test_disabled_setting_gates(repo, notebook_id, service, monkeypatch):
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    monkeypatch.setattr(repo.settings, "paper_meta_enabled", False)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "disabled"
    assert fake.calls == 0


# ---------------------------------------------------------------------------
# ensure_paper_metadata: failure handling (never raises, never leaves a row)
# ---------------------------------------------------------------------------


def test_llm_exception_is_swallowed(repo, notebook_id, service):
    bind_chat_client(repo, "paper_metadata", _RaisingLLM())
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "failed"
    assert repo.get_paper_meta("src-1") is None


def test_setup_phase_exception_is_swallowed(repo, notebook_id, service, monkeypatch):
    """Final-review Fix 2: the try/except must cover the whole body from the
    get_paper_meta idempotency read onward, not just the chat_json call —
    ANY internal failure (not only an LLM error) must degrade to "failed"
    with no row and no escaping exception, so a setup-phase bug can never
    propagate out of ensure_paper_metadata and interrupt its callers
    (process_source / run_extraction's historical-source catch-up, which
    both run KG extraction right after this call)."""
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")

    def _boom(*_a, **_k):
        raise RuntimeError("boom in idempotency read")

    monkeypatch.setattr(service.sources, "get_paper_meta", _boom)

    status = service.ensure_paper_metadata(source)

    assert status == "failed"
    assert fake.calls == 0  # never reached the LLM
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM source_paper_meta WHERE source_id=?", ("src-1",)
        ).fetchone()[0] == 0


def test_ensure_non_object_json_fails_without_row(repo, notebook_id, service):
    """Controller amendment: safe_json() 契约是「总返回 dict」,对顶层是数组/
    标量的畸形输出会静默折叠成 {}。若把这个 {} 直接喂给 verify_paper_meta,会
    产出合法的 is_paper=False 标记行——把一次瞬态的模型输出错误,永久固化成
    「已判定非论文」,压掉后续重试。ensure_paper_metadata 必须在 unwrap 后仍非
    dict 时 raise,让外层 except 落到 failed(不写行,下次可重试)。"""
    fake = _RawJsonLLM("[]")
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "failed"
    assert repo.get_paper_meta("src-1") is None
    assert fake.calls == 1


def test_ensure_single_element_array_json_is_unwrapped(repo, notebook_id, service):
    """姊妹场景:模型把同一个合法 payload 包了一层数组(常见 LLM 误习惯)。这
    应该被无损展开、正常入库,而不是被上面那条防护误伤。"""
    fake = _RawJsonLLM(json.dumps([PAYLOAD]))
    bind_chat_client(repo, "paper_metadata", fake)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "stored"
    meta = repo.get_paper_meta("src-1")
    assert meta is not None
    assert meta["paper_title"] == "Gate Sizing Under Variability"


# ---------------------------------------------------------------------------
# run_extraction: historical-source catch-up mount
# ---------------------------------------------------------------------------


def test_run_extraction_catch_up(repo, notebook_id, monkeypatch):
    """run_extraction 开头(force=False)补论文元数据;KG 抽取本身用最小 stub
    绕开(同 test_kg_llm_client.py 的既有惯例),把断言收窄到「catch-up 发生
    了」,不与 kg_ingest 内部 JSON 形状耦合。"""
    import app.services.kg_ingest as kg_ingest

    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source_id = "src-1"
    _insert_source(repo, notebook_id, source_id)
    assert repo.get_paper_meta(source_id) is None

    monkeypatch.setattr(
        kg_ingest, "extract_graph",
        lambda *a, **k: type("G", (), {
            "objects": [], "relations": [],
            "total_windows": 0, "failed_windows": 0,
            "windows_skipped": 0, "concepts_dropped": 0, "claims_dropped": 0,
        })(),
    )
    monkeypatch.setattr(kg_ingest, "build_records", lambda *a, **k: ([], []))

    repo._run_extraction(source_id)

    meta = repo.get_paper_meta(source_id)
    assert meta is not None
    assert meta["is_paper"] is True
    # 唯一一次 chat_json 调用来自 paper-meta;KG 抽取路径被 stub 绕开,没有
    # 二次调用 fake——证明 catch-up 独立生效,不依赖/污染 KG 抽取本身。
    assert fake.calls == 1


def test_run_extraction_catch_up_is_idempotent_on_rerun(repo, notebook_id, monkeypatch):
    """已有 meta 行的源再跑 run_extraction:catch-up 跳过(不二次调用 LLM)。"""
    import app.services.kg_ingest as kg_ingest

    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    source_id = "src-1"
    source = _insert_source(repo, notebook_id, source_id)
    assert repo._runtime.source_ingestion.ensure_paper_metadata(source) == "stored"
    assert fake.calls == 1

    monkeypatch.setattr(
        kg_ingest, "extract_graph",
        lambda *a, **k: type("G", (), {
            "objects": [], "relations": [],
            "total_windows": 0, "failed_windows": 0,
            "windows_skipped": 0, "concepts_dropped": 0, "claims_dropped": 0,
        })(),
    )
    monkeypatch.setattr(kg_ingest, "build_records", lambda *a, **k: ([], []))

    repo._run_extraction(source_id)

    assert fake.calls == 1  # catch-up saw the existing row and skipped


# ---------------------------------------------------------------------------
# backfill_paper_metadata: counts + progress + idempotent rerun
# ---------------------------------------------------------------------------


def test_backfill_counts_and_progress(repo, notebook_id, monkeypatch):
    # 本例真跑 backfill 且 stored>0,会走 _notify_paper_meta_done —— 那是 emit 到
    # 进程级共享单例 pending_bus。不隔离的话事件会滞留,同进程后跑的
    # test_me_pending_stream_first_frame_is_snapshot 读到的首帧就变成这条事件而非
    # snapshot(xdist 下按分配随机复现,表现为 flaky)。与本文件其它 backfill 用例
    # 一致地把 emit 打桩掉;本例只断言 counts/progress,不关心事件。
    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty", lambda uid: None)

    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)
    repo.recording_model_provider.calls.clear()
    _insert_source(repo, notebook_id, "src-1")
    _insert_source(repo, notebook_id, "src-2")
    _insert_source(repo, notebook_id, "src-3")
    repo._runtime.source_store.upsert_paper_meta(
        "src-3", notebook_id,
        {
            "is_paper": True, "paper_title": None, "venue": None, "pub_year": None,
            "doi": None, "keywords": [], "model": "m", "raw_json": "{}", "authors": [],
        },
    )

    calls = []

    def _progress(done, total, source_id, status):
        calls.append((done, total, source_id, status))

    counts = repo.backfill_paper_metadata(notebook_id, progress=_progress)

    assert counts == {"total": 2, "stored": 2}
    assert len(calls) == 2
    assert {c[1] for c in calls} == {2}                      # total is stable
    assert {c[3] for c in calls} == {"stored"}
    assert {c[2] for c in calls} == {"src-1", "src-2"}        # src-3 excluded (has meta)
    assert {c[0] for c in calls} == {1, 2}                    # done counted 1..N
    assert fake.calls == 2
    assert repo.recording_model_provider.calls.count(
        ("chat", "paper_metadata")
    ) == 1  # resolved once before the raw worker pool

    counts2 = repo.backfill_paper_metadata(notebook_id)
    assert counts2 == {"total": 0}


# ---------------------------------------------------------------------------
# backfill_paper_metadata: in-memory runtime state (paper-meta-status Task 1)
# ---------------------------------------------------------------------------


def test_backfill_runtime_registers_and_pops_progress(repo, notebook_id, service, monkeypatch):
    """job 期间 facade paper_meta_backfilling(nb)=True + progress dict 反映 done/total；
    结束后（正常/异常）自动 pop 为 False/None。"""
    # 同 test_backfill_counts_and_progress:本例 stored>0 会真 emit 到进程级共享
    # 单例 pending_bus,滞留事件会让同进程后跑的 SSE 首帧断言拿到 event 而非
    # snapshot。本例只关心 building/progress,事件与否无关,打桩隔离。
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit", lambda uid, evt: None)
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty", lambda uid: None)

    _insert_source(repo, notebook_id, "src-a")
    _insert_source(repo, notebook_id, "src-b")
    fake = _FakeKgLLM(PAYLOAD)
    bind_chat_client(repo, "paper_metadata", fake)

    captured = {"during": None}
    original = service.ensure_paper_metadata

    def _spy(source, **kw):
        # 第一次调用时读一次进度快照
        if captured["during"] is None:
            captured["during"] = (
                repo.paper_meta_backfilling(notebook_id),
                repo.paper_meta_backfill_progress(notebook_id),
            )
        return original(source, **kw)

    monkeypatch.setattr(service, "ensure_paper_metadata", _spy)
    counts = service.backfill_paper_metadata(notebook_id)
    assert counts["total"] == 2
    during_flag, during_prog = captured["during"]
    assert during_flag is True
    assert during_prog is not None
    assert during_prog["total"] == 2
    assert 0 <= during_prog["done"] <= 2
    # 结束后 pop
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None


def test_backfill_runtime_pops_on_exception(repo, notebook_id, service, monkeypatch):
    """每个 source 内异常被 _one 内 try 吞成 status=failed；finally 保证
    dict pop、counts 累加 failed。"""
    _insert_source(repo, notebook_id, "src-c")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM(PAYLOAD))

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "ensure_paper_metadata", _boom)
    # backfill 内部 _one 里 try 包了 ensure（已存在），异常不冒到 as_completed
    counts = service.backfill_paper_metadata(notebook_id)
    assert counts["total"] == 1
    assert counts.get("failed", 0) == 1
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None


def test_backfill_empty_no_registration(repo, notebook_id):
    """queued=0 不 register。"""
    counts = repo.backfill_paper_metadata(notebook_id)
    assert counts == {"total": 0}
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None


def test_backfill_generation_guard_protects_foreign_entry(
    repo, notebook_id, service, monkeypatch
):
    """同 nb 并发 backfill 的 clobber race：先来者 A 注册后被后来者 B
    覆盖 entry；A 若在 finally 无脑 pop 会把 B 的 live entry 弹掉。构造
    A 的 finally 路径命中"当前 entry 属于别人（_gen 不匹配）"分支，断言
    A 不会 pop 掉这个"外来" entry。做法：monkeypatch ensure_paper_metadata
    在处理时抢先把 dict 里 A 的 entry 覆写成 _gen=999 的假 entry（模拟 B
    已经覆盖），然后让 A 的 backfill 走完 finally——外来 entry 必须仍在。
    附带断言 paper_meta_backfill_progress 剥掉 _gen（对外只暴露 total/done）。
    """
    _insert_source(repo, notebook_id, "src-race")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM(PAYLOAD))

    def _clobber_then_delegate(source, **kw):
        # 模拟"后来者 B 已经覆盖 A 的 entry"：把整条 entry 换成外来 _gen
        with service._paper_meta_backfilling_lock:
            service._paper_meta_backfilling[notebook_id] = {
                "total": 42, "done": 7, "_gen": 999,
            }
        # ensure_paper_metadata 本身不关心 dict；直接返回 "skipped" 让
        # backfill 记 counts 后自然收尾走进 finally 的世代校验分支
        return "skipped"

    monkeypatch.setattr(service, "ensure_paper_metadata", _clobber_then_delegate)
    counts = service.backfill_paper_metadata(notebook_id)
    assert counts["total"] == 1

    # 关键断言：A 的 finally 校验 entry._gen != my_gen（999 vs A's real
    # gen），跳过 pop——外来 entry 仍在 dict 里
    assert repo.paper_meta_backfilling(notebook_id) is True
    prog = repo.paper_meta_backfill_progress(notebook_id)
    assert prog == {"total": 42, "done": 7}
    assert "_gen" not in prog

    # 清理，避免污染其它测试（fixture 隔离，防御性）
    with service._paper_meta_backfilling_lock:
        service._paper_meta_backfilling.pop(notebook_id, None)


# ---------------------------------------------------------------------------
# backfill_paper_metadata: paper_meta_done 铃铛事件 (paper-meta-status Task 6)
# ---------------------------------------------------------------------------


def test_backfill_emits_paper_meta_done_on_success(repo, notebook_id, service, monkeypatch):
    """成功路径 emit paper_meta_done（stored=N）+ mark_dirty；且 building
    entry 必须在 emit 之前就已经 pop 掉，否则 mark_dirty 用仍含 building
    项的旧快照重算，铃铛会在 done toast 后瞬间又闪回"补全中"。"""
    _insert_source(repo, notebook_id, "src-x")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM(PAYLOAD))

    events: list = []
    marked: list = []
    progress_at_emit: list = []
    from app.services import pending_bus as pb_module

    def _emit(uid, evt):
        progress_at_emit.append(repo.paper_meta_backfill_progress(notebook_id))
        events.append((uid, evt))

    monkeypatch.setattr(pb_module.pending_bus, "emit", _emit)
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty",
                        lambda uid: marked.append(uid))
    service.backfill_paper_metadata(notebook_id)
    done = [e for _, e in events if e.get("event") == "paper_meta_done"]
    assert len(done) == 1
    assert done[0]["notebook_id"] == notebook_id
    assert done[0]["stored"] >= 1
    assert len(marked) >= 1
    # entry 必须在 emit 那一刻已经被 pop（成功路径清状态先于通知）
    assert progress_at_emit == [None]


def test_backfill_no_done_on_zero_queue(repo, notebook_id, monkeypatch):
    """零队列不 emit done。"""
    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    repo.backfill_paper_metadata(notebook_id)
    assert not any(e.get("event") == "paper_meta_done" for _, e in events)


def test_backfill_no_done_on_all_failed(repo, notebook_id, service, monkeypatch):
    """全 failed 不 emit done（stored=0 视为无成果）。"""
    _insert_source(repo, notebook_id, "src-y")
    bind_chat_client(repo, "paper_metadata", _RaisingLLM())
    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    service.backfill_paper_metadata(notebook_id)
    assert not any(e.get("event") == "paper_meta_done" for _, e in events)


def test_backfill_superseded_generation_does_not_emit_done(
    repo, notebook_id, service, monkeypatch
):
    """被后来者覆盖的旧世代跑完时不得 emit paper_meta_done。

    对照 test_backfill_generation_guard_protects_foreign_entry：那条锁住的是
    「不 pop 别人的 entry」，本条锁住同一 race 的通知侧。旧实现里 _pop_backfilling
    有世代守卫但不返回结果，_notify_paper_meta_done 无条件跟在后面——于是 A 跑完
    就报「补全完成」，而 B 还在跑：多标签页/重复提交下用户会看到早到的 done toast，
    并可能收到重复完成事件。断言 A（stored>0，本会走通知分支）在 entry 已被 B
    覆盖时保持沉默，且不误弹 B 的 live entry。
    """
    _insert_source(repo, notebook_id, "src-superseded")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM(PAYLOAD))

    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))

    real_ensure = service.ensure_paper_metadata

    def _clobber_then_store(source, **kw):
        # 先真跑一遍拿到 stored（保证 A 满足 stored>0 的通知前置条件），再模拟
        # 「后来者 B 已覆盖 entry」——A 收尾时必须发现 entry 不归自己。
        status = real_ensure(source, **kw)
        with service._paper_meta_backfilling_lock:
            service._paper_meta_backfilling[notebook_id] = {
                "total": 42, "done": 7, "_gen": 999,
            }
        return status

    monkeypatch.setattr(service, "ensure_paper_metadata", _clobber_then_store)
    counts = service.backfill_paper_metadata(notebook_id)

    assert counts.get("stored", 0) > 0, "前置条件：A 确实存了元数据"
    assert not any(e.get("event") == "paper_meta_done" for _, e in events), \
        "旧世代被覆盖后不该报完成"
    # B 的 live entry 未被误弹
    assert repo.paper_meta_backfill_progress(notebook_id) == {"total": 42, "done": 7}

    with service._paper_meta_backfilling_lock:
        service._paper_meta_backfilling.pop(notebook_id, None)


def test_backfill_publishes_pending_snapshot_after_registering(
    repo, notebook_id, service, monkeypatch
):
    """登记进行中状态后立刻发布快照——「论文信息补全中」项才会在运行期间就
    出现在已连接的铃铛里(此前只有 job 结束的 notify_pending 会刷新,用户不
    重连就永远看不到它)。

    关键断言是**时序**:首次发布时 entry 必须已经在 dict 里。发早了(登记之前)
    推出去的快照压根不含这一项,等于没修。
    """
    _insert_source(repo, notebook_id, "src-pub")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM(PAYLOAD))

    seen_building: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit", lambda uid, evt: None)
    monkeypatch.setattr(
        pb_module.pending_bus, "mark_dirty",
        lambda uid: seen_building.append(repo.paper_meta_backfilling(notebook_id)),
    )
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty_throttled",
                        lambda uid, *a, **kw: False)

    service.backfill_paper_metadata(notebook_id)

    assert seen_building, "运行期间至少发布过一次快照"
    assert seen_building[0] is True, "首次发布时 entry 必须已登记"
    assert seen_building[-1] is False, "收尾发布时 entry 已 pop(终态)"


def test_backfill_all_non_paper_still_reports_completion(
    repo, notebook_id, service, monkeypatch
):
    """整批都判定为非论文 = 成功完成,必须发 done。

    ensure_paper_metadata 对非论文落标记行并返回 "not_paper",于是 stored 停在
    0。只看 stored 会让这种完全成功的 job 静默收场——前端刻意不自己弹完成
    toast(交给铃铛),用户就只能看着按钮悄悄复位。事件同时带 not_paper,供前端
    区分「已补全 N 篇」与「N 篇均非论文」。
    """
    _insert_source(repo, notebook_id, "src-np1")
    _insert_source(repo, notebook_id, "src-np2")
    bind_chat_client(repo, "paper_metadata", _FakeKgLLM({"is_paper": False}))

    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty", lambda uid: None)
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty_throttled",
                        lambda uid, *a, **kw: False)

    counts = service.backfill_paper_metadata(notebook_id)

    assert counts.get("stored", 0) == 0 and counts["not_paper"] == 2
    done = [e for _, e in events if e.get("event") == "paper_meta_done"]
    assert len(done) == 1, "全非论文也要报完成"
    assert done[0]["stored"] == 0
    assert done[0]["not_paper"] == 2
