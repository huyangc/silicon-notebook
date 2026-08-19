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
  / `self.queries` / `self.ask_state` / `self.access` 五个座位调用的方法名,必须在**它所属那一类**
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
  · **挡不住**:函数里 `import` 另一个模块再从它读(层二只看五个座位上的属性调用);
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
from app.repositories.ports import AskStateStorePort, SharingStorePort


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
    # P2(claim 代际):把上面两个的调用条件收进一处,自己不发任何端口调用——
    # 它只按 settle 的三值结局决定「抹不抹 / 要不要再排一轮」。归覆盖层而不是
    # 中性,是因为它决定的那两件事都只作用于该成员自己的行。
    "_after_overlay_settle",
    # P2-T3:成员资格复核。归覆盖层而不是中性,是因为它**确实取数**(读侧那条
    # 授权谓词),而中性的含义是「不取业务数据」。它也绝不该被底座调用——底座的
    # 产物一库一份、全体成员可见,按某一个人的读权决定要不要刷新它没有意义。
    "_member_can_read",
})

#: 与取数无关的函数:构造、事件、结算、纯文本处理。登记在这里表示「我看过它,它不
#: 取业务数据」。`_safe_settle`/`_fail` 只写**调用方指定的那一行** job 状态(两条链
#: 路共用,owner 是必填参数而不是默认值),`_emit` 只发计数,`sweep_on_start` 只调一
#: 个无参的全表结算,`_clip_name` 是纯字符串函数。
#: `_retire_requested` / `retire_disposition` 是两条链**共用**的纯函数(codex #520
#: R2 P2 的退役协议):前者只读模型回复里的一个键,后者只读已存那一行的 value 与
#: updated_origin。两者都不发查询、也拿不到座位,所以放中性而不是复制两份进各自
#: 链路——「用户手写的块不许退役」这条边界在两条链上必须是同一个答案。
NEUTRAL_FUNCTIONS = frozenset({
    "__init__",
    "_clip_name",
    "_emit",
    "_fail",
    "_retire_requested",
    "_safe_settle",
    "retire_disposition",
    # codex #520 R3 P1:与 retire_disposition 同族的纯函数——只读已存行的
    # value/updated_origin,回答「job 能不能碰这个块」。两条链共用同一个答案。
    "user_authoritative",
    "sweep_on_start",
})

#: 层二白名单:底座链路允许调用的**端口方法名**。
#:
#: 每一条都对应 `corpus_stats` docstring 里登记的那几样读,外加块本身的读写。
#: 注意这里登记的是**方法名**而不是 SQL:座位背后是两套适配器,唯一可静态检查的
#: 共同面就是端口方法名。
#:
#: ⚠ 加一条之前先问:它读的是 notebook 级数据,还是某个成员的数据?
#: `top_concept_names` 与 `knowledge_type_count_rows_excluding_memory` 之所以在列,
#: 正是因为它们在**自己的语句里**排掉私有 Memory;而任何按 user 取数的方法都不该
#: 出现在这里。
#:
#: ⚠ 白名单**只留实际用的**(codex #520 R2 P1):`memory_source_ids` /
#: `knowledge_type_count_rows` / `knowledge_type_count_rows_for_sources` 三条随
#: 「先读 id 清单再相减」那套写法一起退场——底座不再读 Memory 的 id 清单,而是让两条
#: 聚合各自在语句内排除。留着它们等于给「把排除拆回跨查询算术」留一条无人报红的路,
#: 而那条路的失败形态是共享块里出现一位成员私有 Memory 的计数与概念名称。
ALLOWED_PORT_CALLS = frozenset({
    # 块本身(notebook + 空 owner = 共享底座那一行)
    "read_blocks",
    "write_block",
    # 语料聚合
    "source_change_signal_rows",
    "visible_parse_status_counts",
    "element_type_count_rows",
    "knowledge_type_count_rows_excluding_memory",
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
#: ⚠ `recent_user_ask_traces` / `recent_user_report_traces`(P2-T4)是这里仅有的
#: 两条取数方法,也是全仓唯一把「这个库」读成「这个人对这个库的使用」的两条读。
#: 它们凭什么在列:两条 SQL 语句都把谓词 `created_by = ?`/`= %s` 写在语句里(层三
#: 静态钉住),产物只写进该成员自己的 owner 行。任何**不按 user 收窄**的使用数据
#: 读法都不该出现在这里。
OVERLAY_ALLOWED_PORT_CALLS = frozenset({
    # 块本身(notebook + 该成员的 owner id = 他自己的覆盖层)
    "read_blocks",
    "write_block",
    # 该成员自己的提问轨迹
    "recent_user_ask_traces",
    # 该成员自己最近完成的深度报告(P2-T4:sections_json[i].attempted 投影)
    "recent_user_report_traces",
    # 单飞与阈值(只碰这条链路自己那一行)
    "bump_signal",
    "claim",
    # codex R1: P1 写前复核与 P2 重排读本链路自己那一行(job_row);P1 写后兜底
    # 删**该成员自己的**覆盖层行(clear_all(notebook, user)——owner 实参是链路
    # 身份,不是任意成员)。
    "job_row",
    "clear_all",
    # P2-T3:成员资格复核。它凭什么在列——① 它回答的是「**这条链自己的** owner
    # 还读不读得到这个库」,输入只有链路身份,读不到任何别人的东西;② 它用的是
    # 读侧唯一那份授权谓词,不是第二种拼写。它**绝不能**出现在底座那张白名单里
    # (`ACCESS_CHECK_METHODS` 的反向护栏正面钉住这一条)。
    "user_can_read_notebook",
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

#: 层二检查的座位。`ask_state` 是 T5 新增的,`access` 是 P2-T3 新增的,登记它们的
#: **主要作用是让底座报红**:
#: 底座函数一旦写出 `self.ask_state.<任何方法>` / `self.access.<任何方法>`,它今天
#: 必然不在 `ALLOWED_PORT_CALLS` 里——但这不是这份守卫自身的性质,而是一个**外部
#: 前提**:`ALLOWED_PORT_CALLS`(底座的层二白名单)与 `AskStateStorePort` /
#: `SharingStorePort` 的方法名集合当前恰好不相交。这个前提本身不是恒真的(明天两边
#: 各自演化,谁都不保证永远不撞名字),所以
#: `test_the_base_allowlist_never_collides_with_data_seat_port_methods` 把它单独钉成
#: 一条断言——前提本身也需要一份守卫,而不是被这句注释当成理所当然。`self.database`
#: 刻意不在这几个座位之中:它上面只调 `connect()`,拿到的连接**只**转交给端口方法
#: (见本文件 docstring 的「挡不住」一节)。
#:
#: ⚠ **新增座位必须同步加进这里**,否则层二对那个座位上的调用**完全看不见**——白名
#: 单机制只扫 `self.<座位>.<方法>`,漏登记一个座位等于给这份守卫开了一扇没有报警的
#: 后门(它不会红,只是什么都不检查)。P2-T3 加 `access` 时正面钉了这一条:
#: `test_the_overlay_chain_actually_checks_membership` 会在这个元组少掉 `access`
#: 的那一刻变红。
PORT_ATTRIBUTES = ("profiles", "sources", "queries", "ask_state", "access")

#: 层三:必须自带 user 谓词的 store 方法(两个后端各一份实现)。P2-T4 追加
#: `recent_user_report_traces`——它读的是 `reports` 表而非 `ask_jobs`/
#: `ask_trace_steps`,但同样只能读发起人自己的行,同一层三判据照样成立。
TRACE_READ_METHODS = ("recent_user_ask_traces", "recent_user_report_traces")

#: P2-T3:**按人判定**的端口方法。它们只属于覆盖层——底座的产物一库一份、全体成员
#: 可见,让它按某一个人的读权改变行为在语义上就是错的(而错法是安静的:块照写,只是
#: 写不写取决于恰好是谁触发了这一轮)。反向护栏见
#: `test_the_access_check_never_reaches_the_base_allowlist`。
ACCESS_CHECK_METHODS = ("user_can_read_notebook",)

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


def _self_calls(node: ast.AST) -> set[str]:
    """函数体内 `self.<方法>(…)` 形态的调用(自身方法,不经座位)。

    ⚠ 必须是 AST 判据而不是文本判据。变异实测:先写成
    ``"_member_can_read" in ast.unparse(node)`` 时,把那次调用**整个删掉**守卫依然
    全绿——`unparse` 会把 docstring 一起吐出来,而这些函数的 docstring 恰恰在解释
    这次调用为什么存在。一条被自己的注释满足的断言,比没有断言更糟。
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "self"
        ):
            found.add(child.func.attr)
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
    assert len(ALLOWED_PORT_CALLS) >= 9
    assert len(OVERLAY_CHAIN_FUNCTIONS) >= 12
    assert "usage_stats" in OVERLAY_CHAIN_FUNCTIONS
    # P2-T4: recent_user_report_traces 加入后 OVERLAY_ALLOWED_PORT_CALLS 从 8
    # 条涨到 9 条;下限跟着抬,免得这条自检本身继续对着一个更宽的白名单打盹。
    assert len(OVERLAY_ALLOWED_PORT_CALLS) >= 9
    assert NEUTRAL_FUNCTIONS and len(NEUTRAL_ALLOWED_PORT_CALLS) == 2
    # P2-T4: TRACE_READ_METHODS 现含两条方法,清空成单元素元组同样必须报红。
    assert len(TRACE_READ_METHODS) >= 2 and len(_STORE_PATHS) == 2
    # P2-T3:层二的第四种空转形态——座位元组少一个,那个座位上的所有调用就静默地
    # 不再被扫描。`test_the_overlay_chain_actually_checks_membership` 是它的功能性
    # 判据(变异实测),这里再正面写一遍两个取数座位必须在列。
    assert {"ask_state", "access"} <= set(PORT_ATTRIBUTES)
    assert ACCESS_CHECK_METHODS


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


def test_the_access_check_never_reaches_the_base_allowlist():
    """反向护栏(P2-T3):**按人判定**的方法绝不能出现在底座的白名单里。

    与上面那条同构,但拦的是另一种失败:轨迹读进底座是「把 A 的使用情况写进大家都
    看得到的块」,而成员资格判定进底座是更隐蔽的一种——共享底座是 notebook 级的,
    它写什么不该取决于**恰好是谁**触发了这一轮。真让它读了,失败形态不是报错,而是
    「同一个库的共享块在 A 触发时刷新、在 B 触发时不刷新」,而两个人看到的是同一份
    块,没有任何地方会显示这个差别从何而来。

    它也不该进中性那张:中性的含义是「不取业务数据」,而这条读的是授权表。
    (`NEUTRAL_ALLOWED_PORT_CALLS` 的长度已被 `..._not_silently_empty` 精确钉死,
    这里再逐条正面写一次,免得那条长度断言将来被放宽时这一面无人看守。)
    """
    for method in ACCESS_CHECK_METHODS:
        assert method not in ALLOWED_PORT_CALLS, (
            f"{method} 出现在**底座**的端口白名单里。底座的产物一库一份、全体成员"
            "可见,按某一个成员的读权决定要不要刷新它,会让同一份共享块的刷新与否"
            "取决于谁恰好触发了这一轮——一个没有任何报错、也没有任何界面能解释的差别。"
        )
        assert method not in NEUTRAL_ALLOWED_PORT_CALLS, (
            f"{method} 出现在**中性**的端口白名单里。中性的含义是「我看过它,它不取"
            "业务数据」,而这条读的是授权表;它一旦被当成中性,层一层二就都不会再问"
            "「它替哪条链路判定」。"
        )
        assert method in OVERLAY_ALLOWED_PORT_CALLS, (
            f"{method} 不在覆盖层的白名单里——这条护栏会变成恒真:它当然不在别处,"
            "因为它已经哪儿都不在了。"
        )


def test_the_memory_exclusion_stays_inside_the_statement():
    """反向护栏(codex #520 R2 P1):底座不许把 Memory 排除拆回跨查询算术。

    白名单只管「不许多」——把 `memory_source_ids` 与
    `knowledge_type_count_rows_for_sources` 从 `ALLOWED_PORT_CALLS` 里删掉之后,
    层二会拦住重新写出这两个调用的形态。但那条判据只在**这两个名字**还没被谁"顺手
    加回来"时成立,所以这里正面钉住它们不在列:一旦回到「先读一次 Memory id 清单、
    再相减/再当排除清单传进去」,两次读之间就又有了一个没有共享快照的窗口,而那个
    窗口漏掉的东西里包含**概念名称**——它会被写进全体成员都读得到的共享块。
    """
    for retired in ("memory_source_ids", "knowledge_type_count_rows_for_sources",
                    "knowledge_type_count_rows"):
        assert retired not in ALLOWED_PORT_CALLS, (
            f"{retired} 又出现在底座的端口白名单里。底座的 Memory 排除必须发生在"
            "聚合语句**内部**(`knowledge_type_count_rows_excluding_memory` /"
            "`top_concept_names`);拆成「先读 id 清单再算」会重新打开一个并发的"
            "Memory 增删就能钻过去的窗口,而它泄漏的是共享块里的私有概念名称。"
        )
    assert "knowledge_type_count_rows_excluding_memory" in ALLOWED_PORT_CALLS, (
        "语句内排除的那条聚合不在白名单里了——这条护栏会变成恒真:三个退役名字"
        "当然不在列,而底座也已经读不到任何知识对象计数。"
    )


@pytest.mark.parametrize(
    "seat, port", [("ask_state", AskStateStorePort), ("access", SharingStorePort)]
)
def test_the_base_allowlist_never_collides_with_data_seat_port_methods(
    seat: str, port: type
):
    """自检(T5 修复轮,P2-T3 扩到第二个座位):给 `PORT_ATTRIBUTES` 那条注释的
    承诺补一份守卫。

    那条注释说「底座函数一旦写出 ``self.<座位>.<任何方法>``,它必然不在
    ``ALLOWED_PORT_CALLS`` 里」——这句话之所以成立,唯一原因是
    ``ALLOWED_PORT_CALLS``(底座的层二白名单)与该座位背后那个端口的方法名集合
    **当前恰好不相交**。这不是一个自动成立的性质:明天端口新增一个方法,名字恰好
    撞上 `ALLOWED_PORT_CALLS` 里已经登记的某个 notebook 级聚合方法名(比如两边都
    叫 ``read_blocks`` 这类巧合),那句注释描述的「必然」就悄悄不成立了——底座函数
    写 ``self.ask_state.read_blocks(...)`` 会被层二误判成在调用块本身的
    `read_blocks`(实际上那是完全不同的、会读某个用户轨迹的方法),从而放行一条
    本该报红的调用。introspect 端口的公开方法名集合,直接钉住这个交集恒为空。

    P2-T3 把它 parametrize 成两个座位而不是复制一份:``access`` 座位背后的
    `SharingStorePort` 有 `is_member` / `list_members` 这类短名字,撞名的可能性
    并不比 `AskStateStorePort` 低,而这条前提对两个座位是同一条。
    """
    assert seat in PORT_ATTRIBUTES, (
        f"{seat} 不在 PORT_ATTRIBUTES 里——这条自检守的是那条注释的前提,而前提的"
        "适用对象本身已经不受层二扫描了。"
    )
    port_methods = {
        name
        for name, value in vars(port).items()
        if callable(value) and not name.startswith("_")
    }
    collisions = sorted(port_methods & ALLOWED_PORT_CALLS)
    assert collisions == [], (
        f"`{port.__name__}` 与底座的 `ALLOWED_PORT_CALLS` 出现同名方法:"
        f"{collisions}。`PORT_ATTRIBUTES` 那条注释的『必然不在白名单里』前提已经"
        f"不成立——底座函数写 `self.{seat}.<这个名字>` 会被层二误判成在调用"
        "无害的 notebook 级聚合方法。请把冲突的名字之一改名,或者重新评估这条"
        "隔离判据是否还站得住。"
    )
    assert port_methods, (
        f"{port.__name__} 的公开方法名一个都没读到——这条断言本身可能被 introspect"
        "写坏了,恒真地绿。"
    )


def test_the_overlay_chain_actually_reads_the_member_trace():
    """反向护栏:层二是白名单,把 `usage_stats` 的读**删光**同样能让它绿。
    所以正面钉住:覆盖层确实在读这两样,而且只读这两样(P2-T4 把「只读一样」的
    旧断言扩成「只读这两样」——报告样本上线后 `usage_stats` 的输入面从一条变成
    两条,仍必须是白名单枚举出的那两条,不多不少)。"""
    calls = _port_calls(_functions(_SERVICE_PATH)["usage_stats"])
    assert calls == {"recent_user_ask_traces", "recent_user_report_traces"}, (
        f"usage_stats 的端口调用变成了 {sorted(calls)}——覆盖层的输入面被改了。"
        "它应当恰好读两样东西:该成员自己的提问轨迹,以及该成员自己最近完成的"
        "深度报告。"
    )


def test_the_overlay_chain_actually_checks_membership():
    """反向护栏(P2-T3):覆盖层确实在**复核成员资格**,而且是经登记座位发出的。

    这条同时是 `PORT_ATTRIBUTES` 的**变异判据**。层二是白名单,所以座位元组里少
    一个名字不会让任何断言变红——`_port_calls` 只是对那个座位上的调用视而不见,
    整份守卫安静地全绿。把这句写成正面断言之后,删掉 `access` 座位的那一刻它就红:
    `_member_can_read` 的调用扫不出来了。

    三个触发点(两个通知钩子 + `start_overlay`)也一并钉住:少任何一个,那条路径
    就重新变回「迟到的通知能替一个已经被移出的成员重建整条链」。
    """
    functions = _functions(_SERVICE_PATH)
    calls = _port_calls(functions["_member_can_read"])
    assert calls == set(ACCESS_CHECK_METHODS), (
        f"_member_can_read 的端口调用变成了 {sorted(calls)}。它应当恰好做一件事:"
        "经登记座位问一次读侧那份授权谓词。扫不出来通常意味着 `PORT_ATTRIBUTES` "
        "少了 `access` 座位——那种形态下层二对这个座位上的**所有**调用都视而不见,"
        "而且不会有任何断言变红。"
    )
    for caller in ("note_ask_completed", "note_report_completed", "start_overlay"):
        assert "_member_can_read" in _self_calls(functions[caller]), (
            f"{caller} 不再复核成员资格。一次在成员被移出之后才落地的通知会经"
            "`bump_signal` 的 upsert 重建 job 行,计数攒满后一轮巡固就用移出前的"
            "轨迹把这个人的块重新写回来(codex #520 R5,P2-T3 关掉的正是它)。"
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
        "knowledge_type_count_rows_excluding_memory",
        "top_concept_names",
        "visible_parse_status_counts",
    ):
        assert required in calls, (
            f"corpus_stats 不再调用 {required}——底座的输入被删掉了一条,"
            "而所有隔离断言仍然全绿(白名单只管「不许多」,不管「不许少」)。"
        )
