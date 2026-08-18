"""守卫:两条巡固链路各自**结构上**只够得到它该够得到的那部分数据。

设计 §5.3 / §12-Q2 把这条隔离建成了「取数 SQL 拿不到」而不是「prompt 里请求别写」,
理由是后者不可验证。这个文件把那句话变成静态判据。两条链路的判据方向相反:

* **共享底座**(一库一份、全体成员可见)——不许读**任何**成员的使用数据;
* **覆盖层**(per (notebook, user)、只有本人可见)——只许读**它自己那位成员**的。

**三层,全部是 allowlist(白名单),不是 denylist(黑名单)。** 这是这份守卫与它上一
版最重要的区别:上一版扫的是「函数体里有没有出现 `ask_jobs` 这六个字符串」。那种形态
只拦得住**已经想到的**六个名字,而 `agent_profile_job.py` 里新加一个读取时,它一个字
都不会说。真正要钉的不是「别读这六张表」,而是「只许读登记过的那几样」。

* **层一(函数分类)**:`agent_profile_job.py` 里的每个模块级函数与
  `AgentProfileConsolidationService` 的每个方法,都必须落进
  `BASE_CHAIN_FUNCTIONS` / `OVERLAY_CHAIN_FUNCTIONS` / `NEUTRAL_FUNCTIONS`
  三个显式集合之一。**新增一个没登记的函数就报红**——那正是「悄悄加一条读」的入口。
* **层二(端口调用白名单,每类一张)**:函数体内对 `self.profiles` / `self.sources`
  / `self.queries` / `self.ask_state` 四个座位调用的方法名,必须在**它所属那一类**
  的白名单里。底座那张不含轨迹读,覆盖层那张含,中性那张只许收尾自己那一行 job 状态
  ——最后这张堵的是分类制度自己的后门(往中性一扔就谁也扫不到它)。它拦的是「经另一
  个 store 端口读使用数据」这条更省事、也更可能被写出来的路径,而且不需要预先知道那
  个方法叫什么。
* **层三(轨迹读的 SQL 谓词)**:覆盖层那条读的判据是反过来的——每条 SQL **字面量**
  必须自带 `created_by` 谓词。这一层扫的不是服务层而是**两个后端的
  `ask_state_store.py`**:`TRACE_READ_METHODS` 里登记的方法,它体内每一条 SQL 字符串
  都必须含该谓词。把谓词从 SQL 挪到 Python 侧过滤即报红——那正是这条隔离最可能被
  「重构掉」的形态,因为过滤写在 Python 里读起来完全正常。

**这份守卫能挡什么、不能挡什么**(docstring 只声称真的挡得住的):
  · 挡得住:模块内新增未分类函数;某类函数调用不属于它那类的端口方法;登记过的轨迹
    读把 user 谓词挪出 SQL。
  · **挡不住**:函数里 `import` 另一个模块再从它读(层二只看四个座位上的属性调用);
    经 `self.database` 手写 SQL 字符串(那条路今天没有,`corpus_stats` 只把连接转交
    给端口方法);覆盖层**新增**一条按 user 收窄的读而不登记进 `TRACE_READ_METHODS`
    ——层二会拦下它的方法名,但层三不会去读它的 SQL。这几条靠评审,不靠本文件——写
    一句它兑现不了的承诺,比不写更糟。

**为什么需要静态判据**:运行时测试只能证明「今天这条代码路径没读」,证不了「明天
也读不到」。而这条隔离一旦破,后果不是报错而是**共享库里 A 的提问出现在 B 也能看到
的块里**——一次静默的隐私事故,没有任何用例会因此变红。

不经 import 定位源文件:只需要读源码文本,不需要把服务层的依赖拖进这条离线判据。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.repositories
import app.services
from app.repositories.ports import AskStateStorePort


_SERVICE_PATH = Path(app.services.__file__).parent / "agent_profile_job.py"
_REPO_ROOT = Path(app.repositories.__file__).parent
_STORE_PATHS = {
    "sqlite": _REPO_ROOT / "sqlite" / "ask_state_store.py",
    "postgres": _REPO_ROOT / "postgres" / "ask_state_store.py",
}

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
    # codex R1 P2: settle 后按剩余 pending 重排下一轮。只读本链路自己那一行
    # (job_row) + 调 start_base,不触任何业务数据。
    "_maybe_requeue_base",
})

#: 覆盖层链路(T5)。名字在这里 = 显式声明「我知道这个函数会读该成员自己的轨迹」。
#: 它的层二白名单是**另一张**(`OVERLAY_ALLOWED_PORT_CALLS`,含 `ask_state` 座位),
#: 判据另有层三:那条读的 SQL 必须自带 `created_by` 谓词。
OVERLAY_CHAIN_FUNCTIONS: frozenset[str] = frozenset({
    "note_ask_completed",
    "note_report_completed",
    "start_overlay",
    "run_overlay",
    "_consolidate_overlay",
    "_write_overlay_blocks",
    "usage_stats",
    "summarize_usage",
    "render_usage_block",
    "render_current_overlay_blocks",
    "parse_overlay_reply",
    # codex R1: P1 撤销竞态的写后兜底(只删该成员自己的行)与 P2 的重排(只读
    # 本链路自己那一行 + 调 start_overlay)。
    "_clear_revoked_overlay",
    "_maybe_requeue_overlay",
})

#: 与取数无关的函数:构造、事件、结算、纯文本处理。登记在这里表示「我看过它,它不
#: 取业务数据」。`_safe_settle`/`_fail` 只写**调用方指定的那一行** job 状态(两条链
#: 路共用,owner 是必填参数而不是默认值),`_emit` 只发计数,`sweep_on_start` 只调一
#: 个无参的全表结算,`_clip_name` 是纯字符串函数。
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
    # codex R1 P2: settle 后重排要读本链路自己那一行的 pending——job_row 与
    # claim/bump_signal 同类:按 (notebook, owner) 主键点查状态行,不触业务数据。
    "job_row",
})

#: 层二白名单:**覆盖层**链路允许调用的端口方法名。刻意是**另一张表**而不是往上面
#: 那张加两条——两条链路的合法输入本来就是互斥的,合成一张会让「底座能不能读轨迹」
#: 这个问题不再有答案(而那正是本文件存在的理由)。
#:
#: ⚠ `recent_user_ask_traces` 是这里唯一的取数方法,也是全仓唯一一条把「这个库」
#: 读成「这个人对这个库的使用」的读。它凭什么在列:谓词 `created_by = ?` 写在 SQL
#: 里(层三静态钉住),产物只写进该成员自己的 owner 行。任何**不按 user 收窄**的
#: 使用数据读法都不该出现在这里。
OVERLAY_ALLOWED_PORT_CALLS = frozenset({
    # 块本身(notebook + 该成员的 owner id = 他自己的覆盖层)
    "read_blocks",
    "write_block",
    # 该成员自己的提问轨迹
    "recent_user_ask_traces",
    # 单飞与阈值(只碰这条链路自己那一行)
    "bump_signal",
    "claim",
    # codex R1: P1 写前复核与 P2 重排读本链路自己那一行(job_row);P1 写后兜底
    # 删**该成员自己的**覆盖层行(clear_all(notebook, user)——owner 实参是链路
    # 身份,不是任意成员)。
    "job_row",
    "clear_all",
})

#: 层二白名单:**中性**函数允许调用的端口方法名——只有这条链路自己那一行 job 状态
#: 的收尾。
#:
#: ⚠ 这一条堵的是分类制度自己的后门:层一只要求「登记过」,而 NEUTRAL 是那三个集合
#: 里唯一不带输入约束的。把一个新的取数函数往 NEUTRAL 一扔,层一绿、两条链路的层二
#: 都不会 parametrize 到它,整份守卫一个字都不会说(变异实测:把 `usage_stats` 从
#: 覆盖层搬进中性集合)。写死「中性 = 不取业务数据」之后,那条路就必须先把方法名加
#: 进某张白名单,而加的那一刻正是「它会读到谁的数据」被想清楚的时刻。
NEUTRAL_ALLOWED_PORT_CALLS = frozenset({
    "settle",
    "sweep_stale_on_start",
})

#: 层二检查的四个座位。`ask_state` 是 T5 新增的,登记它的**主要作用是让底座报红**:
#: 底座函数一旦写出 `self.ask_state.<任何方法>`,它今天必然不在 `ALLOWED_PORT_CALLS`
#: 里——但这不是这份守卫自身的性质,而是一个**外部前提**:`ALLOWED_PORT_CALLS`
#: (底座的层二白名单)与 `AskStateStorePort` 的方法名集合当前恰好不相交。这个前提
#: 本身不是恒真的(明天两边各自演化,谁都不保证永远不撞名字),所以
#: `test_the_base_allowlist_never_collides_with_ask_state_port_methods` 把它单独钉成
#: 一条断言——前提本身也需要一份守卫,而不是被这句注释当成理所当然。`self.database`
#: 刻意不在这四个座位之中:它上面只调 `connect()`,拿到的连接**只**转交给端口方法
#: (见本文件 docstring 的「挡不住」一节)。
PORT_ATTRIBUTES = ("profiles", "sources", "queries", "ask_state")

#: 层三:必须自带 user 谓词的 store 方法(两个后端各一份实现)。
TRACE_READ_METHODS = ("recent_user_ask_traces",)

#: 层三认的谓词形状。两个后端占位符不同(`?` / `%s`),所以只钉列名与紧随其后的
#: 比较——列名本身就是判据,`created_by` 出现在一条读 `ask_jobs` 的 SQL 里,没有第
#: 二种含义。
USER_PREDICATE_TOKENS = ("created_by = ?", "created_by = %s",
                         "created_by=?", "created_by=%s")


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


def _sql_literals(node: ast.AST) -> list[str]:
    """函数体内的「像 SQL 的」字符串:相邻字面量已被解析器折成一个 Constant;
    f-string(占位符拼接用)只取其中的常量片段拼起来——被插值的部分本来就不是
    可静态检查的内容。判据是同时含 SELECT 与 FROM,避免把普通字符串当 SQL 判。
    """
    found: list[str] = []

    def _record(text: str) -> None:
        upper = text.upper()
        if "SELECT" in upper and "FROM" in upper:
            found.append(text)

    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            _record(child.value)
        elif isinstance(child, ast.JoinedStr):
            _record("".join(
                part.value for part in child.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ))
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


@pytest.mark.parametrize("function_name", sorted(OVERLAY_CHAIN_FUNCTIONS))
def test_the_overlay_chain_only_calls_allowlisted_ports(function_name: str):
    """层二(覆盖层):只许调**它自己那张**白名单里的端口方法。"""
    node = _functions(_SERVICE_PATH)[function_name]
    offenders = sorted(_port_calls(node) - OVERLAY_ALLOWED_PORT_CALLS)
    assert offenders == [], (
        f"覆盖层的 {function_name} 调用了未登记的端口方法:{offenders}。\n"
        "覆盖层可以读使用数据,但只能读**它自己那位成员**的,而且产物只写进该成员"
        "自己的 owner 行。一条不按 user 收窄的读会把别人的提问搅进这个人的私有块,"
        "而共享笔记本里没有任何报错会提示这件事。要加一条读,先确认它的谓词按 user"
        "收窄、且写在 SQL 里(层三会静态复核),再把它加进 OVERLAY_ALLOWED_PORT_CALLS"
        "并在那里写明理由。"
    )


@pytest.mark.parametrize("function_name", sorted(NEUTRAL_FUNCTIONS))
def test_neutral_functions_do_not_read_business_data(function_name: str):
    """层二(中性):登记成「不取业务数据」的函数,必须真的不取。

    没有这一条,`NEUTRAL_FUNCTIONS` 就是整份守卫的旁路:新增一个读使用数据的函数、
    往中性集合一放,层一满意、两条链路的层二都扫不到它。
    """
    node = _functions(_SERVICE_PATH)[function_name]
    offenders = sorted(_port_calls(node) - NEUTRAL_ALLOWED_PORT_CALLS)
    assert offenders == [], (
        f"登记为中性的 {function_name} 调用了取数端口方法:{offenders}。\n"
        "中性的含义是「我看过它,它不取业务数据」。它一旦开始取数,就必须先归到底座"
        "或覆盖层某一条链路上——那两条各有自己的输入约束,而中性没有。"
    )


@pytest.mark.parametrize("backend", sorted(_STORE_PATHS))
@pytest.mark.parametrize("method_name", TRACE_READ_METHODS)
def test_the_trace_read_carries_the_user_predicate_in_sql(
    backend: str, method_name: str
):
    """层三:覆盖层的轨迹读,每一条 SQL 字面量都必须自带 `created_by` 谓词。

    这一条钉的不是「有没有按用户过滤」,而是**过滤写在哪儿**。写在 SQL 里,它是
    语句的性质;挪到 Python 侧(先取回再 `if row["created_by"] == user_id`),读起来
    同样正常、测试同样能全绿,但下一次重构可以把那一行删掉而不留痕迹——而被删掉之
    后的失败形态是「别人的提问进了这个人的私有块」,没有任何报错。
    """
    functions = _functions(_STORE_PATHS[backend])
    assert method_name in functions, (
        f"{backend} 的 ask_state_store 里找不到 {method_name}——覆盖层的取数方法"
        "被改名或删掉了,而层三会因此静默地什么都不检查。"
    )
    statements = _sql_literals(functions[method_name])
    assert statements, (
        f"{backend}.{method_name} 里一条 SQL 字面量都扫不到:要么它不再自己发查询"
        "(那这条隔离的判据就落到了别处,必须重新登记),要么 SQL 被拼成了这个守卫"
        "读不到的形状。"
    )
    for sql in statements:
        assert any(token in sql for token in USER_PREDICATE_TOKENS), (
            f"{backend}.{method_name} 里有一条读不带 `created_by` 谓词:\n"
            f"  {' '.join(sql.split())[:200]}\n"
            "覆盖层的输入必须在**语句层面**只包含发起人自己的行。Python 侧过滤不算"
            "——那是一行随时可能被重构掉的代码,而它一旦没了,别人的提问就会被总结进"
            "这个人的私有理解块里。"
        )


def test_the_allowlists_are_not_silently_empty():
    """自检:三层的判据都不能被清空成恒真断言。

    层一的空转形态是「三个集合都空」(那时 unclassified 会报红,所以它自防);
    层二的空转形态是 BASE_CHAIN_FUNCTIONS / OVERLAY_CHAIN_FUNCTIONS 被清空——那样
    parametrize 出零个用例,整个层二一个字都不检查而 pytest 全绿;层三同理,清空
    TRACE_READ_METHODS 或 _STORE_PATHS 就没有任何用例被生成。
    """
    assert len(BASE_CHAIN_FUNCTIONS) >= 9
    assert "corpus_stats" in BASE_CHAIN_FUNCTIONS
    assert len(ALLOWED_PORT_CALLS) >= 11
    assert len(OVERLAY_CHAIN_FUNCTIONS) >= 11
    assert "usage_stats" in OVERLAY_CHAIN_FUNCTIONS
    assert len(OVERLAY_ALLOWED_PORT_CALLS) >= 5
    assert NEUTRAL_FUNCTIONS and len(NEUTRAL_ALLOWED_PORT_CALLS) == 2
    assert TRACE_READ_METHODS and len(_STORE_PATHS) == 2


def test_the_two_chains_never_share_a_port_allowlist_entry_that_reads_usage():
    """反向护栏:轨迹读绝不能悄悄出现在**底座**的白名单里。

    两张白名单是分开的,但分开本身挡不住有人把 `recent_user_ask_traces` 往两张里
    各加一条(「反正都要用」)。共享底座一旦读到任何成员的使用数据,它写出的块是
    全体成员可见的——这正是整份守卫存在的那一个失败形态。
    """
    for method in TRACE_READ_METHODS:
        assert method not in ALLOWED_PORT_CALLS, (
            f"{method} 出现在**底座**的端口白名单里。底座的产物一库一份、"
            "全体成员可见,它读到任何人的提问轨迹都等于把那个人的使用情况公开。"
        )
    assert "ask_state" not in " ".join(sorted(ALLOWED_PORT_CALLS))


def test_the_base_allowlist_never_collides_with_ask_state_port_methods():
    """自检(T5 修复轮):给 `PORT_ATTRIBUTES` 那条注释的承诺补一份守卫。

    那条注释说「底座函数一旦写出 ``self.ask_state.<任何方法>``,它必然不在
    ``ALLOWED_PORT_CALLS`` 里」——这句话之所以成立,唯一原因是
    ``ALLOWED_PORT_CALLS``(底座的层二白名单)与 ``AskStateStorePort`` 的方法名
    集合**当前恰好不相交**。这不是一个自动成立的性质:明天 `AskStateStorePort`
    新增一个方法,名字恰好撞上 `ALLOWED_PORT_CALLS` 里已经登记的某个 notebook 级
    聚合方法名(比如两边都叫 ``read_blocks`` 这类巧合),那句注释描述的「必然」
    就悄悄不成立了——底座函数写 ``self.ask_state.read_blocks(...)`` 会被层二
    误判成在调用块本身的 `read_blocks`(实际上那是完全不同的、会读某个用户轨迹
    的方法),从而放行一条本该报红的调用。introspect `AskStateStorePort` 的
    公开方法名集合,直接钉住这个交集恒为空。
    """
    ask_state_methods = {
        name
        for name, value in vars(AskStateStorePort).items()
        if callable(value) and not name.startswith("_")
    }
    collisions = sorted(ask_state_methods & ALLOWED_PORT_CALLS)
    assert collisions == [], (
        f"`AskStateStorePort` 与底座的 `ALLOWED_PORT_CALLS` 出现同名方法:"
        f"{collisions}。`PORT_ATTRIBUTES` 那条注释的『必然不在白名单里』前提已经"
        "不成立——底座函数写 `self.ask_state.<这个名字>` 会被层二误判成在调用"
        "无害的 notebook 级聚合方法。请把冲突的名字之一改名,或者重新评估这条"
        "隔离判据是否还站得住。"
    )
    assert ask_state_methods, (
        "AskStateStorePort 的公开方法名一个都没读到——这条断言本身可能被 introspect"
        "写坏了,恒真地绿。"
    )


def test_the_overlay_chain_actually_reads_the_member_trace():
    """反向护栏:层二是白名单,把 `usage_stats` 的读**删光**同样能让它绿。
    所以正面钉住:覆盖层确实在读那一条,而且只读那一条。"""
    calls = _port_calls(_functions(_SERVICE_PATH)["usage_stats"])
    assert calls == {"recent_user_ask_traces"}, (
        f"usage_stats 的端口调用变成了 {sorted(calls)}——覆盖层的输入面被改了。"
        "它应当恰好读一样东西:该成员自己的提问轨迹。"
    )


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
