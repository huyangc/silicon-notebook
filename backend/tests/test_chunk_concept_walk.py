import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """两源各一 MoE 概念,经 concept_clusters(K-moe)桥接;evidence 指向本源 chunk。
    复刻 test_ppr_retrieve.py 同名助手。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


class _AnswerLLM:
    configured = True
    def __init__(self, text): self.text = text
    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"answer": self.text, "grounded": True})


class _FakeRerank:
    def __init__(self, configured=True): self._c = configured
    @property
    def configured(self): return self._c
    def rerank(self, query, documents, on_error=None): return list(range(len(documents)))


def test_mix_retrieve_adds_concept_walk_stream_when_flag_on(repo):
    nb = _seed_two_doc_moe(repo)
    cand, _block, _idmap, _hits, ppr_n = repo._mix_retrieve(
        nb.id, "DeepSeek-V3 Mixture-of-Experts", "", ["DeepSeek-V3 Mixture-of-Experts"])
    assert ppr_n > 0                                       # 概念漫游(PPR)贡献了 chunk
    ids = [c.chunk_id for c in cand]
    assert "cA" in ids and "cB" in ids                     # 跨文档 chunk 都进候选池
    assert len(ids) == len(set(ids))                       # 三路去重:无重复 chunk_id


def test_mix_retrieve_no_concept_walk_when_flag_off(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    cand, _block, _idmap, _hits, ppr_n = repo._mix_retrieve(
        nb.id, "DeepSeek-V3 Mixture-of-Experts", "", ["DeepSeek-V3 Mixture-of-Experts"])
    assert ppr_n == 0                                      # flag 关 → 不跑 PPR
    assert len(set(c.chunk_id for c in cand)) == len(cand) # 仍去重


class _AnswerOnlyReasoningLLM:
    """plan 单子查询;reflect 永远 answer → reasoning 只靠 seed pass 跑出 ppr 轨迹。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "都用 MoE [k1].", "grounded": True})


def test_reasoning_trace_uses_concept_walk_name(repo):
    """reasoning 的 ppr 轨迹 summary 改叫「概念漫游」,机器键 step_type 仍 'ppr'。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyReasoningLLM()
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ppr_steps = [s for s in result.trace if s.step_type == "ppr"]
    assert ppr_steps                                            # 机器键不变
    assert any("概念漫游" in s.summary for s in ppr_steps)      # 文案已改名
    assert not any("PPR 跨文档" in s.summary for s in result.trace)
