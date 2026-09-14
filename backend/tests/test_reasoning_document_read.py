"""逐步推理接入 `read_document`(按篇读取有界原文取样,PR-A T3/T4)。

覆盖**接入面**:三处投影(prompt / schema / allowed_actions)与字段解析共读同一把
闸、动作分发与 trace、六条 skip、两个 run 级预算池、回喂账目、参考库归属。取样
执行体本身的合同在 ``test_document_overview`` / ``test_document_source_overview``,
这里不重测它。

reflect 的参数一律经**生产的那两道校验**(``_ValidatingLLM``):`coverage` 是一个
字符串枚举,而 `model_json._validate_against_example` 会在解析器之前先按 schema
示例判一次形状——只测解析器的用例会给出「模型这样填是可以的」这个结论,而生产上
那一轮早就被前一道打成兜底了。
"""
from __future__ import annotations

import json

import pytest

from app.models.schemas import NotebookCreate
from app.services import reasoning_retrieval as rr_module
from app.services.collection_enumeration import (
    SOURCE_ROW_FIELD_SEPARATOR,
    UNNAMED_SOURCE_LABEL,
)
from app.services.reasoning_retrieval import (
    READ_DOCUMENT_ACTION,
    ReasoningRetriever,
)
from tests.model_testkit import bind_chat_client
from tests.test_reasoning_enumeration_tools import (  # noqa: F401
    _ValidatingLLM,
    _enumerate_sources_action,
    _skips,
    _steps,
    repo,  # pytest fixture, resolved by name
)


NOW = "2026-09-14T00:00:00+08:00"

ANSWER = {"next_action": "answer", "sufficient": True}


def _seed_document(repo, notebook_id, source_id, title, summary, texts):
    """一篇文档 + 它的原文元素(镜像 ``test_document_overview.seed``)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,summary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "markdown", "parsed", "parsed",
             summary, NOW, NOW),
        )
        for index, text in enumerate(texts):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-{index:03}", source_id, "paragraph",
                 f"第{index + 1}节", text,
                 json.dumps({"section_path": f"第{index + 1}节"}), NOW),
            )
    repo.collection_catalog.invalidate()


def _read_action(source, reason="读这一篇", **extra):
    request = {"source": source}
    request.update(extra)
    return {"next_action": READ_DOCUMENT_ACTION, "read_document": request,
            "reason": reason}


def _reader(repo, llm, *, wire_sources=True):
    """接好 `sources` 座位的 retriever(T5 才在 ask_service 侧接线)。"""
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    if wire_sources:
        retriever.sources = repo._runtime.source_store
        retriever.source_generation = (
            repo._runtime.content_tools.source_generation)
    return retriever


def _notebook_with_documents(repo):
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-empty", "无摘要文档", "", (
        "开头:这篇讲的是版图设计的取样方法。",
        "中段:实验设置与参数。",
        "末尾结论:该方法优于既有做法。",
    ))
    _seed_document(repo, notebook.id, "s-summed", "有摘要文档", "已存摘要", (
        "另一篇的开头。", "另一篇的末尾。",
    ))
    return notebook


# --------------------------------------------------------------- 正常路径


def test_enumerate_then_read_document_then_answer(repo):  # noqa: F811
    """①`enumerate sources → read_document → answer` 的完整一轮。

    detail 带显示标题、命中数与 `result_ids`(含**末元素**——等距取样必含末位是
    执行体的合同,这里钉住它真的传到了轨迹上);产物带着末尾结论进
    `result.document_reads`;第二轮 reflect prompt 带上账目与本轮次数上限。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库的文档分别讲了什么", "")

    step = next(s for s in _steps(result, "read_document"))
    assert step.detail["source"] == "无摘要文档"
    assert step.detail["coverage"] == "spread"
    assert step.detail["found"] == 3
    assert step.detail["result_ids"] == [
        "s-empty-000", "s-empty-001", "s-empty-002"]
    # 末元素在场:等距取样必含末位,这是「文档结论常在最后一段」的承重合同。
    assert "s-empty-002" in step.detail["result_ids"]

    assert len(result.document_reads) == 1
    outcome = result.document_reads[0]
    assert outcome.source_id == "s-empty"
    assert outcome.summary_was_empty is True
    assert "末尾结论" in outcome.context_block
    assert outcome.citations and outcome.coverage_note

    # 回喂账目 + 次数上限都在**第二轮**的 reflect prompt 里。
    assert "勿重复请求" in llm.reflect_prompts[2]
    assert "《无摘要文档》" in llm.reflect_prompts[2]
    assert "at most 4 document(s)" in llm.reflect_prompts[2]


def test_document_read_counts_as_progress_not_as_a_stale_turn(repo):  # noqa: F811
    """按篇取样不进候选池,但它带来了新材料:那一轮不得被记成「无进展」。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    reflects = _steps(result, "reflect")
    # 取样那一轮之后的反思步:stale 必须是 0(被清零),no_progress 为假。
    assert reflects[-1].detail["stale"] == 0
    assert reflects[-1].detail["no_progress"] is False


def test_opening_coverage_reads_only_the_beginning(repo):  # noqa: F811
    """③ `coverage:"opening"` 只取开头,不含末元素。"""
    notebook = _notebook_with_documents(repo)
    repo.settings.document_overview_max_elements = 2
    repo.settings.reasoning_max_document_reads = 1
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档", coverage="opening"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    step = next(s for s in _steps(result, "read_document"))
    assert step.detail["coverage"] == "opening"
    assert step.detail["result_ids"] == ["s-empty-000", "s-empty-001"]
    assert "末尾结论" not in result.document_reads[0].context_block


@pytest.mark.parametrize("action", [
    _read_action("无摘要文档"),                    # coverage 字段整个缺省
    _read_action("无摘要文档", coverage=""),        # 显式留空(F1 的宽容规则)
])
def test_an_absent_or_empty_coverage_lands_as_spread_and_keeps_the_turn(
    repo, action,  # noqa: F811
):
    """③ 缺省与空串都落地 spread,并且**不**废掉这一轮。

    这是「字符串枚举而不是布尔」在生产上的承重判据,所以必须经 `_ValidatingLLM`:
    布尔示例会让空串直接撞上 `invalid_boolean`,整轮反思掉进 fail-open 兜底;字符串
    示例继承 F1 的宽容规则(空串永远接受),不动这个旋钮的模型照样能把动作落地。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(), action, ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    step = next(s for s in _steps(result, "read_document"))
    assert step.detail["coverage"] == "spread"
    assert step.detail["found"] == 3
    assert not [s for s in _steps(result, "reflect")
                if "fallback_reason" in s.detail]


@pytest.mark.parametrize("coverage", ["true", "SPREAD", "全文"])
def test_a_non_empty_illegal_coverage_costs_the_turn_at_the_validation_layer(
    repo, coverage,  # noqa: F811
):
    """③ 非空非法值在**解析器之前**就被校验层拒掉(`invalid_enum`)。

    这条用例的价值是钉住那个代价落在哪一层:与 `enumerate.scope`/`direction` 同
    一条既有合同——`_validate_against_example` 对含 `|` 的示例按封闭集判,非空的
    集外值废掉整轮反思。只测解析器的话会得出「模型这样填也可以」这个在生产上不
    成立的结论。解析器那一侧的落回规则由下面那条纵深防御用例单独钉。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档", coverage=coverage),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert _steps(result, "read_document") == []
    assert [s.detail["fallback_reason"] for s in _steps(result, "reflect")
            if "fallback_reason" in s.detail] == ["invalid_enum"]


@pytest.mark.parametrize("coverage", ["true", "SPREAD", "全文", 7, None])
def test_the_parser_itself_falls_back_to_spread_and_never_raises(repo, coverage):  # noqa: F811
    """③ 纵深防御:绕过校验层(畸形响应/替身)时,解析器落回 spread 且不抛。

    非法值一律落回默认、**不留**被拒原值:非空非法值在校验层就按封闭枚举整轮拒掉
    (上面那条用例),到得了解析器的只有缺省/空串/非字符串,而那不是「给错了」,没有
    教学文案要说。`fail_closed` 也不抛——取样形状不是动作合法性问题。
    """
    from tests.test_reasoning_enumeration_tools import _SeqLLM

    notebook = _notebook_with_documents(repo)
    llm = _SeqLLM([_read_action("无摘要文档", coverage=coverage)],
                  plan={"sub_queries": [{"query": "版图设计"}]})
    retriever = _reader(repo, llm)
    retriever.fail_closed = True
    # 这个动作的总闸只要求**枚举工具**在场(清单要不要真的列过是执行体那一层的
    # `document_read_no_roster` 判据),所以这里不必先跑一轮 run 就已经成立。
    assert retriever.document_read_active() is True

    decision = retriever.reflect("q", "candidates")

    assert decision.next_action == READ_DOCUMENT_ACTION
    assert decision.read_document_coverage == "spread"
    assert not hasattr(decision, "read_document_coverage_rejected")
    assert notebook is not None


def test_reference_library_source_carries_its_own_notebook_id(repo):  # noqa: F811
    """⑥ 参考库来源:Citation.notebook_id 是那个参考库的 id,不是活动库、不是空。"""
    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _seed_document(repo, reference.id, "s-ref", "参考库文档", "", (
        "参考库开头。", "参考库末尾。",
    ))
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()

    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("参考库文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "参考"}]})
    result = _reader(repo, llm).run(active.id, "这个库讲了什么", "")

    outcome = result.document_reads[0]
    assert outcome.notebook_id == reference.id
    assert [c.notebook_id for c in outcome.citations] == [reference.id] * len(
        outcome.citations)
    assert all(entry["notebook_id"] == reference.id
               for entry in outcome.id_map.values())


# ------------------------------------------------------------------ 六条 skip


def test_skip_when_no_roster_has_been_listed(repo):  # noqa: F811
    """② 没列过清单就直接读 → 教模型先列清单,零 I/O。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    skip = _skips(result)["document_read_no_roster"]
    assert "先列出清单" in skip.summary
    assert "result_ids" not in skip.detail
    assert result.document_reads == []


@pytest.mark.parametrize("title", ["不存在的文档", "无摘要", ""])
def test_skip_when_the_title_resolves_to_no_document(repo, title):  # noqa: F811
    """② 标题不在清单里(含**子串**不算命中)→ unresolved,detail 不带内部 id。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action(title),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    skip = _skips(result)["document_read_unresolved"]
    assert skip.detail["requested_title"] == title
    assert skip.detail["matches"] == 0
    assert "source_id" not in skip.detail
    assert "s-empty" not in json.dumps(skip.detail, ensure_ascii=False)
    assert "result_ids" not in skip.detail


def test_an_untitled_document_is_readable_by_its_roster_placeholder(repo):  # noqa: F811
    """② 无标题文档按花名册显示的占位串「未命名来源」读得到。

    花名册预览行与结果卡都把没有显示名的文档渲染成
    `UNNAMED_SOURCE_LABEL`,而动作的指令是「逐字复制清单里的标题」。解析侧若拿
    库里的空标题去比,模型逐字照做反而永远匹配不上——那一篇于是「列得出来、
    却永远读不到」,而没有标题的文档通常也没有摘要,正是这个动作最该覆盖的一行。
    """
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-anon", "", "", (
        "无名文档的开头。", "无名文档的末尾结论。",
    ))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action(UNNAMED_SOURCE_LABEL),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert "document_read_unresolved" not in _skips(result)
    assert len(result.document_reads) == 1
    assert result.document_reads[0].source_id == "s-anon"
    step = _steps(result, "read_document")[0]
    assert step.detail["found"] == 2
    # 花名册预览行渲染的也是同一个占位串(两处口径一分叉这条路就断了)。
    assert UNNAMED_SOURCE_LABEL in llm.reflect_prompts[1]


def test_a_whole_roster_line_copied_back_still_resolves_exactly(repo):  # noqa: F811
    """② 模型把**整行**抄回来(`标题 · 类型: 摘要`)时,按第一个 ` · ` 之前的部分
    再精确匹配一次。

    指令说的是「逐字复制标题」,而它看到的那一行还带着类型与摘要——两者差一步,
    模型会踩。兜底仍然是**确定性精确匹配**:切出来的前缀与真标题不相等就照样
    0 命中,不做任何模糊化。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action(f"无摘要文档{SOURCE_ROW_FIELD_SEPARATOR}Markdown: 暂无已存摘要"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert "document_read_unresolved" not in _skips(result)
    assert [o.source_id for o in result.document_reads] == ["s-empty"]


def test_a_title_wrapped_in_book_marks_still_resolves_exactly(repo):  # noqa: F811
    """③ 模型把标题包在书名号里(`《无摘要文档》`)——真模型抽问里的第一次
    unresolved 就是这个形状,而回喂账目自己就用书名号括标题。只剥**一对**成对的
    包裹符再精确匹配;剥完仍不相等就照样 unresolved,不做模糊化。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("《无摘要文档》"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert "document_read_unresolved" not in _skips(result)
    assert [o.source_id for o in result.document_reads] == ["s-empty"]


def test_book_marks_around_a_wrong_title_are_not_a_fuzzy_match(repo):  # noqa: F811
    """③ 剥包裹符不是模糊匹配:`《无摘要》` 剥完仍与任何标题不等 → unresolved。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("《无摘要》"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert _skips(result)["document_read_unresolved"].detail["matches"] == 0


def test_the_whole_line_fallback_stays_exact_and_never_guesses(repo):  # noqa: F811
    """② 前缀兜底不是模糊匹配:切出来的头与任何标题都不等时照样 unresolved。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action(f"无摘要{SOURCE_ROW_FIELD_SEPARATOR}Markdown"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert _skips(result)["document_read_unresolved"].detail["matches"] == 0
    assert result.document_reads == []


def test_a_title_that_itself_contains_the_separator_matches_whole(repo):  # noqa: F811
    """② 标题本身含 ` · ` 的文档不受兜底影响:整串精确匹配先命中,走不到切分。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    title = f"上篇{SOURCE_ROW_FIELD_SEPARATOR}下篇"
    _seed_document(repo, notebook.id, "s-dot", title, "", ("正文。",))
    _seed_document(repo, notebook.id, "s-head", "上篇", "", ("另一篇正文。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action(title),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert [o.source_id for o in result.document_reads] == ["s-dot"]


def test_skip_when_two_documents_share_the_title(repo):  # noqa: F811
    """② 同名两篇 → 同样 unresolved(服务端绝不替模型挑一篇)。

    ⑥ 文案与「一篇都没匹配上」分叉:0 篇是抄错了标题,重抄能成;≥2 篇是这个动作
    的参数(标题)根本区分不开它们,再教它「逐字复制标题」只会让模型把剩下的轮数
    花在同一个死循环上。
    """
    notebook = _notebook_with_documents(repo)
    _seed_document(repo, notebook.id, "s-dup", "无摘要文档", "", ("重名的一篇。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    skip = _skips(result)["document_read_unresolved"]
    assert skip.detail["matches"] == 2
    assert "清单里有 2 篇同名文档" in skip.summary
    assert "无法区分" in skip.summary
    assert "逐字复制标题" not in skip.summary
    assert result.document_reads == []


def test_the_roster_is_deduplicated_before_the_title_is_matched(repo):  # noqa: F811
    """③ 同一篇被两条范围不同的清单链各列一次,仍然读得到。

    「先只列本库、后要全部」是**新开一条链**(范围在续跑键里),所以同一个
    `source_id` 会在 `state.enumerations` 里出现两次。花名册不先按 source_id 折叠
    的话,它会被当成「两篇同名」而误判成解析不出唯一目标——一次完全合理的
    「先看本库、再看全部、然后读这一篇」会被服务端拒掉。
    """
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _enumerate_sources_action(scope="current_notebook"),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    # 前提:同一篇确实被两条链各列了一次。
    listed = [outcome for outcome in result.enumerations
              if outcome.collection == "sources"]
    assert len(listed) == 2
    assert sum(row.source_id == "s-empty"
               for outcome in listed for row in outcome.items) == 2
    # 结论:读取照常成立,没有掉进 unresolved。
    assert "document_read_unresolved" not in _skips(result)
    assert [o.source_id for o in result.document_reads] == ["s-empty"]


def test_skip_when_the_same_document_is_requested_twice(repo):  # noqa: F811
    """② 同一篇读两次 → repeat(只会烧预算、把同一批取样再送一遍)。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert len(_steps(result, "read_document")) == 1
    assert "document_read_repeat" in _skips(result)
    assert len(result.document_reads) == 1


def test_skip_when_the_per_run_cap_is_reached(repo):  # noqa: F811
    """② 次数上限:cap skip 报的 N 与 prompt 里那句是同一个数。"""
    notebook = _notebook_with_documents(repo)
    repo.settings.reasoning_max_document_reads = 1
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        _read_action("有摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    skip = _skips(result)["document_read_cap"]
    assert "1" in skip.summary
    assert "at most 1 document(s)" in llm.reflect_prompts[0]
    assert len(_steps(result, "read_document")) == 1


def test_skip_when_the_sampling_budget_is_exhausted(repo):  # noqa: F811
    """② 预算池耗尽 → budget skip,零 I/O(份额算出来 <= 0 就不发起读取)。

    总闸的「首读份额可行」判据之后,这条 skip 是**纵深防御**:配置层面的不可行
    (池太小、次数太多)已经让整个动作不提供了,而按份额切池的算术保证正常流程里
    每一次读都还剩得下一份。所以这里直接驱动执行体、把 run 级账目置成耗尽态——
    夹子被拿掉时会变成一次 `max_elements=0` 的读,而那是执行体合同外的输入。
    """
    from app.services.collection_enumeration import SourceItem

    notebook = _notebook_with_documents(repo)
    retriever = _reader(repo, _ValidatingLLM([]))
    state = retriever._new_run_state(
        notebook.id, "q", "", None,
        max_steps=3, intent_queries=None, limits=None, intent_detail=None)
    state.enumerations.append(rr_module.CollectionEnumerationOutcome(
        collection="sources", kind="", source_id="",
        items=[SourceItem("s-empty", "无摘要文档", "Markdown", "",
                          notebook.id, "notebook")]))
    state.document_read_elements_used = int(
        repo.settings.document_overview_max_elements)
    retriever._action_read_document(state, rr_module.ReflectDecision(
        next_action=READ_DOCUMENT_ACTION, read_document_source="无摘要文档"))

    skip = state.trace[-1]
    assert skip.detail["reason"] == "document_read_budget"
    assert "result_ids" not in skip.detail
    assert state.document_reads == []


def test_skip_when_the_channel_is_switched_off_and_the_model_forces_it(repo):  # noqa: F811
    """② 闸关时硬吐这个动作:三处投影逐字节回到接入前,动作走 `invalid_action` 兜底。

    关闭态下模型压根看不到这个动作,所以它只可能来自畸形响应或测试替身;那条路径
    必须零 I/O,而且 prompt / schema / allowed_actions 三处都不能留下痕迹。
    """
    notebook = _notebook_with_documents(repo)
    repo.settings.reasoning_document_read_enabled = False
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert result.document_reads == []
    assert _steps(result, "read_document") == []
    # 三处逐字节回到接入前。
    assert all("read_document" not in prompt for prompt in llm.reflect_prompts)
    assert all("read_document" not in hint for hint in llm.schema_hints)
    # 代价落在校验层:这个动作名不在 schema 的 next_action 枚举里,所以整轮反思
    # 在到达白名单之前就被拒了(`invalid_enum`)。白名单那一层的合同由下面那条
    # 纵深防御用例单独钉。
    assert [s.detail["fallback_reason"] for s in _steps(result, "reflect")
            if "fallback_reason" in s.detail] == ["invalid_enum"]


def test_the_action_whitelist_rejects_it_when_the_gate_is_shut(repo):  # noqa: F811
    """② 绕过校验层直达 reflect():白名单退成兜底,原因带上被拒的动作名。"""
    from tests.test_reasoning_enumeration_tools import _SeqLLM

    repo.settings.reasoning_document_read_enabled = False
    llm = _SeqLLM([_read_action("无摘要文档")],
                  plan={"sub_queries": [{"query": "版图设计"}]})
    retriever = _reader(repo, llm)
    assert retriever.document_read_active() is False

    decision = retriever.reflect("q", "candidates")

    assert decision.fallback is True
    assert decision.fallback_reason == "invalid_action:read_document"
    assert decision.next_action == "answer"


def test_the_field_parser_does_not_even_read_the_payload_when_shut(repo):  # noqa: F811
    """② 闸关时字段解析**连读都不读**——第五处投影。

    走一个**合法**动作(answer)带一份 read_document 载荷:这样 next_action 不会
    在校验层或白名单被拒,决定真的会被构造出来,唯一还能观察到差别的就是解析器
    有没有去读那一格。这是四处投影都关上之后剩下的最后一条缝,也是唯一一条不靠
    动作分发就能漏出去的:解析器若无条件读,一份畸形载荷就能在关闭态下把
    `read_document_source` 写进决定,下一个消费者(或下一个版本的分发链)拿到的
    就是一份自称要读文档的决定。
    """
    from tests.test_reasoning_enumeration_tools import _SeqLLM

    repo.settings.reasoning_document_read_enabled = False
    llm = _SeqLLM([{
        "next_action": "answer", "sufficient": True,
        "read_document": {"source": "无摘要文档", "coverage": "opening"},
    }], plan={"sub_queries": [{"query": "版图设计"}]})
    retriever = _reader(repo, llm)
    assert retriever.document_read_active() is False

    decision = retriever.reflect("q", "candidates")

    assert decision.next_action == "answer"
    assert decision.read_document_source == ""
    assert decision.read_document_coverage == "spread"


def test_fail_closed_raises_on_the_forced_action_instead_of_falling_back(repo):  # noqa: F811
    """② `fail_closed` 下同一条路径照既有合同抛,不静默退成 answer。"""
    from tests.test_reasoning_enumeration_tools import _SeqLLM

    repo.settings.reasoning_document_read_enabled = False
    llm = _SeqLLM([_read_action("无摘要文档")],
                  plan={"sub_queries": [{"query": "版图设计"}]})
    retriever = _reader(repo, llm)
    retriever.fail_closed = True

    with pytest.raises(ValueError, match="invalid action"):
        retriever.reflect("q", "candidates")


def test_the_disabled_skip_is_reachable_as_defense_in_depth(repo):  # noqa: F811
    """纵深防御:执行体自己也判一次闸(替身可以绕过白名单直达这个方法)。"""
    notebook = _notebook_with_documents(repo)
    repo.settings.reasoning_document_read_enabled = False
    retriever = _reader(repo, _ValidatingLLM([]))
    state = retriever._new_run_state(
        notebook.id, "q", "", None,
        max_steps=3, intent_queries=None, limits=None, intent_detail=None)
    decision = rr_module.ReflectDecision(
        next_action=READ_DOCUMENT_ACTION, read_document_source="无摘要文档")
    retriever._action_read_document(state, decision)

    skip = state.trace[-1]
    assert skip.step_type == "skip"
    assert skip.detail == {"reason": "document_read_disabled"}


# ------------------------------------------------- 三处投影与预算的窄判据


def test_narrowed_source_scope_takes_the_action_out_of_every_projection(repo):  # noqa: F811
    """④ 来源范围收窄 ⇒ 枚举闸关 ⇒ 这个动作整体消失(它与枚举同门)。"""
    from app.models.source_scope import SourceScope
    from app.services.source_scope import source_scope_context

    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([ANSWER], plan={"sub_queries": [{"query": "版图"}]})
    retriever = _reader(repo, llm)

    with source_scope_context(
        notebook.id, SourceScope(mode="include", source_ids=["s-empty"]),
    ):
        assert retriever.document_read_active() is False
        retriever.run(notebook.id, "这个库讲了什么", "")

    assert all("read_document" not in prompt for prompt in llm.reflect_prompts)
    assert all("read_document" not in hint for hint in llm.schema_hints)


@pytest.mark.parametrize("overrides, why", [
    ({"reasoning_max_document_reads": 40},
     "字符份额减去包头预留之后不够一个元素的正文"),
    ({"document_overview_max_elements": 1000},
     "号段总宽越过按节合成的步长,会与第 2 节的证据撞 [kN]"),
    ({"reasoning_max_document_reads": 0},
     "第二把部署级 kill switch"),
])
def test_an_infeasible_budget_takes_the_action_out_of_every_projection(
    repo, overrides, why,  # noqa: F811
):
    """⑦ 三个旋钮各自独立配,组合起来可以让这个动作在任何一轮都只会 skip。

    那是**部署配置错误**,这把闸的选择是静默降级:动作整体不提供(prompt / schema /
    白名单 / 解析 / 执行五处同步消失),模型看到的仍是一个自洽的动作空间,而不是一
    个每次调用都被服务端拒绝的工具。
    """
    notebook = _notebook_with_documents(repo)
    for name, value in overrides.items():
        setattr(repo.settings, name, value)
    llm = _ValidatingLLM([
        _enumerate_sources_action(), ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    retriever = _reader(repo, llm)

    assert retriever.document_read_active() is False, why
    retriever.run(notebook.id, "这个库讲了什么", "")

    assert all("read_document" not in prompt for prompt in llm.reflect_prompts)
    assert all("read_document" not in hint for hint in llm.schema_hints)


def test_the_feasibility_gate_reads_this_run_s_effort_budget(repo):  # noqa: F811
    """⑦ 判据读的是**本 run 的档位**,不是默认档:同一份部署配置在预算宽的档位上
    可行、在窄的档位上不可行,两侧必须给出不同的答案(否则 prompt 提供了这个动作、
    执行体却恒 budget skip,或者反过来白白关掉一个用得起的通道)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    _notebook_with_documents(repo)
    repo.settings.reasoning_max_document_reads = 12
    retriever = _reader(repo, _ValidatingLLM([]))

    # exhaustive: 120000 // 4 // 12 - 200 = 2300 >= 320 ⇒ 开
    assert retriever.document_read_active(
        ask_retrieval_limits("exhaustive")) is True
    # overview: 12000 // 4 // 12 - 200 = 50 < 320 ⇒ 关
    assert retriever.document_read_active(
        ask_retrieval_limits("overview")) is False
    # 下界本身是**按 body 口径**定的(`kN: ` 前缀 + JSON 引号 + 章节面包屑 + 一句
    # 正文),不是净正文口径。按净正文估会把「够一个元素」判得比实际乐观一倍,
    # 于是面包屑稍长的文档上每条都被二分截成只剩面包屑。
    assert rr_module.DOCUMENT_READ_MIN_CHARS_PER_ELEMENT == 320


def test_the_first_read_takes_at_most_its_fair_share_of_the_element_pool(
    repo, monkeypatch,  # noqa: F811
):
    """⑤ `max_document_reads=2` ⇒ 首读的元素份额 <= 池 / 2(后一篇还读得到)。"""
    notebook = _notebook_with_documents(repo)
    repo.settings.reasoning_max_document_reads = 2
    pool = int(repo.settings.document_overview_max_elements)
    calls: list[dict] = []
    original = rr_module.prepare_source_overview

    def _spy(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(rr_module, "prepare_source_overview", _spy)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert len(calls) == 1
    assert calls[0]["max_elements"] <= pool // 2
    assert calls[0]["budget_chars"] > 0
    assert calls[0]["key_offset"] == rr_module.DOCUMENT_READ_KEY_BASE
    assert calls[0]["active_notebook_id"] == notebook.id


def test_sampling_depth_is_derived_from_the_character_share_not_the_element_pool(
    repo, monkeypatch,  # noqa: F811
):
    """⑤ 取样深度由**字符份额**反推,元素池份额只当上界。

    overview 档的字符池只够一两个元素的**计费行**。宽度优先(直接用元素池份额 16)
    会发 16 次单行分页查询,把每条正文二分截到不足 50 字——每条只剩章节标题,贵且
    读不出这篇文档讲什么。深度优先把 I/O 降一个量级,而送进合成的每条正文反而长得多。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-long", "长文档", "", tuple(
        f"第{index + 1}节正文:" + "版图设计的取样方法与实验结论。" * 14
        for index in range(40)))
    store = repo._runtime.source_store
    original_page = store.source_elements_page
    pages: list[int] = []

    def _counting_page(source_id, *, offset, limit):
        pages.append(offset)
        return original_page(source_id, offset=offset, limit=limit)

    monkeypatch.setattr(store, "source_elements_page", _counting_page)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("长文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(
        notebook.id, "这个库讲了什么", "",
        limits=ask_retrieval_limits("overview"))

    pool_share = int(repo.settings.document_overview_max_elements) // int(
        repo.settings.reasoning_max_document_reads)
    assert pool_share == 16                       # 宽度优先会取到的那个深度
    outcome = result.document_reads[0]
    assert 0 < len(outcome.id_map) <= 4           # 实际只读了这么几个元素
    assert len(pages) <= 4                        # 单行分页查询也只发了这么几次
    # 换回来的是正文:每个落地元素的引文都远超「一个元素值不值得查一次」的下界。
    assert all(
        len(entry["snippet"]) >= rr_module.DOCUMENT_READ_MIN_CHARS_PER_ELEMENT // 2
        for entry in outcome.id_map.values())


def test_the_header_reserve_covers_the_real_wrapper_header(repo):  # noqa: F811
    """⑩ 包头预留是**保守**预留:它恒 ≥ 空标题时那行真实包头的长度。

    真实包头由 `supplemental_excerpt_header(key, title)` 决定,长度里有标题这个运行
    期事实,而份额必须在读之前就定下来。预留低于空标题下界时,最后一篇会在合成装配
    时被自己的包头挤掉——这条不等式是那件事唯一的静态防线。
    """
    from app.services.document_source_overview import supplemental_excerpt_header

    floor_chars = len(supplemental_excerpt_header(
        f"k{rr_module.DOCUMENT_READ_KEY_BASE + 1}", ""))

    assert rr_module.DOCUMENT_READ_HEADER_RESERVE_CHARS >= floor_chars
    assert repo is not None


def test_the_second_read_gets_a_disjoint_key_segment(repo):  # noqa: F811
    """两次读取的 `[kN]` 号段不重叠(号段按池宽递进,不按本次份额)。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        _read_action("有摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert len(result.document_reads) == 2
    first, second = result.document_reads
    assert first.key_offset == rr_module.DOCUMENT_READ_KEY_BASE
    assert second.key_offset == (
        rr_module.DOCUMENT_READ_KEY_BASE
        + int(repo.settings.document_overview_max_elements))
    assert not set(first.id_map) & set(second.id_map)
    assert second.summary_was_empty is False


def test_a_witness_failure_is_a_zero_hit_step_not_a_skip(repo, monkeypatch):  # noqa: F811
    """见证失败:I/O 真的发生过 ⇒ 记零命中 `read_document` 步(写 `result_ids: []`),
    空产物进账目,coverage_note 走 detail.note 并进回喂账目。"""
    from app.services.document_source_overview import SourceOverview

    notebook = _notebook_with_documents(repo)
    monkeypatch.setattr(
        rr_module, "prepare_source_overview",
        lambda *a, **k: SourceOverview("", {}, [], "读取期间文档重新解析,请重试。"))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    step = next(s for s in _steps(result, "read_document"))
    assert step.detail["found"] == 0
    assert step.detail["result_ids"] == []
    assert step.detail["note"] == "读取期间文档重新解析,请重试。"
    assert "document_read" not in json.dumps(
        [s.detail for s in _steps(result, "skip")], ensure_ascii=False)
    assert len(result.document_reads) == 1
    assert result.document_reads[0].context_block == ""
    # 账目渲染的是执行体自己的 `coverage_note`,不是一句硬编码的猜测。
    assert "本次没有取到原文的文档及各自原因" in llm.reflect_prompts[2]
    assert "《无摘要文档》——读取期间文档重新解析,请重试。" in llm.reflect_prompts[2]


def test_a_zero_line_sample_never_asks_the_model_to_introduce_from_nothing(repo):  # noqa: F811
    """① 有原文元素、但一条都没落地时,账目说的是「未能取样 / 暂无依据」。

    极长的章节面包屑会让每个元素的二分都停在面包屑本身,执行体于是把每一条都
    整个丢掉。这一轮的 `coverage_note` 绝不能是有界摘录那一句——它以「请仅依据
    这些原文介绍」结尾,而这里一个字的原文都没有,那等于请模型依据空证据去介绍
    这篇文档。账目侧照常逐条渲染执行体自己的原因。
    """
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    breadcrumb = "第一章之下的很长小节标题 " * 400
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,summary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("s-deep", notebook.id, "深层目录文档", "markdown", "parsed",
             "parsed", "", NOW, NOW),
        )
        for index in range(3):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"s-deep-{index:03}", "s-deep", "paragraph", breadcrumb,
                 f"正文 {index}。", json.dumps({"section_path": breadcrumb}), NOW),
            )
    repo.collection_catalog.invalidate()
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("深层目录文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    outcome = result.document_reads[0]
    assert outcome.context_block == "" and not outcome.id_map
    assert "未能取样" in outcome.coverage_note
    assert "3 个元素" in outcome.coverage_note
    assert "请仅依据这些原文介绍" not in outcome.coverage_note
    # 见证失败不是 skip:I/O 真的发生过,所以是一条零命中的 read_document 步。
    step = _steps(result, "read_document")[0]
    assert step.detail["found"] == 0 and step.detail["result_ids"] == []
    assert "未能取样" in step.detail["note"]
    # 账目逐条渲染执行体自己的原因,不编一个「正在重新解析」出来。
    ledger = llm.reflect_prompts[2]
    assert "《深层目录文档》——" in ledger
    assert "未能取样" in ledger and "暂无依据" in ledger
    assert "正在重新解析" not in ledger


def test_an_unparsed_document_reports_its_own_reason_not_a_reparse_story(repo):  # noqa: F811
    """① 回喂账目不得编造失败原因:空产物有四种原因,账目逐条渲染执行体的
    `coverage_note`。

    这一篇的 `source_elements` 是 0(从来没解析出原文),它的原因是「没有可读取的
    原文」——与「读取期间文档重新解析」是两件完全不同的事,后者会让模型以为等一等
    重试就能拿到。账目一旦会撒谎,模型据它做的每一个决定都失去依据。
    """
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-none", "未解析文档", "", ())
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("未解析文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert result.document_reads[0].context_block == ""
    ledger = llm.reflect_prompts[2]
    assert "没有可读取的原文" in ledger
    assert "正在重新解析" not in ledger
    assert "《未解析文档》——" in ledger
