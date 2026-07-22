import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_embedding_client
from tests.model_testkit import bind_chat_client


class _DescLLM:
    configured = True
    def __init__(self): self.calls = 0
    def chat_json(self, messages, schema_hint):
        self.calls += 1
        return json.dumps({"description": "Cascode is a stacked-transistor configuration that raises output resistance."})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def _ev(q):
    return {"quoted_span": q, "element_id": "", "source_title": "", "source_id": ""}


def _seed_two_doc_concept(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "cascode", "section_path": "1"},
        "evidence": [_ev("cascode stacks transistors")]}], [])
    repo.store_kg(nb.id, None, [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "Cascode", "section_path": "2"},
        "evidence": [_ev("cascode raises output resistance")]}], [])
    return nb


def test_concept_desc_generated_for_merged_cluster(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    repo.settings.kg_concept_desc_enabled = True
    nb = _seed_two_doc_concept(repo)
    repo.rebuild_unified_kg(nb.id)
    # the merged concept cluster now has a canonical_description persisted
    with repo._connect() as db:
        rows = db.execute("SELECT DISTINCT canonical_description FROM concept_clusters "
                          "WHERE notebook_id=? AND object_type='concept' AND canonical_description!=''",
                          (nb.id,)).fetchall()
    assert rows and "output resistance" in rows[0]["canonical_description"]
    assert llm.calls >= 1


def test_concept_desc_off_when_disabled(repo):
    llm = _DescLLM()
    bind_chat_client(repo, "kg_concept_description", llm)
    repo.settings.kg_concept_desc_enabled = False   # 默认已改为开(2026-06-18),这里显式关以测 off 路径
    nb = _seed_two_doc_concept(repo)
    repo.rebuild_unified_kg(nb.id)
    assert llm.calls == 0        # no LLM calls when disabled
    with repo._connect() as db:
        rows = db.execute("SELECT canonical_description FROM concept_clusters WHERE notebook_id=?",
                          (nb.id,)).fetchall()
    assert all(r["canonical_description"] == "" for r in rows)


def test_node_context_uses_cluster_description(repo):
    bind_chat_client(repo, "kg_concept_description", _DescLLM())
    repo.settings.kg_concept_desc_enabled = True
    nb = _seed_two_doc_concept(repo)
    repo.rebuild_unified_kg(nb.id)
    # pick one member concept id; node_context.definition should be the cluster desc
    import json as _j
    with repo._connect() as db:
        oid = db.execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept' LIMIT 1",
                         (nb.id,)).fetchone()["id"]
    ctx = repo.node_context(nb.id, oid)
    assert "output resistance" in (ctx.get("definition") or "")
