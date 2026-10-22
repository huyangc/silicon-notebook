"""逐步推理接入文档内导航(T6):``browse_sections`` / ``read_section`` / 章节线索块。

覆盖**接入面**:一把闸(``section_navigation_active``)被 prompt、schema、白名单、
解析、执行体与线索块共读;两个动作的每条 skip 原因码;越界与不存在不可区分;挂载库
与对等模式按属主库路由;重复与上限;浏览不推进也不重置 stale;读回的整节在合成预算
下靠保留席位存活;三种地址来源的 relevance 是同一把尺子;向量不可用时线索块失败开放;
新增子查询不重复解码向量;线索排除已覆盖的节与导航页。

纯函数(大纲折叠、排序、渲染)的合同在 ``test_section_outline``,检索端口(权限、
归属核验、导航剔除)在 ``test_section_outline_ports``,这里不重测它们。

夹具是一篇平铺带编号的规范(与生产 UCIe 那份同形):``5`` / ``5.7`` / ``5.7.2.1`` /
``5.7.3`` / ``Notes`` / ``6`` / ``Notes``,开头一页目录(导航页,向量与问题最像)。
查询向量由表驱动的嵌入替身给出,相似度因此可以手算。
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.domain.vector_index import encode_vector
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    BROWSE_SECTIONS_ACTION,
    CORE_REFLECT_ACTION_IDS,
    READ_SECTION_ACTION,
    ReasoningRetriever,
    ReflectDecision,
)
from app.services.retrieval import _fuse, _tokens, keyword_basis
from app.services.retrieval_run import retrieval_run
from app.services.section_outline import HINT_BLOCK_TITLE, RENDER_MIN_CHARS
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client
from tests.test_reasoning_enumeration_tools import (
    _enumerate_sources_action,
    _skips,
    _steps,
    _ValidatingLLM,
)

NOW = "2026-10-22T00:00:00+08:00"
DIM = 16
QUESTION = "Standard Package 插损是多少"
ANSWER = {"next_action": "answer", "sufficient": True, "reason": "够了"}


def _unit(*pairs):
    vector = [0.0] * DIM
    for index, value in pairs:
        vector[index] = value
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


# (chunk id, section path, text, navigation, vector)
SPEC = (
    ("c-nav", "Contents", "5.7.2.1 Loss ..... 12 5.7.3 Package ..... 14", True,
     _unit((2, 1.0))),
    ("c-s0", "5 Electrical", "第五章 电气规范概述", False, _unit((0, 1.0))),
    ("c-s1", "5.7 Channel", "通道规范概述", False, _unit((1, 1.0))),
    ("c-s2", "5.7.2.1 Loss and Crosstalk Mask", "插损与串扰掩模限值", False,
     _unit((2, 1.0))),
    ("c-s3", "5.7.3 Standard Package", "标准封装插损表 5-18 数值", False,
     _unit((2, 0.7), (3, math.sqrt(0.51)))),
    ("c-s4", "Notes", "注释一", False, _unit((4, 1.0))),
    ("c-s5", "6 Protocol", "协议层说明", False, _unit((5, 1.0))),
    ("c-s6", "Notes", "注释二", False, _unit((6, 1.0))),
)
QUERY_VECTORS = {
    # cos(c-s0) = 0.6, cos(c-s2) = 0.8, cos(c-s3) = 0.56, cos(c-nav) = 0.8
    QUESTION: _unit((0, 0.6), (2, 0.8)),
    "insertion loss": _unit((2, 1.0)),
    "protocol layer": _unit((5, 1.0)),
}


class _TableEmbedder:
    """Query vectors from a table; ``None`` for anything else (counts calls)."""

    configured = True

    def __init__(self, table=QUERY_VECTORS):
        self.table = dict(table)
        self.calls: list[str] = []

    def embed_query(self, text):
        self.calls.append(text)
        return list(self.table[text]) if text in self.table else None


def make_section_repo(tmp_path, monkeypatch):
    """A scrubbed SQLite repository (``test_reasoning_enumeration_tools.repo``'s
    shape), a plain function so other suites can build one too."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=DIM))
    instance.settings.graph_ppr_enabled = False
    return instance


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return make_section_repo(tmp_path, monkeypatch)


def _source(db, nb, sid, title):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,"
        "parse_status,summary,created_at,updated_at) "
        "VALUES (?,?,?,'markdown','extracted','extracted','',?,?)",
        (sid, nb, title, NOW, NOW))


def _seed(repo, *, vectors=True, name="规范", rows=SPEC, extra_sources=()):
    notebook = repo.create_notebook(NotebookCreate(name=name))
    nb = notebook.id
    with repo._write() as db:
        _source(db, nb, "src-spec", "互连规范")
        _source(db, nb, "src-blank", "空白文档")
        for sid, title in extra_sources:
            _source(db, nb, sid, title)
        for chunk_id, path, text, navigation, vector in rows:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at,navigation) VALUES (?,?,?,?,?,?,?,?)",
                (chunk_id, nb, "src-spec", text, path, "[]", NOW,
                 1 if navigation else 0))
            if vectors:
                db.execute(
                    "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,"
                    "created_at) VALUES (?,?,?,?)",
                    (chunk_id, nb, encode_vector(vector), NOW))
    repo.collection_catalog.invalidate()
    return nb


def _hit(chunk_id, *, notebook_id="", relevance=0.5, rows=SPEC):
    row = next(item for item in rows if item[0] == chunk_id)
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="src-spec", source_title="互连规范",
        section_path=row[1], text=row[2], relevance=relevance, score=relevance,
        notebook_id=notebook_id)


def _reasoner(repo, monkeypatch, llm, *, seed=("c-s0",), embedder=None,
              seed_notebook_id="", rows=SPEC, extra_hits=()):
    """真实 SQLite 检索端口 + 表驱动的查询向量;首轮原文播种固定交回 ``seed``。"""
    monkeypatch.setattr(repo.retrieval.candidates, "embedder",
                        embedder or _TableEmbedder())
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    hits = [_hit(chunk_id, notebook_id=seed_notebook_id, rows=rows)
            for chunk_id in seed] + list(extra_hits)
    retriever.search_chunks = lambda notebook_id, query, *, k=None: [
        replace(hit) for hit in hits]
    return retriever


def _run(retriever, nb, effort="standard", question=QUESTION):
    return retriever.run(nb, question, "", limits=ask_retrieval_limits(effort))


def _browse(source, under="", query=""):
    return {"next_action": BROWSE_SECTIONS_ACTION, "reason": "看目录",
            "browse_sections": {"source": source, "under": under, "query": query}}


def _read(section, source=""):
    return {"next_action": READ_SECTION_ACTION, "reason": "读这一节",
            "read_section": {"source": source, "section": section}}


def _llm(*reflects, plan_query="插损"):
    return _ValidatingLLM(list(reflects) + [ANSWER],
                          plan={"sub_queries": [{"query": plan_query}]})


# --------------------------------------------------------------------- the gate


def test_browse_max_chars_floor_is_the_renderer_floor():
    """Settings 的下界写成字面量(core 不 import services),相等关系钉在这里。"""
    field = Settings.model_fields["reasoning_section_browse_max_chars"]
    floor = next(m.ge for m in field.metadata if getattr(m, "ge", None) is not None)
    assert floor == RENDER_MIN_CHARS


def test_settings_defaults_and_policy():
    from app.services.reports.policy import reasoning_action_policy

    s = Settings(_env_file=None)
    assert s.reasoning_section_nav_enabled is True
    assert (s.reasoning_max_section_browses, s.reasoning_max_section_reads) == (3, 3)
    assert s.reasoning_section_read_max_chunks == 12
    assert s.reasoning_section_read_reserve == 6
    assert s.reasoning_section_browse_max_chars == 3000
    assert (s.reasoning_section_hint_max_entries, s.reasoning_section_hint_sources) == (8, 4)
    assert s.reasoning_section_hint_relative_floor == 0.5
    policy = reasoning_action_policy(s)
    assert (policy.max_section_browses, policy.max_section_reads) == (3, 3)
    # settings 替身缺这两格时退回策略常量
    assert reasoning_action_policy(SimpleNamespace()).max_section_reads == 3


@pytest.mark.parametrize("close", [
    "overview", "switch", "policy", "chunk_switch", "chunk_policy",
    "caps", "outline_rows", "port",
])
def test_every_gate_condition_closes_the_whole_channel(repo, monkeypatch, close):
    """七个条件缺一不可;任何一个关掉,prompt / schema / 白名单里都没有这两个动作,
    模型硬吐一个只会被标成「本 run 不提供」。两个上限只关一个时闸仍开着。"""
    retriever = _reasoner(repo, monkeypatch, _llm())
    limits = ask_retrieval_limits("standard")
    assert retriever.section_navigation_active(limits) is True
    repo.settings.reasoning_max_section_browses = 0
    assert retriever.section_navigation_active(limits) is True
    repo.settings.reasoning_max_section_browses = 3
    if close == "overview":
        limits = ask_retrieval_limits("overview")
    elif close == "switch":
        repo.settings.reasoning_section_nav_enabled = False
    elif close == "policy":
        retriever.allow_section_nav = False
    elif close == "chunk_switch":
        repo.settings.reasoning_chunk_search_enabled = False
    elif close == "chunk_policy":
        retriever.allow_search_chunks = False
    elif close == "caps":
        repo.settings.reasoning_max_section_browses = 0
        repo.settings.reasoning_max_section_reads = 0
    elif close == "outline_rows":
        repo.settings.reasoning_section_outline_max_rows = 0
    elif close == "port":
        retriever.retrieval = SimpleNamespace(
            section_outline=lambda *a: ((), False))
    assert retriever.section_navigation_active(limits) is False
    if close == "port":
        return
    llm = _ValidatingLLM([_browse("互连规范")])
    bind_chat_client(repo, "reasoning_agent", llm)
    decision = retriever.reflect("q", "c", limits=limits)
    assert "browse_sections" not in llm.reflect_prompts[-1]
    assert "read_section" not in llm.schema_hints[-1]
    assert decision.withheld is True
    assert decision.browse_sections_source == ""


def test_open_gate_reaches_prompt_schema_whitelist_and_parser(repo, monkeypatch):
    retriever = _reasoner(repo, monkeypatch, _llm())
    llm = _ValidatingLLM([_read("s3", source="互连规范")])
    bind_chat_client(repo, "reasoning_agent", llm)
    decision = retriever.reflect("q", "c", limits=ask_retrieval_limits("standard"))
    assert decision.next_action == READ_SECTION_ACTION and not decision.withheld
    assert (decision.read_section_section, decision.read_section_source) == (
        "s3", "互连规范")
    assert "- browse_sections:" in llm.reflect_prompts[-1]
    assert "You may browse at most 3 outline(s) in this run." in llm.reflect_prompts[-1]
    assert "You may read at most 3 section(s) this way" in llm.reflect_prompts[-1]
    schema = json.loads(llm.schema_hints[-1])
    assert {"browse_sections", "read_section"} <= set(schema["next_action"].split("|"))
    assert {"browse_sections", "read_section"} <= CORE_REFLECT_ACTION_IDS


def test_fail_closed_rejects_missing_arguments_and_overlong_fields(repo, monkeypatch):
    retriever = _reasoner(repo, monkeypatch, _llm())
    retriever.fail_closed = True
    limits = ask_retrieval_limits("standard")
    for payload, message in (
        (_browse(""), "browse_sections action is missing source"),
        (_read(""), "read_section action is missing section"),
        (_browse("互连规范", query="x" * 2001), "field is too long"),
        (_read("s1", source="y" * 2001), "field is too long"),
    ):
        bind_chat_client(repo, "reasoning_agent", _ValidatingLLM([payload]))
        with pytest.raises(ValueError, match=message):
            retriever.reflect("q", "c", limits=limits)


def test_closed_gate_executors_are_zero_io_skips(repo, monkeypatch):
    """纵深防御:闸关时畸形响应/替身直达执行体,也只记 section_nav_disabled。"""
    nb = _seed(repo)
    repo.settings.reasoning_section_nav_enabled = False
    retriever = _reasoner(repo, monkeypatch, _llm())
    calls = []
    original = retriever.retrieval.section_outline
    retriever.retrieval.section_outline = lambda *a: calls.append(a) or original(*a)
    queue = [ReflectDecision(next_action=BROWSE_SECTIONS_ACTION),
             ReflectDecision(next_action=READ_SECTION_ACTION),
             ReflectDecision(next_action="answer", sufficient=True)]
    retriever.reflect = lambda *a, **k: queue.pop(0)
    result = _run(retriever, nb)
    reasons = [s.detail["reason"] for s in _steps(result, "skip")
               if s.detail.get("reason", "").startswith("section_")]
    assert reasons == ["section_nav_disabled", "section_nav_disabled"]
    assert all("result_ids" not in s.detail for s in _steps(result, "skip"))
    assert calls == []


# ----------------------------------------------------------------- hint block


def test_hint_block_lists_uncovered_sections_never_navigation_pages(repo, monkeypatch):
    """线索块:按向量把第 5 章的两节送到模型眼前;已覆盖的 ``5``、导航页(向量与问题
    同样像)与被后代吸收的祖先都不出现;位置在集合地图之前。"""
    nb = _seed(repo)
    llm = _llm()
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    prompt = llm.reflect_prompts[0]
    assert HINT_BLOCK_TITLE in prompt
    block = prompt[prompt.index(HINT_BLOCK_TITLE):].split("\n\n")[0].splitlines()
    assert block[1:] == [
        "[s1]《互连规范》 5.7.2.1 Loss and Crosstalk Mask（1 段）",
        # 0.56 ≥ 0.6 × 0.8;范围含其后的 Notes 叶
        "[s2]《互连规范》 5.7.3 Standard Package（2 段）",
    ]
    assert "Contents" not in "\n".join(block)
    # the candidate summary's map line (the rules above it name it too)
    assert prompt.index(HINT_BLOCK_TITLE) < prompt.rindex("\n[Collections in scope]")
    # 线索不是证据,不进候选池
    assert [c.chunk_id for c in result.chunks] == ["c-s0"]


def test_hint_floor_is_relative_to_the_best_run_chunk(repo, monkeypatch):
    nb = _seed(repo)
    repo.settings.reasoning_section_hint_relative_floor = 1.0
    llm = _llm()
    _run(_reasoner(repo, monkeypatch, llm, seed=("c-s3",)), nb)
    prompt = llm.reflect_prompts[0]
    # 已有 5.7.3(0.56);floor 1.0 → 只剩 5.7.2.1(0.8);5.7.3 本身已覆盖
    lines = prompt[prompt.index(HINT_BLOCK_TITLE):].split("\n\n")[0].splitlines()
    assert lines[1:] == ["[s1]《互连规范》 5.7.2.1 Loss and Crosstalk Mask（1 段）"]


def test_hint_block_fails_open_without_vectors(repo, monkeypatch):
    """没有 chunk 向量:没有相似度基准,整块不出,轨迹里也没有任何报错步。"""
    nb = _seed(repo, vectors=False)
    llm = _llm()
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    assert HINT_BLOCK_TITLE not in llm.reflect_prompts[0]
    assert not [s for s in _steps(result, "skip")
                if s.detail.get("reason", "").startswith("section_")]


def test_hint_block_fails_open_when_the_port_raises(repo, monkeypatch):
    nb = _seed(repo)
    llm = _llm()
    retriever = _reasoner(repo, monkeypatch, llm)

    def boom(*args):
        raise RuntimeError("vector store down")

    retriever.retrieval.section_chunk_similarities = boom
    result = _run(retriever, nb)
    assert HINT_BLOCK_TITLE not in llm.reflect_prompts[0]
    assert result.trace[-1].step_type != "skip"


def test_a_new_sub_query_re_ranks_without_re_decoding_vectors(repo, monkeypatch):
    """新增子查询让线索重算(查询文本变了),但同一来源的 chunk 向量在 run 内只读
    一次;大纲也只读一次。查询向量走 run 内记忆。"""
    nb = _seed(repo)
    candidates = repo.retrieval.candidates
    reads = {"vectors": 0, "outline": 0}
    original_rows = candidates.embeddings.vector_rows_for_ids
    original_outline = candidates.chunks.source_outline_rows

    def counting_rows(*args, **kwargs):
        reads["vectors"] += 1
        return original_rows(*args, **kwargs)

    def counting_outline(*args, **kwargs):
        reads["outline"] += 1
        return original_outline(*args, **kwargs)

    monkeypatch.setattr(candidates.embeddings, "vector_rows_for_ids", counting_rows)
    monkeypatch.setattr(candidates.chunks, "source_outline_rows", counting_outline)
    embedder = _TableEmbedder()
    llm = _llm({"next_action": "add_subquery", "reason": "补",
                "new_sub_query": {"query": "protocol layer", "types": [],
                                  "prefer": "balanced", "reason": "补"}})
    retriever = _reasoner(repo, monkeypatch, llm, embedder=embedder)
    with retrieval_run(run_kind="ask_reasoning"):
        _run(retriever, nb)
    assert reads == {"vectors": 1, "outline": 1}
    assert embedder.calls.count(QUESTION) == 1
    # 第二轮的查询文本多了一条,所以 6 Protocol(只对它相似)进了线索
    assert "6 Protocol" not in llm.reflect_prompts[0].split(HINT_BLOCK_TITLE)[1]
    assert "6 Protocol" in llm.reflect_prompts[1].split(HINT_BLOCK_TITLE)[1]


# ---------------------------------------------------------------- read_section


def _expected_relevance(chunk_id, question=QUESTION):
    row = next(item for item in SPEC if item[0] == chunk_id)
    cosine = float(np.dot(row[4], QUERY_VECTORS[question]))
    text = row[2]
    return _fuse(keyword_basis(question).coverage(set(_tokens(text)), text),
                 cosine, True)


def test_relevance_is_one_ruler_for_hint_browse_and_title_addresses(repo, monkeypatch):
    """三种地址来源(线索 sN、目录 sN、标题文本)读回的块,relevance 都是
    ``_fuse(关键词覆盖, 该块向量与问题向量的余弦)``,没有 floor,也不设 exact_lookup。"""
    nb = _seed(repo)
    llm = _llm(
        _read("s1"),                                  # hint id → 5.7.2.1
        _browse("互连规范", under="6"),                 # listing → [s3] 6 Protocol
        _read("s3"),
        _read("5.7.3 Standard Package", source="互连规范"),  # title text
    )
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    reads = _steps(result, "read_section")
    assert [s.detail["section"] for s in reads] == [
        "5.7.2.1 Loss and Crosstalk Mask", "6 Protocol", "5.7.3 Standard Package"]
    # a trailing Notes leaf is part of the section before it
    assert [s.detail["result_ids"] for s in reads] == [
        ["c-s2"], ["c-s5", "c-s6"], ["c-s3", "c-s4"]]
    by_id = {c.chunk_id: c for c in result.chunks}
    for chunk_id in ("c-s2", "c-s5", "c-s6", "c-s3", "c-s4"):
        chunk = by_id[chunk_id]
        assert chunk.section_read is True and chunk.exact_lookup is False
        assert chunk.relevance == pytest.approx(_expected_relevance(chunk_id))
    assert reads[0].summary == (
        "按章节读取原文:《互连规范》 5.7.2.1 Loss and Crosstalk Mask,新增 1 段")
    assert result.trace and "已读《互连规范》" in llm.reflect_prompts[-1]


def test_read_section_truncation_is_disclosed(repo, monkeypatch):
    nb = _seed(repo)
    repo.settings.reasoning_section_read_max_chunks = 1
    llm = _llm(_read("5", source="互连规范"))
    result = _run(_reasoner(repo, monkeypatch, llm, seed=("c-s5",)), nb)
    step = _steps(result, "read_section")[0]
    # 「5」的范围:5 / 5.7 / 虚节点 5.7.2 / 5.7.2.1 / 5.7.3 / Notes(导航页不在内)
    assert step.detail["truncated"] is True
    assert step.detail["result_ids"] == ["c-s0"]
    assert step.summary.endswith("(截取本节前 1 段)")


def test_read_section_zero_hit_is_tracked_and_writes_empty_result_ids(repo, monkeypatch):
    """整节都已在池里:读取照常发生 I/O,新增 0 段 → `result_ids: []`。"""
    nb = _seed(repo)
    llm = _llm(_read("5.7.2.1", source="互连规范"))
    retriever = _reasoner(repo, monkeypatch, llm, seed=("c-s2",))
    result = _run(retriever, nb)
    step = _steps(result, "read_section")[0]
    assert step.detail["found"] == 0 and step.detail["result_ids"] == []


def test_mounted_library_reads_route_to_the_owner(repo, monkeypatch):
    """挂载库的 chunk 带属主 id:大纲、相似度、整节读取都按属主库发起。"""
    nb = _seed(repo)
    seen = []
    llm = _llm(_read("s1"))
    retriever = _reasoner(repo, monkeypatch, llm, seed_notebook_id=nb)
    real = retriever.retrieval
    for name in ("section_outline", "section_chunk_similarities", "section_chunks"):
        method = getattr(real, name)

        def recorder(*args, _name=name, _method=method):
            seen.append((_name, args[0], args[1]))
            return _method(*args)

        setattr(real, name, recorder)
    other = repo.create_notebook(NotebookCreate(name="提问库")).id
    result = retriever.run(other, QUESTION, "",
                           limits=ask_retrieval_limits("standard"))
    assert {(owner, source) for _name, owner, source in seen} == {(nb, "src-spec")}
    assert {name for name, *_ in seen} == {
        "section_outline", "section_chunk_similarities", "section_chunks"}
    read = _steps(result, "read_section")[0]
    assert read.detail["result_ids"] == ["c-s2"]
    assert next(c for c in result.chunks if c.chunk_id == "c-s2").notebook_id == nb


def test_peer_run_keeps_the_gate_and_routes_by_owner(repo, monkeypatch):
    from tests.test_reasoning_retrieval import _peer_override

    _seed(repo)
    retriever = _reasoner(repo, monkeypatch, _llm())
    with _peer_override(("nb-a", "nb-b")):
        assert retriever.section_navigation_active(
            ask_retrieval_limits("standard")) is True
        # 对等 run 里每条 chunk 都带属主:候选来源行按它路由
        state = SimpleNamespace(
            chunks=[_hit("c-s0", notebook_id="nb-b")], notebook_id="nb-a",
            enumerations=[])
        rows = retriever._section_source_rows(state)
    assert [(r.source_id, r.notebook_id) for r in rows] == [("src-spec", "nb-b")]


# ------------------------------------------------------------ browse_sections


def test_browse_query_ranks_the_document_by_meaning(repo, monkeypatch):
    nb = _seed(repo)
    llm = _llm(_browse("互连规范", query="insertion loss"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    step = _steps(result, "browse_sections")[0]
    assert step.detail == {"source": "互连规范", "under": "", "query": "insertion loss",
                           "listed": step.detail["listed"]}
    assert step.summary.startswith("查看章节目录:《互连规范》 按「insertion loss」排序,列出")
    listing = llm.reflect_prompts[1]
    listing = listing[listing.index("（章节目录:"):]
    lines = listing.splitlines()
    # 名次第一的是 5.7.2.1;导航页不在目录里
    assert "5.7.2.1 Loss and Crosstalk Mask" in lines[1]
    assert "Contents" not in listing
    assert result.chunks == [c for c in result.chunks if c.chunk_id == "c-s0"]


def test_browse_holds_stale_and_the_next_listing_replaces_the_last(repo, monkeypatch):
    """目录不是证据:交付目录的那一轮 stale 不清零也不递增;同一来源的下一份目录替换
    上一份,更早的只留一行账目。"""
    nb = _seed(repo)
    llm = _llm(_browse("互连规范"), _browse("互连规范", under="5.7"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    reflects = _steps(result, "reflect")
    assert [(r.detail["no_progress"], r.detail["stale"]) for r in reflects[1:3]] == [
        (True, 0), (True, 0)]
    last = llm.reflect_prompts[2]
    assert last.count("（章节目录:") == 1
    assert "（章节目录:《互连规范》 under 5.7,列出" in last
    assert last.count("已浏览《互连规范》") == 2
    browses = _steps(result, "browse_sections")
    assert [b.summary.split(",")[0] for b in browses] == [
        "查看章节目录:《互连规范》", "查看章节目录:《互连规范》 under 5.7"]
    assert all("result_ids" not in b.detail for b in browses)


def test_listed_ids_are_a_closed_set(repo, monkeypatch):
    """只有真正列出来的条目拿到地址:目录被字符上限截断时,未列出的条目读不到。"""
    nb = _seed(repo, vectors=False)
    repo.settings.reasoning_section_browse_max_chars = RENDER_MIN_CHARS
    llm = _llm(_browse("互连规范"), _read("s9"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    listed = _steps(result, "browse_sections")[0].detail["listed"]
    assert 0 < listed < 8
    assert _skips(result)["section_not_surfaced"].detail["section"] == "s9"


# ------------------------------------------------------------ skips & budgets


def test_every_skip_reason_teaches_through_the_ledger(repo, monkeypatch):
    nb = _seed(repo, vectors=False)
    repo.settings.reasoning_max_section_browses = 2
    repo.settings.reasoning_max_section_reads = 1
    repo.settings.reasoning_stale_limit = 50
    llm = _llm(
        _enumerate_sources_action(),
        _browse("不存在的文档"),
        _browse("空白文档"),
        _browse("互连规范", under="附录甲"),
        _browse("互连规范"),
        _browse("互连规范"),                 # replayed from the cache, no skip
        _browse("互连规范", under="9"),
        _read("Notes", source="互连规范"),
        _read("s99"),
        _read("Appendix Z", source="互连规范"),
        _read("5.7", source="不存在的文档"),
        _read("6", source="互连规范"),
        _read("6", source="互连规范"),        # reads spent: no longer offered
        _browse("互连规范", under="5"),
        _browse("互连规范", under="6"),       # browses spent: no longer offered
    )
    retriever = _reasoner(repo, monkeypatch, llm)
    result = retriever.run(nb, QUESTION, "")  # limits=None: the raw step budget
    skips = [s for s in _steps(result, "skip")
             if s.detail.get("reason", "").startswith("section_")]
    assert [s.detail["reason"] for s in skips] == [
        "section_source_unresolved", "section_outline_empty",
        "section_under_unsupported", "section_under_empty",
        "section_ambiguous", "section_not_surfaced", "section_not_surfaced",
        "section_source_unresolved"]
    withheld = [s.detail["action"] for s in _steps(result, "skip")
                if s.detail.get("reason") == "action_not_offered"]
    assert withheld == ["read_section", "browse_sections"]
    ambiguous = next(s for s in skips if s.detail["reason"] == "section_ambiguous")
    assert ambiguous.detail["matches"] == 2
    assert "出现 2 处" in ambiguous.summary and "[s" in ambiguous.summary
    assert all("result_ids" not in s.detail for s in skips)
    # 每条(除闸关)都进了下一轮的账目,未执行的与执行记录分前缀
    ledger = llm.reflect_prompts[-1]
    assert "（章节导航记录,勿重复请求:" in ledger
    assert "未执行:查看章节目录:under「附录甲」不是章节编号" in ledger
    assert "未执行:按章节读取原文:「Notes」在《互连规范》里出现 2 处" in ledger
    assert "已浏览《互连规范》(列出" in ledger and "已读《互连规范》 6 Protocol" in ledger
    assert "已浏览《互连规范》(本轮已列过,目录已放回)" in ledger
    # 用完之后两个动作都不再出现在 prompt 里
    assert "- read_section:" not in ledger and "- browse_sections:" not in ledger
    assert "- read_section:" in llm.reflect_prompts[1]


def test_an_ambiguous_title_is_readable_by_the_returned_id(repo, monkeypatch):
    nb = _seed(repo, vectors=False)
    llm = _llm(_read("Notes", source="互连规范"), _read("s2"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    ambiguous = _skips(result)["section_ambiguous"]
    assert "[s1] Notes、[s2] Notes" in ambiguous.summary
    assert _steps(result, "read_section")[0].detail["result_ids"] == ["c-s6"]


def test_read_repeat_and_a_spent_allowance(repo, monkeypatch):
    """同一节读两次 → section_read_repeat(不扣次数);次数用完后 read_section 不再
    提供,线索块也不再出现(没有可用的下一步);执行体的上限是纵深防御。"""
    nb = _seed(repo)
    repo.settings.reasoning_max_section_reads = 2
    llm = _llm(_read("6", source="互连规范"), _read("6 Protocol", source="互连规范"),
               _read("5.7", source="互连规范"), _read("5", source="互连规范"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    reasons = [s.detail["reason"] for s in _steps(result, "skip")
               if s.detail.get("reason", "").startswith("section_")]
    assert reasons == ["section_read_repeat"]
    assert len(_steps(result, "read_section")) == 2
    assert HINT_BLOCK_TITLE in llm.reflect_prompts[0]
    assert HINT_BLOCK_TITLE not in llm.reflect_prompts[3]
    assert "- read_section:" not in llm.reflect_prompts[3]
    assert _skips(result)["action_not_offered"].detail["action"] == "read_section"


def test_the_read_cap_is_the_executors_own_defence(repo, monkeypatch):
    nb = _seed(repo, vectors=False)
    repo.settings.reasoning_max_section_reads = 1
    retriever = _reasoner(repo, monkeypatch, _llm())
    queue = [ReflectDecision(next_action=READ_SECTION_ACTION,
                             read_section_section="6", read_section_source="互连规范"),
             ReflectDecision(next_action=READ_SECTION_ACTION,
                             read_section_section="5", read_section_source="互连规范"),
             ReflectDecision(next_action="answer", sufficient=True)]
    retriever.reflect = lambda *a, **k: queue.pop(0)
    result = _run(retriever, nb)
    assert _skips(result)["section_read_cap"].summary == "跳过按章节读取原文(已达次数上限 1)"


def test_out_of_ceiling_reads_exactly_like_a_missing_source(repo, monkeypatch):
    """越界与不存在不可区分:一篇天花板外的来源即便混进了候选(这里由替身塞进来),
    检索层也只交回空大纲——与一篇根本没有 chunk 的来源同一条 skip、同一个 detail。"""
    from app.services.source_scope import source_scope_context

    nb = _seed(repo, vectors=False)
    llm = _llm(_enumerate_sources_action(), _browse("互连规范"), _browse("空白文档"))
    retriever = _reasoner(repo, monkeypatch, llm)
    with source_scope_context(nb, {"mode": "include", "source_ids": ["src-blank"]}):
        result = _run(retriever, nb)
    empties = [s for s in _steps(result, "skip")
               if s.detail.get("reason") == "section_outline_empty"]
    assert [s.detail for s in empties] == [
        {"reason": "section_outline_empty"}, {"reason": "section_outline_empty"}]
    assert [s.summary.replace("互连规范", "X").replace("空白文档", "X")
            for s in empties] == ["跳过查看章节目录:《X》没有可列出的章节"] * 2
    assert not _steps(result, "browse_sections")


def test_not_a_name_note_points_at_section_navigation(repo, monkeypatch):
    nb = _seed(repo, vectors=False)
    llm = _llm({"next_action": "exact_lookup", "query": "5.7.3 节", "reason": "查"})
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    skip = _skips(result)["exact_term_not_identifier"]
    assert skip.summary.endswith("如果这是章节标题,请改用 browse_sections 或 read_section")
    repo.settings.reasoning_section_nav_enabled = False
    llm = _llm({"next_action": "exact_lookup", "query": "5.7.3 节", "reason": "查"})
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    assert "browse_sections" not in _skips(result)["exact_term_not_identifier"].summary


# -------------------------------------------------------- synthesis seats


def _passage(chunk_id, relevance, *, notebook_id="", section_read=False,
             exact=False):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="src", source_title="T",
        section_path="1", text=f"{chunk_id} 正文", relevance=relevance,
        score=relevance, notebook_id=notebook_id, section_read=section_read,
        exact_lookup=exact)


def test_section_read_passages_hold_seats_behind_the_exact_prefix():
    from app.services.retrieval import passage_floor, reasoning_passage_order

    chunks = ([_passage(f"n{i}", 0.9 - i * 0.01) for i in range(10)]
              + [_passage("x", 0.2, exact=True)]
              + [_passage(f"r{i}", 0.1 - i * 0.01, section_read=True)
                 for i in range(3)])
    order = reasoning_passage_order(
        chunks, exact_reserve=1, active_reserve=0, library_reserve=0,
        active_notebook_id="nb", section_read_reserve=2)
    assert [c.chunk_id for c in order.passages[:4]] == ["x", "r0", "r1", "n0"]
    assert order.prefix == 3
    # 席位前缀受 passage_floor 保护:它们一定在合成分区里渲染得下
    assert passage_floor(order.passages[:order.prefix], 4000, heading_chars=10) > 0
    off = reasoning_passage_order(
        chunks, exact_reserve=1, active_reserve=0, library_reserve=0,
        active_notebook_id="nb")
    assert off.prefix == 1 and off.passages[1].chunk_id == "n0"


def test_own_id_stamped_section_reads_are_the_active_library():
    """端口给本库的整节块戳本库 id(不是空串):按库分席位的地方必须把它当作本库,
    与不戳 id 的普通本库块是同一个库——结果与把它改成空串逐项相同。"""
    from app.services.retrieval import reasoning_passage_order

    def build(stamp):
        return ([_passage("f0", 0.95, notebook_id="nb-peer")]
                + [_passage(f"a{i}", 0.5 - i * 0.01) for i in range(3)]
                + [_passage("r0", 0.05, notebook_id=stamp, section_read=True)])

    def order(stamp):
        result = reasoning_passage_order(
            build(stamp), exact_reserve=0, active_reserve=2, library_reserve=0,
            active_notebook_id="nb", section_read_reserve=1)
        return [c.chunk_id for c in result.passages], result.prefix

    assert order("nb") == order("")
    # the section seat already holds one of the two active seats, so the floor
    # pulls in one more active passage (a0), not two
    assert order("nb") == (["r0", "a0", "f0", "a1", "a2"], 2)


def test_reasoning_order_for_reads_the_section_read_reserve():
    from app.services.chunk_federation import reasoning_order_for

    chunks = [_passage("n0", 0.9), _passage("r0", 0.1, section_read=True)]
    settings = SimpleNamespace(reasoning_exact_reserve=0,
                               reasoning_section_read_reserve=1)
    result = reasoning_order_for(settings, chunks, "nb")
    assert [c.chunk_id for c in result.passages][:1] == ["r0"]
    settings.reasoning_section_read_reserve = 0
    assert reasoning_order_for(settings, chunks, "nb").passages[0].chunk_id == "n0"


def test_outline_synthesis_never_injects_section_reads_per_section():
    from app.services.outline_synthesis import plan_outline_sections
    from app.services.reasoning_retrieval import OutlineSection

    read = _passage("r0", 0.5, section_read=True)
    exact = _passage("x0", 0.5, exact=True)
    slices, _ = plan_outline_sections(
        [OutlineSection(id="a", title="A", parent="", evidence_keys=["n0"])],
        kg_by_id={}, element_by_id={},
        chunk_by_id={"n0": _passage("n0", 0.5), "r0": read, "x0": exact},
        exact_reserve=4)
    assert [c.chunk_id for c in slices[0].exact_chunks] == ["x0"]


# ------------------------------------------------------ review round (T6 R1)


@pytest.mark.parametrize("broken", ["section_outline", "section_chunks"])
def test_a_failed_section_read_never_voids_the_run(repo, monkeypatch, broken):
    """P2-1:读大纲 / 读回整节抛错收成一次 skip(扣一次、不写 result_ids、账目教学),
    本 run 已收集的证据与轨迹都在,run 照常作答。"""
    nb = _seed(repo, vectors=False)
    llm = _llm(_browse("互连规范"), _read("6", source="互连规范"))
    retriever = _reasoner(repo, monkeypatch, llm)

    def boom(*args, **kwargs):
        raise RuntimeError("store down")

    setattr(retriever.retrieval, broken, boom)
    result = _run(retriever, nb)
    assert result.trace[-1].step_type == "answer"
    assert [c.chunk_id for c in result.chunks] == ["c-s0"]
    failed = [s for s in _steps(result, "skip")
              if s.detail.get("reason", "").endswith("_failed")]
    expected = (["section_browse_failed", "section_read_failed"]
                if broken == "section_outline" else ["section_read_failed"])
    assert [s.detail["reason"] for s in failed] == expected
    assert all(s.detail["error"] == "RuntimeError" and "result_ids" not in s.detail
               for s in failed)
    assert "未执行:按章节读取原文:读取时出错(RuntimeError)" in llm.reflect_prompts[-1]


def test_a_failed_read_still_spends_the_allowance(repo, monkeypatch):
    nb = _seed(repo, vectors=False)
    repo.settings.reasoning_max_section_reads = 1
    llm = _llm(_read("6", source="互连规范"), _read("5", source="互连规范"))
    retriever = _reasoner(repo, monkeypatch, llm)
    retriever.retrieval.section_chunks = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("down"))
    result = _run(retriever, nb)
    assert _skips(result)["section_read_failed"]
    assert _skips(result)["action_not_offered"].detail["action"] == "read_section"


def test_cancellation_and_control_errors_still_propagate(repo, monkeypatch):
    from app.domain.retrieval_control import RetrievalControlError

    nb = _seed(repo, vectors=False)
    retriever = _reasoner(repo, monkeypatch, _llm(_browse("互连规范")))

    def control(*args, **kwargs):
        raise RetrievalControlError("participant override mismatch")

    retriever.retrieval.section_outline = control
    with pytest.raises(RetrievalControlError):
        _run(retriever, nb)


def test_after_a_new_listing_the_next_turn_is_told_to_read_not_to_answer(
    repo, monkeypatch,
):
    """P2-2:交付目录的下一轮不发「无新证据,请直接 answer」,改发一行「已列出目录」;
    no_progress 本身不变。"""
    from app.services.reasoning_retrieval import NO_NEW_EVIDENCE_NOTE

    nb = _seed(repo, vectors=False)
    llm = _llm(_browse("互连规范"), {"next_action": "search_chunks", "query": "x",
                                     "reason": "再查"})
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    assert _steps(result, "reflect")[1].detail["no_progress"] is True
    after_listing, after_search = llm.reflect_prompts[1], llm.reflect_prompts[2]
    assert "上一步列出了章节目录" in after_listing
    assert "可用 read_section 按目录里的 [sN] 读取" in after_listing
    assert NO_NEW_EVIDENCE_NOTE not in after_listing
    assert NO_NEW_EVIDENCE_NOTE in after_search
    assert "上一步列出了章节目录" not in after_search


def test_the_plugin_nudge_is_withheld_right_after_a_listing(repo, monkeypatch):
    retriever = _reasoner(repo, monkeypatch, _llm())
    monkeypatch.setattr(retriever, "_plugin_channel_open", lambda *a: True)
    state = SimpleNamespace(
        plugin_specs=("spec",), plugin_actions_offered=(), action_policy=None,
        limits=None, zero_hit_by_action={}, steps=3, section_listing_turn=2,
        enum_limits=ask_retrieval_limits("standard"), section_reads=0,
        section_browses=0)
    state.action_policy = SimpleNamespace(max_section_reads=3, max_section_browses=3)
    kwargs: dict = {}
    after_listing = retriever._add_plugin_reflect_context(
        state, "S", kwargs, True, 0, True)
    assert after_listing == "S" and kwargs["plugin_actions"] == ("spec",)
    state.section_listing_turn = 1
    assert retriever._add_plugin_reflect_context(
        state, "S", {}, True, 0, True).startswith("S（系统提示:库内检索已无新证据")


def test_a_listing_neither_resets_nor_advances_a_stale_count(repo, monkeypatch):
    """先用一轮零进展把 stale 推到 1,再浏览:仍为 1。"""
    nb = _seed(repo, vectors=False)
    llm = _llm({"next_action": "search_chunks", "query": "x", "reason": "查"},
               _browse("互连规范"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    stale = [r.detail["stale"] for r in _steps(result, "reflect")]
    assert stale == [0, 1, 1]
    assert _steps(result, "browse_sections")


def test_an_empty_under_never_overwrites_the_last_listing(repo, monkeypatch):
    """P3-1:under 下没有条目 → section_under_empty(不扣次数),同源上一份目录留着;
    同参数再浏览零 I/O 地放回缓存目录。"""
    nb = _seed(repo, vectors=False)
    # 2 次:若空目录或重复浏览扣了次数,第三次浏览前 browse 就会被摘掉
    repo.settings.reasoning_max_section_browses = 2
    llm = _llm(_browse("互连规范", under="5.7"), _browse("互连规范", under="9"),
               _browse("互连规范", under="5.7"), _browse("互连规范", under="6"))
    retriever = _reasoner(repo, monkeypatch, llm)
    reads = []
    original = retriever.retrieval.section_outline
    retriever.retrieval.section_outline = (
        lambda *a: reads.append(a) or original(*a))
    result = _run(retriever, nb)
    assert _skips(result)["section_under_empty"].detail["under"] == "9"
    for prompt in llm.reflect_prompts[1:4]:
        assert prompt.count("（章节目录:《互连规范》 under 5.7,列出") == 1
    browses = _steps(result, "browse_sections")
    assert [(b.detail["under"], b.detail.get("repeat", False)) for b in browses] == [
        ("5.7", False), ("5.7", True), ("6", False)]
    assert len(reads) == 1
    assert "action_not_offered" not in _skips(result)


def test_a_same_titled_reference_library_document_yields_to_the_local_one(
    repo, monkeypatch,
):
    """P3-3:本库与参考库同名 → 优先本库那篇。"""
    nb = _seed(repo, vectors=False)
    other = RetrievedChunk(
        chunk_id="x-1", source_id="src-other", source_title="互连规范",
        section_path="1", text="参考库的同名文档", relevance=0.4, score=0.4,
        notebook_id="nb-reference")
    llm = _llm(_browse("互连规范"))
    result = _run(_reasoner(repo, monkeypatch, llm, extra_hits=(other,)), nb)
    assert _steps(result, "browse_sections")[0].detail["listed"] > 0


def test_a_roster_ordinal_placeholder_picks_one_of_two_local_namesakes(
    repo, monkeypatch,
):
    """P3-3:两篇本库同名文档,裸标题无法区分;抄花名册的「（第 N 篇）」就能指定。"""
    nb = _seed(repo, vectors=False, extra_sources=(("src-dup", "互连规范"),))
    llm = _llm(_enumerate_sources_action(), _browse("互连规范"),
               _browse("互连规范（第 2 篇）"), _browse("互连规范（第 1 篇）"))
    result = _run(_reasoner(repo, monkeypatch, llm), nb)
    skips = [s.detail["reason"] for s in _steps(result, "skip")
             if s.detail.get("reason", "").startswith("section_")]
    # 第 1 篇 = 先列出的 src-spec(有章节),第 2 篇 = src-dup(没有 chunk)
    assert skips == ["section_source_unresolved", "section_outline_empty"]
    assert len(_steps(result, "browse_sections")) == 1


_FLOOR_ROWS = (
    ("f-1", "1 Overview", "概述", False, _unit((0, 0.6), (1, 0.8))),
    ("f-2", "2 Target", "目标节", False, _unit((0, 0.35), (2, math.sqrt(1 - 0.35 ** 2)))),
    ("f-3", "3 Faint", "弱相关节", False, _unit((0, 0.2), (3, math.sqrt(1 - 0.04)))),
    ("f-4", "4 Other", "无关节", False, _unit((4, 1.0))),
)


def test_the_default_floor_admits_an_uncovered_target_below_the_covered_best(
    repo, monkeypatch,
):
    """默认 0.5:已覆盖块最高 0.6,未覆盖的目标节 0.35 进线索,0.2 的不进(基准来自
    已覆盖的高分块,而线索只列未覆盖节)。"""
    nb = _seed(repo, rows=_FLOOR_ROWS)
    question = "floor question"
    embedder = _TableEmbedder({question: _unit((0, 1.0))})
    llm = _llm()
    retriever = _reasoner(repo, monkeypatch, llm, seed=("f-1",), rows=_FLOOR_ROWS,
                          embedder=embedder)
    _run(retriever, nb, question=question)
    prompt = llm.reflect_prompts[0]
    block = prompt[prompt.index(HINT_BLOCK_TITLE):].split("\n\n")[0]
    assert "2 Target" in block
    assert "3 Faint" not in block and "1 Overview" not in block


# ------------------------------------------------- section_hints trace key


def test_a_round_that_computes_hints_reports_them_in_the_reflect_step(
    repo, monkeypatch,
):
    """算了线索的那一轮,reflect 步 detail 带稀疏键 `section_hints`:参与来源数、
    列出条数与这一轮检索层向量读取的增量(`section_vector_stats` 的差值)。"""
    nb = _seed(repo)
    llm = _llm(_browse("互连规范"))
    retriever = _reasoner(repo, monkeypatch, llm)
    with retrieval_run(run_kind="ask_reasoning"):
        result = _run(retriever, nb)
    first, second = (step.detail for step in _steps(result, "reflect")[:2])
    hints = first["section_hints"]
    assert {key: hints[key] for key in (
        "sources", "entries", "vector_loads", "process_cache_hits")} == {
        "sources": 1, "entries": 2, "vector_loads": 1, "process_cache_hits": 0}
    # the outline's vector rows, the navigation page's included
    assert hints["vectors_read"] == len(SPEC)
    assert isinstance(hints["vector_ms"], int) and hints["vector_ms"] >= 0
    # the next round reuses the run's similarities: nothing read, the block is
    # still shown, so the key is there with zero I/O
    assert second["section_hints"] == {
        "sources": 1, "entries": 2, "vectors_read": 0, "vector_loads": 0,
        "process_cache_hits": 0, "vector_ms": 0}


@pytest.mark.parametrize("why", ["gate_closed", "reads_spent", "no_vectors_run"])
def test_a_round_that_computes_no_hints_writes_no_key(repo, monkeypatch, why):
    """闸关、read 不可用(线索没有下一步)、或没有向量也不在检索 run 里:都不写键。"""
    nb = _seed(repo, vectors=why != "no_vectors_run")
    if why == "gate_closed":
        repo.settings.reasoning_section_nav_enabled = False
    elif why == "reads_spent":
        repo.settings.reasoning_max_section_reads = 0
    llm = _llm()
    result = _run(_reasoner(repo, monkeypatch, llm), nb)  # no retrieval run: stats are 0
    assert all("section_hints" not in step.detail
               for step in _steps(result, "reflect"))
