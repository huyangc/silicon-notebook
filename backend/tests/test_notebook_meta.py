"""Tests for _augment_notebook_meta: auto-fill notebook name+description
from processed sources (only when name is a default placeholder / purpose_auto=1).
"""
import json
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_embedding_client
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
    bind_embedding_client(r, FakeEmbedder(dim=16))
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
