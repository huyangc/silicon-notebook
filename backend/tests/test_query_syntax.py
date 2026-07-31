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
    split_quoted_phrases,
    strip_quote_markers,
    unquoted_remainder,
)
from app.repositories.lexical_query import exact_probe_terms, lexical_recall_terms


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


def test_at_the_limit_the_syntax_still_applies():
    text = " ".join(f'"phrase{index}"' for index in range(MAX_QUOTED_PHRASES))
    assert len(quoted_phrases(text)) == MAX_QUOTED_PHRASES


def test_typographic_quotes_are_not_the_syntax():
    # 中文排版引号在散文里就是普通引用/强调,认它等于把大量既有提问悄悄变成
    # 带约束的提问。
    assert quoted_phrases("他说“静态时序分析”很重要") == []


def test_quotes_do_not_span_lines():
    assert quoted_phrases('第一行有个 "\n第二行也有个 "') == []


def test_strip_quote_markers_keeps_the_words():
    assert strip_quote_markers('什么是 "static timing" 呢') == "什么是 static timing 呢"


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
