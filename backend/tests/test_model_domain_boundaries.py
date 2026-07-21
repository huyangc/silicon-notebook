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
    owners = {
        "common": ("Evidence",),
        "identity": (
            "UserProfile", "AgentProfile", "AgentProfileCreate", "AgentProfileUpdate",
            "AgentTokenCreate", "AgentTokenSummary", "AgentTokenIssued", "AgentPrincipal",
            "AuthRequest", "AuthResult",
        ),
        "memory": (
            "MemoryOrigin", "MemoryStatus", "MemoryPromotionState", "MemoryRecord", "MemoryHit",
            "MemoryNotebookOption", "PaginatedMemories", "MemoryPreview", "MemoryCreateFromAnswer",
            "AnswerMemoryLinksRequest", "AnswerMemoryLinksResponse", "MemoryBulkDeleteRequest",
            "MemoryUpdate", "MemoryReviewRequest", "MemoryTransferRequest",
        ),
        "sources": (
            "PaperAuthor", "PaperMeta", "SourceElement", "SourceSummary", "PaginatedSources",
            "SourceImportFile", "SourceImportRequest", "AddUrlSourcesRequest", "RejectedUrl",
            "AddUrlSourcesResult", "SourceDetail", "DetectDocTypeItem", "DetectDocTypesRequest",
            "DetectedDocType",
        ),
        "notebooks": (
            "NotebookCreate", "NotebookUpdate", "NotebookRef", "MountedBase", "NotebookSummary",
            "ShareResponse", "SharedPreview", "SharedByMeItem", "NotebookTemplate", "SetTierRequest",
            "SetBasesRequest", "MountedByCount", "NotebookAnalytics",
        ),
        "kg": ("KgBuildJobStatus",),
        "reports": (
            "ReportCreate", "ReportOutlineUpdate", "ReportGenerateRequest", "ReportSummary",
            "ReportExportRequest", "ReportDetail",
        ),
    }
    for module_name, names in owners.items():
        owner = importlib.import_module(f"app.models.{module_name}")
        for name in names:
            assert getattr(facade, name) is getattr(owner, name), name
