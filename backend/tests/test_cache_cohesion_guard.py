"""内聚约束：具体缓存实现类不得泄漏到模块之外。

这条约束是"将来能低成本替换缓存组件"的保障——消费者只依赖 Protocol 与工厂，
换实现时只改 app/core/cache/ 内部。

覆盖边界（如实标注，别假装全覆盖）：
- 扫描根对齐 tests/architecture/repository_callers.py 的 PRODUCTION_ROOTS——
  backend/app（应用代码）+ 仓库根 scripts/（有文档化用法的运维/离线 CLI）。
  不包含 backend/tests/ 下的其余测试文件：白盒测试允许直接碰实现细节。
- 检测手段是 AST 静态扫描字面的 `import`/`from ... import` 语句，是
  best-effort 检查，不是安全边界。`importlib.import_module(
  "app.core.cache.sqlite_backend")`、拼接模块名字符串等动态 import 手法能
  绕过它而不留任何可静态匹配的节点。
"""
import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_CACHE_PKG = _BACKEND / "app" / "core" / "cache"

# 对齐 tests/architecture/repository_callers.py::PRODUCTION_ROOTS 的两棵生产
# 代码树：backend/app 之外，仓库根 scripts/ 同样是真实消费者（各脚本头部都
# 写明 `PYTHONPATH=backend python scripts/xxx.py` 的运行方式）。
_SCAN_ROOTS = (_BACKEND / "app", _REPO_ROOT / "scripts")

# 允许 import 具体实现的位置：模块自身（工厂函数在这里唯一构造具体后端）。
_EXEMPT = {
    _CACHE_PKG,
}

_FORBIDDEN_MODULE = "app.core.cache.sqlite_backend"
_FORBIDDEN_NAME = "SqliteCacheBackend"


def _is_exempt(path: Path) -> bool:
    return any(path == e or e in path.parents for e in _EXEMPT)


def _python_files():
    for root in _SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or _is_exempt(path):
                continue
            yield path


def _concrete_backend_imports(path: Path) -> list[str]:
    """只看真正的 import 语句。

    **不能用文本包含**：`llm.py` 的注释里合法地提到了 `SqliteCacheBackend`
    （解释为何用 `cache is not None` 而非真值判断），`repository_callers.py`
    的 reason 映射里有该文件的路径字符串——两者都不是导入，纯文本扫描会把它们
    误报成违规，逼着后人删掉有价值的注释来讨好守卫。

    **覆盖边界（best-effort，非安全边界）**：本函数只识别字面的
    `import app.core.cache.sqlite_backend`、
    `from app.core.cache.sqlite_backend import ...`、
    `from app.core.cache import sqlite_backend`（子模块导入——逃过前两种
    匹配的常见写法）、以及 `from ... import SqliteCacheBackend`。它挡不住
    `importlib.import_module(...)`、拼接模块名字符串等动态导入——AST 静态
    扫描看不到运行期才决定的字符串。这是静态检查方法本身的天花板，不是本
    守卫要修的缺陷，如实记录避免后人误以为它是硬边界。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                qualified = f"{module}.{alias.name}"
                if (
                    module == _FORBIDDEN_MODULE
                    or alias.name == _FORBIDDEN_NAME
                    or qualified == _FORBIDDEN_MODULE
                ):
                    hits.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _FORBIDDEN_MODULE:
                    hits.append(f"import {alias.name}")
    return hits


def test_concrete_backend_is_not_imported_outside_the_cache_module():
    offenders = []
    for path in _python_files():
        for hit in _concrete_backend_imports(path):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {hit}")
    assert not offenders, (
        "具体缓存实现泄漏到模块之外，替换组件将不再是局部改动：\n  "
        + "\n  ".join(offenders)
    )


def test_guard_actually_scans_files():
    """守卫自身的健全性：确保扫描范围真的覆盖到关键生产文件，否则上面那条会假绿。

    用具名文件断言而非文件数阈值：数字阈值会随仓库自然增长而失真，也测不出
    "整段目录被误塞进豁免名单" 或 "扫描根写错" 这类塌陷——具名文件一旦漏扫会
    立刻从集合里消失，比数字阈值更敏感，也不会随仓库增长而失真。
    """
    scanned = {path.relative_to(_REPO_ROOT).as_posix() for path in _python_files()}
    must_include = {
        "backend/app/services/embedding.py",
        "scripts/batch_ingest.py",
    }
    missing = must_include - scanned
    assert not missing, f"扫描集合遗漏关键生产文件（扫描根写错或豁免过宽）：{missing}"


def test_public_surface_exports_factory_and_protocols():
    import app.core.cache as cache_pkg

    for name in ("make_cache_backend", "CacheBackend", "CacheAdmin", "NoCacheBackend"):
        assert name in cache_pkg.__all__, f"{name} 应在公开面中"
