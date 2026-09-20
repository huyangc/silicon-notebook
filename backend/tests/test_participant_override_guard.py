"""C1 -- 参与集覆盖的结构性隔离守卫(零松弛)。

``app/services/retrieval_participants.py`` 能**替换**一次检索的参与集,而
``resolve_participants``/``mount_sql.py`` 那个谓词是检索与**鉴权**共用的
(``source_scope.scoped_participants`` 的注释逐字写明:跨库来源代理、引用解析、
资产读取都吃它)。覆盖一旦漏进鉴权路径就是越权。

本文件钉这些事,任何一条单独成立都足以挡住越权:

1. 谁能 import 这个模块——**相等**断言,不是 ⊆(C3 接线后读者集合已经确定);
2. 谁能 import 这个模块的**名字**(再导出旁路:白名单模块把访问器再导出,
   鉴权文件从它那里拿,三层守卫会全绿);
3. 谁能安装覆盖(只有 ``global_ask``,含 ``as`` 改名的调用);
4. 鉴权路径既不 import 它,也仍然在调真实的挂载谓词——第二半才是关键:
   只断言"没 import"挡不住把某个鉴权点整体改道成别的取数方式;
5. **注入式谓词的来源**:``knowledge_query`` / ``knowledge_lifecycle`` /
   ``plugin_ask_engine`` 拿到的 ``participant_notebook_ids`` 是组合根注入的
   callable,组合根把它换成一个读座位的 lambda,消费方文件一字未动、组合文件
   也不 import 覆盖模块 —— 前四条全绿而鉴权谓词已经被覆盖感知的座位换掉;
6. **登记清单内**的 fail-soft handler 不许吞掉身份复核的 raise(见本文件末尾的
   `_SEAT_FAILSOFT_SITES`:那是一份人工核出的、try 体能触达参与集座位的清单,
   不是「所有 handler」;新增一个能触达座位的 fail-soft handler 必须同 diff 登记)。

写入方白名单仍是 ⊆:``global_ask`` 在 PR-D 才接。
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

import app.services.retrieval_participants as override_module
from tests.architecture.semantic_source import PythonSourceIndex


_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"

_OVERRIDE_MODULE = "app.services.retrieval_participants"
_OVERRIDE_PACKAGE, _, _OVERRIDE_LEAF = _OVERRIDE_MODULE.rpartition(".")
_OVERRIDE_PATH = "app/services/retrieval_participants.py"

# 白名单以 ``backend/`` 下的真实路径为准,不是模块基名:``app/api/mcp_tools/
# global_ask.py`` 与 ``app/services/global_ask.py`` 是两个文件,按基名匹配会
# 把 MCP 工具面一起放进写入方白名单。
_READER_WHITELIST = frozenset({
    # 参与集座位 ``_RetrievalState._retrieval_participants``(B1 建)。
    "app/services/retrieval_candidates.py",
    # 联邦关系图 / PPR / scale PPR 的参与集与缓存键。
    "app/services/graph_retrieval.py",
    # ``collection_map`` 的参与集。
    "app/services/collection_catalog.py",
    # typed 枚举的参与集(行与分母必须同源)。
    "app/services/collection_enumeration.py",
    # ``mounted_base_ids``(对比题的兄弟库)。
    "app/services/communities.py",
    # ``knowledge_context`` 的 canonical 折叠范围(同一 canonical 的知识对象折到
    # 一起、``in_network_relations`` 去哪些库取关系行)。**只有那一个消费点**:
    # 同文件的 ``collection_item_citations`` 是鉴权用途,必须继续直调真实挂载
    # 谓词,由 ``test_evidence_context_authorization_site_keeps_mount_predicate``
    # 反向钉住。
    "app/services/evidence_context.py",
    # 对等模式的三件套分叉(D0-4):保底份额恒 0、每条腿都是 peer(打标 / 天花板
    # 下推 / peek-only / 关补召回)、跨库合并阈值换成全局栏杆。它同时是覆盖预检
    # ``assert_override_matches_run()`` 的落点:``_bounded_participants`` 是每个
    # 联邦消费方读参与集的唯一入口,跑在父线程、不在任何 ``try`` 里,所以那一处
    # 刻意**不**登记进下面的 ``_SEAT_FAILSOFT_SITES``。
    "app/services/chunk_federation.py",
})
_WRITER_WHITELIST = frozenset({
    # D1-2:``global_ask_run`` —— 唯一同时安装覆盖、逐库天花板、detached turn 与
    # 联邦运行计划的管理器,也是 ``participant_override(...)`` 的**唯一调用点**。
    # 写入方与读者是两种角色:读者白名单上的模块读覆盖是对的、装覆盖不是,而这
    # 一个模块只装不读(它一行也不调 ``current_participant_override`` /
    # ``federated_ask_active``)。所以它进写入方白名单而**不**进
    # ``_EXPECTED_IMPORTERS`` 那条相等断言——那条断言钉的是读者接线有没有被悄悄
    # 回退,把一个纯写入方混进去会让它开始钉错的东西。
    #
    # **只有这一个文件。** D1-4 接线之后 ``global_ask.py`` 只**构造**
    # ``ParticipantOverride``(那是一次鉴权动作,与 ``can_read_many`` 同处),再把
    # 它交给这个管理器去装;构造一个值不安装任何座位,所以它不需要、也不应该有
    # 写入方资格。由 ``test_global_ask_never_installs_seats_itself`` 反向钉住。
    "app/services/global_run.py",
})
# 第三种角色:**构造**一个 ``ParticipantOverride`` 而既不读也不装。建覆盖是一次
# 鉴权动作(``can_read_many`` 刚刚在同一个函数里跑过,``attested_actor_id`` 说的
# 就是那件事),它必须与授权同处;安装则交给管理器,由它的全映射断言把两个座位
# 绑在一起。所以全局入口可以 import 这个类型,但既不在读者白名单上(它一行也不
# 调访问器),也不在写入方白名单上(它一行也不调 ``participant_override(...)``,
# 由 ``test_global_ask_never_installs_seats_itself`` 反向钉住)。
_CONSTRUCTOR_WHITELIST = frozenset({
    "app/services/global_ask.py",
})
_IMPORT_WHITELIST = _READER_WHITELIST | _WRITER_WHITELIST | _CONSTRUCTOR_WHITELIST

# C3 接线之后读者集合已经确定,所以这一条是**相等**而不是 ⊆:一个白名单里却
# 不再 import 的模块意味着接线被悄悄回退(例如 ``collection_catalog`` 改回直调
# ``participant_ids``),而 ⊆ 断言对回退是沉默的。
#
# 今天是 7 个读者(D0-3 加入 ``evidence_context``,D0-4 加入
# ``chunk_federation``)。
_EXPECTED_IMPORTERS = frozenset(_READER_WHITELIST)

# 被守的**名字**面 = 模块 ``__all__`` 去掉异常类型。异常刻意不守:它的规范定义
# 点是 ``app/domain/retrieval_control.py``,任何 fail-soft handler 都要能 import
# 它来 re-raise(见该模块 docstring),而"能命名一个异常"不授予任何权限。
_ERROR_NAMES = frozenset({"ParticipantOverrideError"})
_OVERRIDE_SURFACE = frozenset(override_module.__all__) - _ERROR_NAMES

_WHITELIST_RATIONALE = (
    "白名单在 backend/tests/test_participant_override_guard.py 的 "
    "_READER_WHITELIST / _WRITER_WHITELIST,论证在 "
    "app/services/retrieval_participants.py 的模块 docstring。零松弛的理由:"
    "参与集覆盖能替换一次 run 搜哪些库,而挂载谓词与鉴权共用;新增一个读者"
    "必须是一次被评审看见的白名单编辑,不能是接别的功能时顺手加的一行 import。"
)

# 文本预筛的 needle。任何 import 本模块的写法都必须拼出 ``retrieval_
# participants`` 这个名字(``from app.services.retrieval_participants import``
# / ``from app.services import retrieval_participants`` / ``import app.
# services.retrieval_participants as ...``),任何调用 ``participant_override``
# 的模块也必然先 import 了它。所以预筛相对于全量 AST 扫描不丢任何东西:唯一
# 能绕开的是 ``importlib.import_module`` 拼字符串,而那同样绕得开纯 AST 扫描。
#
# 再导出旁路(A1)要求预筛同时覆盖**公开面的每个名字**,否则一个
# ``from app.services.retrieval_candidates import current_participant_override``
# 的鉴权文件连解析都轮不到。下面这组 needle 是公开面的前缀覆盖——
# ``resolve_retrieval_participant(_id)s`` 含 ``retrieval_participants``、
# ``current_participant_override`` 含 ``participant_override``——由
# ``test_needles_cover_the_whole_override_surface`` 逐字钉住,新增一个公开名
# 而漏掉 needle 会当场报红,而不是把守卫悄悄变成空转。
_NEEDLES = (
    "retrieval_participants",
    # ``resolve_retrieval_participant_ids`` 的公共前缀止于单数形式,所以上面那条
    # 复数 needle 覆盖不到它。
    "retrieval_participant_ids",
    "participant_override",
    "federated_ask_active",
    "override_fingerprint",
    "ParticipantOverride",
    "assert_override_matches_run",
)


@lru_cache(maxsize=1)
def _candidate_sources() -> tuple[tuple[str, str], ...]:
    """``backend/app`` 下可能触到覆盖模块的文件,路径相对 ``backend/``。

    实测(519 个文件、12.9 MB):全量 ``read_text`` 0.023s + 预筛 0.003s,而全量
    ``ast.parse`` 是 0.526s——本机暖缓存的数字,慢 runner 上还要乘几倍,而
    ``backend/tests`` 在 xdist 下每个 worker 各付一次。预筛把解析面从 519 个
    文件压到 2 个,结果与全量扫描逐字相同。同一条账 ``test_federated_
    participants_seat.py`` 已经算过一次:不为一行过滤条件付整仓的账。
    """
    sources: dict[str, str] = {}
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in _NEEDLES):
            sources[path.relative_to(_BACKEND).as_posix()] = text
    return tuple(sources.items())


def _candidate_index() -> PythonSourceIndex:
    return PythonSourceIndex.from_sources(dict(_candidate_sources()))


def _absolute_module(path: str, module: str) -> str:
    """把 ``from . import x`` 这类相对 import 解析成绝对模块名。

    仓库里确实有相对 import(``app/api/mcp_tools/*`` 的 ``from ._shared``),
    按字面量比 ``app.services.retrieval_participants`` 会整类漏掉。
    """
    level = len(module) - len(module.lstrip("."))
    if level == 0:
        return module
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    package = parts[:-1]
    if level > 1:
        package = package[: len(package) - (level - 1)]
    tail = module[level:]
    return ".".join([*package, tail]) if tail else ".".join(package)


def _references_override_module(path: str, target: str) -> bool:
    module, separator, name = target.partition(":")
    if not separator:
        # ``import app.services.retrieval_participants[.x][ as y]``
        return module == _OVERRIDE_MODULE or module.startswith(
            f"{_OVERRIDE_MODULE}."
        )
    absolute = _absolute_module(path, module)
    if absolute == _OVERRIDE_MODULE:
        return True
    # ``from app.services import retrieval_participants``
    return absolute == _OVERRIDE_PACKAGE and name == _OVERRIDE_LEAF


def _production_importers() -> set[str]:
    return {
        finding.key.path
        for finding in _candidate_index().imports()
        if finding.key.path != _OVERRIDE_PATH
        and _references_override_module(finding.key.path, finding.key.target)
    }


def _override_installers(sources: dict[str, str]) -> set[str]:
    """安装了覆盖的文件集合(``participant_override(...)`` 的调用方)。

    按 dotted 名的最后一段匹配,所以 ``retrieval_participants.participant_
    override(...)`` 这种限定调用也算得到;``current_participant_override``
    末段不同,不会被误计。别名另算一遍:import 记账记的是**原名**,所以
    ``from ... import participant_override as install`` 之后的 ``install(...)``
    只有先把局部名收集起来才看得见。
    """
    index = PythonSourceIndex.from_sources(sources)
    aliases: dict[str, set[str]] = {}
    for path, source in sources.items():
        for node in ast.walk(ast.parse(source, filename=path)):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == "participant_override" and alias.asname:
                    aliases.setdefault(path, set()).add(alias.asname)
    return {
        finding.key.path
        for finding in index.calls()
        if finding.key.path != _OVERRIDE_PATH
        and (
            finding.key.target.rsplit(".", 1)[-1] == "participant_override"
            or finding.key.target in aliases.get(finding.key.path, set())
        )
    }


def test_needles_cover_the_whole_override_surface():
    """预筛的 needle 必须覆盖公开面的每一个名字。

    这条是**守卫自己的守卫**。预筛把解析面从 519 个文件压到几个,代价是:一个
    没被 needle 覆盖的公开名,其再导出旁路连解析都进不去,下面所有断言对它恒真
    通过。新增公开名而忘了补 needle,要在这里响亮失败,而不是在别处静默。
    """
    uncovered = sorted(
        name for name in _OVERRIDE_SURFACE
        if not any(needle in name for needle in _NEEDLES)
    )
    assert not uncovered, (
        f"这些公开名没有任何 needle 覆盖,预筛会把它们的再导出旁路整类漏掉:"
        f"{uncovered}。把名字(或它的一个子串)加进 _NEEDLES。"
    )


def test_only_retrieval_modules_may_read_the_override():
    """``backend/app`` 下 import 覆盖模块的文件集合 **等于** 冻结读者白名单。

    相等而不是 ⊆:多出来的是越权面,少掉的是接线被悄悄回退——后者 ⊆ 断言看不见,
    而"``collection_catalog`` 改回直调 ``participant_ids``"恰恰是回退的样子。
    写入方仍是 ⊆(``global_ask`` 在 PR-D 才接)。

    先自检白名单本身都指向真实文件:路径打错时断言会恒真地通过,守卫就变成了
    空转。
    """
    missing = sorted(
        name for name in _IMPORT_WHITELIST if not (_BACKEND / name).is_file()
    )
    assert not missing, (
        f"白名单指向了不存在的文件 {missing};路径写错会让下面的断言恒真。"
        f" {_WHITELIST_RATIONALE}"
    )
    assert (_BACKEND / _OVERRIDE_PATH).is_file(), _OVERRIDE_PATH
    assert _candidate_sources(), "预筛扫空了,守卫会恒真通过"

    importers = _production_importers()
    unexpected = sorted(importers - _IMPORT_WHITELIST)
    assert not unexpected, (
        f"这些生产模块 import 了 {_OVERRIDE_MODULE},但不在白名单里:"
        f"{unexpected}。{_WHITELIST_RATIONALE}"
    )
    missing_readers = sorted(_EXPECTED_IMPORTERS - importers)
    assert not missing_readers, (
        f"这些模块应当读参与集座位/覆盖,却不再 import {_OVERRIDE_MODULE}:"
        f"{missing_readers}。接线被回退了吗?{_WHITELIST_RATIONALE}"
    )


def _surface_importers(sources: dict[str, str]) -> dict[str, list[str]]:
    """文件 -> 它从**任何**模块 import 到的覆盖公开名。

    白名单成员不在结果里:它们 import 覆盖模块是本来就允许的。挡的是「从某个
    白名单模块再导出的名字那里拿」这种写法——它 import 的不是覆盖模块,所以
    ``_production_importers`` 看不见它。
    """
    found: dict[str, list[str]] = {}
    for finding in PythonSourceIndex.from_sources(sources).imports():
        path = finding.key.path
        if path == _OVERRIDE_PATH or path in _IMPORT_WHITELIST:
            continue
        _module, separator, name = finding.key.target.partition(":")
        if separator and name in _OVERRIDE_SURFACE:
            found.setdefault(path, []).append(finding.key.target)
    return found


def test_re_export_detection_catches_the_bypass():
    """正对照:A1 那条断言必须真的会红,而不是一条恒绿的摆设。

    对照臂是异常类型——它刻意不在被守的名字面里:任何 fail-soft handler 都要能
    import 它来 re-raise,而命名一个异常不授予任何权限。
    """
    caught = _surface_importers({
        "app/api/source_routes.py":
            "from app.services.retrieval_candidates import "
            "current_participant_override\n",
        "app/services/knowledge_query.py":
            "from app.services.graph_retrieval import "
            "resolve_retrieval_participants as resolve\n",
        "app/services/ask_service.py":
            "from app.domain.retrieval_control import "
            "ParticipantOverrideError\n",
        # 白名单成员自己 import 覆盖模块是允许的,不得被这条误伤。
        "app/services/communities.py":
            "from app.services.retrieval_participants import "
            "resolve_retrieval_participant_ids\n",
    })
    assert sorted(caught) == [
        "app/api/source_routes.py", "app/services/knowledge_query.py",
    ], caught


def test_no_module_may_re_export_the_override_surface():
    """再导出旁路:非白名单文件不得从**任何**模块 import 覆盖的公开名。

    与上一条不重复,挡的是另一种写法:白名单模块 import 了访问器,鉴权文件再写
    ``from app.services.retrieval_candidates import current_participant_override``
    ——它 import 的不是覆盖模块,上一条的"谁 import 了覆盖模块"因此全绿,而访问器
    已经到手。``as`` 改名也算得到:记账里记的是**原名**,不是别名。

    第二半:白名单读者自己不得把这些名字再导出(写进 ``__all__``,或做一个模块级
    别名赋值 ``foo = current_participant_override``)。没有这半句,上面那种写法只要
    对面先加一行 ``__all__`` 就又成立了。
    """
    offenders = _surface_importers(dict(_candidate_sources()))
    assert not offenders, (
        f"这些非白名单模块 import 了覆盖的公开名(可能经由某个白名单模块再导出):"
        f"{ {k: sorted(v) for k, v in sorted(offenders.items())} }。"
        f"{_WHITELIST_RATIONALE}"
    )

    re_exporters: dict[str, list[str]] = {}
    for path in sorted(_READER_WHITELIST):
        source = (_BACKEND / path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
        leaked = sorted(_module_level_assignments(source, path) & _OVERRIDE_SURFACE)
        leaked += sorted(_declared_all(tree) & _OVERRIDE_SURFACE)
        if leaked:
            re_exporters[path] = leaked
    assert not re_exporters, (
        f"白名单读者把覆盖的公开名再导出了:{re_exporters}。再导出会让"
        f"「谁 import 了覆盖模块」这条断言绕得过去。{_WHITELIST_RATIONALE}"
    )


def test_writer_is_only_global_ask():
    """调用 ``participant_override(...)`` 的生产模块 ⊆ ``_WRITER_WHITELIST``。

    与上一条不重复:读者白名单允许若干模块 import 本模块,而它们**读**覆盖
    是对的、**装**覆盖不是。安装参与集是一次授权动作(``can_read_many`` 已经
    在 ``global_ask`` 侧跑过),检索层任何一个消费点自己装一份,就是自己给自己
    发的授权。

    按 dotted 名的最后一段匹配,所以 ``retrieval_participants.participant_
    override(...)`` 这种限定调用也算得到;``current_participant_override``
    末段不同,不会被误计。别名同样算得到:先把每个文件里绑到
    ``participant_override`` 的局部名收集起来(import 记账记的是原名),再拿它们
    去比调用目标——否则 ``from ... import participant_override as install`` 之后
    ``install(...)`` 就是一个守卫看不见的写入方。
    """
    assert _candidate_sources(), "预筛扫空了,守卫会恒真通过"
    unexpected = sorted(
        _override_installers(dict(_candidate_sources())) - _WRITER_WHITELIST
    )
    assert not unexpected, (
        f"这些生产模块安装了参与集覆盖,但唯一写入方是 "
        f"{sorted(_WRITER_WHITELIST)}:{unexpected}。{_WHITELIST_RATIONALE}"
    )


def _seat_calls(source: str, path: str, function_name: str) -> bool:
    """``function_name`` 被**调用**了吗(限定名、别名都算)?

    只认调用。``ParticipantOverride(...)`` 这种构造不算,它造的是一个值;安装
    座位的是 ``participant_override(...)`` 这个上下文管理器。
    """
    tree = ast.parse(source, filename=path)
    local_names = {function_name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == function_name:
                    local_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else ""
        )
        if name in local_names:
            return True
    return False


@pytest.mark.parametrize(
    "seat", ["participant_override", "source_scope_context"],
)
def test_global_ask_never_installs_seats_itself(seat):
    """全局入口只**构造**覆盖,不装任何座位。

    两个座位必须同时安装才有意义(见 ``global_run`` 的模块 docstring:只装一半
    是错的 run,不是降级的 run),而「同时」这件事只有那个管理器的全映射断言保证
    得了。全局入口自己调这两个函数里的任何一个,都是绕过那道断言的形状——
    ``participant_override(...)`` 单独装会让 ask 侧留在单库模式,
    ``source_scope_context(...)`` 单独装会让检索腿按锚点库的挂载表扇出。

    ``source_scope_context`` 也覆盖了一个更普通的回归:旧的逐库检索循环曾在
    ``global_ask`` 里自己开来源范围,那条路已经被引擎取代,不该回来。
    """
    path = "app/services/global_ask.py"
    source = (_BACKEND / path).read_text(encoding="utf-8")
    assert not _seat_calls(source, path, seat), (
        f"{path} 调用了 {seat}(...);座位只许由 app/services/global_run.py 的"
        f"管理器安装。{_WHITELIST_RATIONALE}"
    )


def test_seat_call_guard_sees_through_an_alias():
    """上一条的检测分支自己要有正对照,否则它是一条永不触发的死守卫。"""
    assert _seat_calls(
        "from app.services.retrieval_participants import "
        "participant_override as install\nwith install(o):\n    pass\n",
        "x.py", "participant_override",
    )
    assert _seat_calls(
        "import app.services.source_scope as m\nm.source_scope_context(a)\n",
        "x.py", "source_scope_context",
    )
    # 构造一个 ``ParticipantOverride`` 不是安装。
    assert not _seat_calls(
        "from app.services.retrieval_participants import ParticipantOverride\n"
        "o = ParticipantOverride(notebook_ids=('a',), tiers={}, "
        "attested_actor_id='u')\n",
        "x.py", "participant_override",
    )


def test_writer_detection_sees_through_an_import_alias():
    """别名分支自己也要有用例,否则它是一条永不触发的死守卫。

    三个合成源:限定调用、别名调用、以及一个**只读**覆盖的对照臂——最后一个
    绝不能被算成写入方,否则守卫会把七个合法读者全判成越权。
    """
    installers = _override_installers({
        "app/x/qualified.py":
            "from app.services import retrieval_participants\n"
            "retrieval_participants.participant_override(o)\n",
        "app/x/aliased.py":
            "from app.services.retrieval_participants import "
            "participant_override as install\n"
            "install(o)\n",
        "app/x/reader.py":
            "from app.services.retrieval_participants import "
            "current_participant_override\n"
            "current_participant_override()\n",
    })
    assert installers == {"app/x/qualified.py", "app/x/aliased.py"}


# 第 2 层:必须保持走真实挂载谓词的鉴权/谓词调用点。
# ``predicate_call`` 的文件里应仍然出现对真实谓词的调用或方法引用;
# ``predicate_sql`` 的两个文件是谓词**定义点**本身(纯 SQL 常量,没有调用),
# 所以改断言常量还在。
_PREDICATE_NAMES = frozenset({
    "participant_notebook_ids",
    "resolve_participants",
    "participant_tiers",
    "participant_rows",
})
_MOUNT_SQL_CONSTANTS = frozenset({
    "MOUNT_JOIN",
    "MOUNT_VALID_EXPR",
    "MOUNTED_BASE_IDS_SUBQUERY",
})
_AUTHORIZATION_SITES = {
    # 跨库来源代理 + 资产读取的鉴权口。
    "app/api/source_routes.py": "predicate_call",
    # 引用点查鉴权。
    "app/api/mcp_tools/citations.py": "predicate_call",
    # 知识对象跨库读的鉴权。
    "app/services/knowledge_query.py": "predicate_call",
    # 生命周期写操作的参与集。
    "app/services/knowledge_lifecycle.py": "predicate_call",
    # 插件的 source_keys 宇宙同时是它 fetch() 后检的权威。
    "app/services/plugin_ask_engine.py": "predicate_call",
    # facade 公开面,消费者含鉴权路径。
    "app/services/repository_facade.py": "predicate_call",
    # 谓词本身(双后端)。
    "app/repositories/sqlite/notebook_store.py": "predicate_call",
    "app/repositories/postgres/notebook_store.py": "predicate_call",
    "app/repositories/sqlite/mount_sql.py": "predicate_sql",
    "app/repositories/postgres/mount_sql.py": "predicate_sql",
}


def _declared_all(tree: ast.Module) -> set[str]:
    """模块级 ``__all__`` 里的字符串字面量。"""
    names: set[str] = set()
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in targets
        ):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            names.update(
                element.value for element in value.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            )
    return names


def _module_level_assignments(source: str, path: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source, filename=path).body:
        targets = (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def test_authorization_sites_keep_the_real_mount_predicate():
    """鉴权路径两头都要钉:不 import 覆盖模块,且仍然在问挂载表。

    只钉前半句是不够的。真正危险的改法不是"在鉴权点 import 覆盖"(太显眼),
    而是把某个鉴权点整体改道到别的取数方式——它同样不会 import 本模块,而"不
    import"的断言会照常通过。所以每个文件还要给出它仍在走真实谓词的正向证据。
    """
    sources = {
        path: (_BACKEND / path).read_text(encoding="utf-8")
        for path in _AUTHORIZATION_SITES
    }
    index = PythonSourceIndex.from_sources(sources)

    offenders = sorted(
        finding.key.path
        for finding in index.imports()
        if _references_override_module(finding.key.path, finding.key.target)
    )
    assert not offenders, (
        f"鉴权路径不得 import {_OVERRIDE_MODULE}:{offenders}。"
        f"{_WHITELIST_RATIONALE}"
    )

    # 正向证据。``calls`` 与 ``attributes`` 取并集:citations.py 是把
    # ``repo.participant_notebook_ids`` 当参数**传下去**的,记账里是 attribute
    # 不是 call,只看 call 会把它判成"不再走谓词"。
    seen = {path: set() for path in _AUTHORIZATION_SITES}
    for finding in index.calls() + index.attributes():
        last = finding.key.target.rsplit(".", 1)[-1]
        if last in _PREDICATE_NAMES:
            seen[finding.key.path].add(last)

    for path, evidence in _AUTHORIZATION_SITES.items():
        if evidence == "predicate_call":
            assert seen[path], (
                f"{path} 不再出现任何真实参与集谓词 "
                f"({sorted(_PREDICATE_NAMES)}) 的调用/方法引用。它被整体改道了"
                f"吗?鉴权口必须继续问挂载表。"
            )
        else:
            assigned = _module_level_assignments(sources[path], path)
            missing = sorted(_MOUNT_SQL_CONSTANTS - assigned)
            assert not missing, (
                f"{path} 是挂载谓词的定义点,却不再定义 {missing}。"
            )


# 第 2 层的第三半(D0-3):**同一个文件里**的两个 ``participant_notebook_ids``。
#
# ``evidence_context.py`` 是白名单读者里唯一一个自己也带鉴权调用点的文件:
# ``knowledge_context`` 的 canonical 折叠范围经覆盖解析(检索消费边界),而
# ``collection_item_citations`` 在元素水合前复核成员资格(鉴权)。两者在源码里只
# 隔几百行、名字一模一样,"顺手把另一处也改道"是这个形状最自然的错法,而上面那条
# 文件级的 ``_AUTHORIZATION_SITES`` 断言看不见它——那份清单按文件登记,而这个文件
# 已经(正当地)在读者白名单里。所以这里按**函数作用域**再钉一次。
_EVIDENCE_CONTEXT_PATH = "app/services/evidence_context.py"
# ``PythonSourceIndex`` 的作用域名从 ``<module>`` 起算(它不为 lambda 另起一层,
# 所以 ``knowledge_context`` 里那个 fallback lambda 的调用也记在本函数名下)。
_SCOPE_ROOT = "<module>."
# 鉴权点:元素水合前的成员资格复核。必须直调真实挂载谓词。
_EVIDENCE_CONTEXT_AUTHORIZATION_SCOPE = (
    f"{_SCOPE_ROOT}EvidenceContextService.collection_item_citations"
)
# 检索消费点:canonical 折叠范围。唯一允许经覆盖解析的作用域。
_EVIDENCE_CONTEXT_RETRIEVAL_SCOPE = (
    f"{_SCOPE_ROOT}EvidenceContextService.knowledge_context"
)
_RESOLVE_NAMES = frozenset({
    "resolve_retrieval_participant_ids", "resolve_retrieval_participants",
})


def _evidence_context_scopes(kind: str):
    """``evidence_context.py`` 里 ``kind`` 类记账的 ``(作用域, 目标)`` 集合。"""
    source = (_BACKEND / _EVIDENCE_CONTEXT_PATH).read_text(encoding="utf-8")
    index = PythonSourceIndex.from_sources({_EVIDENCE_CONTEXT_PATH: source})
    findings = index.calls() if kind == "call" else index.attributes()
    return {(finding.key.scope, finding.key.target) for finding in findings}


def test_evidence_context_authorization_site_keeps_mount_predicate():
    """鉴权那处仍然直调挂载谓词,覆盖解析只出现在 ``knowledge_context``。

    两半都要:

    1. ``collection_item_citations`` 里有对 ``self.notebooks
       .participant_notebook_ids`` 的**直接**调用,且该作用域内不出现任何
       ``resolve_*``——证明两处没有被一把改掉;
    2. ``resolve_retrieval_participant_ids`` 的调用作用域**恰好**是
       ``knowledge_context``(相等,不是 ⊆)——多一个作用域就是覆盖在本文件里
       扩面了,少一个就是 D0-3 的接线被回退了(那时折叠范围又回到名义 active
       的挂载表,覆盖集里另一个库的对象恒折不到)。

    **变异锚点**:把鉴权那处改成经 ``resolve_*`` -> 第 1 条红;把
    ``knowledge_context`` 改回直调 -> 第 2 条红。
    """
    assert (_BACKEND / _EVIDENCE_CONTEXT_PATH).is_file(), _EVIDENCE_CONTEXT_PATH
    calls = _evidence_context_scopes("call")

    direct = {
        scope for scope, target in calls
        if target.rsplit(".", 1)[-1] == "participant_notebook_ids"
        and target.startswith("self.notebooks")
    }
    assert _EVIDENCE_CONTEXT_AUTHORIZATION_SCOPE in direct, (
        f"{_EVIDENCE_CONTEXT_PATH}::"
        f"{_EVIDENCE_CONTEXT_AUTHORIZATION_SCOPE} 不再直调 "
        f"self.notebooks.participant_notebook_ids。它是元素水合前的成员资格"
        f"复核,必须继续问挂载表,不得改道经参与集覆盖。"
        f"{_WHITELIST_RATIONALE}"
    )

    leaked = sorted(
        target for scope, target in calls
        if scope.startswith(_EVIDENCE_CONTEXT_AUTHORIZATION_SCOPE)
        and target.rsplit(".", 1)[-1] in _RESOLVE_NAMES
    )
    assert not leaked, (
        f"鉴权作用域 {_EVIDENCE_CONTEXT_AUTHORIZATION_SCOPE} 里出现了覆盖解析"
        f"{leaked}。{_WHITELIST_RATIONALE}"
    )

    resolving = {
        scope for scope, target in calls
        if target.rsplit(".", 1)[-1] in _RESOLVE_NAMES
    }
    assert resolving == {_EVIDENCE_CONTEXT_RETRIEVAL_SCOPE}, (
        f"{_EVIDENCE_CONTEXT_PATH} 里经覆盖解析参与集的作用域应当恰好是 "
        f"{{{_EVIDENCE_CONTEXT_RETRIEVAL_SCOPE!r}}},实际是 {sorted(resolving)}。"
        f"多出来的是越权面,少掉的是 D0-3 的接线被回退。{_WHITELIST_RATIONALE}"
    )

    # 折叠范围的 fallback 仍然是真实谓词本身:``resolve_*`` 无覆盖时直通
    # ``fallback()``,fallback 换成别的取数方式会让「无覆盖逐字不变」失效。
    assert _EVIDENCE_CONTEXT_RETRIEVAL_SCOPE in direct, (
        f"{_EVIDENCE_CONTEXT_RETRIEVAL_SCOPE} 的覆盖解析 fallback 不再是 "
        f"self.notebooks.participant_notebook_ids;无覆盖时它必须与今天逐值相等。"
    )


# 第 2 层的第二半(A2):注入式谓词的**来源**。
#
# ``knowledge_query`` / ``knowledge_lifecycle`` / ``plugin_ask_engine`` 上面那条
# 断言看的是它们**调用**了 ``participant_notebook_ids``——但它们调的是组合根注入
# 进来的一个 callable。组合根把实参从 ``notebook_store.participant_notebook_ids``
# 换成 ``lambda nb: [i for i, _ in candidates._retrieval_participants(nb)]``,消费
# 方文件一字未动、组合文件也不 import 覆盖模块,于是三层守卫全绿,而鉴权谓词已经
# 变成了覆盖感知的座位。所以实参本身要被钉住:必须是那个谓词的**属性引用**,
# 不能是 lambda、不能是别的可调用。
#
# 位置实参同样要看:``lambda`` 不必写成关键字实参就能注入进去,而「只遍历
# ``node.keywords``」的守卫对位置传参是瞎的。所以本节两条一起钉——登记点的关键字
# 实参必须是属性引用,且这些文件里**任何**提到参与集的 lambda 实参一律报红。
#
# 登记到 ``(文件 -> 必须扫到的关键字名)``,而不是「全局至少一个」:后者在某个
# 文件的注入点被整段删掉时仍然绿。
_INJECTION_POINTS = {
    "app/services/repository_runtime.py": frozenset({
        # knowledge_query / knowledge_lifecycle 的注入。
        "participant_notebook_ids",
        # 插件面第一跳。
        "ask_engine_participant_notebooks",
    }),
    "app/services/ask_service.py": frozenset({
        # 插件面第二跳(PluginRetrievalAccess)。
        "participant_notebook_ids",
    }),
}
_INJECTION_SITES = frozenset(_INJECTION_POINTS)
# 注入实参名的后缀:一个新的注入点若沿用这套命名,自动进入检查面。
_INJECTION_ARG_SUFFIXES = (
    "participant_notebook_ids", "participant_notebooks",
)
# lambda 实参里出现这些名字 = 它在就地合成一份参与集谓词。
_LAMBDA_RED_FLAGS = ("_retrieval_participants", "participant")
_PREDICATE_ATTRIBUTE_TAILS = frozenset({
    "participant_notebook_ids",
    # 插件面是两跳注入:组合根把真实谓词放进 ``AskService
    # .ask_engine_participant_notebooks``,``AskService`` 再把那个属性交给
    # ``PluginRetrievalAccess``。第一跳的实参已经在本断言里被钉成真实谓词,第二
    # 跳照样必须是属性引用而不是就地写的 lambda——否则第一跳钉住的东西可以在第
    # 二跳被包一层换掉。
    "ask_engine_participant_notebooks",
})


def _injection_findings(
    path: str, source: str
) -> tuple[set[str], list[str]]:
    """``(扫到的关键字名, 违规描述)``。

    两类违规:登记的关键字实参不是真实谓词的属性引用;以及**任何**位置或关键字
    位置上的 lambda 在就地合成参与集谓词——后者是「只遍历 ``node.keywords``」的
    守卫完全看不见的那一半。
    """
    from tests.architecture.semantic_source import dotted_name

    seen: set[str] = set()
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if not isinstance(node, ast.Call):
            continue
        arguments = [(None, arg) for arg in node.args]
        arguments += [(kw.arg, kw.value) for kw in node.keywords]
        for name, value in arguments:
            if isinstance(value, ast.Lambda):
                body = ast.dump(value)
                if any(flag in body for flag in _LAMBDA_RED_FLAGS):
                    problems.append(
                        f"{path}: 实参 {name or '<位置>'} 是一个就地合成参与集谓词"
                        f"的 lambda"
                    )
                continue
            if not name or not name.endswith(_INJECTION_ARG_SUFFIXES):
                continue
            seen.add(name)
            dotted = dotted_name(value)
            if not dotted.endswith(tuple(_PREDICATE_ATTRIBUTE_TAILS)):
                problems.append(
                    f"{path}: 实参 {name} 不是 "
                    f"{sorted(_PREDICATE_ATTRIBUTE_TAILS)} 的属性引用 "
                    f"({ast.dump(value)[:100]})"
                )
    return seen, problems


def test_injected_participant_predicate_is_the_real_mount_predicate():
    """注入下去的参与集谓词必须是真实谓词的属性引用,不得是 lambda 或别的可调用。

    正向证据:**每个登记的注入点**都要被扫到(不是「全局至少一个」——那样某个
    文件的注入点被整段改名/删掉时守卫仍然绿),且它的实参解析成的 dotted 名以
    ``participant_notebook_ids`` 结尾。位置实参上的 lambda 同样报红。
    负向证据:组合根里不出现 ``_retrieval_participants``——座位是检索消费边界的
    东西,出现在组合根就意味着它正被喂给某个鉴权消费方。
    """
    problems: list[str] = []
    for path, expected in sorted(_INJECTION_POINTS.items()):
        source = (_BACKEND / path).read_text(encoding="utf-8")
        seen, found = _injection_findings(path, source)
        problems.extend(found)
        missing = sorted(expected - seen)
        if missing:
            problems.append(
                f"{path}: 登记的注入点 {missing} 一个都没扫到;改名或删掉了吗?"
                f"这条登记已经空转"
            )
        if "_retrieval_participants" in source:
            problems.append(
                f"{path}: 组合根/注入点不得出现检索座位 "
                f"``_retrieval_participants``"
            )
    assert not problems, (
        "参与集谓词的注入面有问题:\n  " + "\n  ".join(problems)
        + f"\n消费方里有鉴权路径,注入一个覆盖感知的可调用等于绕过白名单。"
        f"{_WHITELIST_RATIONALE}"
    )


def test_injection_guard_catches_a_positional_lambda():
    """正对照:位置传进去的 lambda 必须被抓到,关键字换成 lambda 同样。"""
    positional, problems = _injection_findings(
        "x.py",
        "wire(lambda nb: [i for i, _ in c._retrieval_participants(nb)])\n",
    )
    assert positional == set()
    assert len(problems) == 1 and "<位置>" in problems[0]

    _seen, keyword_problems = _injection_findings(
        "y.py",
        "wire(participant_notebook_ids=lambda nb: participants(nb))\n",
    )
    assert len(keyword_problems) == 1
    assert "participant_notebook_ids" in keyword_problems[0]

    # 对照臂:真实谓词的属性引用必须通过。
    seen, clean = _injection_findings(
        "z.py", "wire(participant_notebook_ids=store.participant_notebook_ids)\n",
    )
    assert seen == {"participant_notebook_ids"} and clean == []


# 第 4 层:fail-soft 不许吞掉身份复核的 raise。
#
# 座位对「actor 错配 / 无 ambient run」是 **raise** 而不是静默回落,但检索路径
# 遍地是刻意的 fail-soft ``except Exception``。中间任何一处把它吞掉,一次越权
# 上下文泄漏就表现成一次「本次检索未命中」的正常回答——正是
# ``retrieval_participants`` 的 docstring 点名要避免的静默,只是发生在更外一层。
#
# 下面是**登记清单**,不是「所有 handler」:每一项都由「这个 try 体能不能触达
# 参与集座位」这一条判定得来(反向可达性 + 逐处人工复核)。定位用**标记调用名**
# 而不是行号:行号会漂,而标记调用正是让这个 try 体可达座位的那一个调用。
#
# ⚠ 新增一个会触达座位的 fail-soft handler 时,必须在同一个 diff 里登记到这里。
_SEAT_FAILSOFT_SITES = (
    # (文件, 函数 qualname, 让它可达座位的标记调用)
    ("app/services/ask_service.py",
     "AskService._no_kg_scope_admits_run", "collection_map"),
    ("app/services/ask_service.py",
     "AskService._run_reasoning_stage", "execute_reasoning_retrieval_stage"),
    ("app/services/ask_service.py",
     "AskService.ask_plugin_engine", "admit_plugin_engine_result"),
    # D0-3:``synth()`` 把 KG 证据装配算在合成里(``_answer_context`` ->
    # ``evidence_context.knowledge_context`` -> canonical 折叠范围读座位),
    # 吞掉就是重试一次再返回空答案。
    ("app/services/ask_service.py", "AskService._answer_with_retry", "synth"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._chunk_seed_search", "search_chunks"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._first_round_search", "search"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._quota_rerank", "search"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._first_round_prompt_blocks", "collection_map"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._action_expand_community", "mounted_base_ids"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._run_enumeration", "resolve_source_title"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._run_enumeration", "enumerate_sources"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever.run", "follow_chain"),
    ("app/services/report_engine.py",
     "ReportEngine._load_probe_query_results._safe", "loader"),
    ("app/services/report_engine.py",
     "ReportEngine._build_corpus_map", "_probe_knowledge_hits"),
    ("app/services/report_engine.py",
     "ReportEngine._build_corpus_map", "ppr_retrieve"),
    ("app/services/report_engine.py",
     "ReportEngine._deep_dive", "federated_retrieve"),
    # D0-3:节撰写腿。``_draft_section`` -> ``knowledge_context_with_outline``
    # -> ``evidence_context.knowledge_context`` -> 折叠范围读座位;吞掉就是
    # 一份交付出去的报告里悄悄多一节「失败」。
    ("app/services/report_engine.py",
     "ReportEngine._run_sections._draft_one", "_draft_section"),
    # D0-3:知识表智能补全。这个 try 体两头都触达座位——推理检索器,以及
    # ``_completion_library_evidence`` -> ``knowledge_context``。宽 handler 会
    # 把身份复核失败改写成「逐步推理检索暂时不可用」并记到推理模型头上。
    ("app/services/knowhow/api.py", "complete_row",
     "_completion_library_evidence"),
    ("app/services/chunk_federation.py", "_run_one", "run"),
)

# D0-6 的逐条复核结论(人工可达性,不是断言;记在这里是为了下一个 diff 不必重推):
# 下面这些 handler 是 D0-1..D0-5 新增或触及的宽 ``except``,**都不登记**,理由各自写明。
# 判据只有一条:这个 try 体能不能触达参与集座位(``_retrieval_participants`` /
# ``resolve_retrieval_participant*``)——不是「它里面有没有 source_scope 读」。
#
# * ``retrieval_candidates._chunk_kg_overlay``(D0-1 的 ``_ceiling_scoped_subgraph``)
#   —— 该函数里**没有任何宽 except**,裁剪整段裸奔。
# * ``communities.sibling_peers`` 的 ``except Exception`` —— try 体是
#   ``_resolve_focal`` + ``comention_peers(**_ceiling_kwargs(...))``;``_ceiling_kwargs``
#   只读 ``source_scope.current_source_scope()`` 与 ``retrieval_run`` 的 memo,座位读发生在
#   **更外层**的 ``mounted_base_ids``(它不在任何 try 里,而它的 reasoning 调用点
#   ``_action_expand_community`` 已在上面登记)。
# * ``communities._note_source_index_fallback._probe`` —— 纯可观测性探针
#   (``source_index_backfilled`` + emit),不触达座位。
# * ``chunk_federation._federated_tasks`` / ``_peek_only._probe`` / ``_emit`` ——
#   分别是逐库可见来源枚举、copy-stats 探针、事件投递;参与集在
#   ``_bounded_participants`` 里(父线程、这些 try 之外)早已读完。
# * ``chunk_lane._lexical_gate_drift_probe`` / ``graph_retrieval._kg_peer_source_ceilings``
#   —— 前者包 ``_unsafe_source_scope_restricted``,后者包 ``all_visible_source_ids`` +
#   ``scoped_allowed_source_ids``,两者都只读 ``source_scope``。
# * D0-5 没有新增任何宽 ``except``;它加在 ``_spreadsheet_reasoning_results``(已有宽
#   handler)里的两行只读 ``source_scope`` 的 ContextVar,同样不触达座位。
# * D1-4 在 ``global_ask.py`` 新增的三个宽 ``except`` —— ``_emit``(事件投递)、
#   ``_append_trace``(``job.trace.append`` + 一次作业行写)、``_same_request``
#   (旧请求串过模型归一)——都不登记:三个 try 体都不读参与集,而且该文件根本不在
#   读者白名单上(它只**构造** ``ParticipantOverride``,见 ``_CONSTRUCTOR_WHITELIST``),
#   连访问器都 import 不到。同文件的 ``_on_library`` / ``_execute`` 里那两个
#   ``except BaseException`` 不是 fail-soft:两者都把异常记下来之后原样 ``raise``,
#   ``_RunState.raise_first_error`` 还会在引擎返回后再抛一次——这正是为了让**别人**
#   的 fail-soft 吞掉回调异常也照样失败。

_CONTROL_ERROR = "RetrievalControlError"


def _called_names(nodes) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
    return names


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    parts = (
        handler.type.elts if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    return any(
        isinstance(part, ast.Name) and part.id in {"Exception", "BaseException"}
        for part in parts
    )


def _catches_control_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    parts = (
        handler.type.elts if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    return any(
        isinstance(part, ast.Name) and part.id == _CONTROL_ERROR
        for part in parts
    )


def _bare_raise_body(body) -> bool:
    return any(
        isinstance(stmt, ast.Raise) and stmt.exc is None for stmt in body
    )


def _reraises_control(handler: ast.ExceptHandler) -> bool:
    """这个 handler 会不会把控制异常原样放出去?

    三种合格写法,都在生产里真实用到:显式 ``except RetrievalControlError:
    raise``、无条件 ``raise``(``except BaseException: …; raise``),以及
    ``if … or isinstance(exc, RetrievalControlError): raise``——最后一种是函数
    行数天花板为零松弛时唯一不新增行的写法。
    """
    if _catches_control_error(handler) and _bare_raise_body(handler.body):
        return True
    if handler.body and isinstance(handler.body[-1], ast.Raise) and (
        handler.body[-1].exc is None
    ):
        return True
    for stmt in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if not isinstance(stmt, ast.If) or not _bare_raise_body(stmt.body):
            continue
        if any(
            isinstance(name, ast.Name) and name.id == _CONTROL_ERROR
            for name in ast.walk(stmt.test)
        ):
            return True
    return False


def _try_is_protected(node: ast.Try) -> bool:
    if any(
        _catches_control_error(handler) and _bare_raise_body(handler.body)
        for handler in node.handlers
    ):
        return True
    broad = [h for h in node.handlers if _is_broad(h)]
    return bool(broad) and all(_reraises_control(h) for h in broad)


def _function_node(tree: ast.Module, qualname: str):
    target = qualname.split(".")
    def walk(nodes, path):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                here = [*path, node.name]
                if here == target:
                    return node
                if target[:len(here)] == here:
                    found = walk(node.body, here)
                    if found is not None:
                        return found
        return None
    return walk(tree.body, [])


def _unprotected_seat_handlers(sources: dict[str, str], sites) -> list[str]:
    """登记点里「宽 except 没有放行控制异常」的那些,附定位信息。"""
    problems: list[str] = []
    for path, qualname, marker in sites:
        tree = ast.parse(sources[path], filename=path)
        function = _function_node(tree, qualname)
        if function is None:
            problems.append(f"{path}::{qualname} 找不到(改名了吗?守卫会空转)")
            continue
        tries = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Try)
            and marker in _called_names(node.body)
            and any(_is_broad(h) for h in node.handlers)
        ]
        if not tries:
            problems.append(
                f"{path}::{qualname} 里找不到包住 {marker}() 的宽 except —— "
                f"调用挪走了,还是 handler 没了?这条登记已经空转"
            )
            continue
        for node in tries:
            if not _try_is_protected(node):
                problems.append(
                    f"{path}::{qualname} 包住 {marker}() 的 fail-soft handler "
                    f"会吞掉 {_CONTROL_ERROR}"
                )
    return problems


def test_registered_failsoft_handlers_reraise_the_control_error():
    """登记清单里的每个 fail-soft handler 都必须放行 ``RetrievalControlError``。"""
    sources = {
        path: (_BACKEND / path).read_text(encoding="utf-8")
        for path, _qual, _marker in _SEAT_FAILSOFT_SITES
    }
    problems = _unprotected_seat_handlers(sources, _SEAT_FAILSOFT_SITES)
    assert not problems, (
        "这些登记的 fail-soft handler 会把参与集覆盖的身份复核失败吞成一次"
        "「未命中」的正常回答:\n  " + "\n  ".join(problems)
        + "\n按 ask_service.AskService._no_kg_scope_admits_run 的写法加一条 "
        "`except RetrievalControlError: raise`(函数受行数天花板约束时,并进既有"
        "的控制异常元组、或用 `isinstance(exc, RetrievalControlError)` 条件放行)。"
    )


def test_failsoft_guard_catches_a_removed_reraise():
    """正对照:删掉 re-raise 必须报红,守卫本身不是摆设。

    三个合成源覆盖三种合格写法各自被拆掉的样子,外加一条「标记调用挪走了」的
    空转检测。
    """
    sources = {
        "a.py":
            "def f():\n"
            "    try:\n"
            "        seat()\n"
            "    except Exception:\n"
            "        return []\n",
        "b.py":
            "def f():\n"
            "    try:\n"
            "        seat()\n"
            "    except (AskCancelled, RetrievalControlError):\n"
            "        raise\n"
            "    except Exception:\n"
            "        return []\n",
        "c.py":
            "def f():\n"
            "    try:\n"
            "        seat()\n"
            "    except Exception as exc:\n"
            "        if closed or isinstance(exc, RetrievalControlError):\n"
            "            raise\n"
            "        return []\n",
        "d.py":
            "def f():\n"
            "    try:\n"
            "        other()\n"
            "    except Exception:\n"
            "        return []\n",
    }
    sites = [(name, "f", "seat") for name in ("a.py", "b.py", "c.py", "d.py")]
    problems = _unprotected_seat_handlers(sources, sites)
    assert len(problems) == 2, problems
    assert "a.py" in problems[0] and "吞掉" in problems[0]
    assert "d.py" in problems[1] and "空转" in problems[1]
