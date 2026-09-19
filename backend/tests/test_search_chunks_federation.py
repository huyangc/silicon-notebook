"""联邦 chunk 通道的**真实接线**(PR-B 任务 B4)。

`test_chunk_federation.py` 测的是合并/扇出策略(脱库替身),
`test_chunk_federation_peek.py` 测的是 warm-peek 读方(单库、直接打开
ContextVar)。本文件跑一个真的 `SQLiteRepository` + 一个真挂载的参考库,钉住
接线本身:

* 参考库自己的原文段落经 `retrieve_chunk_candidates` / `search_chunks` 到达,
  且每条带自己的 `notebook_id`;
* 无图早退的放行判据改成参与集口径;
* `CHUNK_FEDERATION_ENABLED=0` 时上面两条逐字回到联邦化之前;
* 单参与者(不挂参考库)的笔记本,改道前后返回**逐值相等**;
* 引用链路:参考库的段落进入 chunk 模式答案后 `Citation.notebook_id` / tier 正确;
* 任务 B3 留下的、需要真实接线才能断言的四项(peer 腿 `producer_explicit=False`、
  逐库天花板 = 该库可见来源且不含隐藏投影、隐藏 Memory 投影恒不出现、被取消
  勾选的库零候选零 SQL),以及 peek 腿返回形状对 `merge_chunk_matrices` 的维度
  假设成立。
"""
import json
import re

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.models.source_scope import BaseNotebookScope
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.services.source_scope import source_scope_context
from tests.model_testkit import (
    bind_all_embedding_clients, bind_chat_client, bind_rerank_client,
)


_QUERY = "共质心版图 matching"
_NOW = "2026-09-19T00:00:00"


def _seed_source(repo, notebook_id: str, prefix: str, *, source_type="markdown",
                 text=None) -> str:
    """一篇真解析过的来源:两个段落元素 → 真 chunk(向量 + FTS)。

    走 `_chunk_and_embed_source` 而不是直插 chunks,是为了让向量与 FTS 都由
    真实写路径产生——联邦腿要的正是「这个库真的能被检索到」。
    """
    source_id = f"src-{prefix}"
    body = text or f"{_QUERY} 的要点在文档 {prefix} 里逐条展开"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, f"Doc {prefix}", source_type, "ready",
             "parsed", _NOW, _NOW),
        )
        for index in (1, 2):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph',?,?,'{}',?)",
                (f"el-{prefix}-{index}", source_id, f"p{index}",
                 f"{body} 第 {index} 节。", _NOW),
            )
    repo._chunk_and_embed_source(source_id)
    return source_id


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    # 本机 .env 里的真实推理端点会让 ask() 打真实网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.query_rewrite_enabled = False
    return instance


@pytest.fixture
def base_only(repo):
    """(active, base, base_source):active **零源零 chunk**,base 有真实段落。

    这正是「无图参考库的原文进不来」这条缺口的最小形态:当前笔记本自己什么都
    没有,能不能答出来全看参考库的段落到不到得了这条通道。
    """
    base = repo.create_notebook(NotebookCreate(name="reference library"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="my notebook"))
    base_source = _seed_source(repo, base.id, "base")
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    repo.collection_catalog.invalidate()
    return active.id, base.id, base_source


def _reasoning_retriever(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    return ReasoningRetriever(
        retrieval=repo.retrieval,
        model_clients=repo._runtime.ask_service().model_clients,
        communities=repo._runtime.ask_service().communities(),
        settings=repo.settings,
    )


# ---------------------------------------------------------------------------
# 1. 参考库的段落真的到达两个入口
# ---------------------------------------------------------------------------

def test_base_library_passages_reach_search_chunks(repo, base_only):
    active, base, base_source = base_only

    scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(active, _QUERY)
    assert scored, "参考库的段落必须经 retrieve_chunk_candidates 到达"
    assert {chunk.notebook_id for chunk in scored} == {base}
    assert {chunk.source_id for chunk in scored} == {base_source}

    selected = _reasoning_retriever(repo).search_chunks(active, _QUERY)
    assert selected, "search_chunks 复用同一条通道,必须一起联邦化"
    assert {chunk.notebook_id for chunk in selected} == {base}


def test_multi_query_lane_also_reaches_the_base_library(repo, base_only):
    """`retrieve_chunk_candidates_multi` 同样改道;`per_query` 的组数变成
    「(库, 子查询)」而不是「子查询」,四元组形状不变。"""
    active, base, _base_source = base_only

    collected, per_query, _ids, _matrix = (
        repo.retrieval.retrieve_chunk_candidates_multi(active, [_QUERY, "matching"])
    )

    assert collected and {c.notebook_id for c in collected.values()} == {base}
    # 库主序、子查询次序:active(零 chunk,两组皆空)在前,base 的两组在后。
    assert len(per_query) == 4, "两个参与库 × 两个子查询 = 四组"
    assert per_query[:2] == [{}, {}]
    assert all(group for group in per_query[2:])
    assert all(cid in collected for group in per_query for cid in group)


# ---------------------------------------------------------------------------
# 2. 无图早退的放行判据
# ---------------------------------------------------------------------------

def test_no_kg_early_exit_admits_base_only_notebook(repo, base_only):
    """active 零源、base 有源、两边都无图 → 放行(联邦化之前这里返回 False)。"""
    active, _base, _base_source = base_only
    ask = repo._runtime.ask_service()
    mapped = repo.collection_catalog.collection_map(active)
    # 前提确认:这条测的确实是「口径之差」,不是被别的非零集合放行的。
    assert mapped.sources == 1 and mapped.active_sources == 0
    ask.settings.reasoning_enum_tools_enabled = False

    assert ask._no_kg_scope_admits_run(active) is True


def test_feature_flag_off_restores_active_only(repo, base_only):
    """`CHUNK_FEDERATION_ENABLED=0`:段落召回与放行判据一起回到今天。"""
    active, _base, _base_source = base_only
    repo.settings.chunk_federation_enabled = False
    ask = repo._runtime.ask_service()
    ask.settings.reasoning_enum_tools_enabled = False

    scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(active, _QUERY)
    assert scored == [], "回退开关下参考库的段落不得进入这条通道"
    assert _reasoning_retriever(repo).search_chunks(active, _QUERY) == []
    assert ask._no_kg_scope_admits_run(active) is False


def test_unchecked_base_library_is_not_admitted_either(repo, base_only):
    """联邦开着,但用户取消勾选了那个库 → 参与集收窄,判据必须跟着收窄。

    与上一条不同:这里开关是开的,收窄的是本次请求的范围。两条一起证明放行
    判据读的是「这次 run 真能搜的库」,不是「挂了哪些库」。
    """
    active, _base, _base_source = base_only
    ask = repo._runtime.ask_service()
    ask.settings.reasoning_enum_tools_enabled = False

    with source_scope_context(
        active, None, BaseNotebookScope(mode="include", notebook_ids=[])
    ):
        scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(
            active, _QUERY
        )
        assert scored == []
        assert ask._no_kg_scope_admits_run(active) is False


# ---------------------------------------------------------------------------
# 3. 单参与者:改道前后逐值相等
# ---------------------------------------------------------------------------

@pytest.fixture
def single_library(repo):
    active = repo.create_notebook(NotebookCreate(name="solo"))
    _seed_source(repo, active.id, "solo")
    return active.id


def _pre_federation(repo, notebook_id, queries):
    """联邦化之前那两条腿的原样调用(端口层当时就是这么写的)。"""
    from app.services.source_scope import filter_retrieval_items

    candidates = repo.retrieval.candidates
    if len(queries) == 1:
        scored, ids, matrix = candidates._retrieve_chunks(notebook_id, queries[0])
        return filter_retrieval_items(notebook_id, "chunk", scored), ids, matrix
    collected, per_query, ids, matrix = candidates._retrieve_chunks_multi(
        notebook_id, queries
    )
    allowed = {
        item.chunk_id: item for item in filter_retrieval_items(
            notebook_id, "chunk", collected.values()
        )
    }
    filtered = [
        {cid: item for cid, item in rows.items() if cid in allowed}
        for rows in per_query
    ]
    return allowed, filtered, ids, matrix


def _same_chunks(left, right):
    return [
        (c.chunk_id, c.source_id, c.text, c.score, c.relevance, c.notebook_id)
        for c in left
    ] == [
        (c.chunk_id, c.source_id, c.text, c.score, c.relevance, c.notebook_id)
        for c in right
    ]


def test_single_participant_single_query_is_value_identical(repo, single_library):
    expected_scored, expected_ids, expected_matrix = _pre_federation(
        repo, single_library, [_QUERY]
    )
    assert expected_scored, "基线必须非空,否则这条断言证不了任何事"

    scored, ids, matrix = repo.retrieval.retrieve_chunk_candidates(
        single_library, _QUERY
    )

    assert _same_chunks(scored, expected_scored)
    assert ids == expected_ids
    assert (matrix is None) == (expected_matrix is None)
    if matrix is not None:
        assert matrix.shape == expected_matrix.shape


def test_single_participant_multi_query_is_value_identical(repo, single_library):
    queries = [_QUERY, "matching 版图"]
    exp_collected, exp_per_query, exp_ids, exp_matrix = _pre_federation(
        repo, single_library, queries
    )
    assert exp_collected

    collected, per_query, ids, matrix = (
        repo.retrieval.retrieve_chunk_candidates_multi(single_library, queries)
    )

    assert list(collected) == list(exp_collected)
    assert _same_chunks(collected.values(), exp_collected.values())
    assert [list(group) for group in per_query] == [
        list(group) for group in exp_per_query
    ]
    assert ids == exp_ids
    assert (matrix is None) == (exp_matrix is None)


def test_single_participant_gather_vector_chunks_is_value_identical(
    repo, single_library
):
    """`_gather_vector_chunks` 的两条腿(单/多子查询)各自与今天逐值相等。"""
    candidates = repo.retrieval.candidates
    queries = [_QUERY, "matching 版图"]

    for sub_queries in ([_QUERY], queries):
        repo.settings.chunk_federation_enabled = False
        expected = candidates._gather_vector_chunks(single_library, list(sub_queries))
        repo.settings.chunk_federation_enabled = True
        actual = candidates._gather_vector_chunks(single_library, list(sub_queries))
        assert expected, f"基线为空:{sub_queries}"
        assert _same_chunks(actual, expected), f"改道后漂移:{sub_queries}"


# ---------------------------------------------------------------------------
# 4. 引用链路:参考库段落 → Citation.notebook_id / tier
# ---------------------------------------------------------------------------

class _MirrorLLM:
    """把 prompt 里真实提供的每个 `kN` 都引回来,所以答案引用面等于检索面。"""

    configured = True
    model = "test"

    def chat_json(self, messages, schema_hint, **kwargs):
        prompt = messages[0]["content"] if messages else ""
        keys = sorted({int(m) for m in re.findall(r"\bk(\d+):", prompt)})
        markers = " ".join(f"[k{k}]" for k in keys)
        return json.dumps({"answer": f"结论。{markers}", "grounded": True})


def test_base_passage_citation_carries_notebook_and_tier(repo, base_only):
    active, base, base_source = base_only
    bind_chat_client(repo, "ask_answer", _MirrorLLM())

    response = repo.ask(active, AskRequest(question=_QUERY, mode="chunk"))

    assert response.citations, "参考库的段落必须能变成引用卡"
    assert {c.source_id for c in response.citations} == {base_source}
    assert all(c.notebook_id == base for c in response.citations), (
        "跨库证据的 notebook_id 必须是它真正的来源库(前端据它显示库名)"
    )
    assert all(c.tier == "base" for c in response.citations)


def test_base_passage_citation_disappears_when_federation_is_off(repo, base_only):
    """对照臂:回退开关下同一个问题拿不到参考库的段落,也就没有那条引用。"""
    active, _base, _base_source = base_only
    repo.settings.chunk_federation_enabled = False
    bind_chat_client(repo, "ask_answer", _MirrorLLM())

    response = repo.ask(active, AskRequest(question=_QUERY, mode="chunk"))

    assert response.citations == []


# ---------------------------------------------------------------------------
# 5. 任务 B3 留下的、需要真实接线的验收项
# ---------------------------------------------------------------------------

def _peer_calls(repo, monkeypatch, base_id):
    """记录每条 `_retrieve_chunks` 调用收到的天花板与 `producer_explicit`。"""
    candidates = repo.retrieval.candidates
    calls = []
    original = candidates._retrieve_chunks

    def _spy(notebook_id, query, recall=0, *, allowed_source_ids=None,
             producer_explicit=False, drifted=None):
        calls.append({
            "notebook_id": notebook_id,
            "allowed_source_ids": allowed_source_ids,
            "producer_explicit": producer_explicit,
        })
        return original(
            notebook_id, query, recall, allowed_source_ids=allowed_source_ids,
            producer_explicit=producer_explicit, drifted=drifted,
        )

    monkeypatch.setattr(candidates, "_retrieve_chunks", _spy)
    return calls


def test_peer_library_ceiling_is_visible_only_and_not_attested(
    repo, base_only, monkeypatch
):
    """外库腿:天花板 = 该库 `all_visible_source_ids`,且 `producer_explicit=False`。

    第二条直接对着 `_lexical_gate_source_scoped` 的返回值断言:外库的词法臂
    不得被误判为「已收窄」——那会关掉语料语言探针、换掉词法臂。
    """
    active, base, base_source = base_only
    candidates = repo.retrieval.candidates
    calls = _peer_calls(repo, monkeypatch, base)

    repo.retrieval.retrieve_chunk_candidates(active, _QUERY)

    peer = [call for call in calls if call["notebook_id"] == base]
    assert peer, "外库腿没跑,后面的断言是空的"
    visible = tuple(candidates.sources.all_visible_source_ids(base))
    assert visible == (base_source,)
    for call in peer:
        assert call["allowed_source_ids"] == visible
        assert call["producer_explicit"] is False
        assert candidates._lexical_gate_source_scoped(
            call["allowed_source_ids"], base,
            explicit=call["producer_explicit"], drifted=False,
        ) is False, "外库的词法臂被误判为已收窄"

    local = [call for call in calls if call["notebook_id"] == active]
    assert local and all(
        call["allowed_source_ids"] is None for call in local
    ), "active 腿必须保持裸位置参数调用形状"


def test_peer_memory_projection_never_retrieved(repo, base_only, monkeypatch):
    """参考库里的隐藏 Memory 投影,结构性不进跨库原文通道。

    请求用户通常不是参考库的成员,那条投影今天连 chunk 通道都到不了;联邦化后
    如果天花板漏了它,就是凭空新增一个越权面。
    """
    active, base, base_source = base_only
    memory_source = _seed_source(
        repo, base, "mem", source_type="memory",
        text=f"{_QUERY} 私有备忘录正文",
    )
    candidates = repo.retrieval.candidates
    assert memory_source not in candidates.sources.all_visible_source_ids(base)
    calls = _peer_calls(repo, monkeypatch, base)

    scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(active, _QUERY)

    assert scored, "基线:可见来源的段落仍要回来"
    assert {chunk.source_id for chunk in scored} == {base_source}
    for call in calls:
        assert memory_source not in (call["allowed_source_ids"] or ())


def test_excluded_library_denies_everything_first(repo, base_only, monkeypatch):
    """库维度排除优先:被取消勾选的参考库零候选,而且一条 SQL 都不发。"""
    active, base, _base_source = base_only
    candidates = repo.retrieval.candidates
    calls = _peer_calls(repo, monkeypatch, base)
    visible_reads = []
    original_visible = candidates.sources.all_visible_source_ids
    monkeypatch.setattr(
        candidates.sources, "all_visible_source_ids",
        lambda notebook_id: (
            visible_reads.append(notebook_id) or original_visible(notebook_id)
        ),
    )

    with source_scope_context(
        active, None, BaseNotebookScope(mode="include", notebook_ids=[])
    ):
        scored, _ids, _matrix = repo.retrieval.retrieve_chunk_candidates(
            active, _QUERY
        )

    assert scored == []
    queried = [call["notebook_id"] for call in calls]
    assert active in queried, "spy 没被触达,下面那条断言是空的"
    assert base not in queried, f"被排除的库仍被检索:{queried}"
    assert base not in visible_reads, "被排除的库不得付出一次来源清单读"


def test_peek_branch_shapes_feed_merge_chunk_matrices(repo, base_only, monkeypatch):
    """peek 腿返回的 `(scored, ids, mat)` 必须满足 `merge_chunk_matrices` 的
    维度假设:`mat` 为二维、行数 == len(ids),否则那一份会被整体丢弃。"""
    from app.services.chunk_federation import merge_chunk_matrices

    active, base, _base_source = base_only
    candidates = repo.retrieval.candidates
    # 把参考库判成大库 → 联邦任务体对它打开 peek lane。
    original_stats = candidates.notebook_copy_stats
    monkeypatch.setattr(
        candidates, "notebook_copy_stats",
        lambda notebook_id: (
            {"copyable": False} if notebook_id == base
            else original_stats(notebook_id)
        ),
    )
    parts = []
    original = candidates._retrieve_chunks

    def _spy(notebook_id, query, recall=0, **kwargs):
        result = original(notebook_id, query, recall, **kwargs)
        parts.append((notebook_id, result))
        return result

    monkeypatch.setattr(candidates, "_retrieve_chunks", _spy)

    repo.retrieval.retrieve_chunk_candidates(active, _QUERY)

    peer_parts = [result for nid, result in parts if nid == base]
    assert peer_parts, "peek 腿没跑"
    for _scored, ids, matrix in peer_parts:
        if matrix is None:
            continue
        assert len(matrix.shape) == 2 and matrix.shape[0] == len(ids)
    merged_ids, merged = merge_chunk_matrices(
        [(ids, matrix) for _scored, ids, matrix in peer_parts]
    )
    assert (merged is None) == (not merged_ids)
    if merged is not None:
        assert merged.shape[0] == len(merged_ids)
