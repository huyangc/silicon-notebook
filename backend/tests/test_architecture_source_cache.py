"""A cached guard must still inspect edits and report current source errors."""
import ast
import os

import pytest

from tests import test_citation_attestation_guard as citation_guard
from tests import test_retrieved_hit_origin_guard as hit_origin_guard
from tests.architecture.source_trees import app_source_trees, read_source_tree
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


def test_two_different_guards_scan_the_very_same_tree_objects(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "services").mkdir(parents=True)
    # unique text: a tree cached by an earlier test must not hide the scan
    (app / "services" / "one.py").write_text(f"X = '{tmp_path.name}-one'\n", encoding="utf-8")
    (app / "two.py").write_text(f"Y = '{tmp_path.name}-two'\n", encoding="utf-8")
    monkeypatch.setattr(citation_guard, "APP", app)

    seen: dict[str, list[tuple[str, int]]] = {"citation": [], "hit_origin": []}

    def recorder(label, real):
        def scan_tree(relative, tree):
            seen[label].append((relative, id(tree)))
            return real(relative, tree)
        return scan_tree

    monkeypatch.setattr(citation_guard, "scan_tree",
                        recorder("citation", citation_guard.scan_tree))
    monkeypatch.setattr(hit_origin_guard, "scan_tree",
                        recorder("hit_origin", hit_origin_guard.scan_tree))
    citation_guard._scan_tree_cached.cache_clear()
    hit_origin_guard._scan_tree_cached.cache_clear()

    citation_guard.scan()
    hit_origin_guard.scan()

    assert sorted(seen["citation"]) == sorted(seen["hit_origin"])
    assert {relative for relative, _ in seen["citation"]} == {"services/one.py", "two.py"}
    # ... and they are the objects the shared entry hands to anyone else
    shared = {entry.relative: id(entry.tree) for entry in app_source_trees(app)}
    assert dict(seen["citation"]) == shared
    assert id(read_source_tree(app / "two.py")) == shared["two.py"]
    assert id(citation_guard.parse_file(app / "two.py")) == shared["two.py"]


def test_identical_text_in_two_files_is_parsed_once(tmp_path):
    (tmp_path / "a.py").write_text(f"Z = '{tmp_path.name}'\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(f"Z = '{tmp_path.name}'\n", encoding="utf-8")
    first, second = app_source_trees(tmp_path)
    assert first.tree is second.tree


def test_whole_app_scan_sees_edited_text_as_a_new_tree(tmp_path):
    path = tmp_path / "services" / "consumer.py"
    path.parent.mkdir()
    allowed = "from app.core.cache import NoCacheBackend"
    forbidden = "from app.core.cache import SqliteCacheBackend"
    width = max(len(allowed), len(forbidden))
    path.write_text(allowed.ljust(width) + "\n", encoding="utf-8")
    original = path.stat()
    (before,) = app_source_trees(tmp_path)
    assert before.relative == "services/consumer.py"

    path.write_text(forbidden.ljust(width) + "\n", encoding="utf-8")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    (after,) = app_source_trees(tmp_path)
    assert after.tree is not before.tree
    assert after.source != before.source
    assert ast.dump(after.tree) == ast.dump(ast.parse(forbidden))
    assert ast.dump(before.tree) == ast.dump(ast.parse(allowed))
