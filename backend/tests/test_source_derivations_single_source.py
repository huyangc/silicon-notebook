"""来源异常两个派生态的单一真源:规则本身 + 「不许再抄一份」的源码级守卫。

两个派生:

* ``extraction_warning_text`` —— 最近一次抽取的 ``windows_failed=N/T`` → 用户可见
  告警文案;
* ``paper_meta_status`` —— 论文元数据四态 has_meta / not_paper / missing / None。

它们一起喂给前端 ``anomaly-severity.ts`` 的 ``sourceAnomalies()``,决定来源上挂哪个
异常徽标。来源页签和用户活动流命名、标注的是**同一批来源**:四份存储层各抄一份的
话,任何一处漂移都会让同一份来源在两个界面显示不同的异常态,而每一处看上去都「对」
——与 ``source_display_title``(见 ``test_source_display_title.py``)是同一类问题,
所以守卫也照它的形状写。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from app.models.sources import extraction_warning_text, paper_meta_status

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

DEFINITION_FILE = "backend/app/models/sources.py"
DEFINITIONS = ("extraction_warning_text", "paper_meta_status")

# 必须 import 这两个派生的四处存储层。它们描述的是同一批来源,是这条规则的理由。
CONSUMERS = (
    "backend/app/repositories/sqlite/source_store.py",
    "backend/app/repositories/postgres/source_store.py",
    "backend/app/repositories/sqlite/query_store.py",
    "backend/app/repositories/postgres/query_store.py",
)

# 「抄了一份告警文案」的形状:文案本身出现在某个字符串字面量里。
WARNING_MARKER = "部分内容因网络问题未完成分析"
# 「抄了一份四态派生」的形状:同一个函数作用域里同时出现这两个终态字面量。
# 单独一个不算:source_ingestion 的抽取回执也返回 "not_paper",那是另一件事。
STATUS_LITERALS = frozenset({"has_meta", "not_paper"})

SCANNED_ROOTS = ("backend/app", "scripts")


# ------------------------------------------------------------------ 规则本身

def test_windows_failed_marker_becomes_the_user_warning():
    assert extraction_warning_text("kg objects=3 windows_failed=2/5") == (
        "部分内容因网络问题未完成分析（2/5 段失败），建议重新上传或重试。"
    )


@pytest.mark.parametrize(
    "clean",
    ["", None, "kg objects=3", "windows_failed=0/5", "windows_skipped=2"],
)
def test_a_clean_run_carries_no_warning(clean):
    """``windows_failed=0/N`` 是**干净**的完成记录,不是降级——必须没有告警。"""
    assert extraction_warning_text(clean) is None


def test_paper_meta_four_states():
    assert paper_meta_status(True, "pdf", "", "parsed") == "has_meta"
    assert paper_meta_status(1, "pdf", "", "parsed") == "has_meta"
    # 标记行(有 meta 行但 is_paper 假)= 已判定非论文,不是「还没抽」。
    assert paper_meta_status(False, "pdf", "", "parsed") == "not_paper"
    assert paper_meta_status(0, "pdf", "", "parsed") == "not_paper"
    # 没有 meta 行:合规候选 = missing。
    assert paper_meta_status(None, "pdf", "", "parsed") == "missing"
    assert paper_meta_status(None, "pdf", "academic_paper", "extracted") == "missing"


@pytest.mark.parametrize(
    "source_type,doc_type,parse_status",
    [
        ("memory", "", "parsed"),        # 隐藏合成源
        ("knowhow", "", "parsed"),       # 隐藏投影行
        ("pdf", "tool_manual", "parsed"),  # 非论文 doc_type
        ("pdf", "", "queued"),           # 还没解析完
        ("pdf", "", "failed"),
    ],
)
def test_not_applicable_is_none_not_missing(source_type, doc_type, parse_status):
    """「不适用」必须是 None:报成 missing 会让看板催用户去抽一批抽不了的源。"""
    assert paper_meta_status(None, source_type, doc_type, parse_status) is None


# ------------------------------------------------- 单一真源(源码级守卫)

def _scopes(text: str):
    """按函数作用域汇总「这段代码里出现了哪些字符串字面量」。

    用 AST 不用正则:正则分不清注释、文档串和 SQL 文本里的同名词,而那三样在存储层
    里到处都是(本文件自己的注释就提到这几个词)。
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
    found: dict[int, tuple[str, int, set[str]]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        scope, name = scope_of.get(id(node), (tree, "<module>"))
        entry = found.setdefault(
            id(scope), (name, getattr(scope, "lineno", 1), set())
        )
        entry[2].add(node.value)
    return found.values()


def _scan(prefilter, matches):
    """遍历 app/ 与 scripts/,返回 (命中列表, 扫描文件数)。

    先按原文子串预筛再解析:扫描只认字符串字面量,字面量的字符必然出现在原文里,
    所以筛不中的文件不可能命中。全仓 rglob + ast.parse 是秒级开销,而后端泳道是并发
    跑的,一条烧 CPU 的守卫会把同批里按墙钟判定的用例挤到超时。
    """
    offenders: list[str] = []
    scanned = 0
    for root in SCANNED_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8")
            if not prefilter(text):
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == DEFINITION_FILE:
                continue
            for name, lineno, literals in _scopes(text):
                if matches(literals):
                    offenders.append(f"{relative}:{lineno} ({name})")
    return offenders, scanned


def test_the_extraction_warning_text_has_exactly_one_implementation():
    offenders, scanned = _scan(
        lambda text: WARNING_MARKER in text,
        lambda literals: any(WARNING_MARKER in value for value in literals),
    )
    assert scanned > 300, scanned          # 扫描面塌了要先红,别静默放行
    assert offenders == [], (
        "抽取告警文案出现了第二份实现,必须改为 import "
        "app.models.sources.extraction_warning_text: " + ", ".join(offenders)
    )


def test_the_paper_meta_status_rule_has_exactly_one_implementation():
    offenders, scanned = _scan(
        lambda text: all(literal in text for literal in STATUS_LITERALS),
        lambda literals: STATUS_LITERALS <= literals,
    )
    assert scanned > 300, scanned
    assert offenders == [], (
        "论文元数据四态派生出现了第二份实现,必须改为 import "
        "app.models.sources.paper_meta_status: " + ", ".join(offenders)
    )


def test_the_definition_site_still_defines_them():
    """白名单必须钉在真实存在的定义点上。

    定义被挪走而白名单还留在原地,上面两条扫描就会变成永远为空的断言——它们只数
    副本,数不出真源没了。
    """
    tree = ast.parse((REPO_ROOT / DEFINITION_FILE).read_text(encoding="utf-8"))
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for function in DEFINITIONS:
        assert function in defined, f"{DEFINITION_FILE} 不再定义 {function}()"


@pytest.mark.parametrize("relative", CONSUMERS)
@pytest.mark.parametrize("function", DEFINITIONS)
def test_every_store_imports_the_shared_derivations(relative, function):
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.models.sources"
        for alias in node.names
    }
    assert function in imported, (
        f"{relative} 没有 import app.models.sources.{function}"
    )
