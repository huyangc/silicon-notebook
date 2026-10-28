"""经验库只推荐本 run 提供的动作。

注入块(规划 + 每轮 reflect 的「检索打法」)与 consult_memory 的回想行,凡推荐的
动作不在本 run reflect 的白名单里(``core_reflect_actions``,与 ``reflect()`` 的
``allowed_actions`` 同一个函数、同一组闸值),该行在**选择之前**就被滤掉:模型
照着它选动作只会撞上白名单、按非法动作兜底提前收尾。判据只看白名单,不看模式,
所以无图闸与对等模式的两把逐动作闸一并适用。

被滤掉的行不占 top-k 名额、不进渲染,因而也不算送达,采用回写(只看送达行)与
consult 的「已送达」排除集天然不含它们。动作全在场的单库有图 run,块逐字节不变。
"""
from __future__ import annotations

import hashlib

from app.services.reasoning_retrieval import (
    _consultable_rows,
    core_reflect_actions,
    experience_action_offered,
)
from app.services.retrieval_experience_block import (
    RETRIEVAL_EXPERIENCE_INJECT_TOP_K,
    render_experience_block,
    select_experiences_layered,
)
from tests.test_peer_mode_ask_steps import _peer_scope
from tests.test_retrieval_experience_injection import (  # noqa: F401 — repo 是夹具
    HEADER, NOW, _retriever, _run, _seed, _SeqLLM, _situation, _steps, _write,
    repo,
)
from app.models.schemas import NotebookCreate


_RETRIEVE = "Retrieve again with a sharper sub-query."
_PPR = "Walk the graph when sources disagree."
_EXPAND = "Expand the central node one hop."
_CHUNKS = "Search the raw passages for literal terms."


def _seed_without_kg(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nokg"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s-nokg", notebook.id, "论文二", "pdf", "extracted", "extracted",
             NOW, NOW),
        )
    repo.collection_catalog.invalidate()
    return notebook


#: 单库有图 run、三条动作都在场时的注入块摘要。取自加过滤**之前**的代码。
_SINGLE_LIBRARY_BLOCK_DIGEST = (
    "fc77ffc4e4daf21921bb95f749e36bc6ba9e77c22efb10fdbdbf45936a4d14dd"
)


def test_single_library_graph_run_injects_the_same_block(repo):
    """动作全在场:块等于「不过滤」的选择 + 渲染,逐字节,并由摘要钉住。"""
    notebook = _seed(repo)
    for action, text in (("retrieve", _RETRIEVE), ("ppr", _PPR),
                         ("expand", _EXPAND)):
        _write(repo, action, "good", text)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    expected = render_experience_block(select_experiences_layered(
        [], repo.retrieval_experiences.read_partition("", 300),
        _situation()).entries)
    assert expected and expected in llm.plan_prompts[0]
    assert _steps(result, "experience")[0].detail["entries"] == 3
    assert hashlib.sha256(expected.encode("utf-8")).hexdigest() == (
        _SINGLE_LIBRARY_BLOCK_DIGEST)


def test_a_graphless_run_does_not_recommend_graph_actions(repo):
    """无图 run:推荐 `expand_graph` 的行滤掉,不占名额,也不算送达。"""
    notebook = _seed_without_kg(repo)
    _write(repo, "expand", "good", _EXPAND)
    _write(repo, "retrieve", "good", _RETRIEVE)

    llm = _SeqLLM([{"next_action": "add_subquery",
                    "new_sub_query": {"query": "另一个角度"}},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert "expand_graph" not in llm.plan_prompts[0]
    assert _RETRIEVE in llm.plan_prompts[0] and _EXPAND not in llm.plan_prompts[0]
    for prompt in llm.reflect_prompts:
        assert _EXPAND not in prompt
    assert _steps(result, "experience")[0].detail["entries"] == 1


def test_a_peer_run_does_not_recommend_ppr(repo):
    """对等 run(有图):推荐 `ppr_retrieve` 的行滤掉;同库单库对照臂照常推荐。"""
    notebook = _seed(repo)
    ppr_id = _write(repo, "ppr", "good", _PPR)
    _write(repo, "retrieve", "good", _RETRIEVE)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    with _peer_scope((notebook.id,)):
        result = _run(retriever, notebook, limits)
    assert _PPR not in llm.plan_prompts[0] and _RETRIEVE in llm.plan_prompts[0]
    assert _steps(result, "experience")[0].detail["entries"] == 1

    # 对照臂:单库 run 里同一条经验照常送达,模型选了它就记采用。
    llm = _SeqLLM([{"next_action": "ppr_retrieve", "query": "x"},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    _run(retriever, notebook, limits)
    assert _PPR in llm.plan_prompts[0]
    assert repo.retrieval_experiences.read_experience(ppr_id)["adopted"] == 1


def test_a_filtered_row_does_not_take_a_slot(repo):
    """被滤掉的行在选择之前就不在候选里:top-k 名额留给提供了的动作。"""
    notebook = _seed_without_kg(repo)
    offered = [("retrieve", _RETRIEVE), ("exact_lookup", "Look the name up."),
               ("search_chunks", _CHUNKS)]
    assert len(offered) == RETRIEVAL_EXPERIENCE_INJECT_TOP_K
    for action, text in offered:
        _write(repo, action, "good", text, support=1)
    # 证据更强的一条,推荐的却是无图 run 里不存在的动作。
    _write(repo, "follow_chain", "good", "Chain the derivation.", support=9)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)
    for _action, text in offered:
        assert text in llm.plan_prompts[0], text
    assert "Chain the derivation." not in llm.plan_prompts[0]
    assert _steps(result, "experience")[0].detail["entries"] == 3


def test_consult_rows_drop_actions_the_run_does_not_offer(repo):
    """consult_memory 的候选与注入块同一把过滤;`None`(未知)不过滤。"""
    notebook = _seed(repo)
    _write(repo, "ppr", "good", _PPR)
    _write(repo, "retrieve", "good", _RETRIEVE)

    def _actions(offered):
        rows = _consultable_rows(
            repo.retrieval_experiences, notebook.id, _situation(),
            offered=offered,
            exclude_ids=set(), zero_hit_actions=set(), top_k=3)
        return {row["action"] for row in rows}

    full = frozenset(core_reflect_actions(
        kg_actions=True, chunk_search=True, enumeration=True,
        read_document=False, outline=False, consult_memory=True,
        search_elements=True, ppr_retrieve=True, section_nav=True))
    peer = frozenset(core_reflect_actions(
        kg_actions=True, chunk_search=True, enumeration=True,
        read_document=False, outline=False, consult_memory=True,
        search_elements=False, ppr_retrieve=False, section_nav=True))
    assert _actions(None) == _actions(full) == {"ppr", "retrieve"}
    assert _actions(peer) == {"retrieve"}


def test_section_navigation_experience_rows_follow_the_section_gate():
    """文档内导航的两个词:闸开才推荐,闸关不渲染(与其余动作同一把过滤)。"""
    for word in ("browse_sections", "read_section"):
        offered = frozenset(core_reflect_actions(
            kg_actions=False, chunk_search=True, enumeration=False,
            read_document=False, outline=False, consult_memory=False,
            search_elements=True, ppr_retrieve=False, section_nav=True))
        closed = frozenset(core_reflect_actions(
            kg_actions=False, chunk_search=True, enumeration=False,
            read_document=False, outline=False, consult_memory=False,
            search_elements=True, ppr_retrieve=False, section_nav=False))
        assert experience_action_offered(word, offered)
        assert not experience_action_offered(word, closed)


def test_the_enumerate_wildcard_is_offered_by_either_listing_action():
    assert experience_action_offered("enumerate", frozenset({"enumerate_elements"}))
    assert experience_action_offered("enumerate", frozenset({"enumerate_kg_objects"}))
    assert not experience_action_offered("enumerate", frozenset({"answer"}))
    assert experience_action_offered("retrieve", frozenset({"add_subquery"}))
    assert not experience_action_offered("ppr", frozenset({"add_subquery"}))


def test_paper_tool_experience_rows_follow_their_own_gates():
    """论文工具的三个词(轨迹步类型)各自折回自己的动作 id:闸开才推荐,闸关不渲染。"""
    for word, gate in (("find_documents", "find_sources"),
                       ("paper_cards", "paper_cards"),
                       ("extract_fields", "extract")):
        base = dict(kg_actions=False, chunk_search=True, enumeration=False,
                    read_document=False, outline=False, consult_memory=False,
                    search_elements=True, ppr_retrieve=False, section_nav=False)
        offered = frozenset(core_reflect_actions(**base, **{gate: True}))
        closed = frozenset(core_reflect_actions(**base))
        assert experience_action_offered(word, offered)
        assert not experience_action_offered(word, closed)
