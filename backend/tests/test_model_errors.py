"""model-call error observability(L1 model_error 事件 + L2 AskResponse.model_errors)。

graceful degradation 验证:embed/rerank/answer 调用失败时记录但不中断作答。
仓库是单例,错误收集走 module-level ContextVar(_ASK_MODEL_ERRORS),逐请求隔离;
本组用例同时覆盖「无 sink(不在 ask 上下文)只 emit 不崩」的边界。
"""
import json as _j

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.models.ask import ModelError
from app.services.embedding import FakeEmbedder
from app.services.rerank_client import RerankClient
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.model_provider import ModelInvocationError
from app.services.model_registry import ModelServiceDefinition, WorkloadSpec
from tests.model_testkit import bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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


@pytest.mark.parametrize(
    "code",
    [
        "model_queue_full",
        "model_queue_timeout",
        "model_service_unavailable",
    ],
)
def test_scheduled_ask_failure_has_safe_support_metadata_and_no_fake_answer(
    repo, code
):
    service = ModelServiceDefinition(
        id="primary-chat", display_name="主模型服务", kind="chat",
        protocol="openai_chat", base_url="https://model.invalid/v1",
        model="safe-model", api_key_env="MODEL_KEY", api_key="secret",
        max_concurrency=2, fingerprint="fp",
    )
    workload = WorkloadSpec(
        id="ask_answer", kind="chat", default_priority="interactive",
        display_label="问答回答",
    )

    class BusyClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            raise ModelInvocationError(
                service=service, workload=workload, code=code,
                support_id="mdl-support-safe",
            )

    bind_chat_client(repo, "ask_answer", BusyClient())
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    notebook = _seed_chunks(repo)

    response = repo.ask_chunk(
        notebook.id, AskRequest(question="cascode", mode="chunk")
    )

    error = next(item for item in response.model_errors if item.stage == "answer")
    assert error.model_dump() == {
        "service_id": "primary-chat",
        "service_name": "主模型服务",
        "workload_id": "ask_answer",
        "workload_label": "问答回答",
        "stage": "answer",
        "model": "safe-model",
        "message": code,
        "support_id": "mdl-support-safe",
    }
    assert response.answer == ""
    assert response.llm_mode == "synthesis_failed"


def test_answer_llm_failure_recorded(repo):
    """答案 LLM 抛异常 → model_errors 记 stage=answer;答案诚实降级(synthesis_failed)而非中断。
    (合成失败——空 content 或抛错——统一走 _answer_with_retry:有界重试 + 诚实降级 +
    可观测;不再冒充成 "Retrieved N passage(s)" 那样的成功占位。)"""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    bind_chat_client(repo, "ask_answer", _RaisingLLM())
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    stages = [e.stage for e in resp.model_errors]
    assert "answer" in stages
    assert resp.llm_mode == "synthesis_failed"   # 诚实降级,未中断
    assert not resp.conclusion.startswith("Retrieved ")


def test_answer_failure_names_dynamic_primary_service_without_persisting_unstamped_double(repo):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    raising = _RaisingLLM()
    raising.model = "runtime-primary-name"
    bind_chat_client(repo, "ask_answer", raising)
    nb = _seed_chunks(repo)
    response = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    error = next(item for item in response.model_errors if item.stage == "answer")
    assert (error.service_id, error.service_name, error.model) == ("", "", "")
    assert error.workload_id == "ask_answer"
    assert error.message == "upstream_error"
    assert repo._runtime.model_status_store.get_all() == {}


def test_safe_tagged_model_name_remains_dynamic_on_failure(repo):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    raising = _RaisingLLM()
    raising.model = "llama3:70b"
    bind_chat_client(repo, "ask_answer", raising)
    nb = _seed_chunks(repo)

    response = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    error = next(item for item in response.model_errors if item.stage == "answer")
    assert (error.service_id, error.model, error.message) == (
        "", "", "upstream_error"
    )


def test_ask_failure_keeps_raw_diagnostic_server_side_only(repo, monkeypatch):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    unsafe_model = "https://10.0.0.8/v1?api_key=sk-private-secret"

    class SensitiveFailure:
        configured = True
        model = unsafe_model

        def chat_json(self, messages, schema_hint, **kwargs):
            raise RuntimeError(
                "provider https://10.0.0.8 leaked sk-private-secret"
            )

    events = []
    monkeypatch.setattr(repo.event_log, "emit", events.append)
    bind_chat_client(repo, "ask_answer", SensitiveFailure())
    nb = _seed_chunks(repo)

    response = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    serialized = response.model_dump_json()
    replay = repo.get_conversation(response.conversation_id).model_dump_json()
    with repo._connect() as db:
        persisted = db.execute(
            "SELECT payload FROM answers WHERE id=?", (response.answer_id,)
        ).fetchone()[0]

    for client_value in (serialized, replay, persisted):
        for private_value in (
            "10.0.0.8", "sk-private-secret", "provider", "RuntimeError",
        ):
            assert private_value not in client_value
        assert "upstream_error" in client_value
    assert all(error.model == "" for error in response.model_errors)
    assert all(error.message == "upstream_error" for error in response.model_errors)
    assert any(
        "10.0.0.8" in event.get("error", "")
        and "sk-private-secret" in event.get("error", "")
        for event in events
    )


def test_legacy_persisted_model_error_is_sanitized_on_replay(repo):
    nb = repo.create_notebook(NotebookCreate(name="legacy"))
    raw_payload = {
        "conclusion": "legacy answer",
        "model_errors": [{
            "service": "https://provider.example/v1",
            "stage": "api_key_sk_private_secret",
            "model": "https://10.0.0.8/v1?api_key=sk-private-secret",
            "message": "RuntimeError: provider secret response body",
        }],
    }
    with repo._write() as db:
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-legacy", nb.id, "legacy", repo.current_user().id, _now(), _now()),
        )
        db.execute(
            "INSERT INTO answers "
            "(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                "ans-legacy", nb.id, "q", _j.dumps(raw_payload), _now(),
                "conv-legacy",
            ),
        )

    replay = repo.get_conversation("conv-legacy")
    error = replay.turns[0].response.model_errors[0]
    assert (error.service_id, error.stage, error.model, error.message) == (
        "", "model_call", "", "upstream_error"
    )
    encoded = replay.model_dump_json()
    for private_value in (
        "api_key", "10.0.0.8", "sk-private-secret", "provider", "RuntimeError",
    ):
        assert private_value not in encoded


def test_legacy_model_error_replays_into_new_safe_metadata_shape():
    error = ModelError.model_validate({
        "service": "llm",
        "stage": "answer",
        "model": "legacy-model",
        "message": "upstream_error",
    })

    assert error.service_id == ""
    assert error.service_name == ""
    assert error.workload_id == ""
    assert error.workload_label == ""
    assert error.support_id == ""
    assert error.stage == "answer"
    assert error.model == "legacy-model"


def test_embed_failure_recorded(repo, monkeypatch):
    """A configured workload embedder failure is recorded at the embed stage."""
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    repo._runtime.models.embedding("retrieval_query_embedding").configured = True
    monkeypatch.setattr(repo._runtime.models.embedding("retrieval_query_embedding"), "embed_query",
                        lambda q: (_ for _ in ()).throw(RuntimeError("embed boom")))
    bind_chat_client(repo, "ask_answer", _AnswerLLM())
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    stages = [e.stage for e in resp.model_errors]
    assert "embed" in stages


def test_embedding_failure_is_embedding_service(repo, monkeypatch):
    repo.settings.query_rewrite_enabled = False
    repo.settings.chunk_kg_overlay_enabled = False
    repo._runtime.models.embedding("retrieval_query_embedding").configured = True
    monkeypatch.setattr(
        repo._runtime.models.embedding("retrieval_query_embedding"),
        "embed_query",
        lambda _query: (_ for _ in ()).throw(RuntimeError("embed boom")),
    )
    bind_chat_client(repo, "ask_answer", _AnswerLLM())
    nb = _seed_chunks(repo)
    response = repo.ask_chunk(
        nb.id, AskRequest(question="cascode", mode="chunk")
    )
    error = next(item for item in response.model_errors if item.stage == "embed")
    assert (error.service_id, error.service_name, error.model) == ("", "", "")
    assert error.support_id == ""


def test_rerank_on_error_called():
    """RerankClient.rerank 失败时调用 on_error 一次并返回 identity 序。"""
    class _S:
        rerank_model = "qwen3-rerank"
        rerank_base_url = "http://fake/v1"
        rerank_api_key = "k"
        rerank_max_docs = 500
        embed_concurrency = 8
        openai_compat_timeout_seconds = 30

    rc = RerankClient(
        _S(),
        model="qwen3-rerank",
        base_url="http://fake/v1",
        api_key="k",
    )
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
    bind_chat_client(repo, "ask_answer", _AnswerLLM())
    nb = _seed_chunks(repo)

    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))

    assert resp.model_errors == []
