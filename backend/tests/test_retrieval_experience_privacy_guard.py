"""守卫:全局经验库的三个模块**结构上**够不到任何自由文本。

这张表是本仓库里唯一**没有任何租户列**的表(没有 `notebook_id`、没有
`created_by`、没有 `owner_id`),而它的每一行都被部署里的每一位用户读到。别处的
隔离是「取数 SQL 里的一条谓词」,这里没有谓词可写,所以保证被挪到了更早一层:
**能被观测成一行的东西本身就没有自由文本**。

这份文件把那句话变成静态判据。七条,方向各不相同(六、七是 Agentic Memory P4
T3 新增,为 ``result_ids``/``anchor_evidence_ids`` 这两个新读取面补的一对
判据——判据二的禁键扫描不认它们:它们是**合法**的新键名,只是必须被关进
``project_run`` 一个函数里,判据六、七补的正是这道"关得住"的证明):

* **判据一(投影层无文本)**:`RunObservation` 与从它可达的每个类型,每个字段的
  注解必须是 `int` / `bool` / `Literal[...]`(或指向它们的模块级别名)/ 由这些
  构成的 `tuple[...]`。裸 `str` / `Any` / `dict` / `list[str]` 一律报红。附一条
  字段数下限:「每个字段都是封闭的」对一个**空** dataclass 恒真,而把类掏空正是
  让一条失败的加宽用例变绿的最短路径。
* **判据二(三个模块的禁键扫描)**:projection / job / block 三个模块里,不得出现
  一组危险键名——作为标识符、属性名、形参名或**非 docstring 的字符串常量**。这是
  五条里唯一能挡住「**移动**变异」的一条:把 `question` 的读取从 job 挪到
  projection(或挪到注入侧的 block)在语义上一模一样,只扫一个模块的守卫会全绿。
* **判据三(写入面封闭词表)**:`action`/`polarity` 必须**精确**落在词表里,
  `situation` 的键与值必须落在注册表里,`rationale` 超长整条丢弃。非法值一律
  **丢弃**而不是修复——猜一个近似值会把一条关于 A 通道的经验记成关于 B 通道的。
* **判据四(注入面无 id)**:带 id 形状串的 rationale 在**写入侧**就被拒;渲染出的
  块里不含任何 id 形状串,也不含来源/库名(条目本身就没有这两个字段可渲染)。
* **判据五(反向守卫:词表够不到检索范围)**:存储动作词表、它渲染给模型的动作
  id、以及采用账目认的那批 id,三张表都不含任何范围类词。经验只影响**怎么查**,
  绝不影响**能读什么**——后者只由用户的勾选决定。
* **判据六(运行时:观测结果里不含 id)**:拿真实携带 id 形状字符串的 run 跑一遍
  ``project_run``,序列化整个 ``ObservedRun.observation`` 后,那两个 id 串在
  任何位置都不出现——证明「两个 int/bool 字段」这个设计没有在哪个分支里把原始
  id 悄悄塞进了某个字段。
* **判据七(静态:`result_ids`/`anchor_evidence_ids` 只活在 `project_run` 里)**:
  这是五条里唯一挡「**移动**」变异的第二处——把交集计算从 ``project_run`` 挪到
  同一模块里的另一个函数(``validate_situation``、``experience_id``……),语义
  不变,但会让"id 只是函数局部变量"这句承诺失去唯一站得住脚的理由。判据只扫
  ``retrieval_experience_projection.py``:这两个键名的读取只应该出现在
  ``project_run`` 一个函数的子树里。

**这份守卫能挡什么、不能挡什么**(只声称真的挡得住的):
  · 挡得住:给观测类型加自由文本字段;把类掏空来蒙混;在三个模块任一处读危险
    键名(含在模块之间搬家);写入面放宽词表;渲染面泄出 id 形状串;动作词表长
    出范围类动作。
  · **挡不住**:在三个模块之外新写一条读(判据二只扫这三个文件——但那也正是本
    特性的全部代码面);把危险键名换成一个别名再读(`row[K]` 里 `K` 是变量)。
    这两条靠评审,不靠本文件——写一句它兑现不了的承诺,比不写更糟。

判据一、二不 import 被测模块(照 P1 隔离守卫的做法):只读源码文本,不把服务层的
依赖拖进一条离线判据。判据三到五是运行时的,因为它们要断言的正是**行为**。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.services
from app.repositories.ports import RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS
from app.services.retrieval_experience_block import (
    ADOPTION_ACTIONS,
    RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS,
    render_experience_block,
    select_experiences,
    usable_entry,
)
from app.services.retrieval_experience_job import (
    _SituationGroup,
    parse_distillation_reply,
)
from app.services.retrieval_experience_projection import (
    EXPERIENCE_POLARITIES,
    RETRIEVAL_ACTIONS,
    SITUATION_KEYS,
    experience_id,
    project_run,
    validate_situation,
)

_SERVICES = Path(app.services.__file__).parent
#: 本特性的**全部**代码面,三个文件。判据二对它们**一起**扫,这是它能挡住
#: 「把读取从一个模块挪到另一个」的唯一原因——分开扫等于给移动变异开一扇门。
_SCANNED_MODULES = {
    "projection": _SERVICES / "retrieval_experience_projection.py",
    "job": _SERVICES / "retrieval_experience_job.py",
    "block": _SERVICES / "retrieval_experience_block.py",
}

#: 危险键名。每一个都附了「为什么」——一条没有理由的禁令,下一个人会当噪声删掉。
_FORBIDDEN_NAMES = {
    # 用户自己写下的问题,以及模型对它的改写。同一份内容的三种拼写。
    "question",
    "resolved_question",
    "questions",
    "query",
    "queries",
    "sub_queries",
    # 从问题派生的词法词项/关键词:它们**就是**问题里的词,只是拆开了。
    "terms",
    "keywords",
    # 轨迹步给人看的那句话。多个发射点往里插模型文本或异常串——它在 P1 的覆盖层
    # 样本里没问题(那份产物只有本人读得到),在这里不行。
    "summary",
    # 文档侧的三样:标题、摘录、正文。
    "title",
    "excerpt",
    "file_name",
    "text",
    # 租户 id。经验条目一旦带上任何一个,「某人在某库问过某类问题」就能被拼回来。
    "notebook_id",
    "source_id",
    "element_id",
    "created_by",
    "user_id",
    "owner_id",
}


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """docstring 的 ``ast.Constant`` 节点 id 集合。

    必须排除:三个模块的 docstring 里**大量**出现「为什么 question 危险」这类
    解释,而那正是我们希望它们写下来的东西。注释根本不进 AST,无需处理。
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if node.body and isinstance(node.body[0], ast.Expr):
            value = node.body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add(id(value))
    return found


def _forbidden_hits(path: Path) -> list[tuple[str, str, int]]:
    """扫出危险键名的出现点,第三元是**纯诊断**行号,不是身份。

    ⚠ 累加变量必须叫 `violations`(或 diagnostic/offender/mismatch/error 之一),
    不能叫 `hits`：`backend/tests/architecture/policy.py` 的
    `line-number-identity` 规则按**累加变量名**判断一处 `node.lineno` 是
    「用行号当身份」还是「行号只是报错时给人看的」,而这条规则是 G2
    (`architecture_contract`) 的硬门。改名会让整条守卫在扩展门上报红。
    """
    tree = _module_tree(path)
    docstrings = _docstring_nodes(tree)
    violations: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            # 读与写是两种形态,而且只有**读**可能被 job 模块的白名单放行
            # (T2 规格评审):``notebook_id = row["nb"]`` 与
            # ``class X: notebook_id: str = ""`` 在语法上都只是一个
            # ``ast.Name``,但它们不是「读一个形参」,而是在本模块里**造出**
            # 一个叫这个名字的东西——前者是把租户 id 绑到本地状态上,后者是给
            # 一个会被序列化的类型加字段。两者都必须仍然违规。
            kind = "赋值目标" if isinstance(node.ctx, (ast.Store, ast.Del)) else "变量"
            violations.append((kind, node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(("属性", node.attr, node.lineno))
        elif isinstance(node, ast.arg) and node.arg in _FORBIDDEN_NAMES:
            violations.append(("形参", node.arg, node.lineno))
        elif isinstance(node, ast.ExceptHandler) and node.name in _FORBIDDEN_NAMES:
            # ``except Exception as notebook_id:`` 绑定的也是一个名字,而且
            # ``ast.ExceptHandler.name`` 是**裸字符串**,不是 ``ast.Name``——
            # 上面那条 ``ctx`` 判据看不见它。归入「赋值目标」:它与
            # ``notebook_id = ...`` 是同一件事(在本模块里造一个绑定),只是
            # 语法糖不同。
            violations.append(("赋值目标", node.name, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg in _FORBIDDEN_NAMES:
            # 2026-09-22 新增的第五种形态。它与「形参」是两件事:``ast.arg`` 是
            # 「本模块声明了一个叫这个名字的参数」,``ast.keyword`` 是「本模块
            # 按这个名字给**别人**传值」。分区化之前这个形态在三个模块里一次
            # 都没出现过,所以扫不扫没差别;分区化之后 job 模块正是靠它把分区
            # 交给 store 与取数,于是它必须被**看见**才能被精确放行——白名单
            # 只对 job 模块的 ``notebook_id`` 生效,projection/block 零豁免。
            violations.append(("实参名", node.arg, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value in _FORBIDDEN_NAMES
        ):
            # 精确相等,不是子串:``"[Recent searches, grouped by question
            # shape]"`` 是渲染给模型的表头,里面的 "question" 是英文单词而不是
            # 一个键。按子串扫会把它误杀,而误杀会让下一个人把整条守卫关掉。
            violations.append(("字面量", node.value, node.lineno))
    return violations


#: job 模块对 ``notebook_id`` 的**显式白名单**(2026-09-22 按笔记本分区)。
#:
#: 禁键表里仍然留着 ``notebook_id``——这不是形式:蒸馏链路能读到分区 id 的地方
#: 只有三处(形参、读这个形参、把它作为实参名传出去),而它**不能**出现的地方
#: 恰恰是那些看起来最顺手的写法:``row["notebook_id"]`` / ``.get("notebook_id")``
#: 是从一条 run 上读租户 id(判据八说的就是这条路必须堵死),``.notebook_id``
#: 是从某个观测对象上取它,裸字符串常量则是把它拼进 prompt 或塞进一个 dict 字段。
#: 三种违规形态在这个模块里与在另外两个模块里同样红。
#:
#: ⚠ 白名单按**形态**放行,不按行号、不按函数名:行号会随任何一次编辑失效,
#: 函数名则会让「把违规读挪进那个函数」变成一次合法重构。
#:
#: ⚠ 「变量」只指**读**(``ast.Load``)。写(``ast.Store``,含注解赋值的目标)
#: 被单列成「赋值目标」,不在白名单里:``notebook_id = ...`` 是在本模块里造一个
#: 叫这个名字的绑定,``notebook_id: str = ""`` 是给一个类型加字段——两者都不是
#: 「分区作为参数流经」。
_JOB_PARTITION_NAME = "notebook_id"
_JOB_PARTITION_ALLOWED_KINDS = frozenset({"形参", "变量", "实参名"})


def _hits_after_job_exemption(
    module_name: str, hits: list[tuple[str, str, int]]
) -> list[tuple[str, str, int]]:
    """判据二的最终判决:只有 job 模块的 ``notebook_id`` 走白名单。"""
    if module_name != "job":
        return hits
    return [
        hit for hit in hits
        if not (hit[1] == _JOB_PARTITION_NAME and hit[0] in _JOB_PARTITION_ALLOWED_KINDS)
    ]


# --------------------------------------------------- 判据一:投影层没有自由文本

def _closed_aliases(tree: ast.Module) -> set[str]:
    """模块级 ``X = Literal[...]`` 的别名名字(如 ``CountBand``)。"""
    aliases: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Subscript):
            continue
        base = value.value
        if not (isinstance(base, ast.Name) and base.id == "Literal"):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)
    return aliases


def _dataclasses(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }


def _reachable_fields(
    root: str, tree: ast.Module
) -> list[tuple[str, str, ast.expr]]:
    """``(类名, 字段名, 注解节点)``,从 ``root`` 出发递归展开嵌套的类型。"""
    aliases = _closed_aliases(tree)
    classes = _dataclasses(tree)
    seen: set[str] = set()
    found: list[tuple[str, str, ast.expr]] = []

    def visit(name: str) -> None:
        if name in seen or name not in classes:
            return
        seen.add(name)
        for stmt in classes[name].body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                stmt.target, ast.Name
            ):
                continue
            found.append((name, stmt.target.id, stmt.annotation))
            for nested in _referenced_classes(stmt.annotation, classes, aliases):
                visit(nested)

    visit(root)
    return found


def _referenced_classes(
    annotation: ast.expr, classes: dict[str, ast.ClassDef], aliases: set[str]
) -> list[str]:
    return [
        node.id
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name)
        and node.id in classes
        and node.id not in aliases
    ]


def _is_closed(
    annotation: ast.expr, classes: dict[str, ast.ClassDef], aliases: set[str]
) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id in {"int", "bool"} or annotation.id in aliases
    if isinstance(annotation, ast.Subscript):
        base = annotation.value
        if isinstance(base, ast.Name) and base.id == "Literal":
            return True
        if isinstance(base, ast.Name) and base.id in {"tuple", "Tuple"}:
            # ``tuple[ActionObservation, ...]``:元素类型必须是本模块里的一个
            # dataclass,它自己的字段会被 ``_reachable_fields`` 递归检查到。
            elements = annotation.slice
            if isinstance(elements, ast.Tuple) and elements.elts:
                head = elements.elts[0]
                return isinstance(head, ast.Name) and head.id in classes
    return False


def test_the_observation_type_has_no_free_text_field_anywhere_it_can_reach():
    """判据一。静态版本——不 import,直接读注解的源码形态。

    与 ``test_retrieval_experience_job`` 里那条同名的运行时判据是**两条不同的**
    守卫,不是重复:那条用 ``typing`` 在导入后解析,这条读的是源码。一个 `str`
    字段无论以哪种形式加进来,两条里至少一条会红;而这条还不需要模块 import 得
    起来,所以它在依赖坏掉时仍然说得出话。
    """
    tree = _module_tree(_SCANNED_MODULES["projection"])
    aliases = _closed_aliases(tree)
    classes = _dataclasses(tree)
    fields = _reachable_fields("RunObservation", tree)
    for owner, name, annotation in fields:
        assert _is_closed(annotation, classes, aliases), (
            f"{owner}.{name}: {ast.unparse(annotation)} 不是 int / bool / "
            "Literal / 由它们构成的 tuple。这个类型是全局经验库的输入面——一个"
            "自由文本字段就足以把某个人的问题、某份文档的标题带进一张全体用户"
            "都读得到的表,而且不会有任何报错。"
        )


def test_gutting_the_observation_type_does_not_satisfy_the_rule():
    """判据一的下限。「每个字段都是封闭的」对空 dataclass 恒真。

    Agentic Memory P4 (T3):11→13——``ActionObservation`` 新增
    ``anchored_hits``/``attributable`` 两个字段后可达字段总数从 14 涨到 16,
    下限保守跟涨到 13(不写死到精确的 16,与本守卫此前"下限低于精确计数"的
    一贯做法一致)。
    """
    fields = _reachable_fields(
        "RunObservation", _module_tree(_SCANNED_MODULES["projection"])
    )
    assert len(fields) >= 13, fields


def test_the_alias_resolution_actually_resolves_something():
    """空转保护:判据一要真的**走到**别名与嵌套类那两条分支上。

    没有它,`_closed_aliases`/`_referenced_classes` 哪天返回空集,上面两条仍然
    全绿——一条什么都没解析的守卫看起来和一条解析成功的守卫一模一样。
    """
    tree = _module_tree(_SCANNED_MODULES["projection"])
    assert _closed_aliases(tree), "模块里应当有 Literal 别名(CountBand)"
    owners = {owner for owner, _name, _ann in _reachable_fields("RunObservation", tree)}
    assert owners == {"RunObservation", "ActionObservation"}, owners


# ------------------------------------- 判据二:三个模块一起扫,禁键一个都不许出现

@pytest.mark.parametrize("module_name", sorted(_SCANNED_MODULES))
def test_no_module_of_this_feature_reads_a_dangerous_field(module_name):
    """判据二,**本守卫的核心**。

    参数化到每个模块,但真正的性质是「三个一起」:把一次 `question` 的读取从
    job 挪到 projection、或挪到注入侧的 block,语义完全不变,只扫一个模块的守卫
    会全绿。这条变异(而不是「删掉某个读」)才是这份守卫是否成立的判据。

    2026-09-22:job 模块的 ``notebook_id`` 有一条按形态的白名单(见
    ``_JOB_PARTITION_ALLOWED_KINDS``)——分区 id 现在确实要流经那个模块,但只能
    以参数的形态流经。另外两个模块**零豁免**。
    """
    hits = _hits_after_job_exemption(
        module_name, _forbidden_hits(_SCANNED_MODULES[module_name])
    )
    assert not hits, (
        f"{module_name} 模块出现了危险键名 {hits}。经验库的输入面必须只有"
        "int / bool / 封闭词表:任何一处对问题、查询词、摘要、标题或租户 id 的"
        "读取,都会让「模型从没见过用户文本」这句话失效,而且没有任何报错。"
    )


def test_the_forbidden_scan_can_actually_see_a_violation():
    """空转保护:扫描器本身必须真的认得出违规形态。

    六种承诺挡得住的形态各来一次(变量、赋值目标、属性、形参、实参名、字面量键)。
    没有它,`_forbidden_hits` 哪天因为一处重构恒返回空列表,上面三条参数化用例
    会全绿。
    """
    import tempfile

    source = (
        '"""模块 docstring 里出现 question 这个词不该被算作违规。"""\n'
        "def f(question):\n"
        "    a = question\n"
        "    b = row.summary\n"
        '    c = row["notebook_id"]\n'
        "    d = store.read(title=a)\n"
        "    user_id = a\n"
        "    return a, b, c, d, user_id\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        kinds = {kind for kind, _name, _line in _forbidden_hits(path)}
        names = {name for _kind, name, _line in _forbidden_hits(path)}
    finally:
        path.unlink()
    assert kinds == {"变量", "赋值目标", "属性", "形参", "实参名", "字面量"}, kinds
    assert names == {
        "question", "summary", "notebook_id", "title", "user_id",
    }, names


def _mutated_job_module(extra_source: str) -> list[tuple[str, str, int]]:
    """把一段变异源码接到 job 模块真身后面,再跑一遍判据二(含白名单)。

    接在**真实模块源码**之后而不是单独造一个小文件:白名单是按模块身份生效的,
    而这条变异要证明的正是「即使在那个有豁免的模块里,这种形态照样被抓住」。
    """
    import tempfile

    source = (
        _SCANNED_MODULES["job"].read_text(encoding="utf-8") + "\n" + extra_source
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        return _hits_after_job_exemption("job", _forbidden_hits(path))
    finally:
        path.unlink()


def test_the_job_modules_exemption_still_catches_a_read_off_a_run():
    """判据二的变异用例(2026-09-22,规格 §11 第 7 条)。

    白名单放行的是「分区 id 作为参数流经」,它必须**不**放行「从一行 run 上把
    notebook_id 读出来」——那正是分区化最容易顺手写出、而整个特性最不能有的
    一行:一条 run 的租户 id 一旦进了这个模块的数据流,它离进 ``ObservedRun``、
    进 prompt、进一行全库可见的 rationale 就只剩一次重构。

    三种形态各来一次,并且是接在**真身之后**跑的——证明豁免没有把整个模块变成
    不设防区,而只是按形态开了三道缝。
    """
    subscript = _mutated_job_module(
        "def _mutant_subscript(row):\n"
        '    return row["notebook_id"]\n'
    )
    getter = _mutated_job_module(
        "def _mutant_getter(row):\n"
        '    return row.get("notebook_id")\n'
    )
    attribute = _mutated_job_module(
        "def _mutant_attribute(run):\n"
        "    return run.notebook_id\n"
    )
    assert [kind for kind, _name, _line in subscript] == ["字面量"], subscript
    assert [kind for kind, _name, _line in getter] == ["字面量"], getter
    assert [kind for kind, _name, _line in attribute] == ["属性"], attribute


def test_the_job_modules_exemption_never_lets_the_name_be_bound_or_declared():
    """变异的第二类(T2 规格评审):不是「读出来」,而是在本模块里**造出来**。

    ``notebook_id = row["nb"]`` 只需要上游换一个键名就能绕开「字面量」那一条,
    而绑定本身才是问题——分区 id 有了一个本地名字,下一步就是把它塞进某个结构。
    ``class X: notebook_id: str = ""`` 更直接:一个会被序列化的字段。两者在
    AST 里都只是 ``ast.Name``,与被放行的「读形参」只差一个 ``ctx``,所以白名单
    必须按 ``Load``/``Store`` 分开——否则它放行的远不止它承诺的三种形态。
    """
    binding = _mutated_job_module(
        "def _mutant_binding(row):\n"
        '    notebook_id = row["nb"]\n'
        "    return notebook_id\n"
    )
    declaration = _mutated_job_module(
        "class _MutantObservation:\n"
        '    notebook_id: str = ""\n'
    )
    # ``except ... as notebook_id`` 是同一件事的第三种语法糖,而它在 AST 里是
    # 处理器上的一个**裸字符串**属性,不是 ``ast.Name``——按 ``ctx`` 判读写的
    # 那条分支根本看不见它。
    caught = _mutated_job_module(
        "def _mutant_caught(fn):\n"
        "    try:\n"
        "        return fn()\n"
        "    except Exception as notebook_id:\n"
        "        return notebook_id\n"
    )
    assert [kind for kind, _name, _line in binding] == ["赋值目标"], binding
    assert [kind for kind, _name, _line in declaration] == ["赋值目标"], declaration
    assert [kind for kind, _name, _line in caught] == ["赋值目标"], caught


def test_the_job_modules_exemption_is_not_vacuous():
    """反向:真身**确实**用到了被豁免的那三种形态。

    没有这一条,白名单哪天变成一条永远匹配不到东西的死规则(或者 job 模块哪天
    不再传分区了)都不会有人发现,而上面那条变异用例照样绿——它证明的是「违规
    形态抓得住」,不是「合法形态真的存在」。
    """
    kinds = {
        kind for kind, name, _line in _forbidden_hits(_SCANNED_MODULES["job"])
        if name == _JOB_PARTITION_NAME
    }
    assert kinds == _JOB_PARTITION_ALLOWED_KINDS, kinds


def test_the_docstrings_that_explain_the_danger_are_not_themselves_violations():
    """反向断言:三个模块的 docstring 里**确实**写着这些词,而它们没有报红。

    这不是洁癖——它证明判据二排除 docstring 的那一步真的在起作用,而不是碰巧
    三个模块从来没提过这些词(那样的话排除逻辑删掉也没人发现)。
    """
    prose = "\n".join(
        path.read_text(encoding="utf-8") for path in _SCANNED_MODULES.values()
    )
    assert "question" in prose and "summary" in prose


# ------------------------------------------------- 判据三:写入面只认封闭词表

def _group(situation: dict, action: str = "enumerate") -> _SituationGroup:
    """一个**真的观测到过** ``action`` 的情境组。

    ⚠ ``action_run_ids`` 必须填,而且要填两个不同的 run:写入面在校验词表之前
    还有两道闸(「本批没有任何 run 用过这个动作」直接丢、「ADD 少于两个 run」
    直接丢),一个空组会让下面每一条「被丢弃」的断言都因为**别的**理由成立——
    判据三、四就会在什么都没检查的情况下全绿。``test_the_baseline_reply_is_
    actually_accepted`` 是这句话的正面证据。
    """
    group = _SituationGroup(experience_id(situation, ""), situation)
    group.run_ids = ["run-a", "run-b"]
    group.action_run_ids[action] = ["run-a", "run-b"]
    return group


def test_the_baseline_reply_is_actually_accepted():
    """空转保护:判据三、四的每一条都是「这一格被丢弃」,而一条恒被丢弃的基线
    会让它们全部无意义。这条钉住基线回复本身是**收得下**的。"""
    accepted = parse_distillation_reply(_reply(), [_group(_situation())])
    assert len(accepted) == 1
    assert accepted[0]["action"] == "enumerate"


def _situation() -> dict:
    return {
        "mode": "reasoning",
        "result_scope": "ranked",
        "retrieval_effort": "standard",
        "completeness_required": False,
        "entity_count": "few",
        "topic_count": "none",
        "has_constraints": False,
        "has_exclusions": False,
    }


def _reply(**overrides) -> dict:
    entry = {
        "op": "ADD",
        "situation": "s0",
        "action": "enumerate",
        "polarity": "good",
        "rationale": "Listing the collection beats keyword hunting on this shape.",
    }
    entry.update(overrides)
    return {"entries": [entry]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "ppr_retrieve"},           # 动作 id ≠ 存储词表拼写
        {"action": "read_all_sources"},       # 词表外的新动作
        {"polarity": "neutral"},              # 极性词表外
        {"polarity": ""},
        {"rationale": ""},
        {"rationale": "x" * (RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS + 1)},
    ],
)
def test_the_write_path_discards_anything_outside_the_closed_vocabularies(overrides):
    """判据三。非法值一律**丢弃**,绝不猜一个近似值。

    ``ppr_retrieve`` 那一格尤其重要:它是模型面真实存在的动作 id,与存储词表的
    ``ppr`` 只差一个后缀,「顺手做个前缀匹配」看起来完全合理——而它会把一条关于
    某个通道的经验记到另一个通道上。
    """
    assert parse_distillation_reply(_reply(**overrides), [_group(_situation())]) == []


@pytest.mark.parametrize(
    "situation",
    [
        {**_situation(), "made_up_key": 1},
        {**_situation(), "result_scope": "everything the user meant"},
        {key: value for key, value in _situation().items() if key != "mode"},
    ],
)
def test_an_unregistered_situation_key_or_value_discards_the_entry(situation):
    assert validate_situation(situation) is None


def test_the_read_side_re_validates_the_same_vocabularies():
    """判据三的读侧那一半。

    写侧校验过了还要再校验一次,不是保险起见:这张表经 ``scripts/merge_dbs.py``
    跨部署并集,行可以来自另一份代码;而一行也可以比一次词表变更活得更久。渲染
    一条动作词已经不存在的经验,等于告诉模型去用一个它调不出来的通道。
    """
    good = {
        "id": "rx_1", "situation": _situation(), "action": "enumerate",
        "polarity": "good", "rationale": "still fine", "support": 3,
    }
    assert usable_entry(good) is True
    assert usable_entry({**good, "action": "enumerate_elements"}) is False
    assert usable_entry({**good, "polarity": "meh"}) is False
    assert usable_entry({**good, "rationale": "   "}) is False
    assert usable_entry({**good, "situation": {"mode": "reasoning"}}) is False


# --------------------------------------------- 判据四:注入面不带 id、不带来源

def test_a_rationale_carrying_an_id_shaped_token_is_refused_at_the_write_side():
    """判据四的写侧那一半。

    id 形状的检查是**输入收窄的绊线**,不是清洗器:rationale 里出现一个 id,说明
    模型看到了它不该看到的东西。整条丢弃(而不是擦掉那个串再存)才让这次失败留
    得下痕迹——擦干净的版本会把事故变成一行看起来正常的经验。
    """
    accepted = parse_distillation_reply(
        _reply(rationale="Prefer listing for run 0a1b2c3d4e5f60718293a4b5c6d7e8f9."),
        [_group(_situation())],
    )
    assert accepted == []


@pytest.mark.parametrize(
    "rationale",
    [
        "在 nb-abc123 这种库里先列目录",          # 最短的 nb- 形状
        "Prefer listing in nb-a73f16940c.",       # 本机真实形状的 notebook id
    ],
)
def test_a_rationale_carrying_a_notebook_shaped_id_is_refused_too(rationale):
    """判据四的第二根绊线(2026-09-22 按笔记本分区)。

    通用的 ``[0-9a-fA-F]{16,}`` 要十六位十六进制才响,而这个仓库的 notebook id
    是 ``nb-`` 加**更短**的一段(本机真实样本 ``nb-a73f16940c``,十位)。分区化
    给这条链路引入了一个新的 id,绊线就必须跟着认得它——否则「模型看到了不该看
    到的东西」这件事,恰好在唯一新增的那种 id 上不会被发现。

    与第一根绊线同款:整条**丢弃**,不擦洗。
    """
    assert parse_distillation_reply(
        _reply(rationale=rationale), [_group(_situation())]
    ) == []


def test_the_rendered_block_carries_no_id_and_no_source_or_library_name():
    """判据四的渲染侧那一半——正向断言,不是「我们没往里放」。

    条目本身就没有来源/库字段可渲染,所以这条钉的是那个结构性事实:即使把
    id 形状串和库名塞进 rationale(跨部署并集能让这种行进到表里),渲染出来的
    行里也**只有** 动作 id、极性词与那句 rationale ——而检索范围从不在其中。
    """
    import re

    situation = _situation()
    entries = [
        {
            "id": "rx_" + "a" * 32,
            "situation": situation,
            "action": "enumerate",
            "polarity": "good",
            "rationale": "Listing beats hunting on this shape.",
            "support": 5,
        }
    ]
    rendered = render_experience_block(select_experiences(entries, situation))
    assert rendered, "前提:这条经验确实被渲染了"
    assert not re.search(r"[0-9a-fA-F]{16,}", rendered), rendered
    assert "rx_" not in rendered, "条目自己的主键也不得出现在块里"
    lowered = rendered.lower()
    for word in ("notebook", "source_id", "library "):
        assert word not in lowered, (word, rendered)
    assert len(rendered) <= RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS


# ---------------------------------- 判据五:三张动作表都够不到「检索范围」

#: 范围类词。任何一个出现在动作词表里,就意味着模型可以被一条经验劝去改变这次
#: run **能读什么**——而那是用户勾选的、本特性一个字都不许碰的东西。
_SCOPE_WORDS = ("source", "base", "scope", "notebook", "mount", "library")


def test_none_of_the_action_tables_can_reach_retrieval_scope():
    """判据五。T5 已经对存储词表钉过一次,这里补上 T6 新增的**两张**表:

    * ``_ACTION_IDS`` 的取值 —— 真正渲染进 prompt、模型照着念的那批词;
    * ``ADOPTION_ACTIONS`` 的键 —— 采用账目认得的那批 reflect 动作 id。

    只钉存储词表是不够的:渲染表是一次映射,完全可以把一个无害的存储词映射成一
    个范围类动作 id;采用表则决定了「哪些选择会被记成对经验的采用」。
    """
    from app.services.retrieval_experience_block import _ACTION_IDS

    surfaces = {
        "stored vocabulary": RETRIEVAL_ACTIONS,
        "rendered action ids": tuple(_ACTION_IDS.values()),
        "adoption action ids": tuple(ADOPTION_ACTIONS),
    }
    for label, words in surfaces.items():
        assert words, label
        for word in words:
            assert not any(scope in word for scope in _SCOPE_WORDS), (label, word)


def test_the_three_action_tables_agree_with_each_other():
    """三张表必须互相自洽,否则判据五能被绕开:一个不在存储词表里的动作 id 出现
    在渲染表里,上面那条会检查它,但一条**只**存在于采用表里的动作会让「采用」
    记到一个渲染面从未推荐过的通道上。
    """
    from app.services.retrieval_experience_block import _ACTION_IDS

    assert set(_ACTION_IDS) == set(RETRIEVAL_ACTIONS)
    assert set(ADOPTION_ACTIONS.values()) <= set(RETRIEVAL_ACTIONS)
    assert set(EXPERIENCE_POLARITIES) == {"good", "bad"}
    assert len(SITUATION_KEYS) == 8


# --------------------------------------------- 判据六:运行时,观测里不含 id

def test_running_project_run_never_leaks_a_result_or_anchor_id_into_the_observation():
    """判据六。判据一是**静态**的——它读注解,不管值。这条造一个真实携带 id 的
    run,跑一遍 ``project_run``,再对序列化后的 ``ObservedRun.observation`` 做
    子串扫描:证明「``anchored_hits``/``attributable`` 两个 int/bool 字段」这
    个设计真的没有在哪个分支里把原始 id 塞进了某个字段。
    """
    from dataclasses import asdict

    anchor_id = "ko-" + "a" * 32
    result_id = "chunk-" + "b" * 32
    run = {
        "run_id": "job-privacy-guard",
        "mode": "reasoning",
        "steps": [
            {"step_type": "intent", "situation": _situation()},
            {"step_type": "ppr", "count": 3, "result_ids": [result_id, anchor_id]},
            {
                "step_type": "synthesis",
                "count": 2,
                "anchor_evidence_ids": [anchor_id, result_id],
            },
        ],
    }
    observed = project_run(run)
    assert observed is not None
    # 前提:这条 run 确实产生了非零的 anchored_hits——否则下面的「不含 id」
    # 断言对一条从没算出过命中的观测毫无区分力。
    by_action = {a.action: a for a in observed.observation.actions}
    assert by_action["ppr"].anchored_hits == 2
    assert by_action["ppr"].attributable is True

    serialized = repr(asdict(observed.observation))
    assert anchor_id not in serialized, serialized
    assert result_id not in serialized, serialized


# ------------------------------ 判据七:result_ids/anchor_evidence_ids 只活在
#                                 project_run 一个函数里

#: Agentic Memory P4 (T3) 新增的读取面。合法(判据二的禁键表不含它们——它们
#: 不是自由文本,是不透明句柄的字段名),但只应该在 ``retrieval_experience_
#: projection.py`` 的 ``project_run`` 一个函数的子树里出现。⚠ 判据必须与判据
#: 二同一副骨架——**三个模块一起扫**,而不是只扫 projection.py:把交集计算从
#: ``project_run`` 挪到 job 模块的 ``_observe``(或 block 模块的任何函数)是
#: 语义完全不变的移动变异,只扫 projection.py 会对它视而不见——那正是「id 只
#: 活在 project_run 局部变量」这句承诺唯一站得住脚的理由。
_ATTRIBUTION_KEYS = {"result_ids", "anchor_evidence_ids"}


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _attribution_hits_outside_project_run(path: Path) -> list[tuple[str, str, int]]:
    """``result_ids``/``anchor_evidence_ids`` 出现在 ``path`` 这个文件里,
    ``project_run`` 子树**之外**的每一处。文件里如果根本没有 ``project_run``
    函数(job.py、block.py,以及任何临时测试文件),"子树之外"就是"整个文件"
    ——这两个键名在那样的文件里出现,一次都不许。
    """
    tree = _module_tree(path)
    scoped = _function_node(tree, "project_run")
    scoped_ids = {id(node) for node in ast.walk(scoped)} if scoped is not None else set()
    docstrings = _docstring_nodes(tree)
    violations: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if id(node) in scoped_ids:
            continue
        if isinstance(node, ast.Name) and node.id in _ATTRIBUTION_KEYS:
            violations.append(("变量", node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _ATTRIBUTION_KEYS:
            violations.append(("属性", node.attr, node.lineno))
        elif isinstance(node, ast.arg) and node.arg in _ATTRIBUTION_KEYS:
            violations.append(("形参", node.arg, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and node.value in _ATTRIBUTION_KEYS
        ):
            violations.append(("字面量", node.value, node.lineno))
    return violations


@pytest.mark.parametrize("module_name", sorted(_SCANNED_MODULES))
def test_result_and_anchor_ids_are_read_only_inside_project_run(module_name):
    """判据七,**本守卫的核心**,参数化到三个模块——但真正的性质与判据二一样是
    「三个一起」:把交集计算从 ``projection.py`` 的 ``project_run`` 挪到
    ``job.py`` 的 ``_observe``,语义完全不变,只扫 projection.py 的判据会全绿。
    在 projection.py 内部,这两个键只许出现在 ``project_run`` 子树里——挪到
    ``validate_situation``、``experience_id``、``situation_similarity`` 或
    任何其他函数同样违规。在 job.py / block.py 里,出现一次就是违规,因为
    ``project_run`` 在那两个文件里根本不存在。
    """
    violations = _attribution_hits_outside_project_run(_SCANNED_MODULES[module_name])
    assert not violations, (
        f"{module_name} 模块在 project_run 之外出现了 {violations}。"
        "result_ids/anchor_evidence_ids 只应该在 projection.py 的 project_run "
        "内部被读取——出现在别处(包括挪到另一个模块)意味着「归因用的原始 "
        "id」有了第二条活路，而这个设计的唯一承诺就是它只活在一个函数的局部"
        "变量里。"
    )


def test_the_attribution_scan_can_actually_see_a_violation_outside_project_run():
    """空转保护:扫描器必须真的认得出两种违规——同模块内挪到别的函数,以及
    挪到一个根本没有 ``project_run`` 的模块。"""
    import tempfile

    same_module_violation = (
        '"""docstring 提到 result_ids 不该报警。"""\n'
        "def project_run(run):\n"
        "    return run.get(\"result_ids\")\n"
        "def validate_situation(row):\n"
        "    a = row.get(\"anchor_evidence_ids\")\n"
        "    b = row[\"result_ids\"]\n"
        "    return a, b\n"
    )
    other_module_violation = (
        "def _observe(row):\n"
        "    return row.get(\"result_ids\"), row.get(\"anchor_evidence_ids\")\n"
    )
    for source in (same_module_violation, other_module_violation):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(source)
            path = Path(handle.name)
        try:
            violations = _attribution_hits_outside_project_run(path)
        finally:
            path.unlink()
        names = {name for _kind, name, _line in violations}
        assert names == {"anchor_evidence_ids", "result_ids"}, (source, violations)


# ------------------------------ 判据八:分区 id 进不了观测类型,也进不了 run 投影
#
# 2026-09-22 按笔记本分区之后新增。判据二挡的是「这个模块里出现了这个名字」,
# 而 job 模块现在按形态豁免了三种出现方式——豁免开了缝,就必须有人在缝的另一头
# 守着「就算它作为参数流经了 job,它也进不了任何一个会被渲染的结构」。这条判据
# 守的正是那一头,而且它扫的是**另外两个文件**(projection 模块的两个 dataclass、
# ports.py 的 ``project_run_row``),所以 job 模块里的任何写法都绕不过它。

#: run 投影的真身位置。它住在 ports.py 而不是三个被扫模块里——这不是巧合:
#: 「一条 run 变成什么」被刻意放在了 job 模块够不着的地方。
_PORTS = Path(app.services.__file__).parent.parent / "repositories" / "ports.py"


def _class_field_names(tree: ast.Module, name: str) -> set[str]:
    node = _dataclasses(tree).get(name)
    assert node is not None, f"{name} 不在 projection 模块里了"
    return {
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }


def _returned_dict_keys(tree: ast.Module, function: str) -> set[str]:
    node = _function_node(tree, function)
    assert node is not None, f"{function} 不在 ports.py 里了"
    keys: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
            continue
        for key in child.value.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                f"{function} 的返回字典出现了非字面量键 {ast.dump(key)}——"
                "键名一旦是个变量,这条判据就再也说不出它返回了什么。"
            )
            keys.add(key.value)
    return keys


def test_neither_observation_type_nor_the_run_projection_can_carry_a_partition_id():
    """判据八(静态)。

    分区 id 在这条链路上只有三个合法落点:取数的谓词、写入的列、内容寻址 id 的
    哈希输入。这条钉住它**不在**第四个地方——一条 run 被投影成的那些结构里。
    顺序很重要:``project_run_row`` 是 run 进入本特性的唯一入口,观测类型是它流向
    prompt 的唯一载体,两头都不含这个键,中间那段(job 模块)就没有任何东西可以
    把它读出来,豁免开的三道缝也就只能是「参数进、参数出」。
    """
    projection = _module_tree(_SCANNED_MODULES["projection"])
    reachable = {name for _owner, name, _ann in _reachable_fields("RunObservation", projection)}
    envelope = _class_field_names(projection, "ObservedRun")
    assert reachable, "前提:RunObservation 确实有可达字段"
    assert envelope == {"run_id", "observation"}, envelope
    for field in sorted(reachable | envelope):
        assert "notebook" not in field, field

    keys = _returned_dict_keys(_module_tree(_PORTS), "project_run_row")
    assert keys == {"run_id", "mode", "steps"}, keys


# -------------------- 判据九:分区蒸馏发给模型的 prompt 与全局蒸馏逐字节相同

class _GuardStore:
    """经验库 store 的最小替身。分区参数照收不误,但**返回同一批行**——这正是
    判据九要的对照条件:两次渲染的输入完全一样,于是任何差异都只可能来自分区
    id 自己漏进了渲染。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.partitions: list[str] = []
        self.upserts: list = []
        self.evictions: list[str] = []

    def read_partition(self, notebook_id, limit):
        self.partitions.append(notebook_id)
        return [dict(row) for row in self.rows]

    def upsert_experience(self, experience_id, **kwargs):
        self.upserts.append((experience_id, kwargs))
        return {"id": experience_id}

    def evict_to_limit(self, max_entries, notebook_id=""):
        self.evictions.append(notebook_id)
        return 0

    def count(self, notebook_id=None):
        return len(self.rows)


class _GuardAskState:
    def __init__(self, runs):
        self.runs = runs
        self.calls: list = []

    def recent_completed_ask_runs(self, *, job_limit, step_limit, notebook_id=None):
        self.calls.append(notebook_id)
        return [dict(run) for run in self.runs]


class _GuardModels:
    """模型替身:只记下收到的完整 prompt。判据九的整条断言都落在这里。"""

    def __init__(self):
        self.prompts: list[str] = []

    def configured(self, workload):
        return True

    def chat(self, workload):
        return self

    def chat_json(self, messages, schema_hint, max_tokens=None):
        self.prompts.append(messages[0]["content"])
        return '{"entries":[]}'


class _GuardSettings:
    retrieval_experience_enabled = True
    retrieval_experience_trigger = 40
    retrieval_experience_notebook_trigger = 10


class _GuardEvents:
    def __init__(self):
        self.emitted: list[dict] = []

    def emit(self, payload):
        self.emitted.append(payload)


#: 特征明显、且**同时**踩中两根 id 绊线形状的假分区 id:它既是 ``nb-`` 前缀的
#: 短 id,也足够长。整份 prompt 里搜这一个串,比搜「像 id 的东西」精确得多。
_GUARD_PARTITION = "nb-guard9deadbeef"


def _guard_runs() -> list[dict]:
    """两条同情境的 run,已经是 store 投影后的形状(``project_run`` 的入参)。"""
    return [
        {
            "run_id": f"job-guard-{index}",
            "mode": "reasoning",
            "steps": [
                {"step_type": "intent", "situation": _situation()},
                {"step_type": "ppr", "count": 0},
                {"step_type": "synthesis", "count": 2},
            ],
        }
        for index in range(2)
    ]


def _guard_existing() -> list[dict]:
    """库里已有的一条,好让 prompt 的 ``[Existing entries]`` 半边也不是空的。"""
    return [
        {
            "id": "rx_" + "c" * 32,
            "situation": _situation(),
            "action": "enumerate",
            "polarity": "good",
            "rationale": "Listing beats hunting on this shape.",
            "support": 4,
        }
    ]


def _guard_service(store, ask_state, models, events):
    from app.services.retrieval_experience_job import (
        RetrievalExperienceDistillationService,
    )

    return RetrievalExperienceDistillationService(
        settings=_GuardSettings(),
        experiences=store,
        ask_state=ask_state,
        models=models,
        event_log=events,
    )


def _run_one_batch(partition: str):
    store = _GuardStore(_guard_existing())
    ask_state = _GuardAskState(_guard_runs())
    models = _GuardModels()
    events = _GuardEvents()
    _guard_service(store, ask_state, models, events).run(partition)
    return store, ask_state, models, events


def test_a_partitioned_batch_never_shows_the_model_which_library_it_is():
    """判据九(运行时),规格 §6/§11 第 7 条。

    判据八是静态的——它证明分区 id 没有**结构上的**落点。这条把一整批真的跑
    出来,拿一个特征 id 当分区,再在发给模型的完整 prompt 里搜它:证明那三条
    「只作为参数流经」的缝里,没有哪一条在渲染前把它拼了进去。

    ⚠ 最后那个逐字节相等才是真正的判据。「prompt 里搜不到这个串」挡得住直接
    拼接,挡不住「按分区换一种措辞/换一个顺序/多渲染一行」——那类改动同样把
    「这是哪个库」写进了模型的输入,只是写成了模型学得会、而子串搜索看不见的
    形式。两批的输入完全一样,唯一的差别只有分区参数,所以两份 prompt 但凡差
    一个字节,差的就是分区。
    """
    store, ask_state, models, events = _run_one_batch(_GUARD_PARTITION)
    global_store, global_ask, global_models, _events = _run_one_batch("")

    # 前提:分区确实一路流到了取数、读既有条目与淘汰——否则下面的「看不到它」
    # 只是因为它根本没参与这一批。
    assert ask_state.calls == [_GUARD_PARTITION]
    assert store.partitions == [_GUARD_PARTITION]
    assert store.evictions == [_GUARD_PARTITION]
    assert global_ask.calls == [None]
    assert global_store.partitions == [""]

    assert len(models.prompts) == 1, models.prompts
    prompt = models.prompts[0]
    assert _GUARD_PARTITION not in prompt, prompt
    assert "nb-" not in prompt, prompt
    assert prompt == global_models.prompts[0]

    # 两个渲染函数各自也不含它(prompt 是它们拼出来的,但一条只钉合成结果的
    # 判据会在将来有人改 prompt 模板时失去落点)。
    from app.services.retrieval_experience_job import (
        _group_by_situation,
        _offered_entries,
        render_existing,
        render_observations,
    )

    groups = _group_by_situation(
        [run for run in map(project_run, _guard_runs()) if run is not None]
    )
    assert groups, "前提:这两条 run 确实成了一个情境组"
    offered = _offered_entries(groups, _guard_existing())
    assert offered, "前提:既有条目确实被展示了,否则 render_existing 是空壳"
    assert _GUARD_PARTITION not in render_observations(groups)
    assert _GUARD_PARTITION not in render_existing(offered)

    # 事件流同样不带它——那是另一个披露面,``_emit`` 只发封闭词。
    assert events.emitted, "前提:这一批确实发了事件"
    for payload in events.emitted:
        assert _GUARD_PARTITION not in repr(payload), payload
        assert payload["partition"] == "notebook", payload
