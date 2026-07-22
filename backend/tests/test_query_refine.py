import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


class _RefineAnswerLLM:
    configured = True

    def __init__(self):
        self.refine_calls = 0
        self.answer_calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if '"relevant"' in schema_hint:
            self.refine_calls += 1
            return json.dumps({"relevant": ["cascode raises output resistance"]})
        if '"answer"' in schema_hint:
            self.answer_calls += 1
            return json.dumps({"answer": "Cascode raises output resistance [k1].", "grounded": True})
        return "{}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _ev(q):
    return {"quoted_span": q, "element_id": "", "source_title": "S", "source_id": ""}


def _seed_hit(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(
        nb.id,
        None,
        [
            {
                "local_id": "A",
                "object_type": "claim",
                "payload": {"name": "cascode raises output resistance", "section_path": "1"},
                "evidence": [_ev("cascode raises output resistance because ...")],
            }
        ],
        [],
    )
    with repo._connect() as db:
        oid = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? LIMIT 1", (nb.id,)
        ).fetchone()["id"]
    hit = RetrievedKnowledge(
        object_id=oid,
        object_type="claim",
        payload={"name": "cascode raises output resistance"},
        evidence=[],
    )
    return nb, [hit]


def test_answer_reasoning_refines_when_enabled(repo):
    """Reasoning answer refinement uses the dedicated evidence workload."""
    refine_client = _RefineAnswerLLM()
    answer_client = _RefineAnswerLLM()
    bind_chat_client(repo, "evidence_refine", refine_client)
    bind_chat_client(repo, "ask_answer", answer_client)
    repo.settings.kg_query_refine_enabled = True
    nb, hits = _seed_hit(repo)
    answer, grounded, anchors = repo._answer_reasoning(
        nb.id, "how does cascode affect output resistance?", hits, [], ""
    )
    assert refine_client.refine_calls == 1
    assert refine_client.answer_calls == 0
    assert answer_client.refine_calls == 0
    assert answer_client.answer_calls == 1
    assert ("chat", "evidence_refine") in repo._runtime.models.calls
    assert ("chat", "ask_answer") in repo._runtime.models.calls
    assert answer  # answer produced


def test_answer_reasoning_no_refine_when_disabled(repo):
    """_answer_reasoning 在 refine disabled 时不触发 refine。"""
    refine_client = _RefineAnswerLLM()
    answer_client = _RefineAnswerLLM()
    bind_chat_client(repo, "evidence_refine", refine_client)
    bind_chat_client(repo, "ask_answer", answer_client)
    repo.settings.kg_query_refine_enabled = False  # default is now True; disable explicitly
    nb, hits = _seed_hit(repo)
    answer, grounded, anchors = repo._answer_reasoning(nb.id, "q?", hits, [], "")
    assert refine_client.refine_calls == 0
    assert answer_client.refine_calls == 0
    assert answer_client.answer_calls == 1


def test_refine_context_prepends_focused_evidence(repo):
    class _FakeRefineLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            import json as _j
            return _j.dumps({"relevant": ["Engram separates storage from computation"]})
    out = repo._refine_context("What is Engram?",
                               "k1: [concept][personal] Engram", _FakeRefineLLM())
    assert out.startswith("Focused relevant evidence")
    assert "k1: [concept][personal] Engram" in out

def test_refine_context_noop_when_disabled(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_query_refine_enabled", False)
    cb = "k1: [concept][personal] Engram"
    class _C:
        configured = True
    assert repo._refine_context("q", cb, _C()) == cb

def test_refine_context_noop_when_client_unconfigured(repo):
    class _C:
        configured = False
    assert repo._refine_context("q", "k1: x", _C()) == "k1: x"
