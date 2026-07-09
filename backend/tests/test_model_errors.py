"""model-call error observability(L1 model_error 事件 + L2 AskResponse.model_errors)。

graceful degradation 验证:embed/rerank/answer 调用失败时记录但不中断作答。
仓库是单例,错误收集走 module-level ContextVar(_ASK_MODEL_ERRORS),逐请求隔离;
本组用例同时覆盖「无 sink(不在 ask 上下文)只 emit 不崩」的边界。
"""
import json as _j

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.rerank_client import RerankClient
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_chunks(repo):
    """一个 notebook + 两个 chunk(不建 KG,走纯 chunk 路径,避免 overlay/rerank 干扰)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s", nb.id, "D", "markdown", "extracted", "parsed",
             "D", "/d", 0, "", "", "textbook", _now(), _now()))
        for cid, els in [("ck-1", ["el-1"]), ("ck-2", ["el-2"])]:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, nb.id, "s", "cascode " + cid, "1", _j.dumps(els), _now()))
    return nb


class _AnswerLLM:
    """合规答案 LLM:返回有效 JSON。"""
    configured = True

    def __init__(self, text="cascode raises rout [k1]"):
        self.text = text

    def chat_json(self, messages, schema_hint, **kw):
        return _j.dumps({"answer": self.text, "grounded": True})


class _RaisingLLM:
    """答案 LLM 调用即抛(模拟模型端故障)。"""
    configured = True

    def chat_json(self, messages, schema_hint, **kw):
        raise RuntimeError("upstream 503")


def test_answer_llm_failure_recorded(repo):
    """答案 LLM 抛异常 → model_errors 记 stage=answer;答案诚实降级(synthesis_failed)而非中断。
    (合成失败——空 content 或抛错——统一走 _answer_with_retry:有界重试 + 诚实降级 +
    可观测;不再冒充成 "Retrieved N passage(s)" 那样的成功占位。)"""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    repo.llm_client = _RaisingLLM()
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    stages = [e.stage for e in resp.model_errors]
    assert "answer" in stages
    assert resp.llm_mode == "synthesis_failed"   # 诚实降级,未中断
    assert not resp.conclusion.startswith("Retrieved ")


def test_embed_failure_recorded(repo, monkeypatch):
    """embed_query 抛异常(且 embedder_configured 为真)→ model_errors 记一条 stage=embed。"""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    # 让 embedder_configured 成立,使 _embed_query 走真实调用路径(而非"未配置"早返回)。
    repo.settings.embed_provider = "dashscope"
    repo.settings.embed_base_url = "http://fake"
    repo.settings.embed_api_key = "k"
    repo.settings.embed_model = "text-embedding-v4"
    assert repo.settings.embedder_configured
    monkeypatch.setattr(repo.embedder, "embed_query",
                        lambda q: (_ for _ in ()).throw(RuntimeError("embed boom")))
    repo.llm_client = _AnswerLLM()
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    stages = [e.stage for e in resp.model_errors]
    assert "embed" in stages


def test_rerank_on_error_called():
    """RerankClient.rerank 失败时调用 on_error 一次并返回 identity 序。"""
    class _S:
        rerank_model = "qwen3-rerank"
        rerank_base_url = "http://fake/v1"
        rerank_api_key = "k"
        rerank_max_docs = 500
        embed_concurrency = 8
        openai_compat_timeout_seconds = 30

    rc = RerankClient(_S())
    captured = []
    rc._rerank_batch = lambda q, d: (_ for _ in ()).throw(RuntimeError("rerank boom"))

    order = rc.rerank("q", ["a", "b", "c"], on_error=lambda exc: captured.append(exc))

    assert order == [0, 1, 2]
    assert len(captured) == 1
    assert isinstance(captured[0], RuntimeError)


def test_note_model_error_without_sink_just_emits(repo):
    """不在 ask 上下文(ContextVar 无 sink)时调用 _note_model_error:只 emit,不抛。"""
    # 不设置 _ASK_MODEL_ERRORS;默认 None。
    repo._note_model_error("embed", "m", RuntimeError("x"))  # 不应抛异常


def test_no_model_errors_on_success(repo):
    """正常 ask_chunk(合规 LLM + FakeEmbedder)→ model_errors 为空。"""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    repo.llm_client = _AnswerLLM()
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    assert resp.model_errors == []
