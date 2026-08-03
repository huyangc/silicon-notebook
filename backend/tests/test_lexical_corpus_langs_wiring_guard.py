"""语料语言探针**绝不在持有外层连接时**再开第二条连接的源码级守卫。

`_lexical_corpus_langs` 冷缓存时自己要开一条读连接。词法对象召回
(`_lexical_object_hits` / `_relation_lexical_candidate_ids`)总是拿着调用方
已经打开的 `db` 跑,在它们内部探测就是**嵌套获取**:并发的报告分节各自持有
外层连接时,谁都拿不到第二条,只能等到池超时——而那个超时会被这些方法自己的
fail-open `except` 吞掉,变成一条空的词法臂。那正是这个语言闸要消灭的故障,
只不过换成从连接池进来。

所以形态是硬的:**探针一律在 `with self._connect()` / `with
self.database.connect()` 之前跑,`corpus_langs` 作为参数传进去。** 三个 chunk
调用点本来就是这个形态,这条守卫把四个 KG/关系调用点也钉在同一条线上。

按原文子串预筛,只解析真正提到探针的文件(全仓 rglob+ast.parse 会把按墙钟判定
的用例挤超时)。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND_APP = REPO_ROOT / "backend" / "app"

# 探针的两个拼写:服务内部私有方法,以及给外部服务的公开端口。
PROBE_NAMES = frozenset({"_lexical_corpus_langs", "lexical_corpus_languages"})

# 打开连接的两个拼写(检索服务 / 其它服务)。
CONNECT_NAMES = frozenset({"_connect", "connect"})

# 必须逐个接收 `corpus_langs` 的词法召回助手。
GATED_HELPERS = frozenset({
    "_lexical_object_hits",
    "_relation_lexical_candidate_ids",
})


def _candidate_files() -> list[pathlib.Path]:
    """先按原文子串预筛,再 ast.parse —— 全仓解析太贵。"""
    needles = (*PROBE_NAMES, *GATED_HELPERS)
    found = []
    for path in BACKEND_APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            found.append(path)
    return found


def _opens_a_connection(node: ast.With) -> bool:
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            if call.func.attr in CONNECT_NAMES:
                return True
    return False


def _probe_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in PROBE_NAMES
    ]


def test_the_guard_actually_sees_the_wiring_it_claims_to_guard():
    """守卫打空(预筛没命中、方法改名)必须自己先红,而不是静默全绿。"""
    files = _candidate_files()
    assert files, "预筛没有命中任何文件——守卫已经打空"
    probes = sum(len(_probe_calls(ast.parse(p.read_text(encoding="utf-8"))))
                 for p in files)
    assert probes >= 5, f"只找到 {probes} 处探针调用,少于已知的调用点数"
    helpers = sum(
        1
        for path in files
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name in GATED_HELPERS
    )
    assert helpers == len(GATED_HELPERS), "词法召回助手改名了,守卫失去目标"


def test_language_probe_never_runs_while_a_connection_is_held():
    """探针一律在打开连接**之前**跑。"""
    offenders = []
    for path in _candidate_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With) or not _opens_a_connection(node):
                continue
            for call in _probe_calls(ast.Module(body=node.body, type_ignores=[])):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{call.lineno} "
                    f"{call.func.attr} 在已打开的连接内被调用"
                )
    assert not offenders, "\n".join(offenders)


def test_lexical_helpers_take_corpus_langs_instead_of_probing_themselves():
    """拿着调用方连接跑的助手不许自己探测,只能收参数。"""
    seen = set()
    for path in _candidate_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in GATED_HELPERS:
                continue
            seen.add(node.name)
            assert not _probe_calls(node), (
                f"{node.name} 自己探测语言——它持有的是调用方的连接"
            )
            names = [arg.arg for arg in node.args.kwonlyargs]
            assert "corpus_langs" in names, f"{node.name} 缺少 corpus_langs 参数"
            index = names.index("corpus_langs")
            assert node.args.kw_defaults[index] is None, (
                f"{node.name}.corpus_langs 有默认值——调用点漏传就会静默不过滤"
            )
    assert seen == GATED_HELPERS


@pytest.mark.parametrize("helper", sorted(GATED_HELPERS))
def test_every_call_site_passes_a_real_corpus_langs(helper):
    """漏传会 TypeError,但**传 `None`** 只会静默关掉这道闸——同样红。"""
    checked = 0
    for path in _candidate_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == helper):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            assert "corpus_langs" in keywords, (
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} 未传 corpus_langs"
            )
            value = keywords["corpus_langs"]
            assert not isinstance(value, ast.Constant), (
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                f"传的是常量 {getattr(value, 'value', value)!r}——"
                "闸会被静默关掉,必须传探针结果"
            )
            checked += 1
    assert checked, f"没找到 {helper} 的任何调用点——守卫打空了"
