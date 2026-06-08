import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


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
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
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


def test_answer_kg_refines_when_enabled(repo):
    repo.llm_client = _RefineAnswerLLM()
    repo.settings.kg_query_refine_enabled = True
    nb, hits = _seed_hit(repo)
    answer, grounded, anchors = repo._answer_kg(
        nb.id, "how does cascode affect output resistance?", hits, []
    )
    assert repo.llm_client.refine_calls == 1  # refinement happened before answering
    assert repo.llm_client.answer_calls == 1
    assert answer  # answer produced


def test_answer_kg_no_refine_when_disabled(repo):
    repo.llm_client = _RefineAnswerLLM()
    repo.settings.kg_query_refine_enabled = False  # default is now True; disable explicitly
    nb, hits = _seed_hit(repo)
    answer, grounded, anchors = repo._answer_kg(nb.id, "q?", hits, [])
    assert repo.llm_client.refine_calls == 0  # no refinement
    assert repo.llm_client.answer_calls == 1
