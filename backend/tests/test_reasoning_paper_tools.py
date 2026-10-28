"""The reasoning agent's paper tools (spec 2026-10-28 §7.1, plan T5):
``find_sources`` / ``get_paper_cards`` / ``extract_from_sources`` and the
``answer_layout`` top-level field.

The full runs go through the PRODUCTION construction (``AskService.
_build_reasoning_retriever`` over the runtime's ``ReasoningPaperTools`` seat)
on the synthetic paper corpus (``tests/paper_topics_testkit.py``): the real
``SourceFinder``, ``PaperCardService`` and ``PaperExtractionService``, a fake
``paper_metadata`` model behind the real scheduled adapter and a replayed
reflect model that is validated against the production schema hint.  No real
model is called.  The gate / parsing / ledger cases swap stub services into
the same seat.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.models.schemas import NotebookCreate
from app.services.paper_card_service import CardResult
from app.services.paper_extraction import (
    EXTRACT_MAX_SOURCES,
    EXTRACTION_SCHEMA_HINT,
    ExtractionRequestError,
    ExtractionResult,
    FieldValue,
)
from app.services.reasoning_paper_tools import (
    ANSWER_LAYOUT_NO_RESULTS,
    PaperHandles,
    ReasoningPaperTools,
    resolve_answer_layout,
    resolve_source_tokens,
)
from app.services.reasoning_retrieval import CORE_REFLECT_ACTION_IDS, ReasoningRetriever
from app.services.reports.policy import (
    REASONING_EXTRACT_SOURCES_PER_CALL,
    REASONING_PAPER_CARDS_PER_CALL,
    reasoning_action_policy,
)
from app.services.retrieval_run import holding_fanout_slot
from app.services.source_finder import (
    BestPassage,
    FindSourcesCursorError,
    FindSourcesError,
    FindSourcesResult,
    FoundSource,
)
from tests import paper_card_testkit as card_kit
from tests.model_testkit import bind_chat_client
from tests.paper_topics_testkit import load_labels, sqlite_corpus
from tests.test_paper_extraction import FakeExtractionModel
from tests.test_reasoning_enumeration_tools import _ValidatingLLM

QUESTION = "列出所有目标检测相关的论文,给出主要指标与创新点"
ANSWER = {"next_action": "answer", "sufficient": True, "reason": "够了"}
TOPIC = load_labels()["detection"]


class _RecordingLLM(_ValidatingLLM):
    """``_ValidatingLLM`` that keeps every reflect prompt and schema hint."""

    def __init__(self, reflects):
        super().__init__(list(reflects), plan={"sub_queries": [{"query": "object detection"}]})
        self.prompts: list[str] = []
        self.hints: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        if '"next_action"' in schema_hint:
            self.prompts.append(messages[-1]["content"])
            self.hints.append(schema_hint)
        return super().chat_json(messages, schema_hint, **kwargs)


class _PaperModel(card_kit.FakePaperModel):
    """One ``paper_metadata`` fake for both services: extraction prompts get
    ``FakeExtractionModel``'s grounded reply, everything else the card fake."""

    def __init__(self):
        super().__init__()
        self.extraction_calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if schema_hint == EXTRACTION_SCHEMA_HINT:
            self.extraction_calls += 1
            reply = FakeExtractionModel.default_reply(messages[-1]["content"])
            return json.dumps(reply)
        return super().chat_json(messages, schema_hint, **kwargs)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    yield from sqlite_corpus(tmp_path, monkeypatch)


@pytest.fixture
def paper_model(corpus):
    repo, _nb, _seeded = corpus
    fake = _PaperModel()
    with card_kit.scheduled_paper_model(repo, fake):
        yield fake


def _retriever(repo, llm) -> ReasoningRetriever:
    """The production Ask construction (``ReasoningPaperTools`` seated by the
    runtime)."""
    bind_chat_client(repo, "reasoning_agent", llm)
    return repo._runtime.ask_service()._build_reasoning_retriever(
        cancel_event=None, user_id=repo.current_user().id)


def _run(retriever, nb, effort="standard"):
    return retriever.run(nb, QUESTION, "", limits=ask_retrieval_limits(effort))


def _find(**request):
    return {"next_action": "find_sources", "reason": "按主题找论文",
            "find_sources": request}


def _cards(*sources, **extra):
    return {"next_action": "get_paper_cards", "reason": "读卡片",
            "get_paper_cards": {"sources": list(sources)}, **extra}


def _extract(sources, fields, **extra):
    return {"next_action": "extract_from_sources", "reason": "抽字段",
            "extract_from_sources": {"sources": list(sources), "fields": list(fields)},
            **extra}


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _skips(result):
    return {step.detail.get("reason"): step for step in _steps(result, "skip")}


# ------------------------------------------------------------ the full run
def test_a_full_run_finds_reads_cards_extracts_and_asks_for_the_paper_table(
    corpus, paper_model,
):
    """find → cards → extract → answer(answer_layout=paper_table), through the
    production seat: every service is the real one, and ``ReasoningResult``
    carries what synthesis (T6) consumes."""
    repo, nb, seeded = corpus
    llm = _RecordingLLM([
        _find(terms=TOPIC["terms"]),
        _cards("p1", "p2"),
        _extract(["p1"], ["COCO AP"]),
        dict(ANSWER, answer_layout="paper_table"),
    ])
    retriever = _retriever(repo, llm)
    assert isinstance(retriever.paper_tools, ReasoningPaperTools)
    result = _run(retriever, nb)

    # find_sources: one page, handles p1… over the detection papers.
    assert len(result.paper_finds) == 1
    found = result.paper_finds[0]
    strong = {seeded[key] for key in TOPIC["strong"]}
    assert found.handles[:2] == ("p1", "p2")
    assert strong <= {item.source_id for item in found.result.items}
    assert found.result.strong >= 2
    first_two = [item.source_id for item in found.result.items[:2]]

    # get_paper_cards: both generated lazily (the fake card model).
    assert [o.result.source_id for o in result.paper_cards] == first_two
    assert [o.handle for o in result.paper_cards] == ["p1", "p2"]
    assert all(o.result.status == "generated" and o.result.card for o in result.paper_cards)
    assert paper_model.card_calls == 2

    # extract_from_sources: reached through the reflect executor.
    assert paper_model.extraction_calls == 1
    (extraction,) = result.paper_extractions
    assert extraction.fields == ("COCO AP",)
    (item,) = extraction.items
    assert (item.handle, item.result.source_id) == ("p1", first_two[0])
    assert item.result.status == "extracted"
    assert item.result.fields[0].value and item.result.fields[0].element_id

    assert result.answer_layout == "paper_table"
    assert result.answer_layout_fallback == ""

    # Trace: one step per action, named apart from the action ids.
    (find_step,) = _steps(result, "find_documents")
    assert find_step.detail["listed"] == len(found.result.items)
    assert "result_ids" not in find_step.detail
    (cards_step,) = _steps(result, "paper_cards")
    assert cards_step.detail["found"] == 2 and cards_step.detail["generated"] == 2
    assert cards_step.detail["result_ids"]
    (extract_step,) = _steps(result, "extract_fields")
    assert extract_step.detail["found"] == 1
    assert extract_step.detail["result_ids"] == [item.result.fields[0].element_id]

    # Ledger feedback: the second turn sees the handles, the third the cards.
    assert "论文工具记录" not in llm.prompts[0]
    assert "find_sources 第 1 次" in llm.prompts[1] and "p1《" in llm.prompts[1]
    assert "get_paper_cards 结果:p1" in llm.prompts[2]
    assert "新生成卡片" in llm.prompts[2]
    assert "extract_from_sources 字段 [0]COCO AP:p1 [0]有值" in llm.prompts[3]
    assert "本次运行剩余额度:find_sources 1 次" in llm.prompts[3]
    # The schema carried the three branches and answer_layout every turn.
    schema = json.loads(llm.hints[0])
    assert {"find_sources", "get_paper_cards", "extract_from_sources",
            "answer_layout"} <= set(schema)


def test_an_out_of_scope_source_id_never_reaches_the_card_service(
    corpus, paper_model, monkeypatch,
):
    """A ``source_id`` outside the run's source ceiling (another notebook's
    paper) is refused at executor entry: the service never sees it, the trace
    records the refusal and the ledger tells the model why."""
    repo, nb, seeded = corpus
    other = repo.create_notebook(NotebookCreate(name="elsewhere")).id
    card_kit.seed_source(repo, other, "src-outside", doc_type="academic_paper")
    seen: list = []
    original = repo._runtime.paper_cards.get_cards

    def spy(items, **kwargs):
        seen.append((list(items), set(kwargs["allowed"])))
        return original(items, **kwargs)

    monkeypatch.setattr(repo._runtime.paper_cards, "get_cards", spy)
    inside = seeded["det-01"]
    llm = _RecordingLLM([_cards("src-outside"), _cards("src-outside", inside)])
    result = _run(_retriever(repo, llm), nb)

    assert "paper_cards_out_of_scope" in _skips(result)
    (call,) = seen
    assert call[0] == [(nb, inside)]
    assert (other, "src-outside") not in call[1]
    assert [o.result.source_id for o in result.paper_cards] == [inside]
    assert "src-outside 不在本次检索范围内" in llm.prompts[1]


def test_card_generation_reports_the_calls_it_reserved(corpus, paper_model):
    """``CardResult.calls``: one card call per generated paper, 0 on a cache hit
    -- the executor accumulates it against the run's generation budget."""
    repo, nb, seeded = corpus
    service = repo._runtime.paper_cards
    items = [(nb, seeded["det-01"])]
    (first,) = service.get_cards(items, allowed=items, generate_limit=3)
    (again,) = service.get_cards(items, allowed=items, generate_limit=3)
    assert (first.status, first.calls) == ("generated", 1)
    assert (again.status, again.calls) == ("ready", 0)


# ------------------------------------------------------------ stub services
class _StubFinder:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list = []

    def find(self, notebook_id, request, *, ceiling_binds=True,
             allowed_source_ids=None, cancel_event=None):
        assert not holding_fanout_slot()
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _found(notebook_id, source_id, title, tier="strong", rank=1):
    return FoundSource(
        rank=rank, source_id=source_id, notebook_id=notebook_id, title=title,
        pub_year=2024, venue="CVPR", authors=("A",), tier=tier,
        reasons=("title",), best_passage=BestPassage("c1", "text", ("e1",)),
        card_status="missing",
    )


def _result(items, *, cursor="", **extra):
    fields = dict(
        items=tuple(items), strong=len(items), related=0,
        filtered_scope_total=len(items), lexical_complete=True,
        semantic_recall_capped=False, next_cursor=cursor, lanes={},
    )
    fields.update(extra)
    return FindSourcesResult(**fields)


class _StubCards:
    def __init__(self, *, deferred_rounds=0, error=None):
        self.calls: list = []
        self.deferred_rounds = deferred_rounds
        self.error = error

    def get_cards(self, items, *, allowed, generate_limit, cancel_event=None, actor_id=""):
        assert not holding_fanout_slot()
        items = list(items)
        self.calls.append((items, generate_limit))
        if self.error is not None:
            raise self.error
        if self.deferred_rounds > 0:
            self.deferred_rounds -= 1
            return [CardResult(nb, sid, "deferred", reason="generate_limit") for nb, sid in items]
        return [
            CardResult(nb, sid, "generated", card={"tldr": {"text": "t", "anchors": [f"el-{sid}"]}},
                       calls=1)
            for nb, sid in items
        ]


class _StubExtraction:
    def __init__(self, error=None):
        self.calls: list = []
        self.error = error

    def extract(self, items, fields, *, allowed, limit, cancel_event=None, actor_id=""):
        assert not holding_fanout_slot()
        items = list(items)
        self.calls.append((items, tuple(fields), limit))
        if self.error is not None:
            raise self.error
        return [
            ExtractionResult(nb, sid, "extracted", fields=(
                FieldValue(0, fields[0], value="42.1", element_id=f"el-{sid}"),))
            if index < limit else ExtractionResult(nb, sid, "deferred", reason="limit")
            for index, (nb, sid) in enumerate(items)
        ]


def _stub_retriever(repo, llm, *, finder=None, cards=None, extraction=None):
    retriever = _retriever(repo, llm)
    retriever.paper_tools = ReasoningPaperTools(
        finder=finder, cards=cards, extraction=extraction)
    return retriever


def test_paging_counts_against_the_budget_and_closes_the_action(corpus):
    """standard: two find_sources calls per run, a page turn included. After
    the second the action leaves the prompt, the schema and the whitelist; a
    third request is a 'not offered' skip, never a third call."""
    repo, nb, seeded = corpus
    sid1, sid2 = seeded["det-01"], seeded["det-02"]
    finder = _StubFinder(
        _result([_found(nb, sid1, "Det one")], cursor="fs1.next"),
        _result([_found(nb, sid2, "Det two", rank=2)]),
    )
    llm = _RecordingLLM([
        _find(terms=["detector"]),
        _find(terms=["detector"], cursor="fs1.next"),
        _find(terms=["detector"]),
    ])
    result = _run(_stub_retriever(repo, llm, finder=finder), nb)

    assert [r.cursor for r in finder.requests] == ["", "fs1.next"]
    assert [f.handles for f in result.paper_finds] == [("p1",), ("p2",)]
    assert len(_steps(result, "find_documents")) == 2
    assert _skips(result)["action_not_offered"].detail["action"] == "find_sources"
    assert "把 find_sources.cursor 设为「fs1.next」" in llm.prompts[1]
    assert "- find_sources:" in llm.prompts[1]
    assert "- find_sources:" not in llm.prompts[2]
    assert "find_sources" not in json.loads(llm.hints[2])


def test_coverage_gaps_reach_the_ledger_and_forbid_a_complete_claim(corpus):
    repo, nb, seeded = corpus
    finder = _StubFinder(_result(
        [_found(nb, seeded["det-01"], "Det one")], lexical_complete=False,
        semantic_recall_capped=True, meta_pending_excluded=2, meta_pending_unchecked=3,
        skipped_libraries={"nb-peer": "semantic:timeout"},
    ))
    llm = _RecordingLLM([_find(terms=["detector"], query="object detection")])
    _run(_stub_retriever(repo, llm, finder=finder), nb)
    ledger = llm.prompts[1]
    assert "语义召回已触顶" in ledger
    assert "有 2 篇论文的元数据尚未抽取" in ledger
    assert "有 3 篇论文的元数据尚未抽取" in ledger
    assert "1 个参考库本次没有读全(语义检索超时)" in ledger
    assert "不得声称已列全" in ledger
    # A strong paper without a card is named as unexpanded.
    assert "已找到但还没取卡片的强相关文档:p1" in ledger


def test_terms_past_the_limit_are_dropped_and_named_not_the_whole_call(corpus):
    """T7 robotrl 的原样 payload(8 个 terms):真 finder 曾整次拒绝
    (`find_sources_invalid`),多花一轮 reflect。执行器按前 6 个执行并在账本里点名
    丢掉的两个;finder 自己的校验(MCP 也走它)不变。"""
    repo, nb, _seeded = corpus
    terms = ["reinforcement learning", "robot manipulation", "dexterous manipulation",
             "offline reinforcement learning", "hierarchical reinforcement learning",
             "reward learning", "visuomotor manipulation", "sim-to-real grasping"]
    llm = _RecordingLLM([_find(
        query="reinforcement learning for robot manipulation", terms=terms,
        kind="papers", scope="current_notebook")])
    result = _run(_retriever(repo, llm), nb)
    assert "find_sources_invalid" not in _skips(result)
    (found,) = result.paper_finds
    assert found.request.terms == tuple(terms[:6])
    assert found.result.items
    ledger = llm.prompts[1]
    assert "find_sources.terms 最多 6 个,这次按前 6 个执行" in ledger
    assert "「visuomotor manipulation」、「sim-to-real grasping」" in ledger


@pytest.mark.parametrize("error, reason", [
    (FindSourcesCursorError("the cursor belongs to other parameters"),
     "find_sources_cursor_invalid"),
    (FindSourcesError("at most 6 terms"), "find_sources_invalid"),
    (RuntimeError("boom"), "paper_tool_error"),
])
def test_finder_errors_become_ledger_notes_and_the_run_goes_on(corpus, error, reason):
    repo, nb, _seeded = corpus
    llm = _RecordingLLM([_find(terms=["detector"])])
    result = _run(_stub_retriever(repo, llm, finder=_StubFinder(error)), nb)
    assert reason in _skips(result)
    assert result.paper_finds == []
    assert "论文工具记录" in llm.prompts[1] and "未执行:" in llm.prompts[1]
    assert _steps(result, "answer")


def test_extraction_request_errors_and_per_run_counting(corpus):
    """An over-long field is refused before the service; a refusal FROM the
    service is a note too; the per-run count charges only admitted sources."""
    repo, nb, seeded = corpus
    sids = [seeded[f"det-0{i}"] for i in range(1, 5)]
    extraction = _StubExtraction()
    llm = _RecordingLLM([
        _extract(sids[:1], ["x" * 81]),
        _extract(sids, ["COCO AP"]),
        _extract(sids, ["mAP"]),
    ])
    result = _run(_stub_retriever(repo, llm, extraction=extraction), nb)
    assert "extract_invalid" in _skips(result)
    # standard: 6 extractions per run → the second call gets limit 4, the
    # third the 2 left (the service defers the rest).
    assert [call[2] for call in extraction.calls] == [4, 2]
    assert "推迟(额度已用完)" in llm.prompts[3]
    assert "extract_unavailable" not in _skips(result)

    failing = _StubExtraction(error=ExtractionRequestError("at most 8 sources"))
    llm = _RecordingLLM([_extract(sids[:1], ["COCO AP"])])
    result = _run(_stub_retriever(repo, llm, extraction=failing), nb)
    assert "extract_invalid" in _skips(result)
    assert "请求不合法:at most 8 sources" in llm.prompts[1]


def test_cards_per_call_cap_dedupe_and_generation_budget(corpus):
    repo, nb, seeded = corpus
    sids = [seeded[key] for key in sorted(seeded)][:12]
    cards = _StubCards()
    llm = _RecordingLLM([_cards(*sids), _cards(*sids[:3])])
    result = _run(_stub_retriever(repo, llm, cards=cards), nb)
    # One call of at most 10; the repeat of already-held papers is not re-sent.
    assert len(cards.calls) == 1
    assert len(cards.calls[0][0]) == REASONING_PAPER_CARDS_PER_CALL
    assert cards.calls[0][1] == ask_retrieval_limits("standard").paper_card_generations_per_run
    assert "这次没有请求" in llm.prompts[1]
    assert "已有结果,未重复请求" in llm.prompts[2]
    # Everything asked for already has a result: a repeat note, not "fill in handles".
    assert "paper_cards_repeat" in _skips(result)
    assert "都已经有结果,不必再请求" in llm.prompts[2]


def test_answer_layout_without_results_falls_back_to_prose(corpus):
    repo, nb, _seeded = corpus
    llm = _RecordingLLM([
        {"next_action": "add_subquery", "reason": "再查",
         "new_sub_query": {"query": "detector"}, "answer_layout": "paper_table"},
        ANSWER,
    ])
    result = _run(_stub_retriever(repo, llm, cards=_StubCards()), nb)
    assert result.answer_layout == "prose"
    assert result.answer_layout_fallback == ANSWER_LAYOUT_NO_RESULTS
    assert _skips(result)["answer_layout_fallback"].detail["requested"] == "paper_table"
    assert "answer_layout=paper_table 目前不会生效" in llm.prompts[1]


def test_the_last_written_layout_wins(corpus):
    repo, nb, seeded = corpus
    llm = _RecordingLLM([
        _cards(seeded["det-01"], answer_layout="paper_table"),
        dict(ANSWER, answer_layout="prose"),
    ])
    result = _run(_stub_retriever(repo, llm, cards=_StubCards()), nb)
    assert result.answer_layout == "prose" and result.answer_layout_fallback == ""


# ------------------------------------------------------------------ gates
def _hint_words(llm) -> set:
    return set(json.loads(llm.hints[0]))


@pytest.mark.parametrize("close", ["seat", "policy", "switches", "no_enumeration"])
def test_every_gate_condition_closes_the_tools(corpus, close):
    repo, nb, _seeded = corpus
    llm = _RecordingLLM([])
    retriever = _stub_retriever(repo, llm, finder=_StubFinder(), cards=_StubCards(),
                                extraction=_StubExtraction())
    if close == "seat":
        retriever.paper_tools = None
    elif close == "policy":
        retriever.allow_paper_tools = False
    elif close == "switches":
        repo.settings.reasoning_find_sources_enabled = False
        repo.settings.reasoning_paper_cards_enabled = False
        repo.settings.reasoning_extract_enabled = False
    else:
        retriever.collection_enumeration = None
    assert retriever.paper_tool_flags(ask_retrieval_limits("standard")) == (False, False, False)
    _run(retriever, nb)
    words = _hint_words(llm)
    assert not words & {"find_sources", "get_paper_cards", "extract_from_sources",
                        "answer_layout"}
    assert "find_sources" not in llm.prompts[0]


def test_each_switch_and_the_overview_tier_close_one_tool(corpus):
    repo, nb, _seeded = corpus
    retriever = _stub_retriever(repo, _RecordingLLM([]), finder=_StubFinder(),
                                cards=_StubCards(), extraction=_StubExtraction())
    standard = ask_retrieval_limits("standard")
    assert retriever.paper_tool_flags(standard) == (True, True, True)
    # overview has no extraction budget.
    assert retriever.paper_tool_flags(ask_retrieval_limits("overview")) == (True, True, False)
    repo.settings.reasoning_paper_cards_enabled = False
    assert retriever.paper_tool_flags(standard) == (True, False, True)
    assert retriever.paper_tool_flags(standard, frozenset({"find_sources"})) == (
        False, False, True)
    retriever.paper_tools = replace(retriever.paper_tools, extraction=None)
    assert retriever.paper_tool_flags(standard) == (True, False, False)


def test_overview_offers_no_extraction_and_withholds_it(corpus):
    repo, nb, seeded = corpus
    extraction = _StubExtraction()
    llm = _RecordingLLM([_extract([seeded["det-01"]], ["COCO AP"])])
    result = _run(_stub_retriever(repo, llm, extraction=extraction, cards=_StubCards()),
                  nb, effort="overview")
    assert extraction.calls == []
    assert _skips(result)["action_not_offered"].detail["action"] == "extract_from_sources"
    assert "extract_from_sources" not in _hint_words(llm)
    assert "answer_layout" in _hint_words(llm)  # the card gate is still open


def test_no_local_source_ticked_but_a_reference_library_keeps_the_tools(corpus):
    """Only a reference library ticked (legal, ``ask_routes._scope_is_empty``):
    the tools stay offered and find_sources reaches the library's paper. The
    active notebook's own empty ceiling is not a reason to close them."""
    from app.services.source_scope import source_scope_context
    from tests.paper_topics_testkit import seed_paper_topics

    repo, _corpus_nb, _seeded = corpus
    bare = repo.create_notebook(NotebookCreate(name="只挂参考库")).id
    ref = repo.create_notebook(NotebookCreate(name="参考库")).id
    ref_ids = seed_paper_topics(repo, ref, keys=["det-01"], prefix="ref-")
    repo.replace_notebook_bases(bare, [ref], repo.current_user().id)
    llm = _RecordingLLM([_find(terms=TOPIC["terms"])])
    with source_scope_context(bare, {"mode": "include", "source_ids": [], "narrowed": True}):
        result = _run(_retriever(repo, llm), bare)
    assert {"find_sources", "get_paper_cards"} <= _hint_words(llm)
    (found,) = result.paper_finds
    assert [(i.notebook_id, i.source_id) for i in found.result.items] == [(ref, ref_ids["det-01"])]


def test_a_truly_empty_run_finds_nothing_and_does_not_crash(corpus):
    """No source at all in scope: find_sources answers 0 with its coverage,
    a card request for a handle-less id is an out-of-scope note."""
    repo, _corpus_nb, _seeded = corpus
    bare = repo.create_notebook(NotebookCreate(name="空库")).id
    llm = _RecordingLLM([_find(terms=["detector"]), _cards("pt-det-01")])
    result = _run(_retriever(repo, llm), bare)
    (found,) = result.paper_finds
    assert found.result.items == () and found.result.filtered_scope_total == 0
    assert "paper_cards_out_of_scope" in _skips(result)
    assert "本页没有命中" in llm.prompts[1]
    assert _steps(result, "answer")


# ---------------------------------------------------------------- parsing
class _OneShot:
    configured = True

    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps(self.payload)


def _reflect(repo, payload, *, fail_closed=False):
    retriever = _stub_retriever(repo, _RecordingLLM([]), finder=_StubFinder(),
                                cards=_StubCards(), extraction=_StubExtraction())
    bind_chat_client(repo, "reasoning_agent", _OneShot(payload))
    retriever.fail_closed = fail_closed
    return retriever.reflect("q", "c", limits=ask_retrieval_limits("standard"))


def test_enum_values_fall_back_and_strings_are_kept(corpus):
    repo, _nb, _seeded = corpus
    decision = _reflect(repo, _find(
        terms=["GAN", " ", 3], query="  gan  ", year_from=2020, year_to="x",
        kind="everything", scope="nowhere", venue="CVPR"))
    assert decision.next_action == "find_sources"
    assert decision.find_sources_terms == ("GAN",)
    assert decision.find_sources_query == "gan"
    assert (decision.find_sources_year_from, decision.find_sources_year_to) == ("2020", "x")
    assert (decision.find_sources_kind, decision.find_sources_scope) == ("papers", "all")
    assert decision.find_sources_venue == "CVPR"
    layout = _reflect(repo, dict(_cards("p1"), answer_layout="TABLE"))
    assert layout.paper_card_sources == ("p1",) and layout.answer_layout == ""
    layout = _reflect(repo, dict(_cards("p1"), answer_layout="Paper_Table"))
    assert layout.answer_layout == "paper_table"


@pytest.mark.parametrize("payload, missing", [
    ({"next_action": "find_sources", "find_sources": {}}, "query"),
    ({"next_action": "get_paper_cards", "get_paper_cards": {"sources": []}}, "sources"),
    ({"next_action": "extract_from_sources",
      "extract_from_sources": {"sources": ["p1"], "fields": []}}, "fields"),
])
def test_fail_closed_refuses_a_call_missing_its_required_argument(corpus, payload, missing):
    repo, _nb, _seeded = corpus
    with pytest.raises(ValueError, match=f"is missing {missing}"):
        _reflect(repo, payload, fail_closed=True)


def test_fail_closed_bounds_paper_tool_free_text(corpus):
    repo, _nb, _seeded = corpus
    with pytest.raises(ValueError, match="too long"):
        _reflect(repo, _extract(["p1"], ["x" * 2001]), fail_closed=True)


def test_a_closed_tool_is_withheld_not_an_invalid_action(corpus):
    repo, _nb, _seeded = corpus
    retriever = _stub_retriever(repo, _RecordingLLM([]), cards=_StubCards())
    bind_chat_client(repo, "reasoning_agent", _OneShot(dict(
        _find(terms=["GAN"]), answer_layout="paper_table")))
    decision = retriever.reflect("q", "c", limits=ask_retrieval_limits("standard"))
    assert decision.withheld and decision.next_action == "find_sources"
    # answer_layout is read on a withheld turn too (the card gate is open).
    assert decision.answer_layout == "paper_table"


# ------------------------------------------------------------- pure parts
def test_resolve_source_tokens_checks_handles_and_ids_against_the_ceiling():
    handles = PaperHandles()
    assert handles.issue("nb", "s1", "One") == ("p1", True)
    assert handles.issue("nb", "s1", "One") == ("p1", False)
    assert handles.issue("ref", "s2", "Two") == ("p2", True)
    allowed = frozenset({("nb", "s1"), ("ref", "s2"), ("nb", "s3")})
    resolved, rejected = resolve_source_tokens(
        ["P1", "s3", "p9", "s-out", "s1", "p2", ""], handles, allowed)
    assert [(r.handle, r.key) for r in resolved] == [
        ("p1", ("nb", "s1")), ("", ("nb", "s3")), ("p2", ("ref", "s2"))]
    assert [(r.token, r.reason) for r in rejected] == [
        ("p9", "unknown_handle"), ("s-out", "out_of_scope")]
    # A handle whose source left the ceiling is refused too.
    resolved, rejected = resolve_source_tokens(["p2"], handles, frozenset({("nb", "s1")}))
    assert resolved == [] and rejected[0].reason == "out_of_scope"


def test_resolve_answer_layout():
    assert resolve_answer_layout("", True) == ("prose", "")
    assert resolve_answer_layout("prose", True) == ("prose", "")
    assert resolve_answer_layout("paper_table", True) == ("paper_table", "")
    assert resolve_answer_layout("paper_table", False) == ("prose", ANSWER_LAYOUT_NO_RESULTS)


def test_policy_per_call_caps_match_the_services():
    policy = reasoning_action_policy(SimpleNamespace())
    assert policy.max_paper_cards_per_call == REASONING_PAPER_CARDS_PER_CALL == 10
    assert policy.max_extract_sources_per_call == REASONING_EXTRACT_SOURCES_PER_CALL
    assert REASONING_EXTRACT_SOURCES_PER_CALL == EXTRACT_MAX_SOURCES


def test_the_three_actions_are_core_ids():
    assert {"find_sources", "get_paper_cards", "extract_from_sources"} <= CORE_REFLECT_ACTION_IDS


# ------------------------------------------------------ review round fixes
def test_answer_layout_stays_writable_after_the_card_budget_is_spent(corpus):
    """overview: 6 card reads per run, no extraction. Reading 6 spends the card
    budget, so get_paper_cards leaves the schema -- but the answering turn can
    still ask for the table (the gate is the UNSPENT one)."""
    repo, nb, seeded = corpus
    sids = [seeded[key] for key in sorted(seeded)][:6]
    llm = _RecordingLLM([_cards(*sids), dict(ANSWER, answer_layout="paper_table")])
    result = _run(_stub_retriever(repo, llm, cards=_StubCards()), nb, effort="overview")
    second = json.loads(llm.hints[1])
    assert "get_paper_cards" not in second and second["answer_layout"] == "prose|paper_table"
    assert "- answer_layout (a top-level field" in llm.prompts[1]
    assert result.answer_layout == "paper_table"


def test_deferred_only_steps_are_not_zero_hits(corpus):
    """All results deferred: no ``found`` key, so the experience projection
    reads no count (not a zero hit); ``deferred`` is reported instead."""
    from app.domain.retrieval_experience import project_trace_step

    repo, nb, seeded = corpus
    sid = seeded["det-01"]
    llm = _RecordingLLM([_cards(sid), _extract([sid], ["COCO AP"])])
    extraction = _StubExtraction()
    extraction.extract = lambda items, fields, **kw: [
        ExtractionResult(n, s, "deferred", reason="model_unavailable") for n, s in items]
    result = _run(_stub_retriever(repo, llm, cards=_StubCards(deferred_rounds=1),
                                  extraction=extraction), nb)
    for step_type in ("paper_cards", "extract_fields"):
        (step,) = _steps(result, step_type)
        assert "found" not in step.detail and step.detail["deferred"] == 1
        projected = project_trace_step({"step_type": step_type, "summary": "",
                                        "detail": step.detail})
        assert projected["count"] is None


def test_deferred_turns_keep_the_stale_counter_flat(corpus):
    """Two consecutive turns whose cards were all deferred (the ledger tells
    the model to retry) do not trip the stale breaker (pinned at 2 here)."""
    repo, nb, seeded = corpus
    repo.settings.reasoning_stale_limit = 2
    sid = seeded["det-01"]
    cards = _StubCards(deferred_rounds=2)
    llm = _RecordingLLM([_cards(sid), _cards(sid), _cards(sid)])
    result = _run(_stub_retriever(repo, llm, cards=cards), nb, effort="deep")
    assert "stale_circuit_breaker" not in _skips(result)
    assert len(cards.calls) == 3
    assert result.paper_cards[0].result.status == "generated"


def test_a_deferred_card_is_not_charged_twice_against_the_read_budget(corpus):
    """overview: 6 reads. 6 deferred, then the same 6 again: the retry is the
    first charge, so the budget is spent only now (not after the first call)."""
    repo, nb, seeded = corpus
    sids = [seeded[key] for key in sorted(seeded)][:6]
    cards = _StubCards(deferred_rounds=1)
    llm = _RecordingLLM([_cards(*sids), _cards(*sids)])
    result = _run(_stub_retriever(repo, llm, cards=cards), nb, effort="overview")
    assert [len(call[0]) for call in cards.calls] == [6, 6]
    assert "get_paper_cards" in json.loads(llm.hints[1])
    assert "get_paper_cards" not in json.loads(llm.hints[2])
    assert all(o.result.status == "generated" for o in result.paper_cards)


def test_a_repeated_extraction_costs_no_budget_and_no_call(corpus):
    repo, nb, seeded = corpus
    sid = seeded["det-01"]
    extraction = _StubExtraction()
    llm = _RecordingLLM([_extract([sid], ["COCO AP"]), _extract([sid], ["coco ap"]),
                         _extract([sid], ["mAP"])])
    result = _run(_stub_retriever(repo, llm, extraction=extraction), nb)
    assert [call[1] for call in extraction.calls] == [("COCO AP",), ("mAP",)]
    assert "extract_repeat" in _skips(result)
    assert "这组字段本次已经抽过" in llm.prompts[2]
    assert "extract_from_sources 4 篇" in llm.prompts[3]  # 6 - 2: the repeat was free


@pytest.mark.parametrize("tool", ["finder", "cards", "extraction"])
def test_cancellation_inside_a_paper_service_stops_the_run(corpus, tool):
    from app.services.cancellation import AskCancelled

    repo, nb, seeded = corpus
    sid = seeded["det-01"]
    stubs = {"finder": _StubFinder(AskCancelled()),
             "cards": _StubCards(error=AskCancelled()),
             "extraction": _StubExtraction(error=AskCancelled())}
    action = {"finder": _find(terms=["detector"]), "cards": _cards(sid),
              "extraction": _extract([sid], ["COCO AP"])}[tool]
    retriever = _stub_retriever(repo, _RecordingLLM([action]), **{tool: stubs[tool]})
    with pytest.raises(AskCancelled):
        _run(retriever, nb)
