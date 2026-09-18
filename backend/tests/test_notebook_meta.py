"""Tests for _augment_notebook_meta: auto-fill notebook name+description
from processed sources (only when name is a default placeholder / purpose_auto=1).
"""
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate, NotebookUpdate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


class FakeLLMMeta:
    """LLM stub that returns a fixed name+description pair."""

    configured = True

    def __init__(self, name="Innovus 流程", description="覆盖 Innovus 实现流程。"):
        self._name = name
        self._description = description

    def chat_json(self, messages, schema_hint):
        return json.dumps({"name": self._name, "description": self._description})


class FakeLLMOff:
    """LLM stub that mimics a non-configured client."""

    configured = False

    def chat_json(self, messages, schema_hint):
        raise RuntimeError("LLM not configured — should never be called")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    bind_chat_client(r, "notebook_metadata", FakeLLMMeta())
    return r


def _insert_source(repo, notebook_id, title="Test Source", status="extracted",
                   doc_type="", summary="A summary of the document."):
    """Insert a source row directly (bypasses file-upload machinery)."""
    from app.services.sqlite_repository import _now
    import uuid
    source_id = f"src-{uuid.uuid4().hex[:8]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """
            INSERT INTO sources
              (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, error_message,
               doc_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, notebook_id, title, "pdf", status, "uploaded",
                "test.pdf", "", 0, "", summary, "", doc_type, now, now,
            ),
        )
    return source_id


# ---------------------------------------------------------------------------
# Test 1: fills default name AND blank description when LLM is active
# ---------------------------------------------------------------------------

def test_augment_fills_default_name_and_blank_desc(repo):
    """Notebook with default placeholder name + blank purpose (purpose_auto=1)
    should get name and description filled from the LLM response."""
    nb = repo.create_notebook(NotebookCreate(name="未命名笔记本", purpose=""))
    # purpose_auto should be 1 because purpose was blank
    with repo._connect() as db:
        row = db.execute("SELECT purpose_auto FROM notebooks WHERE id=?", (nb.id,)).fetchone()
    assert row["purpose_auto"] == 1

    _insert_source(repo, nb.id, title="Innovus 实现流程手册", status="extracted")

    repo._augment_notebook_meta(nb.id)

    nb_after = repo.get_notebook(nb.id)
    assert nb_after.name == "Innovus 流程"
    assert nb_after.purpose == "覆盖 Innovus 实现流程。"


# ---------------------------------------------------------------------------
# Test 2: preserves user-set name and description (no clobber)
# ---------------------------------------------------------------------------

def test_augment_preserves_user_set_values(repo):
    """A notebook with a real user name and user-set purpose (purpose_auto=0)
    must NOT be overwritten, even if the LLM returns something different."""
    nb = repo.create_notebook(
        NotebookCreate(name="我的笔记本", purpose="用户手写的描述，不能被覆盖。")
    )
    with repo._connect() as db:
        row = db.execute("SELECT purpose_auto FROM notebooks WHERE id=?", (nb.id,)).fetchone()
    # purpose was provided → purpose_auto=0
    assert row["purpose_auto"] == 0

    _insert_source(repo, nb.id, title="Some Paper", status="extracted")

    repo._augment_notebook_meta(nb.id)

    nb_after = repo.get_notebook(nb.id)
    # Both fields must be unchanged
    assert nb_after.name == "我的笔记本"
    assert nb_after.purpose == "用户手写的描述，不能被覆盖。"


# ---------------------------------------------------------------------------
# Test 3: deterministic fallback when LLM is off
# ---------------------------------------------------------------------------

def test_augment_fallback_without_llm(repo):
    """When the LLM client is not configured, the method should fall back to
    a deterministic name (source title) and a description containing
    '本笔记本收录了'."""
    bind_chat_client(repo, "notebook_metadata", FakeLLMOff())

    nb = repo.create_notebook(NotebookCreate(name="未命名笔记本", purpose=""))

    _insert_source(repo, nb.id, title="Innovus 实现手册", status="extracted")

    repo._augment_notebook_meta(nb.id)

    nb_after = repo.get_notebook(nb.id)
    assert nb_after.name  # non-empty
    assert "本笔记本收录了" in nb_after.purpose


# ---------------------------------------------------------------------------
# Test 4: pending_source_id counts a still-'extracting' source
# ---------------------------------------------------------------------------

def test_augment_counts_pending_source(repo):
    """The pending_source_id param allows the finishing source (still in
    'extracting' state) to be included so the FIRST source triggers
    name/description generation."""
    nb = repo.create_notebook(NotebookCreate(name="未命名笔记本", purpose=""))

    # Insert source with status='extracting' (not yet 'extracted')
    src_id = _insert_source(repo, nb.id, title="Innovus 流程文档", status="extracting")

    repo._augment_notebook_meta(nb.id, pending_source_id=src_id)

    nb_after = repo.get_notebook(nb.id)
    # LLM stub returns "Innovus 流程" and "覆盖 Innovus 实现流程。"
    assert nb_after.name == "Innovus 流程"
    assert nb_after.purpose == "覆盖 Innovus 实现流程。"


def test_automatic_name_and_description_follow_later_sources(repo):
    nb = repo.create_notebook(NotebookCreate())
    _insert_source(repo, nb.id, title="Timing")
    repo._augment_notebook_meta(nb.id)
    _insert_source(repo, nb.id, title="Power")
    bind_chat_client(repo, "notebook_metadata", FakeLLMMeta("时序与功耗", "覆盖两类资料。"))
    repo._augment_notebook_meta(nb.id)
    assert (repo.get_notebook(nb.id).name, repo.get_notebook(nb.id).purpose) == (
        "时序与功耗", "覆盖两类资料。",
    )


@pytest.mark.parametrize("field,value", [("name", "未命名笔记本"), ("purpose", "")])
def test_manual_fields_remain_manual_even_when_cleared_or_placeholder(repo, field, value):
    nb = repo.create_notebook(NotebookCreate())
    _insert_source(repo, nb.id)
    repo._augment_notebook_meta(nb.id)
    repo.update_notebook(nb.id, NotebookUpdate(**{field: value}))
    bind_chat_client(repo, "notebook_metadata", FakeLLMMeta("新标题", "新描述"))
    repo._augment_notebook_meta(nb.id)
    result = repo.get_notebook(nb.id)
    assert getattr(result, field) == value
    assert getattr(result, "purpose" if field == "name" else "name") == (
        "新描述" if field == "name" else "新标题"
    )


def test_all_sources_and_summary_tails_reach_model(repo):
    nb = repo.create_notebook(NotebookCreate())
    seen = []

    class Capture(FakeLLMMeta):
        def chat_json(self, messages, schema_hint):
            seen.append(messages[0]["content"])
            return super().chat_json(messages, schema_hint)

    bind_chat_client(repo, "notebook_metadata", Capture())
    for i in range(25):
        _insert_source(repo, nb.id, title=f"source-{i:02d}",
                       status="parsed", summary="前文" * 150 + f"尾部主题-{i:02d}")
    _insert_source(repo, nb.id, title="等待解析的来源", status="queued")
    repo._augment_notebook_meta(nb.id)
    prompts = "\n".join(seen)
    for i in range(25):
        assert f"source-{i:02d}" in prompts
        assert f"尾部主题-{i:02d}" in prompts
    assert "等待解析的来源" in prompts


def test_large_inputs_reduce_all_batches_without_truncating(repo):
    nb = repo.create_notebook(NotebookCreate())
    repo.settings.notebook_metadata_batch_chars = 4096
    seen = []

    class Summaries(FakeLLMMeta):
        def chat_json(self, messages, schema_hint):
            block = messages[0]["content"].split("Sources:\n", 1)[1]
            assert len(block) <= 4096
            seen.append(block)
            return json.dumps({"name": f"分组{len(seen)}", "description": "多主题摘要"})

    bind_chat_client(repo, "notebook_metadata", Summaries())
    _insert_source(repo, nb.id, title="Large", summary="全文" * 5000 + "结尾主题")
    _insert_source(repo, nb.id, title="第二来源", summary="其他主题")
    repo._augment_notebook_meta(nb.id)
    assert any("结尾主题" in block for block in seen)
    assert any("第二来源" in block for block in seen)
    assert all(f"分组{i}" in seen[-1] for i in range(1, len(seen)))


def test_concurrent_refresh_coalesces_and_does_not_publish_old_response(repo):
    nb = repo.create_notebook(NotebookCreate())
    _insert_source(repo, nb.id)
    entered, release = Event(), Event()
    waiting_second, waiting_third = Event(), Event()
    caller = ContextVar("metadata_caller", default="missing")

    class ObservedSettled(Event):
        """Observe actual waiter admission, never assume scheduler ordering."""
        waiters = 0

        def wait(self, timeout=None):
            self.waiters += 1
            (waiting_second if self.waiters == 1 else waiting_third).set()
            return super().wait(timeout)

    def invoke(identity):
        caller.set(identity)
        repo._augment_notebook_meta(nb.id)

    calls = []

    class Slow(FakeLLMMeta):
        def chat_json(self, messages, schema_hint):
            calls.append(caller.get())
            if len(calls) == 1:
                entered.set()
                assert release.wait(10)
                return json.dumps({"name": "过期标题", "description": "过期描述"})
            # The first result was rejected before the newer pass began.
            assert repo.get_notebook(nb.id).name == "Untitled notebook"
            return json.dumps({"name": "完整标题", "description": "完整描述"})

    bind_chat_client(repo, "notebook_metadata", Slow())
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(invoke, "first")
        try:
            assert entered.wait(10)
            coordinator = repo._runtime.source_ingestion._metadata_refresh
            with coordinator._lock:
                coordinator._active[nb.id].settled = ObservedSettled()
            second = pool.submit(invoke, "second")
            assert waiting_second.wait(10)
            third = pool.submit(invoke, "third")
            assert waiting_third.wait(10)
        finally:
            release.set()
        for future in (first, second, third):
            future.result(timeout=10)
    assert calls == ["first", "third"]
    assert repo.get_notebook(nb.id).name == "完整标题"


def test_manual_edit_during_model_call_wins_and_other_field_updates(repo):
    nb = repo.create_notebook(NotebookCreate())
    _insert_source(repo, nb.id)

    class EditWhileGenerating(FakeLLMMeta):
        def chat_json(self, messages, schema_hint):
            repo.update_notebook(nb.id, NotebookUpdate(name="手动标题"))
            return super().chat_json(messages, schema_hint)

    bind_chat_client(repo, "notebook_metadata", EditWhileGenerating())
    repo._augment_notebook_meta(nb.id)
    assert repo.get_notebook(nb.id).name == "手动标题"
    assert repo.get_notebook(nb.id).purpose == "覆盖 Innovus 实现流程。"


def test_delete_last_source_clears_only_automatic_metadata(repo):
    nb = repo.create_notebook(NotebookCreate())
    source_id = _insert_source(repo, nb.id)
    repo._augment_notebook_meta(nb.id)
    repo.update_notebook(nb.id, NotebookUpdate(name="保留标题"))
    repo.delete_source(source_id)
    result = repo.get_notebook(nb.id)
    assert result.name == "保留标题"
    assert result.purpose == "尚未添加来源。"


def test_offline_multi_source_fallback_does_not_claim_first_source_topic(repo):
    nb = repo.create_notebook(NotebookCreate())
    bind_chat_client(repo, "notebook_metadata", FakeLLMOff())
    _insert_source(repo, nb.id, title="Timing")
    _insert_source(repo, nb.id, title="Power")
    repo._augment_notebook_meta(nb.id)
    assert repo.get_notebook(nb.id).name == "资料集（2 个来源）"
    assert "2 个来源" in repo.get_notebook(nb.id).purpose


def test_explicit_copy_name_becomes_manual(repo):
    nb = repo.create_notebook(NotebookCreate())
    copied = repo.copy_notebook(nb.id, new_owner_id=repo.current_user().id, new_name="手动副本")
    _insert_source(repo, copied.id)
    repo._augment_notebook_meta(copied.id)
    assert repo.get_notebook(copied.id).name == "手动副本"
    assert repo.get_notebook(copied.id).purpose == "覆盖 Innovus 实现流程。"
