"""A cached guard must still inspect edits and report current source errors."""
import ast
import os

import pytest

from tests.architecture.source_trees import read_source_tree
from tests.test_cache_cohesion_guard import _concrete_backend_imports
from tests.test_global_run import _call_sites, _subjectless_keyword_sites


def test_cached_guard_sees_same_size_edits_with_restored_timestamp(tmp_path):
    path = tmp_path / "consumer.py"
    allowed = "from app.core.cache import NoCacheBackend"
    forbidden = "from app.core.cache import SqliteCacheBackend"
    width = max(len(allowed), len(forbidden))
    path.write_text(allowed.ljust(width) + "\n", encoding="utf-8")
    original = path.stat()
    assert _concrete_backend_imports(path) == []

    path.write_text(forbidden.ljust(width) + "\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _concrete_backend_imports(path) == [forbidden]

    path.write_text(allowed.ljust(width) + "\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert _concrete_backend_imports(path) == []


def test_cached_source_does_not_hide_current_parse_or_read_errors(tmp_path):
    path = tmp_path / "consumer.py"
    path.write_text("pass\n", encoding="utf-8")
    read_source_tree(path)
    path.write_text("def incomplete(\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        read_source_tree(path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        read_source_tree(path)


def test_global_guard_keeps_synthetic_sources_independent_of_cached_files(tmp_path):
    path = tmp_path / "consumer.py"
    path.write_text("pass\n", encoding="utf-8")
    sources: dict[str, str | ast.AST] = {str(path): read_source_tree(path)}
    assert _call_sites(sources, "global_ask_run") == set()
    assert _subjectless_keyword_sites(sources) == set()

    sources[str(path)] = (
        "from app.services.global_run import global_ask_run as install\n"
        "install(subjectless=flag)\n"
    )
    assert _call_sites(sources, "global_ask_run") == {str(path)}
    assert _subjectless_keyword_sites(sources) == {str(path)}
