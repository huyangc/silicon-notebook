"""内聚约束：具体缓存实现类不得泄漏到模块之外。

这条约束是"将来能低成本替换缓存组件"的保障——消费者只依赖 Protocol 与工厂，
换实现时只改 app/core/cache/ 内部。
"""
import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_CACHE_PKG = _BACKEND / "app" / "core" / "cache"

# 允许 import 具体实现的位置：模块自身，以及模块的白盒测试。
_EXEMPT = {
    _CACHE_PKG,
    _BACKEND / "tests" / "test_cache_backend_contract.py",
    _BACKEND / "tests" / "test_cache_sqlite_backend.py",
    _BACKEND / "tests" / "test_cache_cohesion_guard.py",
}

_FORBIDDEN_MODULE = "app.core.cache.sqlite_backend"
_FORBIDDEN_NAME = "SqliteCacheBackend"


def _is_exempt(path: Path) -> bool:
    return any(path == e or e in path.parents for e in _EXEMPT)


def _python_files():
    for path in _BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts or _is_exempt(path):
            continue
        yield path


def _concrete_backend_imports(path: Path) -> list[str]:
    """只看真正的 import 语句。

    **不能用文本包含**：`llm.py` 的注释里合法地提到了 `SqliteCacheBackend`
    （解释为何用 `cache is not None` 而非真值判断），`repository_callers.py`
    的 reason 映射里有该文件的路径字符串——两者都不是导入，纯文本扫描会把它们
    误报成违规，逼着后人删掉有价值的注释来讨好守卫。
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
                if module == _FORBIDDEN_MODULE or alias.name == _FORBIDDEN_NAME:
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
    """守卫自身的健全性：确保扫描范围非空，否则上面那条会假绿。"""
    assert sum(1 for _ in _python_files()) > 50


def test_public_surface_exports_factory_and_protocols():
    import app.core.cache as cache_pkg

    for name in ("make_cache_backend", "CacheBackend", "CacheAdmin", "NoCacheBackend"):
        assert name in cache_pkg.__all__, f"{name} 应在公开面中"
