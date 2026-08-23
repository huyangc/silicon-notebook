"""来源显示名的单一真源:规则本身 + 「不许再写第四份」的源码级守卫。

规则:接地判定为论文且解析出非空 `paper_title` 时显示论文标题,否则退回普通
来源名、再退回文件名。它同时决定引用卡、证据卡与清单卡上的同一个来源叫什么
名字——三处一旦分叉,用户会在同一屏里看到同一篇论文有两个名字,所以这里既测
规则,也守住它只有一份实现。
"""
from __future__ import annotations

import ast
import pathlib
import sqlite3

import pytest

from app.services.source_display import source_display_title, summary_display_title

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# 唯一定义点:(仓库相对路径, 函数名)。B3:定义下沉到 app/domain,
# app/services/source_display.py 现在只 re-export,不再自己定义它。
DEFINITION_SITE = ("backend/app/domain/source_display.py", "source_display_title")

# 必须 import 它的三处显示路径。三处命名的是同一批来源,是这条规则存在的理由。
CONSUMERS = (
    "backend/app/services/evidence_context.py",
    "backend/app/services/retrieval_candidates.py",
    "backend/app/services/collection_enumeration.py",
)

# 读全这三个行键即构成「在自己算一遍显示名」的形状。存储层只是搬运列、
# 不做取舍,所以它们读不全这三个键(实测:全仓仅本文件登记的这几处命中)。
RULE_COLUMNS = frozenset({"is_paper", "paper_title", "file_name"})

# 豁免:全都是「只证明 SQL 选出了这几列」的地方,不含任何取舍逻辑。
ALLOWED_SITES = {
    DEFINITION_SITE,
    # PostgreSQL 存储层对等测试断言 `source_display_rows` 确实返回了这几列。
    (
        "backend/tests/postgres/test_core_store_conformance.py",
        "test_typed_collection_enumeration_primitives_match_sqlite_semantics",
    ),
    # 用户活动流的两份仓储实现:LEFT JOIN 一次取回 is_paper/paper_title,与
    # file_name 一起**原样**投影进返回行。显示名怎么取由路由层调
    # source_display_title 兑现——存储层一个分支都没有,这里读全三个键只是
    # 「把这几列搬出来」,与上面那条 PG 对等测试同一性质。
    (
        "backend/app/repositories/sqlite/query_store.py",
        "list_user_activity",
    ),
    (
        "backend/app/repositories/postgres/query_store.py",
        "list_user_activity",
    ),
}


# ------------------------------------------------------------------ 规则本身

def _row(**columns):
    """一行 `source_metadata` 形态的普通 dict 行。"""
    base = {"is_paper": None, "paper_title": None, "title": None, "file_name": None}
    base.update(columns)
    return base


def test_grounded_paper_title_wins_over_upload_name():
    assert source_display_title(_row(
        is_paper=True, paper_title="Attention Is All You Need",
        title="1706.03762v7", file_name="1706.03762v7.pdf",
    )) == "Attention Is All You Need"


def test_paper_title_is_ignored_when_the_row_is_not_a_paper():
    """`is_paper=false` 上残留的旧标题不得夺走普通来源名。"""
    assert source_display_title(_row(
        is_paper=False, paper_title="Stale title from an earlier parse",
        title="Design Notes", file_name="notes.md",
    )) == "Design Notes"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_paper_title_falls_back_even_on_a_paper_row(blank):
    """只有空白的标题不是标题——抽取被接地校验驳回时就是这个形状。"""
    assert source_display_title(_row(
        is_paper=True, paper_title=blank, title="Gate Sizing", file_name="gs.pdf",
    )) == "Gate Sizing"


def test_ordinary_title_falls_back_to_file_name():
    assert source_display_title(
        _row(title=None, file_name="uploaded.pdf")
    ) == "uploaded.pdf"
    assert source_display_title(
        _row(title="", file_name="uploaded.pdf")
    ) == "uploaded.pdf"


def test_whitespace_only_source_title_still_beats_the_file_name():
    """既有行为,原样保留:普通名分支先选 `title or file_name` 再 strip,所以
    只有空白的 `title` 是真值、会盖住文件名,最后 strip 成空串。

    三份旧实现逐字相同,合并时刻意不顺手「修」它:那是行为改动,得由一个说得
    出用户可见后果的改动来做,而不是混在提取共享函数里。空白标题本身是上游
    问题(来源入库时就不该存只有空格的标题)。
    """
    assert source_display_title(_row(title="   ", file_name="uploaded.pdf")) == ""


def test_titles_are_stripped_on_both_branches():
    assert source_display_title(
        _row(is_paper=True, paper_title="  Padded Paper  ")
    ) == "Padded Paper"
    assert source_display_title(_row(title="  Padded Source  ")) == "Padded Source"


def test_a_row_with_no_name_at_all_is_empty_not_a_placeholder():
    """调用方靠空串决定「跳过 / 用自己的兜底标签」,这里不许发明占位名。"""
    assert source_display_title(_row()) == ""
    assert source_display_title({}) == ""
    assert source_display_title(None) == ""


def test_driver_rows_without_get_are_supported():
    """枚举路径直接把 `sqlite3.Row` 交过来,而它没有 `.get`。"""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT 1 AS is_paper, 'Parsed Paper' AS paper_title, "
        "'upload' AS title, 'upload.pdf' AS file_name"
    ).fetchone()
    assert source_display_title(row) == "Parsed Paper"
    # 少了整列的行同样不能炸:引用路径就会传一个连键都没有的 {}。
    partial = connection.execute("SELECT 'only-a-name' AS title").fetchone()
    assert source_display_title(partial) == "only-a-name"
    connection.close()


# ------------------------------------------------- SourceSummary 的取数管道

def _summary_row(**columns):
    """`sources` 行的形态:论文那两列**不在**这张行上(它们住 source_paper_meta)。"""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    base = {"title": "", "file_name": ""}
    base.update(columns)
    select = ", ".join(f"? AS {name}" for name in base)
    row = connection.execute(f"SELECT {select}", tuple(base.values())).fetchone()
    connection.close()
    return row


def test_summary_display_title_prefers_the_grounded_paper_title():
    """左栏来源清单与中栏活动条目命名的是同一批来源,必须给出同一个名字。

    回归的是「同一篇论文在同一屏里有两个名字」:来源清单走 SourceSummary、
    活动流走 list_user_activity,两条路都必须落到 source_display_title。
    """
    row = _summary_row(title="1706.03762.pdf", file_name="1706.03762.pdf")
    meta = {"is_paper": True, "paper_title": "Attention Is All You Need"}
    assert summary_display_title(row, meta) == "Attention Is All You Need"


def test_summary_display_title_falls_back_when_the_source_has_no_paper_meta():
    """没有 source_paper_meta 行(meta=None)= 不是论文,不是「缺标题」。"""
    row = _summary_row(title="", file_name="q3-report.pdf")
    assert summary_display_title(row, None) == "q3-report.pdf"


def test_summary_display_title_does_not_reinstate_the_shadowed_file_name():
    """纯空白 title 遮蔽 file_name 返回空串——管道不得把它「修好」。

    这条正是 test_whitespace_only_source_title_still_beats_the_file_name 钉住的
    刻意行为;取数管道多写一层 `or file_name` 就会让同一份来源在来源清单里有名、
    在引用卡上无名。
    """
    row = _summary_row(title="   ", file_name="q3-report.pdf")
    assert summary_display_title(row, None) == ""


# ------------------------------------------------- 单一真源(源码级守卫)

def _row_key_reads_by_scope(text: str):
    """按函数作用域汇总「这段代码从行里读了哪些列名」。

    「读一个列名」= 字符串字面量出现在下标位 (`row["is_paper"]`) 或 `.get()`
    的第一个实参 (`row.get("is_paper")`)。用 AST 不用正则:正则分不清注释、
    文档串和 SQL 文本里的同名词,而那三样在存储层里到处都是。
    """
    tree = ast.parse(text)
    scope_of: dict[int, tuple[ast.AST, str]] = {}

    def descend(node, scope, name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                descend(child, child, child.name)
            else:
                scope_of[id(child)] = (scope, name)
                descend(child, scope, name)

    descend(tree, tree, "<module>")
    reads: dict[int, tuple[str, int, set[str]]] = {}

    def record(node, key):
        scope, name = scope_of.get(id(node), (tree, "<module>"))
        entry = reads.setdefault(
            id(scope), (name, getattr(scope, "lineno", 1), set())
        )
        entry[2].add(key)

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                record(node, index.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            record(node, node.args[0].value)
    return reads.values()


def test_display_title_rule_has_exactly_one_implementation():
    """全仓只能有一处自己读齐 `is_paper`/`paper_title`/`file_name` 的代码。

    再抄一份的代价不是风格:三处显示路径命名的是同一批来源,副本各自漂移
    (谁先 strip、谁认 `is_paper`、谁退回文件名)时,同一篇论文会在引用卡、
    证据卡和清单卡上叫三个名字,而每一处看上去都「对」。
    """
    scanned = 0
    offenders = []
    for root in ("backend/app", "backend/tests", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8")
            # 先按原文筛一遍再解析。扫描只认字符串字面量,而字面量的字符必然
            # 出现在原文里,所以三个列名缺任何一个的文件都不可能命中——省掉
            # 全仓七百多次 ast.parse(实测 3.5s → 0.1s)。这不只是测试自己快
            # 慢:后端泳道是并发跑的,一条烧 CPU 的守卫会把同批里靠墙钟判定
            # 的用例(backend.sh 生命周期测试只给子进程 4 秒)挤到超时。
            # (唯一漏网形状是跨字面量拼接出的键名,`row["paper_" "title"]`;
            # 那已经不是「照着抄一份」会写出来的东西。)
            if not all(column in text for column in RULE_COLUMNS):
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            for name, lineno, keys in _row_key_reads_by_scope(text):
                if not RULE_COLUMNS <= keys:
                    continue
                if (relative, name) in ALLOWED_SITES:
                    continue
                offenders.append(f"{relative}:{lineno} ({name})")
    assert scanned > 300, scanned          # 扫描面塌了要先红,别静默放行
    assert offenders == [], (
        "来源显示名的规则出现了第二份实现,必须改为 import "
        "app.services.source_display.source_display_title: " + ", ".join(offenders)
    )


def test_the_definition_site_still_defines_it():
    """守卫的白名单必须钉在真实存在的定义点上。

    定义被挪走而白名单还留在原地,上面那条扫描就会变成一条永远为空的断言
    ——它只数副本,数不出真源没了。
    """
    path, function = DEFINITION_SITE
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function in defined, f"{path} 不再定义 {function}(),白名单已失效"


@pytest.mark.parametrize("relative", CONSUMERS)
def test_every_display_path_imports_the_shared_rule(relative):
    """三处显示路径必须 import 同一个函数,而不是各自复刻。"""
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "app.services.source_display"
        for alias in node.names
    }
    assert "source_display_title" in imported, (
        f"{relative} 没有 import app.services.source_display.source_display_title"
    )
