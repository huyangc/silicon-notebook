"""AST guard: every entry point that can create an ask_jobs/reports/
global_ask_jobs row must say which submission surface created it.

``submitted_via`` (app.models.ask.SubmittedVia/StoredSubmittedVia) is a
keyword-only, non-request-model argument threaded explicitly through the
store/service/facade layers by design (see app/models/ask.py's docstring on
``SubmittedVia``) precisely so a browser cannot claim "mcp" by setting a
request field. That design only holds if every real HTTP/MCP entry point
actually passes the keyword with a literal "web"/"mcp" value -- a future
route or MCP tool that forgets it compiles fine and silently records ""
("not recorded") forever, indistinguishable from a legacy pre-migration row.
This test scans the production entry-point surface for exactly that mistake
so it fails loudly at review time instead of silently at runtime.

The global Ask job record (SQLite v81 / PostgreSQL 0061) carries the same
per-row ``submitted_via`` column, written the same way: a literal keyword
wherever ``global_ask_service().start(...)`` is invoked -- directly in the
MCP tool (app/api/mcp_tools/global_ask.py), and indirectly through the
``_call(...)`` forwarding helper in the HTTP route
(app/api/global_ask_routes.py) -- scanned here alongside the three
``repo.<attr>(...)`` call shapes above.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests.architecture.semantic_source import qualified_scopes

ROOT = Path(__file__).resolve().parents[1]  # backend/
API_ROOT = ROOT / "app" / "api"

# The three attribute names that create an ask_jobs/reports row. `ask` and
# `create_report` always create; `start_ask_stream` only creates when NOT
# called with `attach_only=True` (a keyed re-submission's read-only attach
# probe, which never inserts a row -- see AskExecutionCoordinator.
# attach_existing / repository_facade.start_ask_stream).
_TARGET_ATTRS = frozenset({"ask", "create_report", "start_ask_stream"})
_VALID_VALUES = frozenset({"web", "mcp"})

# The global Ask twin: `global_ask_service().start(...)` creates a
# global_ask_jobs row the same way `.ask(...)` creates an ask_jobs one (see
# GlobalAskStore.create / app.services.global_ask.GlobalAskService.start), so
# it must carry the same literal `submitted_via` keyword. Its receiver is not
# the `repo` local the three attrs above are narrowed to -- it is the
# call expression `global_ask_service()` itself (app.api.deps).
_GLOBAL_ASK_TARGET_ATTR = "start"
_GLOBAL_ASK_RECEIVER = "global_ask_service()"
_GLOBAL_ASK_METHOD_REF = f"{_GLOBAL_ASK_RECEIVER}.{_GLOBAL_ASK_TARGET_ATTR}"

# app/api/global_ask_routes.py never calls `.start(...)` directly: every route
# in that file goes through a local `_call(method, *args, **kwargs): return
# method(*args, **kwargs)` indirection (grep-verified -- the ONLY definition of
# a function literally named `_call` under app/api/), so the AST never has a
# `Call` node whose `.func` is the `.start` attribute itself. The keyword
# lives one level out, on the `_call(global_ask_service().start, payload,
# user_id=user.id, submitted_via="web")` call, with the bound method passed
# BY REFERENCE as the first positional argument. Matching only direct
# `global_ask_service().start(...)` calls (as the MCP tool at
# app/api/mcp_tools/global_ask.py does) would miss this indirection entirely
# -- verified by temporarily deleting the literal from global_ask_routes.py
# and confirming the guard still passed before this second pattern was added.
_INDIRECT_CALL_FUNC_NAME = "_call"


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive, ast.unparse is total for valid ASTs
        return ""


def _kwarg(call: ast.Call, name: str) -> "ast.AST | None":
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_true_constant(node: "ast.AST | None") -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _iter_candidate_calls():
    """Yield (relative_path, scope_label, attr, call) for every call in
    backend/app/api/**/*.py that creates an ask_jobs/reports/global_ask_jobs
    row, in one of three shapes:

    1. ``repo.<attr>(...)`` where ``<attr>`` is one of ``_TARGET_ATTRS`` and
       the receiver is the repository handle these routes/tools all bind to a
       local named ``repo`` (``repo = repository()`` / ``repo =
       notebook_catalog_repository()`` etc., grep-verified against every call
       site at authoring time -- see the module docstring above).
    2. ``global_ask_service().start(...)`` -- a DIRECT call, receiver
       ``_GLOBAL_ASK_RECEIVER``, attribute ``_GLOBAL_ASK_TARGET_ATTR`` -- the
       shape the MCP tool (app/api/mcp_tools/global_ask.py) uses.
    3. ``_call(global_ask_service().start, ..., submitted_via=...)`` -- the
       HTTP route's shape (app/api/global_ask_routes.py): the method is
       passed BY REFERENCE as the first positional argument to the file's own
       ``_call(method, *args, **kwargs)`` indirection helper, so there is no
       ``Call`` node whose ``.func`` is the ``.start`` attribute itself; the
       ``submitted_via`` keyword lives on the outer ``_call(...)`` node
       instead. Yielding THAT node (not the referenced-but-uncalled
       ``.start`` attribute) is what lets ``_kwarg`` find it.

    Narrowing to these three exact shapes is deliberate scope-narrowing per
    the task spec: the api/ tree has other objects with an ``ask``/``start``
    attribute name that are NOT ask_jobs/global_ask_jobs entry points (e.g.
    plain dict-shaped payloads), and without this narrowing the guard would
    need per-callsite exemptions instead of a small set of structural rules.
    If a future entry point binds its repository handle to a different local
    name, calls the global service through a different receiver expression,
    or renames the indirection helper, this guard's self-check below
    (``compliant_count`` / both values present) will start failing FIRST
    because the real entry points would silently drop out of the scan --
    that failure is the signal to widen the match here.
    """
    for path in sorted(API_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        scopes = qualified_scopes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                receiver = _unparse(func.value)
                if func.attr in _TARGET_ATTRS and receiver == "repo":
                    yield rel, scopes.get(node, "<module>"), func.attr, node
                elif (
                    func.attr == _GLOBAL_ASK_TARGET_ATTR
                    and receiver == _GLOBAL_ASK_RECEIVER
                ):
                    yield rel, scopes.get(node, "<module>"), func.attr, node
            elif (
                isinstance(func, ast.Name)
                and func.id == _INDIRECT_CALL_FUNC_NAME
                and node.args
                and _unparse(node.args[0]) == _GLOBAL_ASK_METHOD_REF
            ):
                yield rel, scopes.get(node, "<module>"), _GLOBAL_ASK_TARGET_ATTR, node


def test_every_ask_report_entry_point_records_submitted_via():
    violations: list[str] = []
    compliant_values: set[str] = set()
    compliant_count = 0

    for rel, scope, attr, call in _iter_candidate_calls():
        if attr == "start_ask_stream" and _is_true_constant(_kwarg(call, "attach_only")):
            # Read-only attach probe for a keyed re-submission -- never
            # inserts an ask_jobs row, so it carries no submitted_via.
            continue
        value_node = _kwarg(call, "submitted_via")
        if (
            value_node is None
            or not isinstance(value_node, ast.Constant)
            or value_node.value not in _VALID_VALUES
        ):
            violations.append(f"{rel} 的 {scope}（.{attr}(...) 调用）")
            continue
        compliant_count += 1
        compliant_values.add(value_node.value)

    assert not violations, (
        "以下调用点建 ask_jobs/reports 行时缺少字面量 submitted_via=\"web\"/\"mcp\" "
        "关键字参数（start_ask_stream(..., attach_only=True) 的续接探测除外）——"
        "每个真实 HTTP/MCP 入口都必须显式声明自己的提交面，不能让新入口静默落成"
        "「未记录」：" + "；".join(violations)
    )

    # 自检：防止扫描范围本身写错导致上面的断言空转（比如 API_ROOT 拼错、接收者
    # 名称收窄过头扫不到任何调用点）。真实合规调用点见 app/api/ask_routes.py 的
    # `.ask(...)`/`.start_ask_stream(...)`（两处）、app/api/report_routes.py 的
    # `.create_report(...)`、app/api/mcp_tools/memory_context.py 的 `.ask(...)`、
    # app/api/mcp_tools/global_ask.py 的直接调用 `global_ask_service().start(...)`、
    # app/api/global_ask_routes.py 经 `_call(...)` 间接转发的同一个 `.start(...)`。
    assert compliant_count >= 6, (
        f"只扫到 {compliant_count} 个合规调用点，守卫可能没有真的扫描到入口"
        "（检查 API_ROOT / 接收者收窄规则是否与当前代码脱节）"
    )
    assert compliant_values == _VALID_VALUES, (
        f"扫到的取值集合是 {sorted(compliant_values)}，"
        f"预期同时出现 web 与 mcp——如果只剩一种，守卫可能漏扫了另一个入口"
    )
