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
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "stored"
    assert fake.calls == 1
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
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1")
    assert service.ensure_paper_metadata(source) == "stored"

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 1


def test_ensure_force_reextracts(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1")
    assert service.ensure_paper_metadata(source) == "stored"

    status = service.ensure_paper_metadata(source, force=True)

    assert status == "stored"
    assert fake.calls == 2


def test_not_paper_marker_prevents_retry(repo, notebook_id, service):
    fake = _FakeKgLLM({"is_paper": False})
    repo._kg_llm_client = fake
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
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "no_llm"
    assert repo.get_paper_meta("src-1") is None
    assert fake.calls == 0


def test_memory_source_gated(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1", source_type="memory")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0
    assert repo.get_paper_meta("src-1") is None


def test_knowhow_source_gated(repo, notebook_id, service):
    """姊妹场景:source.type 门控是单个 in ("memory", "knowhow") 检查,两个成员
    都要各自覆盖,免得元组打错字只有一半被测试兜住。"""
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1", source_type="knowhow")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0
    assert repo.get_paper_meta("src-1") is None


def test_textbook_doc_type_gated(repo, notebook_id, service):
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1", doc_type="textbook")

    status = service.ensure_paper_metadata(source)

    assert status == "skipped"
    assert fake.calls == 0


def test_disabled_setting_gates(repo, notebook_id, service, monkeypatch):
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
    monkeypatch.setattr(repo.settings, "paper_meta_enabled", False)
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "disabled"
    assert fake.calls == 0


# ---------------------------------------------------------------------------
# ensure_paper_metadata: failure handling (never raises, never leaves a row)
# ---------------------------------------------------------------------------


def test_llm_exception_is_swallowed(repo, notebook_id, service):
    repo._kg_llm_client = _RaisingLLM()
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "failed"
    assert repo.get_paper_meta("src-1") is None


def test_ensure_non_object_json_fails_without_row(repo, notebook_id, service):
    """Controller amendment: safe_json() 契约是「总返回 dict」,对顶层是数组/
    标量的畸形输出会静默折叠成 {}。若把这个 {} 直接喂给 verify_paper_meta,会
    产出合法的 is_paper=False 标记行——把一次瞬态的模型输出错误,永久固化成
    「已判定非论文」,压掉后续重试。ensure_paper_metadata 必须在 unwrap 后仍非
    dict 时 raise,让外层 except 落到 failed(不写行,下次可重试)。"""
    fake = _RawJsonLLM("[]")
    repo._kg_llm_client = fake
    source = _insert_source(repo, notebook_id, "src-1")

    status = service.ensure_paper_metadata(source)

    assert status == "failed"
    assert repo.get_paper_meta("src-1") is None
    assert fake.calls == 1


def test_ensure_single_element_array_json_is_unwrapped(repo, notebook_id, service):
    """姊妹场景:模型把同一个合法 payload 包了一层数组(常见 LLM 误习惯)。这
    应该被无损展开、正常入库,而不是被上面那条防护误伤。"""
    fake = _RawJsonLLM(json.dumps([PAYLOAD]))
    repo._kg_llm_client = fake
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
    repo._kg_llm_client = fake
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
    repo._kg_llm_client = fake
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


def test_backfill_counts_and_progress(repo, notebook_id):
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake
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

    counts2 = repo.backfill_paper_metadata(notebook_id)
    assert counts2 == {"total": 0}
