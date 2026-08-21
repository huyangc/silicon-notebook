import ast

from tests.architecture.semantic_source import (
    PythonSourceIndex,
    SemanticKey,
    qualified_scopes,
)


def test_line_movement_does_not_change_semantic_identity():
    before = {"app/a.py": "def run(repo):\n    return repo.save()\n"}
    after = {"app/a.py": "\n\n\ndef run(repo):\n    return repo.save()\n"}

    assert PythonSourceIndex.from_sources(before).call_keys() == (
        PythonSourceIndex.from_sources(after).call_keys()
    )


def test_scope_movement_changes_semantic_identity():
    before = {"app/a.py": "def run(repo):\n    return repo.save()\n"}
    after = {
        "app/a.py": (
            "class Worker:\n"
            "    def run(self, repo):\n"
            "        return repo.save()\n"
        )
    }

    assert PythonSourceIndex.from_sources(before).call_keys() != (
        PythonSourceIndex.from_sources(after).call_keys()
    )


def test_duplicate_count_is_semantic_and_lines_are_diagnostics_only():
    index = PythonSourceIndex.from_sources(
        {"app/a.py": "def run(repo):\n    repo.save()\n    repo.save()\n"}
    )

    finding = index.calls(target="repo.save")[0]
    assert finding.key == SemanticKey(
        path="app/a.py",
        scope="<module>.run",
        kind="call",
        target="repo.save",
    )
    assert finding.count == 2
    assert finding.diagnostic_lines == (2, 3)


def test_import_identity_includes_scope_and_symbol_without_line():
    index = PythonSourceIndex.from_sources(
        {
            "app/a.py": (
                "def load():\n"
                "    from app.repositories.ports import AskPort\n"
                "    return AskPort\n"
            )
        }
    )

    assert index.import_keys() == frozenset(
        {
            SemanticKey(
                path="app/a.py",
                scope="<module>.load",
                kind="import",
                target="app.repositories.ports:AskPort",
            )
        }
    )


def test_module_from_import_keeps_the_imported_module_as_a_semantic_target():
    index = PythonSourceIndex.from_sources(
        {"app/a.py": "from app.services import sqlite_repository\n"}
    )

    assert index.import_keys() == frozenset(
        {
            SemanticKey(
                path="app/a.py",
                scope="<module>",
                kind="import",
                target="app.services:sqlite_repository",
            )
        }
    )


def test_session_index_contains_known_repository_import(python_source_index):
    imports = python_source_index.import_keys()

    assert SemanticKey(
        path="backend/app/api/deps.py",
        scope="<module>",
        kind="import",
        target="app.bootstrap:create_application_repository",
    ) in imports


def test_attribute_identity_tracks_scope_and_occurrence_count():
    index = PythonSourceIndex.from_sources(
        {
            "app/a.py": (
                "def run(repo):\n"
                "    repo._runtime.start()\n"
                "    return repo._runtime\n"
            )
        }
    )

    finding = index.attributes(target="repo._runtime")[0]
    assert finding.key == SemanticKey(
        path="app/a.py",
        scope="<module>.run",
        kind="attribute",
        target="repo._runtime",
    )
    assert finding.count == 2
    assert finding.diagnostic_lines == (2, 3)


def test_qualified_scopes_identify_nested_calls_without_positions():
    tree = ast.parse(
        "class Worker:\n"
        "    def run(self, port):\n"
        "        return port.fetch()\n"
    )
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))

    assert qualified_scopes(tree)[call] == "<module>.Worker.run"
