"""多篇无标题文档在 `read_document` 花名册里编号(用户 2026-10-28 裁决)。

无标题文档在花名册里都显示成「未命名来源」。以前它们共用这一个占位串、刻意不编号:
账目对占位串「最多占一个标题位」是 codex #724 R2 钉死的(20 篇无名文档不得挤掉真
标题),于是两篇以上时模型无法点名其中任何一篇。裁决是**无名条目共享子预算并编号**:

* 编号沿用同名序号那一套(`_RosterNumbering`,「未命名来源（第 N 篇）」,凑够两篇才
  发号、发号后不变),账目、`read_document` 解析、已读账目、轨迹摘要、
  `enumerate_elements` 来源名与章节导航的回显读同一份发号簿;
* 所有无名条目整组仍只占改前那一个标题位,主预算对它的收费与改前那一个
  「《未命名来源》」相同,所以真标题列到哪一篇、`(+N more)` 是几与无名文档多少无关;
* 组内按清单顺序列前 N 篇(N = 本 run 实际生效的按篇读取次数上限),其余折叠成
  「(另有 N 篇未命名来源从略)」——折叠的几篇照样有号、照样读得到;
* 花名册里没有无名文档时,账目与解析与改前逐字节相同;只有一篇时只差 `(+N more)`
  这个计数——顺带修了它:以前有无名文档时少报 1。
"""
from __future__ import annotations

from dataclasses import replace

from app.models.schemas import NotebookCreate
from app.services import reasoning_retrieval as rr
from app.services.collection_enumeration import UNNAMED_SOURCE_LABEL
from app.services.reports.policy import DEFAULT_REASONING_MAX_DOCUMENT_READS
from app.services.section_outline import HINT_BLOCK_TITLE
from tests import test_reasoning_enumeration_tools as _enumeration_tests
from tests import test_reasoning_section_nav as nav
from tests.test_reasoning_document_read import (
    ANSWER,
    _read_action,
    _reader,
    _seed_document,
)
from tests.test_reasoning_enumeration_tools import (
    _add_titled_source,
    _enumerate_action,
    _enumerate_sources_action,
    _skips,
    _ValidatingLLM,
)

repo = _enumeration_tests.repo          # pytest fixture, resolved by name

PLAN = {"sub_queries": [{"query": "报告"}]}
NOW = "2026-10-28T00:00:00+08:00"
MARK = rr._ENUM_NOTE_NO_SUMMARY_MARK
K = rr._ENUM_NOTE_UNNAMED_TITLES


class _Row:
    """只带标题/摘要/id 的花名册行(`SourceItem` 的鸭子替身)。"""

    def __init__(self, source_id, title, summary="x"):
        self.source_id = source_id
        self.source_title = title
        self.summary = summary


class _FakeChain:
    """`_EnumChain` 的鸭子替身(`_RosterNumbering.sync` 只读 outcome.collection/items)。"""

    def __init__(self, items):
        class _Outcome:
            collection = "sources"

        self.outcome = _Outcome()
        self.outcome.items = items


def _unnamed(count, prefix="u", summary=""):
    return [_Row(f"{prefix}{index}", "", summary) for index in range(count)]


def _real(count, title=lambda index: f"论文{index:02d}"):
    return [_Row(f"r{index}", title(index), "" if index % 3 == 0 else "摘要")
            for index in range(count)]


def _note(rows):
    return rr._source_titles_note(rows, rr._roster_placeholders(rows))


def _resolved_ids(rows, requested):
    return [row.source_id for row in rr._resolve_roster_rows(
        rows, requested, rr._roster_placeholders(rows))]


def _group(numbers, folded=0):
    text = "".join(f"《未命名来源（第 {n} 篇）》{MARK}" for n in numbers)
    return text + (f"(另有 {folded} 篇未命名来源从略)" if folded else "")


# ------------------------------------------------------------ 预算与不变量


def test_the_shared_sub_budget_defaults_to_the_default_per_run_read_cap():
    """子预算跟本 run 的按篇读取次数上限走;不传上限时(单测、无 run 上下文)的缺省
    就是默认上限,所以默认配置下的输出与这里的 K 相同。"""
    assert K == DEFAULT_REASONING_MAX_DOCUMENT_READS == 4


def test_the_sub_budget_follows_the_limit_it_is_given():
    """组内列几篇由传入的上限决定(生产传本 run 的 `max_document_reads`),折叠数随之变;
    主预算那一位与真标题不受影响。"""
    rows = _unnamed(6) + [_Row("a", "甲")]
    placeholders = rr._roster_placeholders(rows)
    for limit in (1, 2, 6, 9):
        note = rr._source_titles_note(rows, placeholders, limit)
        shown = min(limit, 6)
        assert note == "，标题: " + _group(range(1, shown + 1), 6 - shown) + "《甲》"
    assert rr._source_titles_note(rows, placeholders) == rr._source_titles_note(
        rows, placeholders, DEFAULT_REASONING_MAX_DOCUMENT_READS)


def test_twenty_unnamed_documents_never_move_or_drop_a_real_title():
    """20 篇无名 + 25 篇真标题:真标题部分与「只有一篇无名文档」时**逐字节相同**
    (同样的 19 篇、同样的顺序、同样的 `(+6 more)`),无名组占的正是改前那一个位置,
    组内前 K 篇编号、其余折叠。"""
    real = _real(25)
    note = _note(_unnamed(20) + real)
    legacy = _note(_unnamed(1) + real)           # 一篇无名:不编号,改前的形态
    single = f"《{UNNAMED_SOURCE_LABEL}》{MARK}"
    assert legacy.startswith("，标题: " + single)
    assert note == legacy.replace(single, _group(range(1, K + 1), 20 - K), 1)
    # 真标题本身一个不少、顺序不变。
    assert [t for t in note.split("《") if t.startswith("论文")] == [
        t for t in legacy.split("《") if t.startswith("论文")]


def test_the_group_is_charged_like_the_old_placeholder_against_the_char_budget():
    """60 字长标题把合计 800 字预算撑到边界:无名组的编号与折叠若按实际字数计入
    主预算,会少列真标题。按改前那一个占位串收费时,列出的真标题与一篇无名时相同。"""
    real = _real(20, title=lambda index: f"{index:02d}" + "长" * 58)
    for position in (0, 3):
        many = real[:position] + _unnamed(9) + real[position:]
        one = real[:position] + _unnamed(1) + real[position:]
        note, legacy = _note(many), _note(one)
        assert "(另有 5 篇未命名来源从略)" in note
        assert note.replace(_group(range(1, K + 1), 9 - K), f"《{UNNAMED_SOURCE_LABEL}》{MARK}") == legacy
        assert legacy.endswith("more)")          # 确实撞到了预算边界
    # 恰好贴着 800 字:占位串按「《未命名来源》(无摘要)」计 12 字,12 篇 60 字标题各
    # 62 字,第 13 篇 42 字标题补满到 800。整组只要多收哪怕一个字(比如按带序号的
    # 占位串收费),第 13 篇就会被挤出去。
    tight = [_Row(f"t{index}", f"{index:02d}" + "长" * 58, "摘要") for index in range(12)]
    tight += [_Row("t-last", "末" * 42, "摘要"), _Row("t-more", "多", "摘要")]
    assert 5 + 2 + len(MARK) + 12 * 62 + 44 == rr._ENUM_NOTE_TITLES_TOTAL_CHARS
    note = _note(_unnamed(6) + tight)
    assert note.endswith("《" + "末" * 42 + "》(+1 more)")  # 第 13 篇在,「多」被挡下
    assert "《多》" not in note
    assert note == _note(_unnamed(1) + tight).replace(
        f"《{UNNAMED_SOURCE_LABEL}》{MARK}", _group(range(1, K + 1), 6 - K), 1)


def test_the_group_sits_where_the_first_unnamed_row_is_and_folds_the_rest_in_order():
    """无名行散在清单各处:整组放在第一篇无名文档的位置,按清单顺序列前 K 篇,之后的
    无名行(包括排在主预算截断点之后的)都计入折叠数。"""
    real = _real(25)
    rows = [row for pair in zip(real, _unnamed(25)) for row in pair]
    note = _note(rows)
    assert note.startswith("，标题: 《论文00》" + MARK + _group(range(1, K + 1), 25 - K) + "《论文01》")
    assert note.count("《未命名来源") == K


def test_zero_or_one_unnamed_document_is_byte_identical_to_before():
    """没有或只有一篇无名文档(且没有被省略的真标题):与无名编号之前逐字节相同
    (一篇时不编号,与同名序号「凑够两篇才发号」同一条规则)。字面值钉死。"""
    rows = [_Row("a", "甲", ""), _Row("u", "", ""), _Row("b", "乙")]
    assert rr._roster_placeholders(rows) == {}
    assert _note(rows) == f"，标题: 《甲》{MARK}《未命名来源》{MARK}《乙》"
    assert _resolved_ids(rows, UNNAMED_SOURCE_LABEL) == ["u"]
    assert _resolved_ids(rows, "未命名来源（第 1 篇）") == []
    assert _note([_Row("a", "甲"), _Row("b", "乙")]) == "，标题: 《甲》《乙》"


def test_without_a_numbering_book_the_ledger_keeps_the_single_placeholder():
    """不传发号簿(单测与无 run 上下文的调用方):多篇无名文档仍是改前那一个占位串。"""
    rows = _unnamed(3) + [_Row("a", "甲")]
    assert rr._source_titles_note(rows) == f"，标题: 《未命名来源》{MARK}《甲》"


def test_unnamed_documents_after_the_main_cut_stay_out_of_the_ledger_as_before():
    """主预算在第一篇无名文档之前就用完:与改前一样不出现(改前那一个占位串也到不了
    这里),真标题与 `(+N more)` 不变。"""
    rows = _real(25) + _unnamed(5)
    assert _note(rows) == _note(_real(25))
    assert "未命名来源" not in _note(rows)


# ------------------------------------------------------- 解析:编号可点名


def test_listed_and_folded_numbers_resolve_to_exactly_one_document():
    """列出的前 K 篇与折叠的几篇都按号精确解析到一篇(折叠只是不占账目的字);抄回
    账目里的整段(书名号 + 标记)同样还原;裸占位串仍是多命中,教模型连序号一起抄。"""
    rows = _unnamed(6) + [_Row("a", "甲")]
    assert _resolved_ids(rows, "未命名来源（第 2 篇）") == ["u1"]
    assert _resolved_ids(rows, f"《未命名来源（第 {K} 篇）》{MARK}") == [f"u{K - 1}"]
    assert _resolved_ids(rows, "未命名来源（第 6 篇）") == ["u5"]   # 折叠的那篇
    assert _resolved_ids(rows, "未命名来源（第 7 篇）") == []
    matches = rr._resolve_roster_rows(rows, UNNAMED_SOURCE_LABEL, rr._roster_placeholders(rows))
    assert len(matches) == 6
    summary = rr._ambiguous_read_summary(matches, rr._roster_placeholders(rows))
    assert "「（第 1 篇）」" in summary and "无法区分" not in summary


def test_numbers_are_issued_once_per_run_and_never_move():
    """跨轮稳定:第一篇无名文档单独出现时不编号;第二篇到来时两篇一并拿 1、2 号;之后
    较早的链续跑插队也不改号,新来的拿下一个号。enumerate_elements 的来源名翻译读的是
    同一份发号簿。"""
    u0, u1, u2 = _unnamed(3)
    chain_a = _FakeChain([u0])
    chains = {("a",): chain_a}
    numbering = rr._RosterNumbering().sync(chains)
    assert numbering.issued == {}
    assert rr._source_titles_note([u0], numbering.issued) == f"，标题: 《未命名来源》{MARK}"
    chains[("b",)] = _FakeChain([u1, u0])
    numbering.sync(chains)
    assert numbering.issued == {"u0": "未命名来源（第 1 篇）", "u1": "未命名来源（第 2 篇）"}
    chain_a.outcome.items.append(u2)             # 较早的链 A 续跑,新行入簿
    numbering.sync(chains)
    assert numbering.issued["u0"] == "未命名来源（第 1 篇）"
    assert numbering.issued["u1"] == "未命名来源（第 2 篇）"
    assert numbering.issued["u2"] == "未命名来源（第 3 篇）"
    # 同一篇在不同链的账目里是同一个串;链内按清单顺序列。
    assert rr._source_titles_note([u1, u0], numbering.issued) == "，标题: " + _group([2, 1])
    assert numbering.lookup("《未命名来源（第 2 篇）》") == ("u1", False)
    assert numbering.lookup(UNNAMED_SOURCE_LABEL) == ("", False)


def test_the_enumeration_note_numbers_unnamed_documents_across_chains():
    """同名判定按整个花名册:两篇无名文档分别躺在两条链里,账目里仍然是第 1/2 篇。"""
    class _Coverage:
        returned_total = 1
        total = 1
        complete = True

    def _chain(rows):
        class _Outcome:
            collection = "sources"
            kind = ""
            local_only = False
            source_scoped = False
            coverage = _Coverage()
            items = rows

        class _Chain:
            outcome = _Outcome()
            state = "complete"

        return _Chain()

    first, second = _unnamed(2)
    note = rr._enumeration_note({("k1",): _chain([first]), ("k2",): _chain([first, second])})
    assert note.count(f"《未命名来源（第 1 篇）》{MARK}") == 2
    assert note.count(f"《未命名来源（第 2 篇）》{MARK}") == 1
    assert "《未命名来源》" not in note


# ------------------------------------------------------------------- e2e


def test_untitled_documents_are_listed_numbered_and_each_is_readable(repo):
    """6 篇无标题 + 1 篇有标题:第二轮 reflect 看到前 K 篇编号 + 折叠计数;模型按号读
    第 2 篇与(折叠的)第 6 篇,读到的正是花名册顺序里的那两篇;已读账目与轨迹摘要
    带同一个编号;裸占位串被跳过并教它连序号一起抄。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    for index in range(6):
        _seed_document(repo, notebook.id, f"s-u{index}", "", "",
                       (f"第{index}篇无名文档的开头。", f"第{index}篇无名文档的结论。"))
    _seed_document(repo, notebook.id, "s-named", "月报", "有摘要", ("月报正文。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("未命名来源（第 2 篇）"),
        _read_action("《未命名来源（第 6 篇）》"),
        _read_action(UNNAMED_SOURCE_LABEL),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    roster = [row.source_id for outcome in result.enumerations
              if outcome.collection == "sources" for row in outcome.items
              if not row.source_title]
    assert sorted(roster) == [f"s-u{index}" for index in range(6)]
    prompt = llm.reflect_prompts[1]
    assert _group(range(1, K + 1), 6 - K) in prompt
    assert "《月报》" in prompt
    assert "《未命名来源（第 5 篇）》" not in prompt

    assert [o.source_id for o in result.document_reads] == [roster[1], roster[5]]
    for outcome in result.document_reads:
        index = outcome.source_id.removeprefix("s-u")
        assert f"第{index}篇无名文档的结论" in outcome.context_block
    steps = [s for s in result.trace if s.step_type == "read_document"]
    assert "《未命名来源（第 2 篇）》" in steps[0].summary
    assert "《未命名来源（第 6 篇）》" in steps[1].summary
    assert steps[0].detail["source"] == ""          # detail 仍是库里的真实标题
    assert ("已按篇读取过原文的文档,勿重复请求: 《未命名来源（第 2 篇）》"
            in llm.reflect_prompts[2])
    skip = _skips(result)["document_read_unresolved"]
    assert skip.detail["matches"] == 6
    assert "「（第 1 篇）」" in skip.summary


def test_a_numbered_unnamed_placeholder_in_enumerate_source_title_targets_that_document(repo):
    """模型把「未命名来源（第 2 篇）」抄进 `enumerate_elements` 的来源名:定向到花名册
    里那一篇(改前按名解析对两篇无名文档只能报多命中)。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _add_titled_source(repo, notebook.id, "s-1", "", formulas=1)
    _add_titled_source(repo, notebook.id, "s-2", "", formulas=2)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _enumerate_action(source_title="未命名来源（第 2 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "列出公式", "")

    roster = [row.source_id for outcome in result.enumerations
              if outcome.collection == "sources" for row in outcome.items]
    elements = [o for o in result.enumerations if o.collection == "elements"]
    assert [o.source_id for o in elements] == [roster[1]]
    assert len(elements[0].items) == {"s-1": 1, "s-2": 2}[roster[1]]


# ------------------------------------------------- (+N more) 省略数(顺带修复)


def test_the_more_count_is_exact_when_untitled_documents_are_present():
    """以前分母不算无名文档、分子却算了无名那一位,有无名文档时 `(+N more)` 少报 1:
    25 篇真标题只列出 19 篇,却写 `(+5 more)`。现在省略数就是没列出的真标题篇数;
    无名组自己报自己的数(列出的 + 折叠计数),不进这个计数。"""
    real = _real(25)
    for unnamed in (_unnamed(1), _unnamed(20)):
        note = _note(unnamed + real)
        listed = [t for t in note.split("《") if t.startswith("论文")]
        assert len(listed) == 19
        assert note.endswith(f"(+{25 - 19} more)")
    # 没有无名文档时这个数本来就对,逐字节不变。
    assert _note(real).endswith(f"(+{25 - 20} more)")
    # 真标题恰好就叫「未命名来源」的那一篇(只有一篇、不编号)以前就算得对,仍然对。
    labelled = [_Row("x", UNNAMED_SOURCE_LABEL)] + real
    assert _note(labelled).endswith("(+6 more)")


# ------------------------------------------------- 子预算跟本 run 的读取上限走(e2e)


def test_a_run_with_a_different_read_cap_lists_that_many_untitled_documents(repo):
    """把 `REASONING_MAX_DOCUMENT_READS` 调成 2:账目里无名组只列 2 篇带号,其余 3 篇
    折叠;调成 6:5 篇全列、没有折叠。默认配置(4)见上面的 e2e。"""
    for cap, shown in ((2, 2), (6, 5)):
        repo.settings.reasoning_max_document_reads = cap
        notebook = repo.create_notebook(NotebookCreate(name=f"资料{cap}"))
        for index in range(5):
            _seed_document(repo, notebook.id, f"s{cap}-u{index}", "", "",
                           (f"第{index}篇。",))
        llm = _ValidatingLLM([_enumerate_sources_action(), ANSWER], plan=PLAN)
        _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")
        prompt = llm.reflect_prompts[1]
        assert "，标题: " + _group(range(1, shown + 1), 5 - shown) + "。" in prompt


# --------------------------------------------------- 章节导航回显带号(e2e)


def _seed_sectioned(repo, notebook_id, source_id, sections):
    """一篇无标题来源 + 按章节路径切好的 chunk(章节导航读的是 chunks.section_path)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,summary,created_at,updated_at) "
            "VALUES (?,?,'','markdown','extracted','extracted','',?,?)",
            (source_id, notebook_id, NOW, NOW))
        for index, (path, text) in enumerate(sections):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at,navigation) VALUES (?,?,?,?,?,?,?,0)",
                (f"{source_id}-c{index}", notebook_id, source_id, text, path, "[]", NOW))
    repo.collection_catalog.invalidate()


def _browse(source):
    return {"next_action": "browse_sections", "reason": "看目录",
            "browse_sections": {"source": source, "under": "", "query": ""}}


def _read_section(section, source=""):
    return {"next_action": "read_section", "reason": "读这一节",
            "read_section": {"source": source, "section": section}}


def _navigate(repo, untitled, browse_as):
    notebook = repo.create_notebook(NotebookCreate(name=f"导航{untitled}"))
    for index in range(untitled):
        _seed_sectioned(repo, notebook.id, f"n{untitled}-u{index}", (
            ("1 概述", f"第{index}篇概述。"), ("2 结论", f"第{index}篇结论。")))
    llm = _ValidatingLLM([
        _enumerate_sources_action(), _browse(browse_as), _read_section("s2"), ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")
    browse = [s for s in result.trace if s.step_type == "browse_sections"]
    read = [s for s in result.trace if s.step_type == "read_section"]
    return result, llm, browse, read


def test_section_navigation_echoes_the_numbered_untitled_title(repo):
    """两篇无标题文档:按「未命名来源（第 2 篇）」看目录、再按 [sN] 读一节,目录块、
    轨迹、导航账目里的来源标题都是「未命名来源（第 2 篇）」——与清单账目、已读账目
    同一个串,模型读完知道读的是哪一篇。"""
    result, llm, browse, read = _navigate(repo, 2, "未命名来源（第 2 篇）")
    roster = [row.source_id for outcome in result.enumerations
              if outcome.collection == "sources" for row in outcome.items]
    wanted = "未命名来源（第 2 篇）"
    assert len(browse) == 1 and len(read) == 1
    assert f"《{wanted}》" in browse[0].summary
    assert browse[0].detail["source"] == wanted
    assert read[0].detail["source"] == wanted
    assert read[0].detail["result_ids"] == [f"{roster[1]}-c1"]
    assert f"章节目录:《{wanted}》" in llm.reflect_prompts[2]
    assert f"已浏览《{wanted}》" in llm.reflect_prompts[2]


def test_reading_a_section_by_numbered_source_title_echoes_that_title(repo):
    """不先看目录,直接按「来源 + 章节标题」读:来源用带号的占位串解析到那一篇,
    轨迹与导航账目里的来源标题同样带号。"""
    notebook = repo.create_notebook(NotebookCreate(name="按名读节"))
    for index in range(2):
        _seed_sectioned(repo, notebook.id, f"r-u{index}", (
            ("1 概述", f"第{index}篇概述。"), ("2 结论", f"第{index}篇结论。")))
    wanted = "未命名来源（第 1 篇）"
    llm = _ValidatingLLM([
        _enumerate_sources_action(), _read_section("2 结论", source=wanted), ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    roster = [row.source_id for outcome in result.enumerations
              if outcome.collection == "sources" for row in outcome.items]
    read = [s for s in result.trace if s.step_type == "read_section"]
    assert len(read) == 1
    assert read[0].detail["source"] == wanted
    assert read[0].detail["result_ids"] == [f"{roster[0]}-c1"]
    assert f"已读《{wanted}》 2 结论" in llm.reflect_prompts[2]


def test_the_section_hint_block_echoes_the_numbered_untitled_title(tmp_path, monkeypatch):
    """章节线索块同一口径:证据来自一篇无标题文档,列出花名册(库里共两篇无标题)之后,
    线索行写「未命名来源（第 N 篇）」;列出之前还没有号,写「未命名来源」。"""
    section_repo = nav.make_section_repo(tmp_path, monkeypatch)
    nb = nav._seed(section_repo, extra_sources=(("src-other", ""),))
    with section_repo._write() as db:
        db.execute("UPDATE sources SET title='' WHERE id='src-spec'")
    section_repo.collection_catalog.invalidate()
    llm = nav._llm(_enumerate_sources_action())
    retriever = nav._reasoner(section_repo, monkeypatch, llm, seed=(),
                              extra_hits=(replace(nav._hit("c-s0"), source_title=""),))
    result = nav._run(retriever, nb)

    untitled = [row.source_id for outcome in result.enumerations
                if outcome.collection == "sources" for row in outcome.items
                if not row.source_title]
    wanted = f"未命名来源（第 {untitled.index('src-spec') + 1} 篇）"

    def _hint_lines(prompt):
        return prompt[prompt.index(HINT_BLOCK_TITLE):].split("\n\n")[0].splitlines()[1:]

    assert _hint_lines(llm.reflect_prompts[0])[0].startswith(f"[s1]《{UNNAMED_SOURCE_LABEL}》 ")
    assert _hint_lines(llm.reflect_prompts[1])[0].startswith(f"[s1]《{wanted}》 ")


def test_section_navigation_keeps_the_bare_label_for_a_single_untitled_document(repo):
    """只有一篇无标题文档:不编号,回显仍是「未命名来源」(与改前相同)。"""
    _result, _llm, browse, read = _navigate(repo, 1, UNNAMED_SOURCE_LABEL)
    assert browse[0].detail["source"] == UNNAMED_SOURCE_LABEL
    assert f"《{UNNAMED_SOURCE_LABEL}》" in browse[0].summary
    assert read[0].detail["source"] == UNNAMED_SOURCE_LABEL
