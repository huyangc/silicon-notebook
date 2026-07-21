import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app


ROOT = Path(__file__).resolve().parents[2]


def _endpoint_modules() -> dict[str, str]:
    return {
        route.name: route.endpoint.__module__
        for route in app.routes
        if isinstance(route, APIRoute)
    }


def test_system_endpoints_are_owned_by_the_system_router():
    modules = _endpoint_modules()
    for endpoint in (
        "health",
        "me",
        "get_model_settings",
        "put_model_settings",
        "test_model_service",
        "list_doc_types",
        "detect_doc_types",
        "list_notebook_templates",
        "me_pending_actions",
        "me_pending_stream",
    ):
        assert modules[endpoint] == "app.api.system_routes", endpoint


def test_domain_router_does_not_import_the_schema_facade():
    path = ROOT / "backend" / "app" / "api" / "system_routes.py"
    assert path.exists()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.models.schemas" not in modules
