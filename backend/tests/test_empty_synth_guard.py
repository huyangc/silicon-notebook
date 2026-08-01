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
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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
    bind_chat_client(repo, "ask_answer", stub)
    nb = _seed_chunks(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert stub.calls == 2                                     # 空 content 触发重试
    assert not resp.conclusion.startswith("Retrieved ")       # 不落误导占位
    assert "cascode raises rout" in resp.conclusion


def test_chunk_empty_degrades_honestly(repo):
    _chunk_only(repo)
    bind_chat_client(repo, "ask_answer", _AlwaysEmpty())
    nb = _seed_chunks(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="cascode", mode="chunk"))
    assert not resp.conclusion.startswith("Retrieved ")       # 不冒充成功占位
    assert resp.llm_mode == "synthesis_failed"
    assert any(e.stage == "answer" for e in resp.model_errors)  # 可观测
    assert resp.citations                                     # 证据保留


# --- 重试成功不留假报警(设计文档 §3.1.1) -----------------------------------
#
# 真机:第一次合成调用耗时 4 分 25 秒返回空 content(上游 malformed_response),
# 有界重试第二次成功、答案完整(78 锚点 / 5829 字),用户面却仍挂着红色横幅
# 「本次回答可能不完整」。那次故障已被同一轮吸收、结果完全没受影响,横幅是假报警。
# 规则与按节合成回退那处同源:恢复成功就摘掉本次尝试自己记的那几条,别的一律不动。

def _sink_scope(repo, synth):
    """在一个干净的响应内报警槽里跑一次 _answer_with_retry,返回 (结果, sink)。"""
    sink: list = []
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        return repo._answer_with_retry(synth, "m"), sink
    finally:
        _ASK_MODEL_ERRORS.reset(tok)


def test_retry_success_drops_the_alarm_it_raised_itself(repo):
    """第一次抛错、第二次答出完整答案 → 响应里一条报警都不留。"""
    calls = []

    def synth():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream malformed_response")
        return ("完整答案 [k1]", True, [])

    (_ans, _g, _anc, ok), sink = _sink_scope(repo, synth)

    assert ok is True and len(calls) == 2
    assert sink == [], "重试已经把这次故障吸收掉了,再挂横幅是在报一个没影响结果的错误"


def test_retry_success_after_empty_content_leaves_no_alarm(repo):
    """空 content(不抛异常、status=ok)那条路径同理 —— 它本来就一条都不记,
    这里钉住「摘除不会反过来制造新条目」,顺带守住真机那次故障的形状。"""
    calls = []

    def synth():
        calls.append(1)
        return ("完整答案 [k1]", True, []) if len(calls) == 2 else ("", False, [])

    (_ans, _g, _anc, ok), sink = _sink_scope(repo, synth)

    assert ok is True and sink == []


def test_two_failures_keep_every_alarm_including_the_terminal_one(repo):
    """两次都失败 = 这一轮真的没答出来:一条都不许摘。

    尤其是最后那条 empty-content 的 RuntimeError —— 「检索到却答不出」是必须可见的
    终态,摘掉它等于把一次静默失败重新变成静默。
    """
    def synth():
        raise RuntimeError("upstream 503")

    (_ans, _g, _anc, ok), sink = _sink_scope(repo, synth)

    assert ok is False
    assert len(sink) == 3, "两次尝试各一条 + 终态那条"
    assert {row["stage"] for row in sink} == {"answer"}

    def empty():
        return ("", False, [])

    (_ans2, _g2, _anc2, ok2), sink2 = _sink_scope(repo, empty)
    assert ok2 is False
    assert len(sink2) == 1, "空 content 不抛异常,只有终态那一条"


def test_earlier_alarms_from_other_workloads_survive(repo):
    """只摘 mark 之后本次尝试自己记的那几条。

    同一 run 里更早的 evidence_refine / embedding / rerank 报警属于别的 workload,
    它们与这次合成能不能恢复毫无关系;`del sink[:]` 那种写法会把整轮的可观测性
    一次清空。
    """
    calls = []

    def synth():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream 503")
        return ("完整答案 [k1]", True, [])

    sink: list = [{"stage": "rerank", "message": "upstream_error"},
                  {"stage": "embedding", "message": "upstream_error"}]
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        _ans, _g, _anc, ok = repo._answer_with_retry(synth, "m")
    finally:
        _ASK_MODEL_ERRORS.reset(tok)

    assert ok is True
    assert [row["stage"] for row in sink] == ["rerank", "embedding"]


def test_cancellation_propagates_and_touches_nothing(repo):
    """用户按了中断不是合成失败:异常原样穿透,报警槽零改动。"""
    from app.services.ask_service import AskCancelled

    sink: list = [{"stage": "rerank", "message": "upstream_error"}]
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        with pytest.raises(AskCancelled):
            repo._answer_with_retry(lambda: (_ for _ in ()).throw(AskCancelled()), "m")
    finally:
        _ASK_MODEL_ERRORS.reset(tok)

    assert [row["stage"] for row in sink] == ["rerank"]


def test_without_a_response_sink_the_helper_is_unchanged(repo):
    """直调/离线路径下 ContextVar 是 None:没有可摘的东西,行为与接入前一致
    (事件日志照常记全 —— note_model_error 的两个副作用里只有响应那一份被摘)。"""
    calls = []

    def synth():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream 503")
        return ("完整答案 [k1]", True, [])

    assert _ASK_MODEL_ERRORS.get() is None
    ans, _g, _anc, ok = repo._answer_with_retry(synth, "m")
    assert ok is True and ans == "完整答案 [k1]"


def test_an_alarm_raised_inside_synth_by_another_workload_survives(repo):
    """synth() **内部**别的 workload 记下的报警不能被摘掉——哪怕首次就成功。

    这条钉的是「按身份过滤」而不是「按位置截断」。今天 `_refine_context` 静默吞
    异常、不记 note_model_error,所以两种写法碰巧等价;但那是个会被下一次改动
    打破的巧合——给证据精炼补一条 note_model_error 是很自然的一步,那之后
    `del sink[mark:]` 就会在「首次即成功 + 精炼失败」时把它一起删掉,正好违反
    「其它 workload 一条都不许动」。用例因此让 synth 自己往 sink 里记一条
    evidence_refine 再返回答案:位置式摘除必红,身份过滤必绿。
    """
    def synth():
        sink_now = _ASK_MODEL_ERRORS.get()
        sink_now.append({"stage": "answer", "workload_id": "evidence_refine",
                         "message": "upstream_error"})
        return ("完整答案 [k1]", True, [])

    sink: list = []
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        _ans, _g, _anc, ok = repo._answer_with_retry(synth, "m")
    finally:
        _ASK_MODEL_ERRORS.reset(tok)

    assert ok is True
    assert [row["workload_id"] for row in sink] == ["evidence_refine"], (
        "首次即成功时,synth 内部别的 workload 记的报警必须原样保留,"
        f"实得 {sink}")


def test_a_recovered_retry_still_drops_only_its_own_answer_alarm(repo):
    """恢复时:自己那条 ask_answer 摘掉,同一区间里别人的那条留下。"""
    calls = []

    def synth():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream 503")
        _ASK_MODEL_ERRORS.get().append(
            {"stage": "answer", "workload_id": "evidence_refine",
             "message": "upstream_error"})
        return ("完整答案 [k1]", True, [])

    sink: list = []
    tok = _ASK_MODEL_ERRORS.set(sink)
    try:
        _ans, _g, _anc, ok = repo._answer_with_retry(synth, "m")
    finally:
        _ASK_MODEL_ERRORS.reset(tok)

    assert ok is True
    assert [row["workload_id"] for row in sink] == ["evidence_refine"], (
        f"只有 ask_answer 那条该被摘掉,实得 {sink}")
