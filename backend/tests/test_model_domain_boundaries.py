import ast
import importlib
import json
from pathlib import Path


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
    "kg",
    "reports",
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
    "NotebookTemplate": "notebooks",
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


def test_moved_legacy_exports_preserve_object_identity():
    facade = importlib.import_module("app.models.schemas")
    for name, module_name in MOVED_MODEL_OWNERS.items():
        owner = importlib.import_module(f"app.models.{module_name}")
        assert getattr(facade, name) is getattr(owner, name), name


def test_production_consumers_do_not_import_moved_models_from_legacy_facade():
    app_root = ROOT / "backend" / "app"
    offenders: dict[str, list[str]] = {}
    for path in app_root.rglob("*.py"):
        if path == MODELS / "schemas.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        moved = sorted(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.models.schemas"
            for alias in node.names
            if alias.name in MOVED_MODEL_OWNERS
        )
        if moved:
            offenders[str(path.relative_to(ROOT))] = moved
    assert not offenders, offenders
