"""Symbol navigation is read-only and reports only its supported static evidence."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "code_context.py"
SPEC = importlib.util.spec_from_file_location("code_context", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
navigation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(navigation)


def test_python_outline_and_complete_decorated_nested_symbol():
    text = (
        "from functools import lru_cache, cache\n"
        "import unused\n"
        "class Reader:\n"
        "    @lru_cache()\n"
        "    def read(self):\n"
        "        def detail():\n"
        "            return 'complete output'\n"
        "        return detail()\n"
    )
    index = navigation.python_index(text, Path("reader.py"))
    assert [s["name"] for s in index["symbols"]] == ["Reader", "Reader.read", "Reader.read.detail"]
    selected = navigation.select_symbol(index, "Reader.read")
    result = navigation.python_symbol(index, selected)
    assert result["source"].startswith("    @lru_cache()")
    assert result["source"].endswith("        return detail()")
    assert result["imports"] == ["from functools import lru_cache"]
    assert navigation.select_symbol(index, "detail")["name"] == "Reader.read.detail"


def test_ambiguous_missing_and_syntax_errors_are_explicit():
    index = navigation.python_index("class A:\n def read(self): pass\nclass B:\n def read(self): pass\n", Path("x.py"))
    with pytest.raises(navigation.NavigationError, match="ambiguous: A.read, B.read"):
        navigation.select_symbol(index, "read")
    with pytest.raises(navigation.NavigationError, match="not found"):
        navigation.select_symbol(index, "unknown")
    with pytest.raises(navigation.NavigationError, match="Python syntax error"):
        navigation.python_index("def broken(:\n", Path("broken.py"))


def test_reexport_lookup_points_to_the_actual_declaration_owner(tmp_path):
    owner = tmp_path / "outline.py"
    owner.write_text("class OutlineSection: pass\n")
    index = navigation.python_index(
        "from .outline import OutlineSection as OutlineSection\n", tmp_path / "legacy.py",
    )
    with pytest.raises(navigation.NavigationError) as raised:
        navigation.select_symbol(index, "OutlineSection")
    assert "imported/re-exported from .outline" in str(raised.value)
    assert f"{owner}::OutlineSection" in str(raised.value)


def test_symbol_context_tracks_same_scope_dependencies_without_unrelated_imports():
    text = (
        "raise RuntimeError('navigation must not execute source')\n"
        "def unrelated():\n    import foreign as helper\n"
        "class Reader:\n"
        "    def read(self):\n"
        "        def contained(): return 'present in selected source'\n"
        "        return self.load() + helper() + contained()\n"
        "    def load(self): return 1\n"
        "def helper(): return 2\n"
    )
    index = navigation.python_index(text, Path("reader.py"))
    result = navigation.python_symbol(index, navigation.select_symbol(index, "Reader.read"))
    assert result["dependencies"] == ["Reader.load", "helper"]
    assert result["imports"] == []
    assert "def contained()" in result["source"]


def test_python_alias_imports_and_shadowed_receivers(tmp_path):
    target = tmp_path / "owner.py"
    source = tmp_path / "caller.py"
    index = navigation.python_index(
        "from owner import work as run\n"
        "import owner as module\n"
        "def confirmed():\n"
        "    return run() + module.work()\n"
        "def uncertain(run, module):\n"
        "    return run() + module.work()\n"
        "def dynamic(obj):\n"
        "    return obj.work()\n", source,
    )
    refs = navigation.python_references(index, target, "work")
    assert {r["binding"] for r in refs if r["kind"] == "import"} == {"run", "module"}
    assert {r["scope"] for r in refs if r["kind"] == "reference"} == {"confirmed"}


def test_python_relative_module_import_and_qualified_class_member(tmp_path):
    target = tmp_path / "pkg" / "owner.py"
    source = tmp_path / "pkg" / "caller.py"
    index = navigation.python_index(
        "from . import owner as module\n"
        "from .owner import Reader as R\n"
        "def use(): return module.Reader.read + R.read\n", source,
    )
    refs = navigation.python_references(index, target, "Reader.read")
    assert {r["scope"] for r in refs if r["kind"] == "reference"} == {"use"}
    assert {r.get("binding") for r in refs if r["kind"] == "import"} == {"module", "R"}


def test_search_roots_do_not_enter_dependencies_or_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    kept = root / "owned.py"
    kept.write_text("def owned(): pass\n")
    dependencies = root / "node_modules"
    dependencies.mkdir()
    (dependencies / "foreign.ts").write_text("export const hidden = 1;\n")
    external = tmp_path / "external"
    external.mkdir()
    (external / "hidden.py").write_text("def hidden(): pass\n")
    (root / "linked").symlink_to(external, target_is_directory=True)
    assert navigation.source_files([root]) == [kept]
    with pytest.raises(navigation.NavigationError, match="Excluded search root"):
        navigation.source_files([dependencies])


@pytest.mark.process_contract
def test_cli_keeps_complete_source_and_requires_explicit_reference_roots(tmp_path):
    target = tmp_path / "owner.py"
    target.write_text("def work():\n    return 'final marker'\n")
    caller = tmp_path / "caller.py"
    caller.write_text("from owner import work as run\nrun()\n# work is a text candidate\n")
    def run(*args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)
    selected = run("symbol", str(target), "work")
    assert selected.returncode == 0
    assert "return 'final marker'" in selected.stdout
    missing_root = run("references", str(target), "work")
    assert missing_root.returncode == 2
    assert "explicit --root" in missing_root.stderr
    refs = run("references", str(target), "work", "--root", str(tmp_path))
    assert refs.returncode == 0
    assert "Static imports/direct references" in refs.stdout
    assert "reference <module>" in refs.stdout
    assert "Text candidates" in refs.stdout
    assert "# work" not in refs.stdout


@pytest.mark.process_contract
def test_cli_typescript_and_error_exit_statuses(tmp_path):
    component = tmp_path / "panel.tsx"
    component.write_text("import { label } from './labels';\nexport function Panel() { return <div>{label}</div>; }\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "symbol", str(component), "Panel"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "<div>{label}</div>" in result.stdout
    assert "import { label }" in result.stdout
    component.write_text("export function Panel( {\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "outline", str(component)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "TypeScript syntax error" in result.stderr


@pytest.mark.process_contract
def test_reference_scan_fails_explicitly_on_malformed_supported_source(tmp_path):
    target = tmp_path / "owner.py"
    target.write_text("def work(): pass\n")
    (tmp_path / "broken.ts").write_text("function broken( {\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "references", str(target), "work", "--root", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "TypeScript syntax error" in result.stderr
    assert "Searched" not in result.stdout
