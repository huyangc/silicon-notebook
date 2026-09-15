"""AST guard: every entry point that can create an ask_jobs/reports row must
say which submission surface created it.

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
    backend/app/api/**/*.py whose attribute name is one of ``_TARGET_ATTRS``
    AND whose receiver is the repository handle these routes/tools all bind
    to a local named ``repo`` (``repo = repository()`` / ``repo =
    notebook_catalog_repository()`` etc., grep-verified against every call
    site at authoring time -- see the module docstring above). Narrowing to
    this receiver spelling is deliberate scope-narrowing per the task spec:
    the api/ tree has other objects with an ``ask`` attribute name that are
    NOT ask_jobs entry points (e.g. plain dict-shaped payloads), and without
    this narrowing the guard would need per-callsite exemptions instead of a
    single structural rule. If a future entry point binds its repository
    handle to a different local name, this guard's self-check below
    (``compliant_count`` / both values present) will start failing FIRST
    because the real entry points would silently drop out of the scan --
    that failure is the signal to widen the receiver match here.
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
            if not isinstance(func, ast.Attribute) or func.attr not in _TARGET_ATTRS:
                continue
            if _unparse(func.value) != "repo":
                continue
            yield rel, scopes.get(node, "<module>"), func.attr, node


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
    # `.ask(...)`/`.start_ask_stream(...)`、app/api/report_routes.py 的
    # `.create_report(...)`、app/api/mcp_tools/memory_context.py 的 `.ask(...)`。
    assert compliant_count >= 4, (
        f"只扫到 {compliant_count} 个合规调用点，守卫可能没有真的扫描到入口"
        "（检查 API_ROOT / 接收者收窄规则是否与当前代码脱节）"
    )
    assert compliant_values == _VALID_VALUES, (
        f"扫到的取值集合是 {sorted(compliant_values)}，"
        f"预期同时出现 web 与 mcp——如果只剩一种，守卫可能漏扫了另一个入口"
    )
