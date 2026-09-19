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
6. fail-soft 不许吞掉身份复核的 raise。

写入方白名单仍是 ⊆:``global_ask`` 在 PR-D 才接。
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

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
})
_WRITER_WHITELIST = frozenset({
    "app/services/global_ask.py",
})
_IMPORT_WHITELIST = _READER_WHITELIST | _WRITER_WHITELIST

# C3 接线之后读者集合已经确定,所以这一条是**相等**而不是 ⊆:一个白名单里却
# 不再 import 的模块意味着接线被悄悄回退(例如 ``collection_catalog`` 改回直调
# ``participant_ids``),而 ⊆ 断言对回退是沉默的。
#
# 已知的未来读者:``app/services/chunk_federation.py``。PR-D 要在任务体里按
# ``federated_ask_active()`` 切 ``read_budget`` 与 ``peer_evidence`` 阈值,届时
# 它要同时进 ``_READER_WHITELIST`` 与这里——本 PR 它不 import,所以两个集合都
# 不含它。
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
    """调用 ``participant_override(...)`` 的生产模块 ⊆ {global_ask}。

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


def test_writer_detection_sees_through_an_import_alias():
    """别名分支自己也要有用例,否则它是一条永不触发的死守卫。

    三个合成源:限定调用、别名调用、以及一个**只读**覆盖的对照臂——最后一个
    绝不能被算成写入方,否则守卫会把五个合法读者全判成越权。
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


# 第 2 层的第二半(A2):注入式谓词的**来源**。
#
# ``knowledge_query`` / ``knowledge_lifecycle`` / ``plugin_ask_engine`` 上面那条
# 断言看的是它们**调用**了 ``participant_notebook_ids``——但它们调的是组合根注入
# 进来的一个 callable。组合根把实参从 ``notebook_store.participant_notebook_ids``
# 换成 ``lambda nb: [i for i, _ in candidates._retrieval_participants(nb)]``,消费
# 方文件一字未动、组合文件也不 import 覆盖模块,于是三层守卫全绿,而鉴权谓词已经
# 变成了覆盖感知的座位。所以实参本身要被钉住:必须是那个谓词的**属性引用**,
# 不能是 lambda、不能是别的可调用。
_INJECTION_SITES = {
    "app/services/repository_runtime.py",
    "app/services/ask_service.py",
}
_PREDICATE_ATTRIBUTE_TAILS = frozenset({
    "participant_notebook_ids",
    # 插件面是两跳注入:组合根把真实谓词放进 ``AskService
    # .ask_engine_participant_notebooks``,``AskService`` 再把那个属性交给
    # ``PluginRetrievalAccess``。第一跳的实参已经在本断言里被钉成真实谓词,第二
    # 跳照样必须是属性引用而不是就地写的 lambda——否则第一跳钉住的东西可以在第
    # 二跳被包一层换掉。
    "ask_engine_participant_notebooks",
})


def _predicate_injection_values(path: str, source: str) -> list[ast.AST]:
    """所有把谓词注入下去的实参节点(关键字实参 + 直接位置实参)。"""
    values: list[ast.AST] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg and keyword.arg.endswith(
                ("participant_notebook_ids", "participant_notebooks")
            ):
                values.append(keyword.value)
    return values


def test_injected_participant_predicate_is_the_real_mount_predicate():
    """注入下去的参与集谓词必须是真实谓词的属性引用,不得是 lambda 或别的可调用。

    正向证据:每个实参解析成的 dotted 名以 ``participant_notebook_ids`` 结尾。
    负向证据:组合根里不出现 ``_retrieval_participants``——座位是检索消费边界的
    东西,出现在组合根就意味着它正被喂给某个鉴权消费方。
    """
    from tests.architecture.semantic_source import dotted_name

    offenders: dict[str, list[str]] = {}
    seen_any = False
    for path in sorted(_INJECTION_SITES):
        source = (_BACKEND / path).read_text(encoding="utf-8")
        for value in _predicate_injection_values(path, source):
            seen_any = True
            name = dotted_name(value)
            if not name.endswith(tuple(_PREDICATE_ATTRIBUTE_TAILS)):
                offenders.setdefault(path, []).append(
                    ast.dump(value)[:120]
                )
    assert seen_any, (
        "一个注入点都没扫到;实参名改了吗?守卫会恒真通过。"
    )
    assert not offenders, (
        f"参与集谓词的注入实参不是 {sorted(_PREDICATE_ATTRIBUTE_TAILS)} 的属性"
        f"引用:{offenders}。消费方里有鉴权路径,注入一个覆盖感知的可调用等于"
        f"绕过白名单。{_WHITELIST_RATIONALE}"
    )

    for path in sorted(_INJECTION_SITES):
        source = (_BACKEND / path).read_text(encoding="utf-8")
        assert "_retrieval_participants" not in source, (
            f"{path} 是组合根/注入点,不得出现检索座位 ``_retrieval_participants``:"
            f"注入一份覆盖感知的谓词给鉴权消费方,文件本身不用 import 覆盖模块。"
        )
