"""`read_document` 的同名篇消歧(PR-A 延后项 (a))。

动作的参数就是标题,花名册里同名的几篇以前区分不开(只能 skip)。现在**仅当**
花名册里真的出现同名时,账目把那几篇列成「标题（第 N 篇）」(按花名册既有顺序编
号,两篇都加),模型原样回传即读到对应那篇;动作参数形状不变;无同名时账目与解析
逐字节不变。

覆盖:两篇同名都可读、无同名逐字节不变、真实标题撞占位串、未命名来源与同名混合、
账目截断形态撞车、大小写、同一篇被两条链各列一次、跨库。接入面(e2e)经生产的
reflect 校验层(`_ValidatingLLM`),纯函数用例直接测 `_roster_placeholders` /
`_resolve_roster_rows` / `_source_titles_note`。
"""
from __future__ import annotations

from app.models.schemas import NotebookCreate
from app.services import reasoning_retrieval as rr
from app.services.collection_enumeration import UNNAMED_SOURCE_LABEL
from tests.test_reasoning_document_read import (  # noqa: F401
    ANSWER,
    _read_action,
    _reader,
    _seed_document,
    _notebook_with_documents,
)
from tests.test_reasoning_enumeration_tools import (  # noqa: F401
    _ValidatingLLM,
    _enumerate_sources_action,
    _skips,
    repo,  # pytest fixture, resolved by name
)

PLAN = {"sub_queries": [{"query": "报告"}]}


class _Row:
    """只带标题/摘要/id 的花名册行(`SourceItem` 的鸭子替身)。"""

    def __init__(self, title, source_id, summary="x"):
        self.source_title = title
        self.source_id = source_id
        self.summary = summary


def _roster_of(*pairs):
    return [_Row(title, source_id) for source_id, title in pairs]


def _resolved_ids(rows, requested):
    return [row.source_id for row in rr._resolve_roster_rows(rows, requested)]


def _sources_roster(result):
    return [row for outcome in result.enumerations
            if outcome.collection == "sources" for row in outcome.items]


# ------------------------------------------------------------------- e2e


def test_two_same_titled_documents_are_both_readable_by_their_ordinal(repo):  # noqa: F811
    """同名两篇:账目把它们列成「标题（第 1 篇）」「标题（第 2 篇）」,模型原样回传
    哪一个就读到哪一个——同一个 run 里两篇都能读。序号按花名册既有顺序编。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-a", "周报", "", ("甲篇开头。", "甲篇结论。"))
    _seed_document(repo, notebook.id, "s-b", "周报", "", ("乙篇开头。", "乙篇结论。"))
    _seed_document(repo, notebook.id, "s-c", "月报", "有摘要", ("丙篇。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("周报（第 2 篇）"),
        _read_action("《周报（第 1 篇）》"),      # 书名号形态照旧被还原
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    roster = [row.source_id for row in _sources_roster(result)
              if row.source_id in ("s-a", "s-b")]
    prompt = llm.reflect_prompts[1]
    assert "《周报（第 1 篇）》" in prompt and "《周报（第 2 篇）》" in prompt
    assert "《周报》" not in prompt
    assert "《月报》" in prompt                      # 没有同名的行原样
    assert "document_read_unresolved" not in _skips(result)
    # 先请求「第 2 篇」→ 读到花名册顺序里的第二个同名篇;随后读第 1 篇。
    assert [o.source_id for o in result.document_reads] == [roster[1], roster[0]]
    by_id = {o.source_id: o for o in result.document_reads}
    assert "甲篇结论" in by_id["s-a"].context_block
    assert "乙篇结论" in by_id["s-b"].context_block


def test_a_bare_title_shared_by_two_documents_teaches_the_ordinal(repo):  # noqa: F811
    """模型给的是**不带序号**的标题 → 仍然 unresolved(服务端绝不替模型挑一篇),
    文案点明「连序号一起抄」,不再说「无法区分」;detail 形状不变。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-a", "周报", "", ("甲篇。",))
    _seed_document(repo, notebook.id, "s-b", "周报", "", ("乙篇。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("周报"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    skip = _skips(result)["document_read_unresolved"]
    assert skip.detail == {"reason": "document_read_unresolved",
                           "requested_title": "周报", "matches": 2}
    assert "清单里有 2 篇同名文档" in skip.summary
    assert "「（第 1 篇）」" in skip.summary      # 示例按实际写法给,且只给后缀、不带标题
    assert "周报" not in skip.summary
    assert "无法区分" not in skip.summary
    assert result.document_reads == []


def test_a_wrong_ordinal_is_an_ordinary_zero_match_skip(repo):  # noqa: F811
    """花名册只有两篇同名,请求「第 3 篇」→ 与抄错标题同一条零命中 skip。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-a", "周报", "", ("甲篇。",))
    _seed_document(repo, notebook.id, "s-b", "周报", "", ("乙篇。",))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("周报（第 3 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert _skips(result)["document_read_unresolved"].detail["matches"] == 0
    assert result.document_reads == []


def test_the_same_document_listed_by_two_chains_is_not_a_shared_title(repo):  # noqa: F811
    """同一篇被两条范围不同的清单链各列一次,不是「两篇同名」:不编号、账目不变。"""
    notebook = _notebook_with_documents(repo)
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _enumerate_sources_action(scope="current_notebook"),
        _read_action("无摘要文档"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "版图设计"}]})
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert "（第" not in llm.reflect_prompts[2]
    assert [o.source_id for o in result.document_reads] == ["s-empty"]


def test_a_title_shared_across_libraries_is_numbered_over_the_whole_roster(repo):  # noqa: F811
    """跨库:同名判定按**本 run 的整个花名册**(活动库 + 参考库的来源清单折叠后),
    不按库——解析域本来就是整个花名册,按库编号会让两个库各自的「第 1 篇」撞车。
    读到的是对应那篇,引用的 notebook_id 是它所在的库。"""
    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _seed_document(repo, active.id, "s-own", "规范", "", ("本库规范开头。", "本库规范结论。"))
    _seed_document(repo, reference.id, "s-ref", "规范", "", ("参考库规范开头。", "参考库规范结论。"))
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()

    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("规范（第 1 篇）"),
        _read_action("规范（第 2 篇）"),
        ANSWER,
    ], plan={"sub_queries": [{"query": "规范"}]})
    result = _reader(repo, llm).run(active.id, "这个库讲了什么", "")

    roster = _sources_roster(result)
    assert [row.source_title for row in roster] == ["规范", "规范"]
    mark = rr._ENUM_NOTE_NO_SUMMARY_MARK     # 两篇都没存摘要
    assert (f"《规范（第 1 篇）》{mark}《规范（第 2 篇）》{mark}"
            in llm.reflect_prompts[1])
    assert "document_read_unresolved" not in _skips(result)
    assert [(o.source_id, o.notebook_id) for o in result.document_reads] == [
        (roster[0].source_id, roster[0].notebook_id),
        (roster[1].source_id, roster[1].notebook_id)]
    assert {o.notebook_id for o in result.document_reads} == {active.id, reference.id}


# ------------------------------------------------------------- 逐字节不变


def test_a_roster_without_shared_titles_is_byte_identical():
    """无同名时账目逐字节不变:传不传 placeholders 是同一串,且钉死字面值。"""
    rows = [_Row("论文一", "s1", ""), _Row("论文二", "s2", "有摘要"),
            _Row("", "s3", ""), _Row("长" * 80, "s4", "x")]
    placeholders = rr._roster_placeholders(rows)
    assert placeholders == {}
    golden = ("，标题: 《论文一》" + rr._ENUM_NOTE_NO_SUMMARY_MARK
              + "《论文二》《未命名来源》" + rr._ENUM_NOTE_NO_SUMMARY_MARK
              + "《" + "长" * rr._ENUM_NOTE_TITLE_CHARS + "》")
    assert rr._source_titles_note(rows) == golden
    assert rr._source_titles_note(rows, placeholders) == golden
    # 解析侧同样不变:真实标题照旧命中,序号形态不存在。
    assert _resolved_ids(rows, "论文二") == ["s2"]
    assert _resolved_ids(rows, "论文二（第 1 篇）") == []


def test_the_whole_enumeration_note_is_unchanged_when_titles_are_unique():
    """`_enumeration_note` 整段(含链账目)在无同名时是接入前的同一串。"""
    class _Coverage:
        returned_total = 2
        total = 2
        complete = True

    class _Outcome:
        collection = "sources"
        kind = ""
        local_only = False
        source_scoped = False
        coverage = _Coverage()
        items = [_Row("甲", "s1", "摘要"), _Row("乙", "s2", "")]

    class _Chain:
        outcome = _Outcome()
        state = "complete"

    note = rr._enumeration_note({("k",): _Chain()})
    assert note == (
        "（本轮已枚举的清单: 「来源清单」已完整列出 2 条，标题: 《甲》《乙》"
        + rr._ENUM_NOTE_NO_SUMMARY_MARK
        + "。同一清单再次请求会从上次停下的位置继续;已完整列出的不要再请求"
        "(换一个 scope 的来源清单不算同一份清单),"
        "改用其他动作或直接作答。)")


def test_the_enumeration_note_numbers_across_chains_not_per_chain():
    """同名判定按整个花名册:两篇同名分别躺在两条链里,账目里仍然是「第 1/2 篇」,
    而且同一篇在不同链的账目里是同一个占位串。"""
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

    first = _Row("周报", "a")
    second = _Row("周报", "b")
    note = rr._enumeration_note({
        ("k1",): _chain([first]),
        ("k2",): _chain([first, second])})
    assert note.count("《周报（第 1 篇）》") == 2
    assert note.count("《周报（第 2 篇）》") == 1


# ----------------------------------------------------- 占位串的构造与消歧


def test_a_real_title_that_looks_like_a_placeholder_is_never_shadowed():
    """真实标题恰好就是「X（第 2 篇）」:占位串构造时就避开所有真实标题——换下一个
    候选写法,于是真实标题照旧只命中它自己,占位串也各自只命中自己;优先级问题在
    构造上不存在(不靠运行时的先后次序)。"""
    rows = _roster_of(("a", "X"), ("b", "X"), ("c", "X（第 2 篇）"))
    placeholders = rr._roster_placeholders(rows)
    assert placeholders["a"] == "X（第 1 篇）"
    assert placeholders["b"] == "X（同名第 2 篇）"      # 首选写法撞了真实标题 → 换写法
    assert "c" not in placeholders                     # 唯一的真标题不编号
    assert _resolved_ids(rows, "X（第 2 篇）") == ["c"]  # 真实标题命中真实那篇
    assert _resolved_ids(rows, "X（同名第 2 篇）") == ["b"]
    assert _resolved_ids(rows, "X（第 1 篇）") == ["a"]
    assert sorted(_resolved_ids(rows, "X")) == ["a", "b"]  # 裸标题仍是歧义
    # 账目里三行是三个互不相同的串。
    assert rr._source_titles_note(rows, placeholders) == (
        "，标题: 《X（第 1 篇）》《X（同名第 2 篇）》《X（第 2 篇）》")

    # 候选写法也被真实标题占满时继续往后走,仍然互不相撞。
    crowded = _roster_of(("a", "X"), ("b", "X"), ("c", "X（第 2 篇）"),
                         ("d", "X（同名第 2 篇）"))
    crowded_ph = rr._roster_placeholders(crowded)
    assert crowded_ph["b"] == "X（同名第 2 篇·2）"
    assert _resolved_ids(crowded, "X（同名第 2 篇·2）") == ["b"]
    assert _resolved_ids(crowded, "X（同名第 2 篇）") == ["d"]
    assert _resolved_ids(crowded, "X（第 2 篇）") == ["c"]


def test_a_unique_real_title_that_looks_like_a_placeholder_resolves_as_itself():
    """花名册没有同名时「X（第 2 篇）」就是一个普通标题,整条路径与接入前相同。"""
    rows = _roster_of(("a", "X"), ("c", "X（第 2 篇）"))
    assert rr._roster_placeholders(rows) == {}
    assert _resolved_ids(rows, "X（第 2 篇）") == ["c"]
    assert _resolved_ids(rows, "X") == ["a"]


def test_unnamed_documents_are_numbered_in_one_slot_next_to_numbered_same_titled_ones():
    """未命名来源与同名混合(用户 2026-10-28 裁决:无名条目共享子预算并编号):多篇
    无名文档与同名篇同一条规则编号,整组仍只占一个标题位(codex #724 R2);裸占位串
    多命中时与同名裸标题一样教模型连序号一起抄。"""
    rows = _roster_of(("u1", ""), ("u2", ""), ("a", "周报"), ("b", "周报"))
    placeholders = rr._roster_placeholders(rows)
    assert placeholders == {
        "u1": "未命名来源（第 1 篇）", "u2": "未命名来源（第 2 篇）",
        "a": "周报（第 1 篇）", "b": "周报（第 2 篇）"}
    assert rr._source_titles_note(rows, placeholders) == (
        "，标题: 《未命名来源（第 1 篇）》《未命名来源（第 2 篇）》"
        "《周报（第 1 篇）》《周报（第 2 篇）》")
    assert _resolved_ids(rows, "未命名来源（第 2 篇）") == ["u2"]
    assert sorted(_resolved_ids(rows, UNNAMED_SOURCE_LABEL)) == ["u1", "u2"]
    unnamed = rr._resolve_roster_rows(rows, UNNAMED_SOURCE_LABEL)
    assert "「（第 1 篇）」" in rr._ambiguous_read_summary(unnamed, placeholders)
    shared = rr._resolve_roster_rows(rows, "周报")
    assert "「（第 1 篇）」" in rr._ambiguous_read_summary(shared, placeholders)
    # 20 篇无名 + 一对同名:真标题不被挤出账目。
    many = _roster_of(*[(f"u{i}", "") for i in range(20)],
                      ("a", "周报"), ("b", "周报"))
    note = rr._source_titles_note(many, rr._roster_placeholders(many))
    assert note.count("《未命名来源（第") == rr._ENUM_NOTE_UNNAMED_TITLES
    assert "(另有 16 篇未命名来源从略)" in note
    assert "《周报（第 1 篇）》《周报（第 2 篇）》" in note


def test_a_real_title_with_the_label_is_numbered_alongside_an_unnamed_document():
    """真标题恰好就叫「未命名来源」与无标题文档显示得一样:同一条规则一起编号,
    两篇各自读得到(改前两篇都只能报歧义)。只有其中一篇时不编号。"""
    rows = _roster_of(("u1", ""), ("u2", UNNAMED_SOURCE_LABEL))
    assert rr._roster_placeholders(rows) == {
        "u1": "未命名来源（第 1 篇）", "u2": "未命名来源（第 2 篇）"}
    assert _resolved_ids(rows, "未命名来源（第 1 篇）") == ["u1"]
    assert _resolved_ids(rows, "未命名来源（第 2 篇）") == ["u2"]
    assert rr._roster_placeholders(rows[:1]) == {}
    assert rr._roster_placeholders(rows[1:]) == {}


def test_titles_that_only_differ_past_the_ledger_cut_are_numbered_and_addressable():
    """两篇标题前 60 字相同、之后不同:账目里显示得一模一样。以前只能报歧义;现在
    按账目形态判同名并编号,模型抄回「60 字 + 序号」就读到对应那篇。"""
    base = "甲" * rr._ENUM_NOTE_TITLE_CHARS
    rows = _roster_of(("a", base + "一"), ("b", base + "二"), ("c", "丙"))
    placeholders = rr._roster_placeholders(rows)
    assert placeholders == {"a": base + "（第 1 篇）", "b": base + "（第 2 篇）"}
    assert _resolved_ids(rows, base + "（第 2 篇）") == ["b"]
    assert _resolved_ids(rows, base + "（第 1 篇）") == ["a"]
    assert sorted(_resolved_ids(rows, base)) == ["a", "b"]
    note = rr._source_titles_note(rows, placeholders)
    assert f"《{base}（第 1 篇）》" in note      # 序号尾巴没有被 60 字那一刀砍掉


def test_case_variants_count_as_the_same_title_like_the_resolver_already_did():
    """解析器一直把 `Abc` 与 `abc` 当同一个标题(trim + casefold);编号用同一把尺子。"""
    rows = _roster_of(("a", "Abc"), ("b", "abc"))
    assert rr._roster_placeholders(rows) == {
        "a": "Abc（第 1 篇）", "b": "abc（第 2 篇）"}
    assert _resolved_ids(rows, "abc（第 2 篇）") == ["b"]


def test_a_copied_ledger_line_with_marker_still_resolves_an_ordinal_title():
    """账目里那行是「《周报（第 2 篇）》(无摘要)」:标记/书名号还原之后照样精确命中。"""
    rows = [_Row("周报", "a", ""), _Row("周报", "b", "")]
    mark = rr._ENUM_NOTE_NO_SUMMARY_MARK
    assert _resolved_ids(rows, f"《周报（第 2 篇）》{mark}") == ["b"]
    assert _resolved_ids(rows, f"周报（第 1 篇）{mark}") == ["a"]


# ------------------------------------------- 发号后不变(跨轮稳定,评审 P1)


class _FakeChain:
    """`_EnumChain` 的鸭子替身:只带 `outcome.collection/items`。"""

    def __init__(self, items, collection="sources"):
        class _Outcome:
            pass

        self.outcome = _Outcome()
        self.outcome.collection = collection
        self.outcome.items = items


def _fold(chains):
    folded = {}
    for chain in chains.values():
        for row in chain.outcome.items:
            folded.setdefault(row.source_id, row)
    return list(folded.values())


def test_a_continued_earlier_chain_never_renumbers_an_issued_placeholder():
    """P1 漂移①:较早的清单链续跑插队(花名册顺序里新行排到了旧行前面)。模型手里
    的旧串「X（第 2 篇）」必须仍指向当初发号的那一篇,新来的 X 拿下一个序号。"""
    a1, r1 = _Row("X", "a1"), _Row("X", "r1")
    chain_a = _FakeChain([a1])                  # 本库清单,尚未列完
    chain_b = _FakeChain([a1, r1])              # scope=all 的新链
    chains = {("a",): chain_a, ("b",): chain_b}
    numbering = rr._RosterNumbering().sync(chains)
    assert numbering.issued == {"a1": "X（第 1 篇）", "r1": "X（第 2 篇）"}

    chain_a.outcome.items.append(_Row("X", "a2"))   # 链 A 续跑,新行入簿
    numbering.sync(chains)
    assert numbering.issued["a1"] == "X（第 1 篇）"
    assert numbering.issued["r1"] == "X（第 2 篇）"   # 没被插队的 a2 顶成第 3
    assert numbering.issued["a2"] == "X（第 3 篇）"
    # 此时花名册折叠顺序已是 a1, a2, r1:旧串仍只解析到 r1。
    rows = _fold(chains)
    assert [r.source_id for r in rows] == ["a1", "a2", "r1"]
    # 对照:每次按当前花名册现算(旧做法)会把「第 2 篇」解析到插队的 a2 上。
    assert _resolved_ids(rows, "X（第 2 篇）") == ["a2"]
    resolved = [r.source_id for r in rr._resolve_roster_rows(
        rows, "X（第 2 篇）", numbering.issued)]
    assert resolved == ["r1"]


def test_a_late_real_title_equal_to_an_issued_placeholder_is_ambiguous_not_a_renumber():
    """P1 漂移②:后到的真实标题恰好等于已发占位串。已发的号不改;旧串与新真标题
    两者都命中 → 歧义(而不是静默指到新来的真标题上)。"""
    numbering = rr._RosterNumbering().add([_Row("系列", "s1"), _Row("系列", "s2")])
    assert numbering.issued == {"s1": "系列（第 1 篇）", "s2": "系列（第 2 篇）"}
    late = _Row("系列（第 2 篇）", "s9")
    numbering.add([late])
    assert numbering.issued == {"s1": "系列（第 1 篇）", "s2": "系列（第 2 篇）"}
    rows = [_Row("系列", "s1"), _Row("系列", "s2"), late]
    matches = rr._resolve_roster_rows(rows, "系列（第 2 篇）", numbering.issued)
    assert sorted(r.source_id for r in matches) == ["s2", "s9"]
    assert [r.source_id for r in rr._resolve_roster_rows(
        rows, "系列（第 1 篇）", numbering.issued)] == ["s1"]
    # 枚举名翻译与解析侧同一个结论:歧义(而不是交回按名解析、静默列出真标题那篇)。
    assert numbering.lookup("系列（第 2 篇）") == ("", True)
    assert numbering.lookup("系列（第 1 篇）") == ("s1", False)
    # 歧义文案:带序号的写法本身也歧义,不能再教模型「连序号一起抄」。
    summary = rr._ambiguous_read_summary(matches, numbering.issued)
    assert "无法区分" in summary and "序号" not in summary


def test_lookup_restores_a_copied_placeholder_like_the_resolver_does():
    """翻译前与 `read_document` 同口径还原:书名号/引号、`(无摘要)` 标记、整行的
    标题段都先剥掉再认占位串。"""
    numbering = rr._RosterNumbering().add([_Row("周报", "a"), _Row("周报", "b")])
    mark = rr._ENUM_NOTE_NO_SUMMARY_MARK
    for copied in ("周报（第 2 篇）", "《周报（第 2 篇）》", f"周报（第 2 篇）{mark}",
                   f"《周报（第 2 篇）》{mark}", "「周报（第 2 篇）」",
                   "周报（第 2 篇） · 类型: 摘要"):
        assert numbering.lookup(copied) == ("b", False), copied
    assert numbering.lookup("周报") == ("", False)         # 裸标题不是占位串
    assert numbering.lookup("") == ("", False)


def test_a_group_reaching_two_issues_both_numbers_once_and_later_members_append():
    """第一次凑够两篇时前两篇一并拿号(第一篇此前以裸标题出现过,没有旧串可漂);
    之后的新成员依次拿下一个序号,已发的不动。"""
    numbering = rr._RosterNumbering()
    numbering.add([_Row("周报", "a")])
    assert numbering.issued == {}
    numbering.add([_Row("周报", "b")])
    assert numbering.issued == {"a": "周报（第 1 篇）", "b": "周报（第 2 篇）"}
    numbering.add([_Row("周报", "c"), _Row("周报", "a")])   # a 重复出现不重复发号
    assert numbering.issued["c"] == "周报（第 3 篇）"
    assert numbering.issued["a"] == "周报（第 1 篇）"


def test_the_enumeration_note_shows_the_same_string_for_a_document_every_round():
    """账目读 run 级发号簿:同一篇在前后两轮、不同链里是同一个串,即使链续跑。"""
    a1, r1 = _Row("X", "a1"), _Row("X", "r1")
    chain_a = _FakeChain([a1])
    chains = {("a",): chain_a, ("b",): _FakeChain([a1, r1])}
    numbering = rr._RosterNumbering()
    first = rr._source_titles_note([r1], numbering.sync(chains).issued)
    chain_a.outcome.items.append(_Row("X", "a2"))
    second = rr._source_titles_note([r1], numbering.sync(chains).issued)
    assert first == second == "，标题: 《X（第 2 篇）》"


def test_the_whole_enumeration_note_reads_the_run_book_not_a_fresh_one():
    """`_enumeration_note(chains, numbering)` 用传入的 run 级发号簿:链 A 在两轮之间
    续跑插队后,账目里 r1 仍是「第 2 篇」(现算会把插队的 a2 排成第 2 篇)。"""
    class _Coverage:
        returned_total = 1
        total = None
        complete = False

    def _chain(items):
        class _Outcome:
            collection = "sources"
            kind = ""
            local_only = False
            source_scoped = False
            coverage = _Coverage()

        class _C:
            outcome = _Outcome()
            state = "open"

        _C.outcome.items = items
        return _C()

    a1, r1 = _Row("X", "a1"), _Row("X", "r1")
    chain_a = _chain([a1])
    chains = {("a",): chain_a, ("b",): _chain([a1, r1])}
    numbering = rr._RosterNumbering()
    before = rr._enumeration_note(chains, numbering)
    chain_a.outcome.items.append(_Row("X", "a2"))
    after = rr._enumeration_note(chains, numbering)
    assert "《X（第 1 篇）》《X（第 2 篇）》" in before
    # 链 B 的账目:a1 第 1 篇、r1 第 2 篇,与续跑前一致;链 A 新增的 a2 是第 3 篇。
    assert "《X（第 1 篇）》《X（第 2 篇）》" in after.split("、")[1]
    assert "《X（第 1 篇）》《X（第 3 篇）》" in after.split("、")[0]


# ------------------------------------------- 已读账目 / 轨迹 / 枚举名解析(评审 P2)


def test_a_real_title_arriving_in_a_later_listing_does_not_renumber_the_ledger(repo):  # noqa: F811
    """P1 端到端:先列本库(两篇「系列」得到第 1/2 篇),再列全部,参考库里恰有一篇
    真标题「系列（第 2 篇）」。已发的号不改——第二轮账目里本库第二篇仍叫
    「系列（第 2 篇）」;模型此时抄这个串 → 两篇都命中 → 歧义 skip,绝不静默读到
    新来的那篇。"""
    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _seed_document(repo, active.id, "s-1", "系列", "有摘要", ("一。",))
    _seed_document(repo, active.id, "s-2", "系列", "有摘要", ("二。",))
    _seed_document(repo, reference.id, "s-ref", "系列（第 2 篇）", "有摘要", ("参考。",))
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()
    llm = _ValidatingLLM([
        _enumerate_sources_action(scope="current_notebook"),
        _enumerate_sources_action(),
        _read_action("系列（第 2 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(active.id, "这个库讲了什么", "")

    assert "《系列（第 1 篇）》《系列（第 2 篇）》" in llm.reflect_prompts[1]
    assert "同名第" not in llm.reflect_prompts[2]
    skip = _skips(result)["document_read_unresolved"]
    assert skip.detail["matches"] == 2
    assert "无法区分" in skip.summary          # 带序号的写法也歧义,不教模型再抄一次
    assert result.document_reads == []


def test_enumerating_a_colliding_placeholder_is_the_same_ambiguity_not_a_silent_pick(repo):  # noqa: F811
    """先发号、后到真标题撞号、再 `enumerate_elements` 该串:与 `read_document` 对同一个
    串的结论一致——走既有的 `enumeration_source_unresolved`(matches=2),既不定向到
    花名册那一篇、也不交回按名解析去静默列出真标题那一篇。"""
    from tests.test_reasoning_enumeration_tools import _add_titled_source, _enumerate_action

    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _add_titled_source(repo, active.id, "s-1", "系列", formulas=1)
    _add_titled_source(repo, active.id, "s-2", "系列", formulas=2)
    _add_titled_source(repo, reference.id, "s-ref", "系列（第 2 篇）", formulas=3)
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()
    llm = _ValidatingLLM([
        _enumerate_sources_action(scope="current_notebook"),
        _enumerate_sources_action(),
        _enumerate_action(source_title="系列（第 2 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(active.id, "列出公式", "")

    assert [o for o in result.enumerations if o.collection == "elements"] == []
    skip = _skips(result)["enumeration_source_unresolved"]
    assert skip.detail["matches"] == 2
    assert skip.detail["requested_title"] == "系列（第 2 篇）"


def test_the_read_ledger_and_trace_use_the_roster_placeholder(repo):  # noqa: F811
    """读完「第 1 篇」后,下一轮已读账目写的是「《周报（第 1 篇）》」(与花名册同口径),
    轨迹摘要同样带序号;detail.source 仍是真实标题。"""
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-a", "周报", "", ("甲篇开头。", "甲篇结论。"))
    _seed_document(repo, notebook.id, "s-b", "周报", "", ("乙篇开头。", "乙篇结论。"))
    llm = _ValidatingLLM([
        _enumerate_sources_action(),
        _read_action("周报（第 1 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(notebook.id, "这个库讲了什么", "")

    assert "已按篇读取过原文的文档,勿重复请求: 《周报（第 1 篇）》" in llm.reflect_prompts[2]
    step = next(s for s in result.trace if s.step_type == "read_document")
    assert "《周报（第 1 篇）》" in step.summary
    assert step.detail["source"] == "周报"


def test_a_placeholder_copied_into_enumerate_source_title_targets_the_roster_document(repo):  # noqa: F811
    """P2:模型把花名册占位串抄进 `enumerate_elements` 的来源名。参考库里另有一篇
    真标题恰为「系列（第 2 篇）」且不在花名册里:必须定向到花名册那一篇,而不是
    落到按名解析撞上参考库那篇。"""
    from tests.test_reasoning_enumeration_tools import _add_titled_source, _enumerate_action

    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _add_titled_source(repo, active.id, "s-1", "系列", formulas=1)
    _add_titled_source(repo, active.id, "s-2", "系列", formulas=2)
    _add_titled_source(repo, reference.id, "s-ref", "系列（第 2 篇）", formulas=3)
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()
    llm = _ValidatingLLM([
        _enumerate_sources_action(scope="current_notebook"),
        _enumerate_action(source_title="系列（第 2 篇）"),
        ANSWER,
    ], plan=PLAN)
    result = _reader(repo, llm).run(active.id, "列出公式", "")

    roster = [r.source_id for r in _sources_roster(result)]
    assert roster == ["s-1", "s-2"]
    elements = [o for o in result.enumerations if o.collection == "elements"]
    assert [o.source_id for o in elements] == ["s-2"]
    assert len(elements[0].items) == 2
