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
            violations.append(("变量", node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(("属性", node.attr, node.lineno))
        elif isinstance(node, ast.arg) and node.arg in _FORBIDDEN_NAMES:
            violations.append(("形参", node.arg, node.lineno))
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
    """
    hits = _forbidden_hits(_SCANNED_MODULES[module_name])
    assert not hits, (
        f"{module_name} 模块出现了危险键名 {hits}。全局经验库的输入面必须只有"
        "int / bool / 封闭词表:任何一处对问题、查询词、摘要、标题或租户 id 的"
        "读取,都会让「模型从没见过用户文本」这句话失效,而且没有任何报错。"
    )


def test_the_forbidden_scan_can_actually_see_a_violation():
    """空转保护:扫描器本身必须真的认得出违规形态。

    四种承诺挡得住的形态各来一次(变量、属性、形参、字面量键)。没有它,
    `_forbidden_hits` 哪天因为一处重构恒返回空列表,上面三条参数化用例会全绿。
    """
    import tempfile

    source = (
        '"""模块 docstring 里出现 question 这个词不该被算作违规。"""\n'
        "def f(question):\n"
        "    a = question\n"
        "    b = row.summary\n"
        '    c = row["notebook_id"]\n'
        "    return a, b, c\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        kinds = {kind for kind, _name, _line in _forbidden_hits(path)}
        names = {name for _kind, name, _line in _forbidden_hits(path)}
    finally:
        path.unlink()
    assert kinds == {"变量", "属性", "形参", "字面量"}, kinds
    assert names == {"question", "summary", "notebook_id"}, names


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

def _group(situation: dict) -> _SituationGroup:
    group = _SituationGroup(experience_id(situation, ""), situation)
    group.run_ids = ["run-a"]
    return group


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
