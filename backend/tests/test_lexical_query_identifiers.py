"""Task 1 (P0-1): identifier-aware lexical terms.

`lexical_recall_terms` splits on non-alnum boundaries, so `_`/`-`/`.`-joined
identifiers (e.g. `set_db`) get shattered into short fragments that a <3-char
SQLite trigram FTS token can never match. These tests pin the pre-change
snapshot for queries with no such identifiers, assert the new identifier
terms show up for queries that do have them, and add an in-memory SQLite
trigram FTS regression proving the true command entry now ranks first.
"""
import sqlite3

from app.repositories.lexical_query import (
    MAX_LEXICAL_TERMS,
    exact_probe_terms,
    identifier_terms,
    lexical_recall_terms,
    model_name_alias_terms,
    sqlite_fts_match_expression,
)


def test_pure_english_query_unchanged():
    # Captured from the pre-change implementation; no `_`/`-`/`.`-joined
    # identifier is present, so the output must be byte-identical.
    assert lexical_recall_terms("current mirror design guidelines") == [
        "current mirror design guidelines",
        "current",
        "mirror",
        "design",
        "guidelines",
    ]


def test_pure_chinese_query_unchanged():
    assert lexical_recall_terms("深度学习系统架构设计指南") == [
        "深度学习系统架构设计指南",
        "深度学",
        "度学习",
        "学习系",
        "习系统",
        "系统架",
        "统架构",
        "架构设",
        "构设计",
        "设计指",
        "计指南",
    ]


def test_mixed_cjk_latin_without_separator_unchanged():
    # "DeepSeek" and "ZXCV9000" are separate alnum runs with no
    # `_`/`-`/`.` between them, so no identifier is extracted.
    assert lexical_recall_terms("DeepSeek ZXCV9000 深度学习系统") == [
        "DeepSeek ZXCV9000 深度学习系统",
        "DeepSeek",
        "ZXCV9000",
        "深度学习系统",
        "深度学",
        "度学习",
        "学习系",
        "习系统",
    ]


def test_underscore_identifier_extracted():
    terms = lexical_recall_terms("set_db 命令是怎样的")
    assert "set_db" in terms


def test_multi_segment_underscore_identifier_extracted():
    terms = lexical_recall_terms("place_opt_design 怎么用")
    assert "place_opt_design" in terms


def test_identifier_placed_after_exact_phrase_before_run_terms():
    terms = lexical_recall_terms("set_db 命令是怎样的")
    assert terms[0] == "set_db 命令是怎样的"
    assert terms[1] == "set_db"


def test_trailing_punctuation_not_absorbed():
    assert identifier_terms("run set_db.") == ["set_db"]


def test_numeric_only_matches_are_rejected():
    # Review-measured cost regression: `2.1`-style matches are low-selectivity
    # substrings (a "第 2.1 节讲了什么" probe went 0.7ms/3 hits → 22ms/200 hits)
    # with zero recall benefit, so an identifier must contain an ASCII letter.
    assert identifier_terms("3.14") == []
    assert identifier_terms("2023-2024") == []
    assert identifier_terms("第 2.1 节讲了什么") == []
    # Letter-bearing version-like identifiers stay in.
    assert identifier_terms("GPT-4 和 v1.2 的差异") == ["GPT-4", "v1.2"]


def test_identifier_terms_dedupes_in_appearance_order():
    assert identifier_terms("set_db uses set_db internally") == ["set_db"]


def test_identifier_terms_requires_a_separator_and_min_length():
    # A run with no `_`/`-`/`.` separator is not an identifier at all.
    assert identifier_terms("hello world") == []
    # The >=4-char floor drops 3-char joins like `a_b`/`e.g` (abbreviation
    # noise), while a 4-char join with a letter is kept.
    assert identifier_terms("a_b") == []
    assert identifier_terms("e.g and i.e are common") == []
    assert identifier_terms("x2_y") == ["x2_y"]


def test_identifier_count_and_length_are_capped():
    # At most MAX_IDENTIFIER_TERMS survive so a pasted command list cannot
    # monopolise the shared 64-term budget.
    blob = " ".join(f"cmd_{index:02d}" for index in range(20))
    assert len(identifier_terms(blob)) == 16
    # A joined run longer than the exact-phrase cap is dropped entirely.
    assert identifier_terms("ab" + "_ab" * 200) == []


# ------------------------------------------------ exact-probe gate (narrower)
def test_exact_probe_terms_keeps_underscore_and_dot_forms():
    # 下划线/点连接的形态散文里根本不会出现,一律保留。
    assert exact_probe_terms("set_db 命令是怎样的") == ["set_db"]
    assert exact_probe_terms("place_opt_design 怎么用") == ["place_opt_design"]
    assert exact_probe_terms("config.yaml 里配什么") == ["config.yaml"]
    assert exact_probe_terms("看 report.tcl 和 timing_report") == [
        "report.tcl", "timing_report"]


def test_exact_probe_terms_rejects_digitless_hyphen_words():
    """整支评审阻塞项 3:纯连字符的普通英文词组不配一次精确探测。

    2 万块的库上 `state-of-the-art` 单次探测 16ms/50 命中;报告引擎每节的问题
    恒抽出这批词,等于每节白付一次探测,而 `real-time` 命中章节标题时整章 12 块
    以 1.0 分进了证据。它们不是名称,是形容词。
    """
    for phrase in ("state-of-the-art", "real-time", "end-to-end", "high-level",
                   "well-known", "so-called"):
        assert identifier_terms(phrase) == [phrase], "词法召回侧保持宽定义"
        assert exact_probe_terms(phrase) == [], phrase


def test_exact_probe_terms_keeps_hyphen_forms_that_carry_a_digit():
    # 带数字的连字符形态是版本/型号名,不是英文词组——这条线是形状判据,
    # 不是对语义的猜测:英文合成词不带数字,版本/型号名基本都带。
    assert exact_probe_terms("GPT-4 和 v1-2 的差异") == ["GPT-4", "v1-2"]
    assert exact_probe_terms("跑 llama-3 还是 qwen3-8b") == ["llama-3", "qwen3-8b"]


def test_model_alias_terms_exclude_numbered_prose_labels():
    assert model_name_alias_terms("cosmos3 and Cosmos 3") == [
        "cosmos 3", "Cosmos3",
    ]
    assert model_name_alias_terms("chapter 4 and version 3") == []


def test_exact_probe_terms_is_a_subset_of_identifier_terms_in_order():
    text = "state-of-the-art 的 set_db 与 real-time 的 GPT-4 和 config.yaml"
    wide = identifier_terms(text)
    narrow = exact_probe_terms(text)
    assert wide == ["state-of-the-art", "set_db", "real-time", "GPT-4",
                    "config.yaml"]
    assert narrow == ["set_db", "GPT-4", "config.yaml"]
    assert narrow == [term for term in wide if term in narrow], "保序的子集"


def test_report_style_section_question_probes_nothing():
    """报告引擎的逐节问题是多行长文,恒含这批词——它们必须一次探测都不发。"""
    sec_question = (
        "本节需要说明该方法与 state-of-the-art 方案的差异,\n"
        "并讨论 end-to-end 的 real-time 表现与 high-level 设计取舍。"
    )
    assert exact_probe_terms(sec_question) == []


def test_cjk_terms_survive_truncation_when_identifiers_overflow():
    # A pasted command list + a Chinese question overflowing MAX_LEXICAL_TERMS:
    # the CJK tri-grams live at the tail and get squeezed out by the identifier
    # terms sitting ahead of the runs. Segments must be distinct >=3-char runs
    # (`verbNN`/`nounNN`) or dedup/the <3 floor keeps the total under 64 and
    # truncation never happens at all.
    commands = " ".join(f"verb{index:02d}_noun{index:02d}" for index in range(26))
    query = f"{commands} 这些命令有什么区别和联系"
    terms = lexical_recall_terms(query)
    assert len(terms) == 64            # truncation really occurred
    assert "verb00_noun00" in terms    # identifiers still prioritized
    cjk_terms = [t for t in terms if any("一" <= c <= "鿿" for c in t)]
    assert len(cjk_terms) >= 8, cjk_terms


def test_identifier_free_overflow_keeps_the_historical_truncation():
    """无标识符的查询在**溢出分支**也必须逐位等于历史截断。

    S3:此前只有「不溢出」这条路径有守卫,`if not ident_terms: return keep` 这
    条捷径删掉全仓零红。它守的恰恰是这个形状——latin 词项先把 64 个名额占满、
    中文三字片段落在窗口之外:没有捷径,CJK 保留配额会把尾部 8 个 latin 词项
    换成中文片段,而那条配额是为「注入了标识符」才付的代价买单的。
    """
    latin = [f"aaa{index:02d}" for index in range(MAX_LEXICAL_TERMS)]
    query = " ".join(latin) + " 这些内容有什么区别和联系呢"
    terms = lexical_recall_terms(query)

    # 整句超过 MAX_EXACT_PHRASE_CHARS,所以首项不是原句,窗口整整 64 个 latin 词项。
    assert identifier_terms(query) == []
    assert terms == latin
    assert not any(any("一" <= char <= "鿿" for char in term) for term in terms)


def _fts_trigram_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='trigram')")
    rows = [
        ("set_db 命令用于设置数据库属性。set_db timing_analysis_type ocv",),
        ("the settings dialog can be set up to use db files",),
        ("place_opt_design performs placement and optimization",),
        ("先 set 好参数,再连接 db,完成 setup",),
    ]
    db.executemany("INSERT INTO t (text) VALUES (?)", rows)
    return db


def test_identifier_term_wins_sqlite_trigram_fts_ranking():
    db = _fts_trigram_db()
    match_query = sqlite_fts_match_expression("set_db 命令是怎样的")
    rows = db.execute(
        "SELECT text, bm25(t) AS rank FROM t WHERE t MATCH ? ORDER BY rank",
        (match_query,),
    ).fetchall()
    assert rows, "expected at least one FTS hit"
    assert rows[0]["text"].startswith("set_db 命令用于设置数据库属性")
