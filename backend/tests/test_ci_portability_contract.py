"""CI-executed tests must be independent of developer-local state."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


ROOT = Path(__file__).resolve().parents[2]
HARNESS_TESTS = ROOT / "fangan" / "testcases" / "harness" / "tests"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _absolute_path_literals(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            matches.append(value)
    return tuple(matches)


def _declared_requirement_names() -> frozenset[str]:
    names: set[str] = set()
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        match = _REQUIREMENT_NAME.match(line)
        assert match is not None, f"unsupported requirement line: {raw_line!r}"
        names.add(re.sub(r"[-_.]+", "-", match.group(0)).lower())
    return frozenset(names)


def test_ci_harness_tests_do_not_embed_absolute_paths() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): _absolute_path_literals(path)
        for path in sorted(HARNESS_TESTS.glob("test_*.py"))
        if _absolute_path_literals(path)
    }
    assert offenders == {}


def test_pytest_controller_matplotlib_import_is_declared() -> None:
    assert "matplotlib" in _declared_requirement_names()


def test_mcp_requirement_keeps_an_upper_bound() -> None:
    """`mcp` 必须保留 `<2` 上界。

    2026-07-28 的真实中断:约束曾是无上界的 `mcp>=1.26.0`,mcp 2.0.0 发布后 CI
    立刻装到它,而 2.x 移走了 `mcp.server.fastmcp` —— master 与所有 PR 的 CI 同时
    以 `ModuleNotFoundError` 失败,本地却因装着 1.x 而全绿。

    这条守卫不是给所有依赖泛化上界(仓库里 25 条 `>=` 都无上界,那是另一个策略
    问题),只钉住这一个已被证明会断的:`backend/app/api/mcp_server.py` 直接 import
    的 `mcp.server.fastmcp` / `mcp.server.auth.*` / `mcp.server.transport_security`
    都是 1.x 的 server API。要升到 2.x,得先迁移那些 import,再来动这条。
    """
    line = next(
        raw.partition("#")[0].strip()
        for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if raw.partition("#")[0].strip().lower().startswith("mcp")
    )
    assert "<2" in line.replace(" ", ""), (
        f"mcp 约束丢了上界:{line!r} —— mcp 2.x 没有 mcp.server.fastmcp"
    )
