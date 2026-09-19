"""C1 -- 参与集覆盖的结构性隔离守卫(零松弛)。

``app/services/retrieval_participants.py`` 能**替换**一次检索的参与集,而
``resolve_participants``/``mount_sql.py`` 那个谓词是检索与**鉴权**共用的
(``source_scope.scoped_participants`` 的注释逐字写明:跨库来源代理、引用解析、
资产读取都吃它)。覆盖一旦漏进鉴权路径就是越权。

本文件钉三件事,任何一条单独成立都足以挡住越权:

1. 谁能 import 这个模块(读者 5 个 + 写入方 1 个的冻结白名单);
2. 谁能安装覆盖(只有 ``global_ask``);
3. 鉴权路径既不 import 它,也仍然在调真实的挂载谓词——第二半才是关键:
   只断言"没 import"挡不住把某个鉴权点整体改道成别的取数方式。

C1 阶段本模块还没有任何 importer,所以 1/2 写成 ⊆ 白名单而不是 ==。C3/PR-D
接线之后这两条断言仍然成立,不需要改写;白名单本身要不要动是一次独立的、
必须被评审看见的编辑。
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

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
_NEEDLES = ("retrieval_participants", "participant_override", "federated_ask_active")


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


def test_only_retrieval_modules_may_read_the_override():
    """``backend/app`` 下 import 覆盖模块的文件集合 ⊆ 冻结白名单。

    先自检白名单本身都指向真实文件:路径打错时"⊆ 白名单"会恒真地通过,守卫
    就变成了空转。
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


def test_writer_is_only_global_ask():
    """调用 ``participant_override(...)`` 的生产模块 ⊆ {global_ask}。

    与上一条不重复:读者白名单允许 5 个模块 import 本模块,而它们**读**覆盖
    是对的、**装**覆盖不是。安装参与集是一次授权动作(``can_read_many`` 已经
    在 ``global_ask`` 侧跑过),检索层任何一个消费点自己装一份,就是自己给自己
    发的授权。

    按 dotted 名的最后一段匹配,所以 ``retrieval_participants.participant_
    override(...)`` 这种限定调用也算得到;``current_participant_override``
    末段不同,不会被误计。
    """
    writers = {
        finding.key.path
        for finding in _candidate_index().calls()
        if finding.key.path != _OVERRIDE_PATH
        and finding.key.target.rsplit(".", 1)[-1] == "participant_override"
    }
    unexpected = sorted(writers - _WRITER_WHITELIST)
    assert not unexpected, (
        f"这些生产模块安装了参与集覆盖,但唯一写入方是 "
        f"{sorted(_WRITER_WHITELIST)}:{unexpected}。{_WHITELIST_RATIONALE}"
    )


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
