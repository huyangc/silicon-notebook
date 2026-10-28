"""论文工具的合成侧(规格 2026-10-28 §7.2 / §7.3,计划 T6)。

两层:``paper_card_answer`` 的纯函数(登记、摘要、表格、覆盖说明),以及走
``AskService.ask_reasoning`` 真链路的端到端(检索整段换成桩,其余全是生产代码:
登记经 ``collection_item_citations`` 读回真实元素、表格拼进正文、锚点与引用卡、
落库)。检索侧的三个动作本身由 ``test_reasoning_paper_tools`` 覆盖,这里不重测。
"""
from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace

import pytest

from app.application.ask_reasoning import PaperToolsSnapshot
from app.core.config import Settings
from app.models.ask import Citation
from app.models.schemas import AskRequest, NotebookCreate
from app.services import ask_evidence
from app.services import reasoning_retrieval as rr_module
from app.services.ask_service import AskService
from app.services.citation_markers import MARKER_RE, marker_keys
from app.services.embedding import FakeEmbedder
from app.services.outline_synthesis import OUTLINE_SECTION_KEY_STRIDE
from app.services.paper_card_answer import (
    PAPER_CARD_KEY_BASE,
    PAPER_DIGEST_GUIDANCE,
    TABLE_CELL_CHARS,
    UNEXPANDED_TITLES,
    build_paper_card_evidence,
    compose_paper_answer,
    document_read_band_overflow,
    paper_digest_block,
    roster_omitted_for,
    value_with_unit,
)
from app.services.paper_card_service import CardResult
from app.services.paper_extraction import ExtractionResult, FieldValue
from app.services.prompts import (
    PAPER_TABLE_ANSWER_RULE,
    PAPER_TABLE_SECTION_RULE,
    answer_prompt,
)
from app.services.reasoning_paper_tools import (
    ANSWER_LAYOUT_PAPER_TABLE,
    ANSWER_LAYOUT_PROSE,
    FoundSourcesOutcome,
    PaperCardOutcome,
    PaperExtractionItem,
    PaperExtractionOutcome,
)
from app.services.reasoning_retrieval import OutlineSection, ReasoningResult, ReasoningRetriever
from app.services.retrieval import RetrievedElement
from app.services.source_finder import FindSourcesRequest, FindSourcesResult, FoundSource
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

NOW = "2026-10-28T00:00:00+08:00"
FIELD = "在 COCO 上的 mAP"
_MARKER = re.compile(r"\[(k\d+)\]")


# ------------------------------------------------------------------ factories


def _eid(source_id: str, index: int) -> str:
    return f"{source_id}-{index:03}"


def _card(source_id: str, *, wide: bool = False) -> dict:
    """A verified card; ``wide`` = 24 distinct anchors (the per-card cap)."""
    e = lambda i: _eid(source_id, i)  # noqa: E731
    if not wide:
        return {
            "tldr": {"text": f"{source_id} 的门控路由跳过冗余 token。", "anchors": [e(0)]},
            "problem": {"text": "稀疏骨干浪费算力。", "anchors": [e(0)]},
            "method": {"text": None, "anchors": []},
            "contributions": [
                {"text": "提出路由块 | 跳过 token\n第二行", "anchors": [e(1)]},
                {"text": "第二条贡献", "anchors": [e(2)]},
                {"text": "第三条贡献不进表", "anchors": [e(2)]},
            ],
            "metrics": [
                {"name": "top-1", "value": "85.2", "unit": "%", "setting": "ImageNet",
                 "compared_to": "ViT-B 81.8", "anchor": e(3)},
                {"name": "FLOPs", "value": "4.1", "unit": "G", "setting": "", "anchor": e(3)},
                {"name": "latency", "value": "12", "unit": "ms", "setting": "A100",
                 "anchor": e(3)},
                {"name": "第四条指标不进表", "value": "1", "unit": "", "anchor": e(3)},
            ],
            "datasets": [{"name": "ImageNet", "anchors": [e(3)]}],
        }
    return {
        "tldr": {"text": "宽卡片", "anchors": [e(0)]},
        "problem": {"text": "问题", "anchors": [e(1)]},
        "method": {"text": "方法", "anchors": [e(2)]},
        "contributions": [{"text": f"贡献{i}", "anchors": [e(3 + i)]} for i in range(5)],
        "metrics": [{"name": f"m{i}", "value": f"{i}.5", "unit": "%", "setting": "s",
                     "anchor": e(8 + i)} for i in range(12)],
        "datasets": [{"name": f"d{i}", "anchors": [e(20 + i)]} for i in range(4)],
    }


def _found(notebook_id, source_id, title, *, tier="strong", year=2024, venue="CVPR"):
    return FoundSource(
        rank=1, source_id=source_id, notebook_id=notebook_id, title=title,
        pub_year=year, venue=venue, authors=(), tier=tier, reasons=("title",),
        best_passage=None, card_status="ready",
    )


def _find(items, *, strong=None, related=None, capped=False, cursor="", next_cursor="",
          meta_unchecked=0, lexical_complete=True):
    strong = sum(1 for i in items if i.tier == "strong") if strong is None else strong
    related = sum(1 for i in items if i.tier != "strong") if related is None else related
    return FoundSourcesOutcome(
        request=FindSourcesRequest(query="门控", terms=("gating",), cursor=cursor),
        result=FindSourcesResult(
            items=tuple(items), strong=strong, related=related,
            filtered_scope_total=len(items), lexical_complete=lexical_complete,
            semantic_recall_capped=capped, next_cursor=next_cursor, lanes={},
            meta_pending_unchecked=meta_unchecked,
        ),
        handles=tuple(f"p{i + 1}" for i in range(len(items))),
    )


def _card_outcome(notebook_id, source_id, title, card, status="ready"):
    return PaperCardOutcome(
        handle="p1", title=title,
        result=CardResult(notebook_id=notebook_id, source_id=source_id, status=status,
                          card=card if status in ("ready", "generated") else {}),
    )


def _extraction(notebook_id, source_id, title, value, element_index=5):
    return PaperExtractionOutcome(
        fields=(FIELD,),
        items=(PaperExtractionItem(handle="p1", title=title, result=ExtractionResult(
            notebook_id=notebook_id, source_id=source_id, status="extracted",
            fields=(FieldValue(field_index=0, field=FIELD, value=value,
                               element_id=_eid(source_id, element_index)),),
        )),),
    )


def _tools(*, layout=ANSWER_LAYOUT_PAPER_TABLE, finds=(), cards=(), extractions=()):
    return PaperToolsSnapshot(answer_layout=layout, finds=tuple(finds), cards=tuple(cards),
                              extractions=tuple(extractions))


def _reader(drop=()):
    """``collection_item_citations`` stand-in: every element resolves to its own
    source (``<source>-NNN``) except the ids in ``drop``."""
    def read(items):
        out = {}
        for item in items:
            if item.object_id in drop:
                continue
            out[item.object_id] = Citation(
                label="论文 · p1", source_id=item.object_id.rsplit("-", 1)[0],
                element_id=item.object_id, location_label="p1",
                quoted_span="原文", tier=item.tier,
            )
        return out
    return read


def _evidence(tools, *, rows=12, drop=()):
    return build_paper_card_evidence(
        tools, read_citations=_reader(drop), tier_of=lambda ids: {}, table_rows=rows)


# ------------------------------------------------------------------ bands


def test_the_paper_card_band_agrees_across_all_four_places():
    """跨层合同:登记按它编号、合成按它分区计数、证据输出按它判 kind、检索侧按它
    关 read_document。任何一处写成别的数都不会报错,只会把引用指错。"""
    assert (PAPER_CARD_KEY_BASE == AskService._PAPER_CARD_KEY_BASE
            == rr_module.PAPER_CARD_KEY_BASE == ask_evidence._PAPER_CARD_BASE == 9000)
    assert rr_module.DOCUMENT_READ_KEY_BASE < PAPER_CARD_KEY_BASE < OUTLINE_SECTION_KEY_STRIDE


def test_kind_for_key_names_the_paper_card_band_in_every_section():
    id_map = {"k9003": {"object_type": "element"},
              f"k{OUTLINE_SECTION_KEY_STRIDE + 9001}": {"object_type": "element"},
              "k7001": {"object_type": "element"}}
    assert ask_evidence.kind_for_key("k9003", id_map) == "paper_card"
    assert ask_evidence.kind_for_key(
        f"k{OUTLINE_SECTION_KEY_STRIDE + 9001}", id_map) == "paper_card"
    assert ask_evidence.kind_for_key("k7001", id_map) == "document_read"


def test_document_read_band_overflow_and_startup_warning(caplog):
    from app.main import _document_read_band_preflight

    assert not document_read_band_overflow(4, 500)        # 2000:恰好放得下
    assert document_read_band_overflow(4, 501)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _document_read_band_preflight(SimpleNamespace(
            reasoning_max_document_reads=4, document_overview_max_elements=64))
    assert not [r for r in caplog.records if "read_document" in r.getMessage()]
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _document_read_band_preflight(SimpleNamespace(
            reasoning_max_document_reads=10, document_overview_max_elements=300))
    assert [r for r in caplog.records if "read_document" in r.getMessage()]


# ------------------------------------------------------------------ table


def _table_lines(table: str) -> list[str]:
    return [line for line in table.splitlines() if line.startswith("|")]


def test_table_escapes_cells_caps_items_and_marks_only_registered_keys():
    tools = _tools(
        finds=[_find([_found("nb", "s1", "Gated | Widgets\nNet")])],
        cards=[_card_outcome("nb", "s1", "Gated | Widgets\nNet", _card("s1"))],
        extractions=[_extraction("nb", "s1", "Gated", "COCO 2017: 42.1 AP")],
    )
    # s1-002(第二条贡献)读不回:它在表里不得出标记。
    paper = _evidence(tools, drop={_eid("s1", 2)})
    lines = _table_lines(paper.table)
    assert lines[0] == "| 论文 | 年份 / 会议 | 主要技术指标 | 关键创新点 | 补充字段 |"
    row = lines[2]
    # 标题里的 `|` 与换行都已转义,整行仍是 5 格。
    assert "Gated ｜ Widgets Net" in row
    assert row.count("|") == 6
    assert "2024 / CVPR" in row
    # 指标至多 3 条、贡献至多 2 条。
    assert "第四条指标不进表" not in row and "第三条贡献不进表" not in row
    assert "top-1 85.2%（ImageNet） vs ViT-B 81.8" in row and "latency 12 ms（A100）" in row
    # 只给已登记的 key 出标记;每个标记都在登记表里。
    keys = set(_MARKER.findall(paper.table))
    assert keys and keys <= set(paper.id_map)
    assert _eid("s1", 2) not in {paper.id_map[k]["element_id"] for k in keys}
    assert "第二条贡献" in row and "第二条贡献 [" not in row
    # 抽取值并进「补充字段」一列(「字段: 值 [kN]」),引用它自己的那个元素。
    value_key = paper.keys[_eid("s1", 5)]
    assert f"{FIELD}: COCO 2017: 42.1 AP [{value_key}]" in row


def test_a_long_cell_is_cut_before_its_marker_and_stays_within_the_limit():
    card = _card("s1")
    card["contributions"] = [{"text": "长" * 400, "anchors": [_eid("s1", 1)]}]
    tools = _tools(cards=[_card_outcome("nb", "s1", "T", card)])
    paper = _evidence(tools)
    cell = _table_lines(paper.table)[2].split(" | ")[3].removesuffix(" |")
    key = paper.keys[_eid("s1", 1)]
    assert len(cell) <= TABLE_CELL_CHARS and cell.endswith(f"… [{key}]")


def test_rows_past_the_limit_and_found_papers_without_a_row_are_listed_unexpanded():
    found = [_found("nb", f"s{i}", f"论文{i}") for i in range(130)]
    tools = _tools(
        finds=[_find(found)],
        cards=[_card_outcome("nb", f"s{i}", f"论文{i}", _card(f"s{i}")) for i in range(5)],
    )
    paper = _evidence(tools, rows=3)
    assert len(_table_lines(paper.table)) == 2 + 3
    tail = paper.table.split("其余相关论文（未展开）：", 1)[1]
    titles = tail.split(" 等共 ")[0].split("、")
    assert len(titles) == UNEXPANDED_TITLES
    assert titles[:2] == ["论文3", "论文4"]          # 超出内联上限的展开篇在前
    assert tail.endswith("等共 127 篇")


def test_coverage_line_complete_incomplete_and_without_find_sources():
    found = [_found("nb", "s1", "A"), _found("nb", "s2", "B", tier="related")]
    cards = [_card_outcome("nb", "s1", "A", _card("s1"))]
    complete = _evidence(_tools(finds=[_find(found)], cards=cards))
    assert complete.coverage == ("> 共找到 2 篇相关论文（强相关 1 篇、相关 1 篇），"
                                 "其中 1 篇展开了要点。")

    gaps = _evidence(_tools(
        finds=[_find(found, capped=True, meta_unchecked=3, next_cursor="c2")], cards=cards))
    assert gaps.coverage.startswith("> 共找到 2 篇相关论文")
    assert "可能还有更多：" in gaps.coverage
    assert "语义召回已触顶" in gaps.coverage
    assert "有 3 篇论文的元数据尚未抽取" in gaps.coverage
    assert "后续页" in gaps.coverage

    direct = _evidence(_tools(cards=cards))
    assert direct.coverage.startswith("> 本表展开了 1 篇文档的要点")
    assert "不代表已列全" in direct.coverage


def test_pages_of_one_request_are_not_counted_twice_but_two_requests_are_merged():
    page1 = _find([_found("nb", "s1", "A")], strong=3, related=0, next_cursor="c")
    page2 = _find([_found("nb", "s2", "B")], strong=3, related=0, cursor="c")
    other = FoundSourcesOutcome(
        request=FindSourcesRequest(query="另一个主题"),
        result=page1.result.__class__(**{**page1.result.__dict__, "next_cursor": "",
                                         "strong": 1, "related": 0}),
        handles=("p3",))
    paper = _evidence(_tools(finds=[page1, page2, other],
                             cards=[_card_outcome("nb", "s1", "A", _card("s1"))]))
    # 3(第一种请求,两页共用计数)+ 1(第二种)- 1(s1 两边都出现)。
    assert "共找到 3 篇相关论文（强相关 3 篇、相关 0 篇）" in paper.coverage


def test_prose_layout_registers_without_a_table_and_leaves_the_answer_alone():
    tools = _tools(layout=ANSWER_LAYOUT_PROSE,
                   cards=[_card_outcome("nb", "s1", "A", _card("s1"))])
    paper = _evidence(tools)
    assert paper.id_map and not paper.table and not paper.coverage
    assert compose_paper_answer("模型正文 [k9001]。", paper) == "模型正文 [k9001]。"
    assert compose_paper_answer("", _evidence(_tools(
        cards=[_card_outcome("nb", "s1", "A", _card("s1"))]))) == ""


def test_no_card_content_and_no_value_means_no_evidence():
    tools = _tools(cards=[_card_outcome("nb", "s1", "A", {}, status="not_paper")])
    assert _evidence(tools) is None
    assert build_paper_card_evidence(
        None, read_citations=_reader(), tier_of=lambda ids: {}, table_rows=6) is None


# ------------------------------------------------------------------ digest


def test_digest_is_whole_papers_within_budget_and_all_or_nothing():
    tools = _tools(cards=[_card_outcome("nb", f"s{i}", f"论文{i}", _card(f"s{i}"))
                          for i in range(3)])
    paper = _evidence(tools)
    full, full_map = paper_digest_block(paper, 100000)
    assert full.startswith(PAPER_DIGEST_GUIDANCE)
    assert all(f"《论文{i}》 · " not in full for i in range(3))  # 没有年份/会议时不留尾巴
    assert all(f"《论文{i}》" in full for i in range(3))
    # 每行带自己的 kN,摘要里的 key 全在登记表里。
    assert set(re.findall(r"(?m)^(k\d+):", full)) == set(full_map) <= set(paper.id_map)
    entries = full.split("\n\n")[1:]
    assert all(len(entry) <= 600 for entry in entries)
    one_paper = len(PAPER_DIGEST_GUIDANCE) + 2 + len(entries[0])
    part, _ = paper_digest_block(paper, one_paper + 10)
    assert "《论文0》" in part and "《论文1》" not in part
    assert paper_digest_block(paper, one_paper - 1) == ("", {})


def test_answer_prompt_rule_16_only_for_a_paper_table():
    plain = answer_prompt("q", "k1: x")
    assert answer_prompt("q", "k1: x", paper_table=False) == plain
    assert PAPER_TABLE_ANSWER_RULE not in plain
    assert PAPER_TABLE_ANSWER_RULE in answer_prompt("q", "k1: x", paper_table=True)


# ------------------------------------------------------------------ e2e


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _seed(repo, notebook_id, source_id, title, n=8):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "pdf", "extracted", "extracted", NOW, NOW))
        for index in range(n):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (_eid(source_id, index), source_id, "paragraph", f"p{index + 1}",
                 f"{title} 原文第 {index + 1} 段", "{}", NOW))
    repo.collection_catalog.invalidate()


class _AnswerLLM:
    """``ask_answer`` stand-in: records each prompt and cites the first ``kN``
    line it sees (so the digest key is what it cites)."""

    configured = True
    model = "stub"

    def __init__(self, answer=None):
        self.prompts: list[str] = []
        # ``answer(prompt) -> str`` overrides the default "cite the first key".
        self.answer = answer

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "门控"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"relevant"' in schema_hint:  # evidence_refine: nothing to prepend
            return json.dumps({"relevant": []})
        self.prompts.append(content)
        if self.answer is not None:
            return json.dumps({"answer": self.answer(content), "grounded": True})
        keys = re.findall(r"(?m)^(k\d+):", content)
        cite = f"[{keys[0]}]" if keys else ""
        return json.dumps({"answer": f"这些论文都在做门控{cite}。", "grounded": True})


def _ask(repo, notebook_id, result, monkeypatch, *, effort="standard", answer=None):
    monkeypatch.setattr(ReasoningRetriever, "run", lambda self, *a, **k: result)
    llm = _AnswerLLM(answer)
    for service in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, service, llm)
    response = repo._runtime.ask_service().ask_reasoning(
        notebook_id,
        AskRequest(question="列出与门控相关的论文并给出指标", mode="reasoning",
                   retrieval_effort=effort),
        user_id=repo.current_user().id,
    )
    return response, llm


def _element_rows(repo, ids):
    return repo._runtime.source_store.evidence_elements(list(ids))


def _all_marker_keys(text: str) -> set:
    """Every key of every marker form ``MARKER_RE`` accepts (``[k1]``,
    ``【k1】``, ``(k1)``, ``（k1）``) -- what the frontend binds."""
    return {key for marker in MARKER_RE.findall(text) for key in marker_keys(marker)}


def _assert_every_marker_resolves(repo, response):
    """表里与正文里的每个 [kN] 都解析成锚点,锚点指向真实元素,且有引用卡。"""
    anchors = {anchor.key: anchor for anchor in response.anchors}
    keys = _all_marker_keys(response.answer)
    assert keys and keys <= set(anchors), keys - set(anchors)
    rows = _element_rows(repo, [anchors[k].element_id for k in keys])
    cited = {c.element_id for c in response.citations}
    for key in keys:
        anchor = anchors[key]
        assert anchor.object_type == "element" and anchor.element_id in rows
        assert rows[anchor.element_id]["source_id"] == anchor.source_id
        assert anchor.element_id in cited
    return keys


def test_paper_table_answer_end_to_end(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    for i in range(3):
        _seed(repo, notebook.id, f"s{i}", f"论文{i}")
    found = [_found(notebook.id, f"s{i}", f"论文{i}") for i in range(3)]
    result = ReasoningResult(
        answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_finds=[_find(found)],
        paper_cards=[_card_outcome(notebook.id, f"s{i}", f"论文{i}", _card(f"s{i}"))
                     for i in range(2)],
        paper_extractions=[_extraction(notebook.id, "s0", "论文0", "COCO 2017: 42.1 AP")],
    )
    response, llm = _ask(repo, notebook.id, result, monkeypatch)

    prompt = llm.prompts[0]
    assert PAPER_DIGEST_GUIDANCE in prompt and PAPER_TABLE_ANSWER_RULE in prompt
    answer = response.answer
    # 覆盖说明 → 模型综述 → 表格 → 未展开清单,顺序固定。
    assert answer.startswith("> 共找到 3 篇相关论文（强相关 3 篇、相关 0 篇），其中 2 篇展开了要点。")
    assert answer.index("这些论文都在做门控") < answer.index("| 论文 |")
    assert answer.rstrip().endswith("其余相关论文（未展开）：论文2")
    keys = _assert_every_marker_resolves(repo, response)
    assert all(int(k[1:]) > PAPER_CARD_KEY_BASE for k in keys)
    # 抽取值的引用解析到包含该值的那个元素。
    value_anchor = next(a for a in response.anchors if a.element_id == _eid("s0", 5))
    assert f"COCO 2017: 42.1 AP [{value_anchor.key}]" in answer
    assert response.evidence_level == "grounded"
    detail = next(s for s in response.reasoning_trace if s.step_type == "synthesis").detail
    assert detail["included_paper_cards"] >= 1 and detail["included_chunks"] == 0

    # 落库的那一份与返回(流式 final 事件序列化的就是它)逐字一致。
    turn = repo.get_conversation(response.conversation_id).turns[-1]
    assert turn.response.answer == answer
    assert [a.key for a in turn.response.anchors] == [a.key for a in response.anchors]
    assert ({c.element_id for c in turn.response.citations}
            == {c.element_id for c in response.citations})
    assert response.model_dump()["answer"] == turn.response.model_dump()["answer"]


def test_forty_cards_exhaustive_every_marker_resolves(repo, monkeypatch):
    """穷尽档 40 张卡 × 24 个 anchor = 960 个登记键:号段装得下、表内每个标记都解析。"""
    notebook = repo.create_notebook(NotebookCreate(name="大库"))
    for i in range(40):
        _seed(repo, notebook.id, f"w{i}", f"宽论文{i}", n=24)
    result = ReasoningResult(
        answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_cards=[_card_outcome(notebook.id, f"w{i}", f"宽论文{i}", _card(f"w{i}", wide=True))
                     for i in range(40)],
    )
    response, _llm = _ask(repo, notebook.id, result, monkeypatch, effort="exhaustive")
    table_rows = [line for line in response.answer.splitlines() if line.startswith("| 宽论文")]
    assert len(table_rows) == 40
    keys = _assert_every_marker_resolves(repo, response)
    # 每一行都带标题、指标、贡献三处标记。
    assert all(len(_MARKER.findall(line)) >= 3 for line in table_rows)
    assert max(int(k[1:]) for k in keys) <= PAPER_CARD_KEY_BASE + 960


def test_prose_layout_answer_is_the_model_text(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    result = ReasoningResult(
        answer_layout=ANSWER_LAYOUT_PROSE,
        paper_cards=[_card_outcome(notebook.id, "s0", "论文0", _card("s0"))],
    )
    response, llm = _ask(repo, notebook.id, result, monkeypatch)
    assert PAPER_TABLE_ANSWER_RULE not in llm.prompts[0]
    assert response.answer == "这些论文都在做门控[k9001]。"
    _assert_every_marker_resolves(repo, response)


def test_a_run_without_paper_tools_sees_no_paper_text(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="普通库"))
    _seed(repo, notebook.id, "s0", "普通文档")
    element = RetrievedElement(
        element_id=_eid("s0", 0), source_id="s0", source_title="普通文档",
        location_label="p1", element_type="paragraph", text="普通原文", score=0.9)
    response, llm = _ask(repo, notebook.id, ReasoningResult(elements=[element]), monkeypatch)
    prompt = llm.prompts[0]
    assert PAPER_DIGEST_GUIDANCE not in prompt and PAPER_TABLE_ANSWER_RULE not in prompt
    assert "| 论文 |" not in response.answer
    detail = next(s for s in response.reasoning_trace if s.step_type == "synthesis").detail
    assert "included_paper_cards" not in detail


def test_sectioned_answer_renders_the_table_once_before_the_first_section(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    _seed(repo, notebook.id, "doc", "综述文档")
    elements = [
        RetrievedElement(element_id=_eid("doc", i), source_id="doc", source_title="综述文档",
                         location_label="p1", element_type="paragraph",
                         text=f"第{i}节材料", score=0.9)
        for i in range(2)
    ]
    outline = [
        OutlineSection(id="a", title="第一节", parent="", evidence_keys=[_eid("doc", 0)]),
        OutlineSection(id="b", title="第二节", parent="", evidence_keys=[_eid("doc", 1)]),
    ]
    result = ReasoningResult(
        elements=elements, outline=outline,
        answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_cards=[_card_outcome(notebook.id, "s0", "论文0", _card("s0"))],
    )
    response, llm = _ask(repo, notebook.id, result, monkeypatch, effort="exhaustive")
    assert len(llm.prompts) == 2, "两节两次合成"
    first, second = llm.prompts
    assert PAPER_DIGEST_GUIDANCE in first and PAPER_TABLE_SECTION_RULE in first
    assert PAPER_TABLE_ANSWER_RULE not in first
    assert PAPER_DIGEST_GUIDANCE not in second and PAPER_TABLE_SECTION_RULE not in second
    answer = response.answer
    assert answer.count("| 论文 | 年份 / 会议 |") == 1
    assert answer.startswith("> 本表展开了 1 篇文档的要点")
    assert answer.index("| 论文 |") < answer.index("## 第一节") < answer.index("## 第二节")
    _assert_every_marker_resolves(repo, response)


def test_retrieval_only_output_delivers_digest_lines_as_paper_card_items(repo, monkeypatch):
    """``output="evidence"`` 交出合成本应收到的上下文:摘要行是 ``paper_card``,
    表格不是证据、不出现。"""
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    monkeypatch.setattr(ReasoningRetriever, "run", lambda self, *a, **k: ReasoningResult(
        answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_cards=[_card_outcome(notebook.id, "s0", "论文0", _card("s0"))]))
    bind_chat_client(repo, "reasoning_agent", _AnswerLLM())
    evidence = repo._runtime.ask_service().ask_evidence(
        notebook.id, AskRequest(question="列出与门控相关的论文", mode="reasoning"),
        user_id=repo.current_user().id)
    kinds = {item.key: item.kind for item in evidence.items if item.key}
    assert kinds and set(kinds.values()) == {"paper_card"}
    assert all(int(key[1:]) > PAPER_CARD_KEY_BASE for key in kinds)
    assert not any("| 论文 |" in item.text for item in evidence.items)


def test_report_restore_does_not_reuse_a_paper_card_digest_line(repo):
    """报告引过的元素若恰好也是卡片摘要的一行,复原时按元素放进 4001+ 段,不复用
    9000 段的摘要键:摘要是模型写的,规则 15 要求对照原文核对。"""
    from app.domain.report_followup import RestoredReportEvidence, SectionSelection

    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    element = RetrievedElement(
        element_id=_eid("s0", 0), source_id="s0", source_title="论文0",
        location_label="p1", element_type="paragraph", text="论文0 原文第 1 段", score=1.0)
    restored = RestoredReportEvidence(
        elements=(element,), key_by_object={("element", element.element_id): ("k3",)})
    id_map = {"k9001": {"object_id": element.element_id, "object_type": "element",
                        "element_id": element.element_id, "source_id": "s0"}}
    service = repo._runtime.ask_service()
    partition = service._append_report_evidence(
        "k9001: TL;DR: 摘要", id_map, SimpleNamespace(restored=restored),
        SectionSelection(keys=("k3",)), notebook_id=notebook.id)
    assert ask_evidence.kind_for_key("k9001", id_map) == "paper_card"
    new_keys = [k for k in partition.id_map if k not in id_map]
    assert new_keys and all(4000 < int(k[1:]) < 5000 for k in new_keys)


# ------------------------------------------------------------ review round 1

_FAKE_MARKERS = "见 [k4001] 【k4002】 (k1) （k2）"


def test_every_marker_form_in_card_text_is_neutralised_in_table_and_digest():
    card = _card("s1")
    card["metrics"][0]["name"] = f"top-1 {_FAKE_MARKERS}"
    card["contributions"][0]["text"] = f"贡献 {_FAKE_MARKERS}"
    card["tldr"]["text"] = f"摘要 {_FAKE_MARKERS}"
    tools = _tools(cards=[_card_outcome("nb", "s1", f"标题 {_FAKE_MARKERS}", card)],
                   extractions=[_extraction("nb", "s1", "t", f"42.1 {_FAKE_MARKERS}")])
    paper = _evidence(tools)
    assert _all_marker_keys(paper.table) <= set(paper.id_map)
    assert _all_marker_keys(paper.coverage) == set()
    digest, digest_map = paper_digest_block(paper, 100000)
    assert _all_marker_keys(digest) == set()          # no marker in the digest text
    assert "〖k4002" in paper.table and "﹙k1" in paper.table


def test_card_text_markers_never_bind_end_to_end(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    card = _card("s0")
    card["metrics"][0]["name"] = f"top-1 {_FAKE_MARKERS}"
    element = RetrievedElement(
        element_id=_eid("s0", 7), source_id="s0", source_title="论文0",
        location_label="p8", element_type="paragraph", text="直接元素", score=0.9)
    result = ReasoningResult(
        elements=[element], answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_cards=[_card_outcome(notebook.id, "s0", f"论文0 {_FAKE_MARKERS}", card)])
    response, _llm = _ask(repo, notebook.id, result, monkeypatch)
    anchor_keys = {a.key for a in response.anchors}
    assert "k4001" not in anchor_keys and "k1" not in anchor_keys
    _assert_every_marker_resolves(repo, response)


def test_a_backslash_in_cell_text_cannot_split_the_row_and_year_venue_is_capped():
    card = _card("s1")
    card["metrics"][0]["name"] = r"误差 \|W\|_F"
    found = _found("nb", "s1", r"范数 \|W\| 论文", venue="V" * 500)
    paper = _evidence(_tools(finds=[_find([found])],
                             cards=[_card_outcome("nb", "s1", "", card)]))
    header, _sep, row = _table_lines(paper.table)[:3]
    assert row.count("|") == header.count("|") == 5
    assert len(row.split(" | ")[1]) <= TABLE_CELL_CHARS


def test_only_anchors_the_answer_can_show_are_registered():
    card = _card("s1", wide=True)
    card["contributions"][0]["anchors"] = [_eid("s1", 3), _eid("s1", 24)]
    tools = _tools(cards=[_card_outcome("nb", "s1", "T", card)])
    table = {row["element_id"] for row in _evidence(tools).id_map.values()}
    # tl;dr, 2 contributions (2 anchors on the first), 3 metrics.
    assert table == {_eid("s1", i) for i in (0, 3, 24, 4, 8, 9, 10)}
    digest_only = build_paper_card_evidence(
        tools, read_citations=_reader(), tier_of=lambda ids: {}, table_rows=12,
        for_answer=False)
    assert {r["element_id"] for r in digest_only.id_map.values()} == table - {_eid("s1", 24)}
    assert not digest_only.table and not digest_only.paper_table


def test_a_hallucinated_paper_key_in_model_text_is_dropped_not_bound(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    for i in range(2):
        _seed(repo, notebook.id, f"s{i}", f"论文{i}")
    cards = []
    for i in range(2):
        card = _card(f"s{i}")
        card["contributions"][0]["anchors"].append(_eid(f"s{i}", 6))
        cards.append(_card_outcome(notebook.id, f"s{i}", f"论文{i}", card))
    result = ReasoningResult(answer_layout=ANSWER_LAYOUT_PAPER_TABLE, paper_cards=cards)

    def answer(prompt):
        # k9003 = s0's second contribution anchor: registered, shown only by the
        # table, never in the prompt -- so the model cannot have seen it.
        assert not re.search(r"(?m)^k9003:", prompt)
        answer.made_up = "k9003"
        return "综述 [k9003]。"

    response, _llm = _ask(repo, notebook.id, result, monkeypatch, answer=answer)
    model_part = response.answer.split("\n\n")[1]
    assert model_part.startswith("综述") and answer.made_up not in model_part
    assert answer.made_up in response.answer          # ...the table carries it legitimately
    _assert_every_marker_resolves(repo, response)


def test_the_table_still_follows_a_failed_synthesis(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    result = ReasoningResult(
        answer_layout=ANSWER_LAYOUT_PAPER_TABLE,
        paper_cards=[_card_outcome(notebook.id, "s0", "论文0", _card("s0"))])
    response, _llm = _ask(repo, notebook.id, result, monkeypatch, answer=lambda prompt: "")
    assert response.llm_mode == "synthesis_failed"
    assert response.answer.startswith("本次答案合成未产出内容")
    assert "| 论文 |" in response.answer and "| 论文 |" in response.conclusion
    _assert_every_marker_resolves(repo, response)


def test_extracted_fields_past_the_table_width_are_named():
    fields = tuple(f"字段{i}" for i in range(6))
    extraction = PaperExtractionOutcome(fields=fields, items=(PaperExtractionItem(
        handle="p1", title="T", result=ExtractionResult(
            notebook_id="nb", source_id="s1", status="extracted",
            fields=tuple(FieldValue(field_index=i, field=f, value=f"v{i}",
                                    element_id=_eid("s1", i)) for i, f in enumerate(fields)),
        )),))
    paper = _evidence(_tools(extractions=[extraction]))
    lines = _table_lines(paper.table)
    assert lines[0].endswith("| 关键创新点 | 补充字段 |")
    assert all(f"字段{i}: v{i}" in lines[2] for i in range(4))
    assert "字段4" not in lines[2]
    assert paper.coverage.endswith("另有 2 个临时字段未列入表：字段4、字段5。")


def test_a_paper_listed_by_two_requests_counts_once_under_its_best_tier():
    first = _find([_found("nb", "s1", "A", tier="related")])
    second = FoundSourcesOutcome(
        request=FindSourcesRequest(query="另一种说法"),
        result=FindSourcesResult(
            items=(_found("nb", "s1", "A", tier="strong"),), strong=1, related=0,
            filtered_scope_total=1, lexical_complete=True, semantic_recall_capped=False,
            next_cursor="", lanes={}),
        handles=("p2",))
    cards = [_card_outcome("nb", "s1", "A", _card("s1")),
             _card_outcome("nb", "s9", "直接读的", _card("s9"))]
    paper = _evidence(_tools(finds=[first, second], cards=cards))
    assert paper.coverage == ("> 共找到 1 篇相关论文（强相关 1 篇、相关 0 篇），"
                              "其中 1 篇展开了要点（另有 1 篇直接按来源读取）。")


def test_table_rows_outside_the_digest_resolve_through_the_registration(repo, monkeypatch):
    """标准档 40 张卡:摘要装不下全部,表里没进摘要的行仍然靠登记表解析。"""
    notebook = repo.create_notebook(NotebookCreate(name="大库"))
    cards = []
    for i in range(40):
        _seed(repo, notebook.id, f"w{i}", f"宽论文{i}", n=24)
        card = _card(f"w{i}", wide=True)
        card["tldr"]["text"] = "长摘要" * 100       # each digest entry hits its 600 cap
        # A second anchor per contribution: shown by the table cell only.
        for n, entry in enumerate(card["contributions"]):
            entry["anchors"].append(_eid(f"w{i}", 20 + n))
        cards.append(_card_outcome(notebook.id, f"w{i}", f"宽论文{i}", card))
    result = ReasoningResult(answer_layout=ANSWER_LAYOUT_PAPER_TABLE, paper_cards=cards)
    response, llm = _ask(repo, notebook.id, result, monkeypatch)
    prompt_keys = set(re.findall(r"(?m)^(k\d+):", llm.prompts[0]))
    shown = len(re.findall(r"《宽论文\d+》", llm.prompts[0]))
    assert 0 < shown < 40, "the digest must not fit every paper"
    keys = _assert_every_marker_resolves(repo, response)
    assert keys - prompt_keys, "some table keys never reached the prompt"


def test_the_digest_reserve_shrinks_the_enumeration_preview_budget(repo, monkeypatch):
    import app.services.ask_service as ask_service_module
    import app.services.collection_enumeration_answer as cea
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    budgets: list[int] = []
    monkeypatch.setattr(cea, "typed_collection_results", lambda *a, **k: [])
    monkeypatch.setattr(cea, "delivered_outcomes", lambda *a, **k: [])
    monkeypatch.setattr(cea, "apply_synthesis_preview_counts", lambda *a, **k: None)
    monkeypatch.setattr(cea, "enumeration_prompt_block", lambda *a, budget_chars, **k: (
        budgets.append(budget_chars) or SimpleNamespace(text="", evidence_by_id={}, shown_rows={})))
    service = repo._runtime.ask_service()
    monkeypatch.setattr(service.evidence_context, "collection_item_citations",
                        lambda *a, **k: {})
    limits = ask_retrieval_limits("standard")
    stage = SimpleNamespace(
        prepared=SimpleNamespace(limits=limits, notebook_id="nb"), structured_batch=None,
        enumerations=[SimpleNamespace(items=())], spreadsheet_results=[], document_reads=())
    paper = _evidence(_tools(cards=[_card_outcome("nb", "s1", "A", _card("s1"))]))
    client = SimpleNamespace(configured=True)
    service._render_structured_side(stage, client, None, {}, None)
    service._render_structured_side(stage, client, None, {}, paper)
    reserve = ask_service_module.paper_digest_reserve(paper, limits.chunk_context_chars)
    assert reserve > 0 and budgets[0] - budgets[1] == reserve


def test_section_mode_rule_16_wording_and_one_shot_bytes():
    sectioned = answer_prompt("q", "k1: x", sectioned=True, section_title="一",
                              section_index=1, section_total=2, paper_table=True)
    assert PAPER_TABLE_SECTION_RULE in sectioned and PAPER_TABLE_ANSWER_RULE not in sectioned
    one_shot = answer_prompt("q", "k1: x", paper_table=True)
    assert PAPER_TABLE_ANSWER_RULE in one_shot and PAPER_TABLE_SECTION_RULE not in one_shot
    assert PAPER_TABLE_ANSWER_RULE.startswith("16. The system builds the paper table itself")


def test_one_card_per_element_when_k4001_and_k9xxx_cite_the_same_element(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="论文库"))
    _seed(repo, notebook.id, "s0", "论文0")
    element = RetrievedElement(
        element_id=_eid("s0", 0), source_id="s0", source_title="论文0",
        location_label="p1", element_type="paragraph", text="论文0 原文第 1 段", score=0.9)
    result = ReasoningResult(
        elements=[element], answer_layout=ANSWER_LAYOUT_PROSE,
        paper_cards=[_card_outcome(notebook.id, "s0", "论文0", _card("s0"))])
    response, _llm = _ask(repo, notebook.id, result, monkeypatch,
                          answer=lambda prompt: "原文 [k4001]。摘要 [k9001]。")
    anchored = [a for a in response.anchors if a.element_id == element.element_id]
    assert {a.key for a in anchored} == {"k4001", "k9001"}
    assert sum(1 for c in response.citations if c.element_id == element.element_id) == 1


# ------------------------------------------------------------------ T7 acceptance fixes


@pytest.mark.parametrize("value, unit, text", [
    # T7 真模型表里拼成一团的写法(「21.4AP」「79GPU days」「412steps」「26FPS」)。
    ("21.4", "AP", "21.4 AP"), ("79", "GPU days", "79 GPU days"),
    ("412", "steps", "412 steps"), ("26", "FPS", "26 FPS"), ("2.40", "USD", "2.40 USD"),
    ("19.4", "meV", "19.4 meV"), ("2.99", "bits/dim", "2.99 bits/dim"),
    # 符号单位贴着数字。
    ("85.2", "%", "85.2%"), ("4", "×", "4×"), ("90", "°", "90°"), ("3.5", "x", "3.5x"),
    ("12", "", "12"), ("", "ms", "ms"),
])
def test_value_and_unit_are_spaced_only_before_a_word_unit(value, unit, text):
    assert value_with_unit(value, unit) == text


def _metric_card(source_id: str, metrics: list) -> dict:
    card = _card(source_id)
    card["metrics"] = metrics
    return card


def test_metric_cell_reads_name_value_unit_setting_vs_baseline_in_table_and_digest():
    """T7 cascaded-diffusion 那一行:本文方法的成绩带测量条件,基线只出现在 vs 之后。"""
    e = _eid("s1", 3)
    card = _metric_card("s1", [
        {"name": "FID", "value": "3.52", "unit": "", "setting": "ImageNet 256, 250 steps",
         "compared_to": "BigGAN-deep 6.95", "anchor": e},
        {"name": "LVIS AP rare", "value": "21.4", "unit": "AP",
         "setting": "LVIS v1", "compared_to": "", "anchor": e},
        {"name": "speed", "value": "26", "unit": "FPS", "setting": "COCO val", "anchor": e},
    ])
    paper = _evidence(_tools(cards=[_card_outcome("nb", "s1", "Cascaded", card)]))
    row = _table_lines(paper.table)[2]
    key = paper.keys[e]
    assert f"FID 3.52（ImageNet 256, 250 steps） vs BigGAN-deep 6.95 [{key}]" in row
    assert f"LVIS AP rare 21.4 AP（LVIS v1） [{key}]" in row
    assert f"speed 26 FPS（COCO val） [{key}]" in row
    digest, _map = paper_digest_block(paper, 10_000)
    assert "Metric: LVIS AP rare 21.4 AP（LVIS v1）" in digest
    assert "21.4AP" not in digest and "26FPS" not in paper.table


def test_extracted_values_share_one_column_and_rows_without_values_show_a_dash():
    tools = _tools(
        cards=[_card_outcome("nb", "s1", "有卡片", _card("s1")),
               _card_outcome("nb", "s2", "卡片失败", {}, status="failed")],
        extractions=[_extraction("nb", "s2", "卡片失败", "8.5 meV/Å")],
    )
    lines = _table_lines(_evidence(tools).table)
    assert lines[0] == "| 论文 | 年份 / 会议 | 主要技术指标 | 关键创新点 | 补充字段 |"
    assert all(line.count("|") == 6 for line in lines)
    assert lines[2].endswith(" | — |")
    assert f"| {FIELD}: 8.5 meV/Å [" in lines[3]


def test_no_extra_column_when_no_inline_row_has_an_extracted_value():
    tools = _tools(
        cards=[_card_outcome("nb", f"s{i}", f"论文{i}", _card(f"s{i}")) for i in range(2)],
        extractions=[_extraction("nb", "s9", "只在表外", "v")],
    )
    paper = _evidence(tools, rows=2)
    lines = _table_lines(paper.table)
    assert lines[0] == "| 论文 | 年份 / 会议 | 主要技术指标 | 关键创新点 |"
    assert all(line.count("|") == 5 for line in lines)
    assert "补充字段" not in paper.table


def test_rule_16_makes_the_coverage_line_authoritative_over_the_model():
    """T7:5/6 题正文自称「本次只预览了其中 32 篇」,与系统覆盖说明矛盾。"""
    for rule in (PAPER_TABLE_ANSWER_RULE, PAPER_TABLE_SECTION_RULE):
        assert "coverage line" in rule and "authoritative" in rule
        assert "read, previewed or listed" in rule


def test_the_roster_preview_is_left_out_only_for_a_find_sources_paper_table(repo, monkeypatch):
    import app.services.collection_enumeration_answer as cea
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    omitted: list = []
    monkeypatch.setattr(cea, "typed_collection_results", lambda *a, **k: [])
    monkeypatch.setattr(cea, "delivered_outcomes", lambda *a, **k: [])
    monkeypatch.setattr(cea, "apply_synthesis_preview_counts", lambda *a, **k: None)
    monkeypatch.setattr(cea, "enumeration_prompt_block", lambda *a, omit_collections, **k: (
        omitted.append(omit_collections)
        or SimpleNamespace(text="", evidence_by_id={}, shown_rows={})))
    service = repo._runtime.ask_service()
    monkeypatch.setattr(service.evidence_context, "collection_item_citations",
                        lambda *a, **k: {})
    stage = SimpleNamespace(
        prepared=SimpleNamespace(limits=ask_retrieval_limits("standard"), notebook_id="nb"),
        structured_batch=None, enumerations=[SimpleNamespace(items=())],
        spreadsheet_results=[], document_reads=())
    cards = [_card_outcome("nb", "s1", "A", _card("s1"))]
    searched = _evidence(_tools(finds=[_find([_found("nb", "s1", "A")])], cards=cards))
    direct = _evidence(_tools(cards=cards))
    prose = _evidence(_tools(layout=ANSWER_LAYOUT_PROSE,
                             finds=[_find([_found("nb", "s1", "A")])], cards=cards))
    client = SimpleNamespace(configured=True)
    for paper in (searched, direct, prose, None):
        service._render_structured_side(stage, client, None, {}, paper)
    assert omitted == [frozenset({"sources"}), frozenset(), frozenset(), frozenset()]
    assert roster_omitted_for(searched) == frozenset({"sources"})
