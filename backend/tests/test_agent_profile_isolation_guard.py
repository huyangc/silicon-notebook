"""守卫:共享底座的巡固输入**结构上**够不到任何成员的使用数据。

设计 §5.3 / §12-Q2 把这条隔离建成了「取数 SQL 拿不到」而不是「prompt 里请求别写」,
理由是后者不可验证。这个文件把那句话变成一条静态判据。

**两层,都是 allowlist(白名单),不是 denylist(黑名单)。** 这是这份守卫与它上一版
最重要的区别:上一版扫的是「函数体里有没有出现 `ask_jobs` 这六个字符串」。那种形态
只拦得住**已经想到的**六个名字,而 `agent_profile_job.py` 里新加一个读取时,它一个字
都不会说。真正要钉的不是「别读这六张表」,而是「只许读登记过的那几样」。

* **层一(函数分类)**:`agent_profile_job.py` 里的每个模块级函数与
  `AgentProfileConsolidationService` 的每个方法,都必须落进
  `BASE_CHAIN_FUNCTIONS` / `OVERLAY_CHAIN_FUNCTIONS` / `NEUTRAL_FUNCTIONS`
  三个显式集合之一。**新增一个没登记的函数就报红**——那正是「悄悄加一条读」的入口。
  `OVERLAY_CHAIN_FUNCTIONS` 今天是空集(覆盖层是 T5),它存在是为了让 T5 落地时
  作者必须显式声明「这个函数属于覆盖层」,而不是往中性集合里一扔。
* **层二(端口调用白名单)**:底座链路的函数体内,对 `self.profiles` /
  `self.sources` / `self.queries` 这三个座位调用的方法名,必须在
  `ALLOWED_PORT_CALLS` 里。白名单之外的端口方法(比如 `queries.list_user_activity`)
  即报红——它拦的是「经另一个 store 端口读使用数据」这条更省事、也更可能被写出来的
  路径,而且不需要预先知道那个方法叫什么。

**这份守卫能挡什么、不能挡什么**(docstring 只声称真的挡得住的):
  · 挡得住:模块内新增未分类函数;底座函数调用未登记的端口方法。
  · **挡不住**:底座函数里 `import` 另一个模块再从它读(层二只看三个座位上的属性
    调用);经 `self.database` 手写 SQL 字符串(那条路今天没有,`corpus_stats` 只把
    连接转交给端口方法)。这两条靠评审,不靠本文件——写一句它兑现不了的承诺,比不
    写更糟。

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

#: 底座链路的函数/方法名。名字改了就报红(见
#: `test_every_function_in_the_module_is_classified`),这是刻意的:改名的人必须
#: 重新确认新名字仍在这条隔离之内。
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

#: 覆盖层链路(T5)。**刻意留空**:空集不是占位符,而是一条断言——今天这个模块里
#: 一个覆盖层函数都还没有。T5 往这里加名字时,那个动作本身就是「我知道这个函数会读
#: 该成员自己的轨迹」的显式声明,而它的判据(每条读必须带 `user_id` 谓词)另立。
OVERLAY_CHAIN_FUNCTIONS: frozenset[str] = frozenset()

#: 与取数无关的函数:构造、事件、结算、纯文本处理。登记在这里表示「我看过它,它不
#: 取业务数据」。`_safe_settle`/`_fail` 只写自己那一行 job 状态,`_emit` 只发计数,
#: `sweep_on_start` 只调一个无参的全表结算,`_clip_name` 是纯字符串函数。
NEUTRAL_FUNCTIONS = frozenset({
    "__init__",
    "_clip_name",
    "_emit",
    "_fail",
    "_safe_settle",
    "sweep_on_start",
})

#: 层二白名单:底座链路允许调用的**端口方法名**。
#:
#: 每一条都对应 `corpus_stats` docstring 里登记的那几样读,外加块本身的读写。
#: 注意这里登记的是**方法名**而不是 SQL:座位背后是两套适配器,唯一可静态检查的
#: 共同面就是端口方法名。
#:
#: ⚠ 加一条之前先问:它读的是 notebook 级数据,还是某个成员的数据?
#: `top_concept_names` 与 `knowledge_type_count_rows_for_sources` 之所以在列,正是
#: 因为它们**减掉**私有 Memory;而任何按 user 取数的方法都不该出现在这里。
ALLOWED_PORT_CALLS = frozenset({
    # 块本身(notebook + 空 owner = 共享底座那一行)
    "read_blocks",
    "write_block",
    # 语料聚合
    "source_change_signal_rows",
    "visible_parse_status_counts",
    "element_type_count_rows",
    "memory_source_ids",
    "knowledge_type_count_rows",
    "knowledge_type_count_rows_for_sources",
    "top_concept_names",
    # 单飞与阈值(只碰这条链路自己那一行)
    "bump_signal",
    "claim",
})

#: 层二检查的三个座位。`self.database` 刻意不在其中:它上面只调 `connect()`,拿到的
#: 连接**只**转交给端口方法(见本文件 docstring 的「挡不住」一节)。
PORT_ATTRIBUTES = ("profiles", "sources", "queries")


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


def _port_calls(node: ast.AST) -> set[str]:
    """函数体内 `self.<座位>.<方法>(…)` 形态的调用,返回被调用的方法名。

    要下钻进嵌套作用域:一个内嵌 helper 里的读取同样是这条链路发出的读取。
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr in PORT_ATTRIBUTES
        ):
            found.add(func.attr)
    return found


def test_every_function_in_the_module_is_classified():
    """层一:模块里不能存在没登记的函数。

    这条同时兼作守卫自检——三个集合里登记的名字必须都还在模块里,否则
    `corpus_stats` 改个名字之后层二会安静地什么都不扫而保持全绿
    (「加了守卫 ≠ 有效」的典型形态)。
    """
    functions = _functions(_SERVICE_PATH)
    classified = BASE_CHAIN_FUNCTIONS | OVERLAY_CHAIN_FUNCTIONS | NEUTRAL_FUNCTIONS
    unclassified = sorted(set(functions) - classified)
    assert unclassified == [], (
        f"`agent_profile_job.py` 里有未分类的函数:{unclassified}。\n"
        "每个函数都必须显式落进 BASE_CHAIN_FUNCTIONS / OVERLAY_CHAIN_FUNCTIONS /"
        " NEUTRAL_FUNCTIONS 之一。这不是登记手续:分类的那一刻正是「这个函数会读什么」"
        "被想清楚的时刻,而共享底座读进一条成员级数据不会报错,只会让 A 的使用情况"
        "出现在 B 也读得到的块里。"
    )
    missing = sorted(classified - set(functions))
    assert missing == [], (
        f"守卫登记的函数不存在了:{missing}。改名/搬家时同步更新对应集合,"
        "并重新确认新的形态仍在这条隔离之内。"
    )


@pytest.mark.parametrize("function_name", sorted(BASE_CHAIN_FUNCTIONS))
def test_the_base_chain_only_calls_allowlisted_ports(function_name: str):
    """层二:底座链路只许调白名单里的端口方法。"""
    node = _functions(_SERVICE_PATH)[function_name]
    offenders = sorted(_port_calls(node) - ALLOWED_PORT_CALLS)
    assert offenders == [], (
        f"共享底座的 {function_name} 调用了未登记的端口方法:{offenders}。\n"
        "共享底座是一库一份、全体成员可见的,它的输入必须全部是 notebook 级、且已经"
        "减掉私有 Memory 的聚合。一旦提问轨迹/答案/私有记忆进得来,共享库里 A 的使用"
        "情况就会出现在 B 也能读到的块里——一次没有任何报错的隐私事故。要读成员自己"
        "的轨迹,请走覆盖层链路(per (notebook, user),产物只有本人可见);要新增一条"
        "notebook 级的读,先把它加进 ALLOWED_PORT_CALLS 并在那里写明它为什么安全。"
    )


def test_the_allowlists_are_not_silently_empty():
    """自检:两层的判据都不能被清空成恒真断言。

    层一的空转形态是「三个集合都空」(那时 unclassified 会报红,所以它自防);
    层二的空转形态是 BASE_CHAIN_FUNCTIONS 被清空——那样 parametrize 出零个用例,
    整个层二一个字都不检查而 pytest 全绿。
    """
    assert len(BASE_CHAIN_FUNCTIONS) >= 9
    assert "corpus_stats" in BASE_CHAIN_FUNCTIONS
    assert len(ALLOWED_PORT_CALLS) >= 11


def test_the_base_chain_actually_reads_something():
    """反向护栏:层二是白名单,清空 `ALLOWED_PORT_CALLS` 会让它变成「一条端口调用都
    不许有」——那种形态下守卫会红,但把 `corpus_stats` 的读**全删掉**同样能让它绿。
    所以这里正面钉住:底座确实在读那几样。"""
    node = _functions(_SERVICE_PATH)["corpus_stats"]
    calls = _port_calls(node)
    for required in (
        "source_change_signal_rows",
        "element_type_count_rows",
        "knowledge_type_count_rows",
        "memory_source_ids",
        "top_concept_names",
        "visible_parse_status_counts",
    ):
        assert required in calls, (
            f"corpus_stats 不再调用 {required}——底座的输入被删掉了一条,"
            "而所有隔离断言仍然全绿(白名单只管「不许多」,不管「不许少」)。"
        )
