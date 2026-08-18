"""守卫:共享底座的巡固输入**结构上**够不到任何成员的使用数据。

设计 §5.3 / §12-Q2 把这条隔离建成了「取数 SQL 拿不到」而不是「prompt 里请求别写」,
理由是后者不可验证。这个文件把那句话变成一条静态判据。

**钉什么**:`agent_profile_job.py` 里底座链路的每个函数体内,不得出现
`ask_jobs` / `ask_trace_steps` / `answers` / `memory_items` / `conversations` /
`reports` 这六个名字——**既不能出现在 SQL 字符串里,也不能出现在标识符里**
(`self.ask_jobs.…` / `from … import ask_state_store` 同样报红)。两种形态都要拦:
只扫字符串会漏掉「经另一个 store 端口读」这条更省事、也更可能被写出来的路径。

**为什么需要静态判据**:运行时测试只能证明「今天这条代码路径没读」,证不了「明天
也读不到」。而这条隔离一旦破,后果不是报错而是**共享库里 A 的提问出现在 B 也能看到
的块里**——一次静默的隐私事故,没有任何用例会因此变红。

⚠ 本守卫只覆盖**底座**链路(T4)。覆盖层链路(T5)读的正是该成员自己的轨迹,它的判据
是反过来的:每条读轨迹的 SQL 必须自带 `WHERE user_id = ?` 谓词。那半条随 T5 一起加。

不经 import 定位源文件:只需要读源码文本,不需要把服务层的依赖拖进这条离线判据。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.services


_SERVICE_PATH = Path(app.services.__file__).parent / "agent_profile_job.py"

#: 使用数据的六张表。判据是**子串**匹配:`ask_jobs` 也拦得住 `INNER JOIN ask_jobs j`
#: 与 `self.ask_jobs`,而 `answers` 同时拦下 `answers` 表与任何以它命名的座位。
FORBIDDEN_USAGE_TABLES = frozenset({
    "ask_jobs",
    "ask_trace_steps",
    "answers",
    "memory_items",
    "conversations",
    "reports",
})

#: 底座链路的函数/方法名。**穷举而不是「扫整个模块」**:T5 的覆盖层函数会合法地
#: 读该成员自己的轨迹,扫整个模块会在那时把一条正确的实现判红,于是被删掉——守卫
#: 因此必须按作用域登记,而不是按文件。
#:
#: 名字改了就报红(见 `test_guard_actually_sees_the_functions_it_claims_to_pin`),
#: 这是刻意的:改名的人必须重新确认新名字仍在这条隔离之内。
BASE_CHAIN_FUNCTIONS = frozenset({
    "note_corpus_change",
    "start_base",
    "run_base",
    "_consolidate_base",
    "_write_blocks",
    "corpus_stats",
    "render_corpus_block",
    "render_current_blocks",
    "parse_base_reply",
})


def _functions(path: Path) -> dict[str, ast.AST]:
    """模块级函数 + `AgentProfileConsolidationService` 的方法,按名字索引。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[child.name] = child
    return found


def _tokens(node: ast.AST) -> set[str]:
    """函数体内出现的字符串常量 + 标识符(裸名字/属性名/import 名)。

    与事务守卫不同,这里**要**下钻进嵌套作用域:一个内嵌 helper 里的读取同样是这条
    链路发出的读取。
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.alias):
            found.add(child.name)
            if child.asname:
                found.add(child.asname)
    return found


def test_guard_actually_sees_the_functions_it_claims_to_pin():
    """守卫自检:登记的每个函数都还在。

    少了这条,`corpus_stats` 改个名字之后守卫会安静地什么都不扫而保持全绿——
    「加了守卫 ≠ 有效」的典型形态。
    """
    functions = _functions(_SERVICE_PATH)
    missing = sorted(BASE_CHAIN_FUNCTIONS - set(functions))
    assert missing == [], (
        f"底座链路登记的函数不存在了:{missing}。改名/搬家时同步更新"
        " BASE_CHAIN_FUNCTIONS,并重新确认新的形态仍在这条隔离之内。"
    )


@pytest.mark.parametrize("function_name", sorted(BASE_CHAIN_FUNCTIONS))
def test_the_base_chain_cannot_reach_any_usage_data(function_name: str):
    node = _functions(_SERVICE_PATH)[function_name]
    tokens = _tokens(node)
    offenders = sorted(
        f"{table} in {token!r}"
        for table in FORBIDDEN_USAGE_TABLES
        for token in tokens
        if table in token
    )
    assert offenders == [], (
        f"共享底座的 {function_name} 够到了成员使用数据:{offenders}。\n"
        "共享底座是一库一份、全体成员可见的,它的输入必须全部是 notebook 级数据。"
        "一旦提问轨迹/答案/私有记忆进得来,共享库里 A 的使用情况就会出现在 B 也能"
        "读到的块里——一次没有任何报错的隐私事故。要读成员自己的轨迹,请走覆盖层"
        "链路(per (notebook, user),产物只有本人可见)。"
    )


def test_the_forbidden_list_is_not_silently_empty():
    """自检之二:判据本身不能被清空成一条恒真断言。"""
    assert len(FORBIDDEN_USAGE_TABLES) == 6
    assert "ask_jobs" in FORBIDDEN_USAGE_TABLES
