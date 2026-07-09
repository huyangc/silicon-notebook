"""空合成退化统一治理:_answer_with_retry 共享 helper + chunk 端到端诚实降级。

根因见 [[reasoning-empty-content-degeneration]]:思考型模型偶把输出预算耗在
reasoning_content(被 _stream_chat_content 丢弃)上 → content 空 → chat_json 兜底
"{}" → 空 answer(不抛、status=ok)→ 原先误导占位/静默丢节。helper 统一:有界重试
一次 + 空则 emit model_error(可观测)+ 调用方诚实降级。
"""
import json as _j

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _ASK_MODEL_ERRORS, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


# --- 共享 helper 单元测(核心逻辑,与检索解耦) --------------------------------

def test_retry_recovers_on_second_attempt(repo):
    calls = []
    def synth():
        calls.append(1)
        return ("real [k1]", True, []) if len(calls) == 2 else ("", False, [])
    ans, g, anc, ok = repo._answer_with_retry(synth, "m")
    assert ok is True and ans == "real [k1]" and len(calls) == 2


def test_retry_all_empty_returns_not_ok_and_notes_error(repo):
    calls = []
    def synth():
        calls.append(1)
        return ("", False, [])
    sink: list = []
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        ans, g, anc, ok = repo._answer_with_retry(synth, "m")
    finally:
        _ASK_MODEL_ERRORS.reset(tok)
    assert ok is False and ans == "" and len(calls) == 2       # 重试一次后仍空
    assert sink and sink[0]["stage"] == "answer"               # 静默失败补记 model_error


def test_retry_recovers_after_exception(repo):
    calls = []
    def synth():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream 503")
        return ("recovered [k1]", True, [])
    ans, g, anc, ok = repo._answer_with_retry(synth, "m")
    assert ok is True and ans == "recovered [k1]" and len(calls) == 2


# --- chunk 模式端到端(纯 chunk 路径,关 rewrite/overlay → 唯一 chat_json 是答案) ---

def _seed_chunks(repo):
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


class _EmptyThenReal:
    configured = True
    model = "m"
    def __init__(self):
        self.calls = 0
    def chat_json(self, messages, schema_hint, **kw):
        self.calls += 1
        return "{}" if self.calls == 1 else _j.dumps({"answer": "cascode raises rout [k1]", "grounded": True})


class _AlwaysEmpty:
    configured = True
    model = "m"
    def chat_json(self, *a, **k):
        return "{}"


def _chunk_only(repo):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False


def test_chunk_empty_retries_and_recovers(repo):
    _chunk_only(repo)
    stub = _EmptyThenReal()
    repo.llm_client = stub
    nb = _seed_chunks(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert stub.calls == 2                                     # 空 content 触发重试
    assert not resp.conclusion.startswith("Retrieved ")       # 不落误导占位
    assert "cascode raises rout" in resp.conclusion


def test_chunk_empty_degrades_honestly(repo):
    _chunk_only(repo)
    repo.llm_client = _AlwaysEmpty()
    nb = _seed_chunks(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert not resp.conclusion.startswith("Retrieved ")       # 不冒充成功占位
    assert resp.llm_mode == "synthesis_failed"
    assert any(e.stage == "answer" for e in resp.model_errors)  # 可观测
    assert resp.citations                                     # 证据保留
