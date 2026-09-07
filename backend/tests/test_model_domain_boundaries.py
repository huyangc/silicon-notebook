import ast
import importlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "backend" / "app" / "models"
LEGACY = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_schema_exports.json").read_text(encoding="utf-8")
)
DOMAIN_MODULES = (
    "common",
    "identity",
    "memory",
    "sources",
    "notebooks",
    "reports",
    "ask",
    "knowledge",
    "kg",
    "knowhow",
    "content_overview",
    "admin",
    "model_services",
)
MOVED_MODEL_OWNERS = {
    "Evidence": "common",
    "UserProfile": "identity",
    "AgentProfile": "identity",
    "AgentProfileCreate": "identity",
    "AgentProfileUpdate": "identity",
    "AgentTokenCreate": "identity",
    "AgentTokenSummary": "identity",
    "AgentTokenIssued": "identity",
    "AgentPrincipal": "identity",
    "AuthRequest": "identity",
    "AuthResult": "identity",
    "MemoryOrigin": "memory",
    "MemoryStatus": "memory",
    "MemoryPromotionState": "memory",
    "MemoryRecord": "memory",
    "MemoryHit": "memory",
    "MemoryNotebookOption": "memory",
    "PaginatedMemories": "memory",
    "MemoryPreview": "memory",
    "MemoryCreateFromAnswer": "memory",
    "AnswerMemoryLinksRequest": "memory",
    "AnswerMemoryLinksResponse": "memory",
    "MemoryBulkDeleteRequest": "memory",
    "MemoryUpdate": "memory",
    "MemoryReviewRequest": "memory",
    "MemoryTransferRequest": "memory",
    "PaperAuthor": "sources",
    "PaperMeta": "sources",
    "SourceElement": "sources",
    "SourceSummary": "sources",
    "PaginatedSources": "sources",
    "SourceImportFile": "sources",
    "SourceImportRequest": "sources",
    "AddUrlSourcesRequest": "sources",
    "RejectedUrl": "sources",
    "AddUrlSourcesResult": "sources",
    "SourceDetail": "sources",
    "DetectDocTypeItem": "sources",
    "DetectDocTypesRequest": "sources",
    "DetectedDocType": "sources",
    "NotebookCreate": "notebooks",
    "NotebookUpdate": "notebooks",
    "NotebookRef": "notebooks",
    "MountedBase": "notebooks",
    "NotebookSummary": "notebooks",
    "ShareResponse": "notebooks",
    "SharedPreview": "notebooks",
    "SharedByMeItem": "notebooks",
    "SetTierRequest": "notebooks",
    "SetBasesRequest": "notebooks",
    "MountedByCount": "notebooks",
    "NotebookAnalytics": "notebooks",
    "KgBuildJobStatus": "kg",
    "ReportCreate": "reports",
    "ReportOutlineUpdate": "reports",
    "ReportGenerateRequest": "reports",
    "ReportSummary": "reports",
    "ReportExportRequest": "reports",
    "ReportDetail": "reports",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def imports_schema_facade(tree_or_source: ast.AST | str) -> bool:
    tree = ast.parse(tree_or_source) if isinstance(tree_or_source, str) else tree_or_source
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "app.models.schemas" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "app.models.schemas":
                return True
            if node.module == "app.models" and any(
                alias.name == "schemas" for alias in node.names
            ):
                return True
    return False


def test_schema_facade_import_helper_detects_every_static_facade_form():
    rejected = (
        "from app.models import schemas",
        "from app.models import schemas as legacy_schemas",
        "import app.models.schemas",
        "import app.models.schemas as schemas",
        "from app.models.schemas import AskRequest",
    )
    allowed = (
        "from app.models import memory",
        "from app.models.ask import AskRequest",
    )
    for source in rejected:
        assert imports_schema_facade(source), source
    for source in allowed:
        assert not imports_schema_facade(source), source


def moved_facade_references(tree_or_source: ast.AST | str) -> set[str]:
    tree = ast.parse(tree_or_source) if isinstance(tree_or_source, str) else tree_or_source
    found = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.models.schemas"
        for alias in node.names
        if alias.name in MOVED_MODEL_OWNERS
    }
    bindings: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.models.schemas" and any(
                alias.name == "*" for alias in node.names
            ):
                found.add("*")
            if node.module == "app.models":
                bindings.update(
                    ((alias.asname or alias.name),)
                    for alias in node.names
                    if alias.name == "schemas"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.models.schemas":
                    bindings.add(
                        (alias.asname,) if alias.asname else ("app", "models", "schemas")
                    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in MOVED_MODEL_OWNERS:
            continue
        parts: list[str] = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
            if tuple(reversed(parts[1:])) in bindings:
                found.add(node.attr)
    return found


def test_first_domain_model_modules_exist_without_reverse_dependencies():
    for module_name in DOMAIN_MODULES:
        path = MODELS / f"{module_name}.py"
        assert path.exists(), module_name
        imports = _imports(path)
        assert not any(name.startswith("app.api") for name in imports)
        assert not any(name.startswith("app.services") for name in imports)
        assert not any(name.startswith("app.repositories") for name in imports)


def test_legacy_schema_exports_resolve_to_domain_objects():
    facade = importlib.import_module("app.models.schemas")
    assert sorted(facade.__all__) == LEGACY
    for name in LEGACY:
        assert hasattr(facade, name), name


def test_schema_facade_contains_no_model_definitions():
    tree = ast.parse((MODELS / "schemas.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ClassDef) for node in tree.body)


def test_legacy_schema_exports_are_object_identical_domain_definitions():
    facade = importlib.import_module("app.models.schemas")
    assert sorted(facade.__all__) == LEGACY
    for name in LEGACY:
        value = getattr(facade, name)
        if isinstance(value, type):
            assert value.__module__ != "app.models.schemas", name


def test_moved_legacy_exports_preserve_object_identity():
    facade = importlib.import_module("app.models.schemas")
    for name, module_name in MOVED_MODEL_OWNERS.items():
        owner = importlib.import_module(f"app.models.{module_name}")
        assert getattr(facade, name) is getattr(owner, name), name


def test_moved_facade_reference_helper_detects_moved_access_paths():
    cases = {
        "from app.models.schemas import Evidence": {"Evidence"},
        "import app.models.schemas as schemas\nschemas.UserProfile": {"UserProfile"},
        "from app.models import schemas as schemas\nschemas.NotebookCreate": {"NotebookCreate"},
        "import app.models.schemas\napp.models.schemas.ReportCreate": {"ReportCreate"},
        "from app.models.schemas import *": {"*"},
    }
    for source, expected in cases.items():
        assert moved_facade_references(source) == expected


def test_moved_facade_reference_helper_allows_unmoved_facade_access():
    cases = (
        "from app.models.schemas import AskRequest",
        "import app.models.schemas as schemas\nschemas.AskRequest",
        "from app.models import schemas\nschemas.AskRequest",
        "import app.models.schemas\napp.models.schemas.AskRequest",
    )
    for source in cases:
        assert not moved_facade_references(source)


@pytest.mark.architecture_contract
def test_first_party_production_uses_domain_model_modules():
    app_root = ROOT / "backend" / "app"
    offenders = []
    for path in app_root.rglob("*.py"):
        if path == MODELS / "schemas.py":
            continue
        if imports_schema_facade(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
