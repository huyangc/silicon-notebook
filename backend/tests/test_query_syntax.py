"""英文双引号 = 完整短语:唯一的用户检索语法。

这批用例是**跨端契约**:`frontend/app/query-syntax.test.mjs` 逐条镜像同一组输入
输出。两侧规则不一致,用户就会在提问框看到「精确短语:X」而检索侧根本没按 X 检索。
"""
import pytest

from app.core.query_syntax import (
    MAX_QUOTED_PHRASES,
    MIN_LEXICAL_TERM_CHARS,
    exact_probe_query,
    quoted_phrases,
    retrieval_query_head,
    split_quoted_phrases,
    strip_accepted_quote_markers,
    strip_quote_markers,
    unquoted_remainder,
)
from app.repositories.lexical_query import exact_probe_terms, lexical_recall_terms


def test_retrieval_query_head_removes_only_service_contract_envelopes():
    head = "cosmos3 主要贡献"
    assert retrieval_query_head(
        head + "\n\n检索必须服从以下已确认问题契约：\n" + "契约" * 100
    ) == head
    assert retrieval_query_head(
        head + "\n\n用户确认的补充信息与问题契约：\n研究对象：cosmos3"
    ) == head
    # Ordinary user prose is not a service envelope.
    prose = "请解释检索必须服从以下已确认问题契约是什么意思"
    assert retrieval_query_head(prose) == prose


def test_mixed_letter_digit_model_names_get_symmetric_lexical_aliases():
    compact = {term.casefold() for term in lexical_recall_terms("cosmos3")}
    spaced = {term.casefold() for term in lexical_recall_terms("Cosmos 3")}
    assert "cosmos 3" in compact
    assert "cosmos3" in spaced


# --- 跨端契约:这份表 frontend/app/query-syntax.test.mjs 必须逐条相同 -----------
PARITY_CASES = [
    ('什么是 "static timing analysis" 的原理', ["static timing analysis"]),
    ('比较 "OCV" 与 "AOCV 分析" 的差异', ["OCV", "AOCV 分析"]),
    ("没有引号的普通问题", []),
    ('单个未闭合的 " 引号', []),
    # 太短:SQLite 的三字符索引根本索引不到,识别了也检索不出来。
    ('他说"这个"和"那个"的区别', []),
    # 大小写不敏感去重,保留首次出现的写法。
    ('"Set DB" 与 "set db" 有区别吗', ["Set DB"]),
    # 内部空白归一,首尾空白丢弃。
    ('"  static   timing  " 是什么', ["static timing"]),
    # 引号太密 = 机器文本(JSON 信封),整段语法不生效。
    ('{"a":"aaa","b":"bbb","c":"ccc","d":"ddd","e":"eee"}', []),
    # 折叠用 .lower() 而非 .casefold():casefold 会把 ß→ss,让这两段折成一段,
    # 而前端的 toLowerCase() 不会——回执与检索就会报出不同的短语集合。
    ('"straße" 与 "STRASSE"', ["straße", "STRASSE"]),
    # 非 BMP 文字按**码点**计长。这两条必须成对:只有贴着下限的那条能分辨
    # 「码点 vs UTF-16 code unit」——两个非 BMP 字符的 len 是 2(拒),
    # 而 JS 的 .length 是 4(收)。三个字符两种口径都通过,分辨不出任何东西。
    ('"\U00020000\U00020001\U00020002" 是什么', ["\U00020000\U00020001\U00020002"]),
    ('"\U00020000\U00020001" 是什么', []),
    # 同一段短语重复出现不算「引号太密」:内部检索问题会把它在目标、规范化问题
    # 和每个必答主题里各留一份,按出现次数数就会在推理/报告里把语法整个关掉。
    ('"静态时序分析" a "静态时序分析" b "静态时序分析" c "静态时序分析" '
     'd "静态时序分析" e "静态时序分析"', ["静态时序分析"]),
]


@pytest.mark.parametrize("text,expected", PARITY_CASES)
def test_quoted_phrases_parity_table(text, expected):
    assert quoted_phrases(text) == expected


def test_remainder_blanks_accepted_spans_and_keeps_offsets():
    text = '什么是 "static timing analysis" 的原理'
    phrases, remainder = split_quoted_phrases(text)
    assert phrases == ["static timing analysis"]
    # 长度不变 → 位置对齐;被识别的那段(连同两个引号)整体变成空白,不再参与分词。
    span = '"static timing analysis"'
    assert remainder == text.replace(span, " " * len(span))
    assert len(remainder) == len(text)


def test_rejected_span_stays_in_the_remainder():
    # 关键不变式:没被识别成短语的引号内容**必须**留在余量里,否则它既不是短语
    # 也不再被分词,等于从查询里凭空消失。
    text = '他说"这个"很重要'
    phrases, remainder = split_quoted_phrases(text)
    assert phrases == []
    assert remainder == text


def test_too_many_spans_disables_the_syntax_entirely():
    text = " ".join(f'"phrase{index}"' for index in range(MAX_QUOTED_PHRASES + 1))
    assert quoted_phrases(text) == []
    assert unquoted_remainder(text) == text


def test_repeating_one_phrase_is_not_too_many_spans(monkeypatch):
    """闸数的是**不同**的引号内容——这是 codex #410 round-4 的阻断形状。

    `confirmed_research_question` 把 objective、规范化问题、实体和最多 16 条必答
    主题拼成内部检索问题,而规划 prompt 恰恰要求模型把用户的引号原样带进每一条。
    按出现次数数,一个再普通不过的单短语问题也会在推理/报告里越界,把语法整个
    关掉——正好关在它最该生效的地方。
    """
    expanded = " ".join(
        [f'目标:"static timing analysis"']
        + [f'必答主题{i}:"static timing analysis"'
           for i in range(MAX_QUOTED_PHRASES + 3)]
    )
    assert quoted_phrases(expanded) == ["static timing analysis"]
    # 但不同的内容仍然照旧被拦(JSON 信封那条形状不受影响)。
    assert quoted_phrases(
        " ".join(f'"distinct{i}"' for i in range(MAX_QUOTED_PHRASES + 1))) == []


def test_at_the_limit_the_syntax_still_applies():
    text = " ".join(f'"phrase{index}"' for index in range(MAX_QUOTED_PHRASES))
    assert len(quoted_phrases(text)) == MAX_QUOTED_PHRASES


def test_typographic_quotes_are_not_the_syntax():
    # 中文排版引号在散文里就是普通引用/强调,认它等于把大量既有提问悄悄变成
    # 带约束的提问。
    assert quoted_phrases("他说“静态时序分析”很重要") == []


def test_quotes_do_not_span_lines():
    assert quoted_phrases('第一行有个 "\n第二行也有个 "') == []


def test_strip_markers_only_around_accepted_spans():
    """语法没生效的引号必须原样留着——那是普通查询内容,不是标记。

    codex #410 round-5 P2:无条件删 `"` 会砸掉密度闸本来要保护的那个场景——
    用户在笔记本搜索框里搜一段字面 JSON/代码,引号被抹掉就再也匹配不上。
    """
    assert strip_accepted_quote_markers('什么是 "static timing" 呢') == "什么是 static timing 呢"
    # 太短 → 不被接受 → 引号留着。
    assert strip_accepted_quote_markers('他说"ab"很重要') == '他说"ab"很重要'
    # 未闭合 → 引号留着。
    assert strip_accepted_quote_markers('只有一个 " 引号') == '只有一个 " 引号'
    # 密度闸关掉语法 → 整段原样(搜字面 JSON 的场景)。
    envelope = '{"a":"aaa","b":"bbb","c":"ccc","d":"ddd","e":"eee"}'
    assert strip_accepted_quote_markers(envelope) == envelope
    # 混合:被接受的那段去标记,被拒的那段留着。
    assert strip_accepted_quote_markers('"static timing" 与 "ab"') == 'static timing 与 "ab"'


def test_probe_term_sanitizer_still_drops_every_quote():
    # `exact_probe_query` 把每个名称重新包成引号跨度,名称自带 `"` 会破坏那层编码,
    # 所以那一处用的是无条件版本——它是单值清洗,不是查询级变换。
    assert strip_quote_markers('a"b') == "ab"
    assert exact_probe_terms(exact_probe_query(['a"b c'])) == ["ab c"]


# --- 词法层:引号内不再被拆开 -------------------------------------------------
def test_quoted_span_is_one_term_and_its_words_are_not_emitted():
    terms = lexical_recall_terms('什么是 "static timing analysis" 的原理')
    assert terms[0] == "static timing analysis"
    # 整句项去掉了引号字符(带引号的整句在任何文档里都不存在)。
    assert terms[1] == "什么是 static timing analysis 的原理"
    # 短语内部的词绝不单独成项——那正是引号要禁止的拆分。
    assert "static" not in terms and "timing" not in terms and "analysis" not in terms


def test_query_without_quotes_is_byte_identical():
    # 无引号查询必须逐位等于本特性之前的输出(这批期望值来自改动前的实现)。
    assert lexical_recall_terms("current mirror design guidelines") == [
        "current mirror design guidelines",
        "current", "mirror", "design", "guidelines",
    ]
    assert lexical_recall_terms("深度学习系统架构设计指南") == [
        "深度学习系统架构设计指南",
        "深度学", "度学习", "学习系", "习系统", "系统架", "统架构",
        "架构设", "构设计", "设计指", "计指南",
    ]


def test_quoted_phrases_survive_the_lexical_term_cap():
    # 短语在最前,64 项截断与 CJK 保留配额都够不到它。
    commands = " ".join(f"verb{index:02d}_noun{index:02d}" for index in range(26))
    terms = lexical_recall_terms(f'"整体不可拆的短语" {commands} 这些命令有什么区别和联系')
    assert terms[0] == "整体不可拆的短语"
    assert len(terms) == 64


def test_sqlite_fts_expression_quotes_the_phrase_as_one_term():
    from app.repositories.lexical_query import sqlite_fts_match_expression

    expression = sqlite_fts_match_expression('"static timing analysis" 原理')
    # FTS5 trigram 下,一个带引号的词项就是一次字面子串匹配。
    assert expression.startswith('"static timing analysis" OR ')


# --- 精确通道:用户的引号进闸,模型的不进 --------------------------------------
def test_user_quoted_phrase_earns_an_exact_probe():
    assert exact_probe_terms('"static timing analysis" 是什么') == [
        "static timing analysis"]


def test_phrases_lead_the_identifiers_they_share_a_query_with():
    assert exact_probe_terms('"整节取齐" 与 set_db') == ["整节取齐", "set_db"]


def test_probe_terms_dedupe_across_both_producers():
    """引号里和引号外是同一个名称时,只值一次探测、一个名额。

    codex #410 round-2 P2:遮蔽只作用于被引起来的那次出现,同名的裸写法仍会被
    标识符抽取拿到,于是同一个名称从两个生产者各来一份。默认 3 个名额下,重复
    一个就意味着真正不同的第三个名称被整个丢掉。
    """
    assert exact_probe_terms('"set_db" 与 set_db 有什么区别') == ["set_db"]
    # 大小写不同也是同一次探测(FTS5 trigram 与 ILIKE 都不分大小写)。
    assert exact_probe_terms('"Set_DB" 与 set_db') == ["Set_DB"]
    # 去重不能吃掉真正不同的名称。
    assert exact_probe_terms('"set_db" set_db config.yaml GPT-4') == [
        "set_db", "config.yaml", "GPT-4"]


def test_model_supplied_text_does_not_get_the_quote_gate():
    # 模型给的 exact_term 走 honor_quotes=False:否则 `x "的方法" y` 就能把
    # 按实测定标的低选择度子串闸绕开。
    assert exact_probe_terms('x "的方法" y', honor_quotes=False) == []
    assert exact_probe_terms('x "的方法" y') == ["的方法"]


def test_probe_query_round_trips_the_terms_it_encodes():
    # 通道会对收到的串重新抽名称,所以编码必须原样往返——多词短语裸拼接会丢。
    terms = ["set_db", "static timing analysis", "config.yaml"]
    assert exact_probe_terms(exact_probe_query(terms)) == terms
    assert exact_probe_terms(" ".join(terms)) != terms, "裸拼接确实丢短语"


def test_probe_query_capacity_matches_the_phrase_limit():
    # 编码后每个名称都是一个引号短语,所以一次能往返的条数受同一个上限约束;
    # `_exact_lookup_terms` 按这个数夹紧,轨迹才不会记多于实际探测的名称。
    terms = [f"cmd_{index:02d}" for index in range(MAX_QUOTED_PHRASES)]
    assert exact_probe_terms(exact_probe_query(terms)) == terms


def test_min_length_floor_is_the_index_floor():
    short = "ab"
    assert len(short) < MIN_LEXICAL_TERM_CHARS
    assert quoted_phrases(f'"{short}" 是什么') == []


# --- PostgreSQL Memory 候选:引号短语的转义与排序 ------------------------------
def test_pg_memory_phrase_probes_are_escaped_and_ranked():
    """短语探测必须转义 LIKE 元字符,并参与排序。

    codex #410 round-7:①`set_db` / `100% coverage` 里的 `_`/`%` 不转义就成了通配,
    把不含该短语的记忆放进有界候选池、挤掉真命中——而短语这条路径的全部意义就是
    精确;②只按整句相似度排序,会在超过 limit 条松散命中时,把「只含短语、不含整句」
    的那条在服务层的短语感知打分看到它之前就砍掉,正好废掉这次新增的独立探测。

    这条不需要活的 PostgreSQL:它断言的是构造出来的 SQL 与参数,而 PG 集成门在
    另一条泳道。同时静态核对占位符与参数数量——数不齐在真库上就是运行时炸。
    """
    import re

    from app.repositories.postgres.search import (
        MemoryCandidateScope,
        _memory_match_predicates,
        _memory_rank_expression,
        memory_match_probes,
    )

    scope = MemoryCandidateScope(owner_id="u", viewer_id="u", notebook_id="n")
    predicates, params = _memory_match_predicates(
        "how does set_db work", scope, ["set_db", "100% coverage"]
    )
    patterns = [p for p in params if isinstance(p, str) and p.startswith("%")]
    assert "%set\\_db%" in patterns, "短语的 `_` 必须转义,否则 setXdb 也进候选"
    assert "%100\\% coverage%" in patterns
    # 整句探测保持历史的未转义形态(放宽候选无害,收紧会静默改变既有召回)。
    assert "%how does set_db work%" in patterns

    probes = memory_match_probes("how does set_db work", ["set_db", "100% coverage"])
    rank = _memory_rank_expression(probes)
    assert rank.count("public.similarity") == 3 * len(probes), "每个探测都要能排序"

    # 占位符与参数必须逐一对齐(ORDER BY 每个探测 3 个 + LIMIT 1)。
    holes = len(re.findall(r"(?<!%)%s", " AND ".join(predicates).replace("%%", "\x00")))
    rank_holes = len(re.findall(r"(?<!%)%s", rank.replace("%%", "\x00")))
    assert holes + rank_holes + 1 == len(params) + 3 * len(probes) + 1


def test_pg_memory_candidate_sql_ranks_every_probe():
    """守的是 `memory_candidate_ids` 里的**接线**,不是那两个 helper。

    ⚠ 只断言 helper 挡不住「helper 正确、调用点仍只按整句排序」——第一版守卫正是
    这样打空的。这里用一个记录 SQL 的假连接直接观察真正发出去的语句,并核对占位符
    与参数逐一对齐(数不齐在真库上就是运行时炸)。
    """
    import re

    from app.repositories.postgres.search import (
        MemoryCandidateScope,
        memory_candidate_ids,
    )

    captured: dict = {}

    class _Cursor:
        def fetchall(self):
            return []

    class _Connection:
        def execute(self, statement, params):
            captured["sql"] = statement
            captured["params"] = list(params)
            return _Cursor()

    memory_candidate_ids(
        _Connection(),
        "how does set_db work",
        10,
        scope=MemoryCandidateScope(owner_id="u", viewer_id="u", notebook_id="n"),
        phrase_queries=["set_db", "timing closure"],
    )
    order_by = captured["sql"].split("ORDER BY", 1)[1]
    assert order_by.count("public.similarity") == 9, (
        "3 个探测 × 3 列都要进排序,否则只含短语的记忆在 LIMIT 前就被砍掉")
    holes = len(re.findall(r"(?<!%)%s", captured["sql"].replace("%%", "\x00")))
    assert holes == len(captured["params"])
