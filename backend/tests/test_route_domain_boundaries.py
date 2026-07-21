import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.api.admin_routes import router as admin_router
from app.api.ask_routes import router as ask_router
from app.api.content_overview_routes import router as content_overview_router
from app.api.kg_routes import router as kg_router
from app.api.knowhow_routes import router as knowhow_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import memory_router
from app.api.notebook_routes import router as notebook_router
from app.api.report_routes import router as report_router
from app.api.source_routes import router as source_router
from app.api.system_routes import router as system_router


ROOT = Path(__file__).resolve().parents[2]
DOMAIN_ROUTERS = (
    memory_router,
    system_router,
    notebook_router,
    content_overview_router,
    source_router,
    knowhow_router,
    knowledge_router,
    ask_router,
    report_router,
    kg_router,
    admin_router,
)
EXPECTED_COMPOSITION_NAMES = (
    "memory_router",
    "system_router",
    "notebook_router",
    "content_overview_router",
    "source_router",
    "knowhow_router",
    "knowledge_router",
    "ask_router",
    "report_router",
    "kg_router",
    "admin_router",
)


def _endpoint_modules() -> dict[str, str]:
    return {
        route.name: route.endpoint.__module__
        for router in DOMAIN_ROUTERS
        for route in router.routes
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
        "get_model_services_status",
        "test_all_model_services",
        "test_current_model_service",
        "list_doc_types",
        "detect_doc_types",
        "list_notebook_templates",
        "me_pending_actions",
        "me_pending_stream",
    ):
        assert modules[endpoint] == "app.api.system_routes", endpoint


def test_notebook_and_source_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "list_notebooks": "app.api.notebook_routes",
        "create_notebook": "app.api.notebook_routes",
        "get_notebook": "app.api.notebook_routes",
        "set_notebook_tier": "app.api.notebook_routes",
        "share_notebook_route": "app.api.notebook_routes",
        "list_sources": "app.api.source_routes",
        "upload_sources": "app.api.source_routes",
        "get_source": "app.api.source_routes",
        "backfill_paper_metadata": "app.api.source_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_knowhow_and_knowledge_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "preview_knowhow_import": "app.api.knowhow_routes",
        "patch_knowhow_cell": "app.api.knowhow_routes",
        "reformat_knowhow_cell": "app.api.knowhow_routes",
        "transfer_knowhow_table": "app.api.knowhow_routes",
        "knowledge_types": "app.api.knowledge_routes",
        "list_knowledge": "app.api.knowledge_routes",
        "merge_knowledge": "app.api.knowledge_routes",
        "edge_review_queue": "app.api.knowledge_routes",
        "review_relation": "app.api.knowledge_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_ask_and_report_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "search_notebook": "app.api.ask_routes",
        "ask": "app.api.ask_routes",
        "ask_stream": "app.api.ask_routes",
        "cancel_ask_job": "app.api.ask_routes",
        "list_conversations": "app.api.ask_routes",
        "submit_feedback": "app.api.ask_routes",
        "create_report": "app.api.report_routes",
        "export_reports_endpoint": "app.api.report_routes",
        "generate_report": "app.api.report_routes",
        "cancel_report_endpoint": "app.api.report_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_kg_and_admin_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "kg_search": "app.api.kg_routes",
        "build_kg": "app.api.kg_routes",
        "rebuild_unified_kg": "app.api.kg_routes",
        "get_unified_kg": "app.api.kg_routes",
        "resolve_conflicts": "app.api.kg_routes",
        "review_unified_kg_merges": "app.api.kg_routes",
        "propose_promotion": "app.api.admin_routes",
        "approve_promotion": "app.api.admin_routes",
        "list_admin_users": "app.api.admin_routes",
        "list_online_users": "app.api.admin_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_aggregate_routes_module_is_composition_only():
    path = ROOT / "backend" / "app" / "api" / "routes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    decorated_functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list
    ]
    assert decorated_functions == []

    composition_loops = [
        node
        for node in tree.body
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "domain_router"
    ]
    assert len(composition_loops) == 1
    loop = composition_loops[0]
    assert isinstance(loop.iter, (ast.Tuple, ast.List))
    assert tuple(
        item.id for item in loop.iter.elts if isinstance(item, ast.Name)
    ) == EXPECTED_COMPOSITION_NAMES
    assert len(loop.body) == 1
    statement = loop.body[0]
    assert isinstance(statement, ast.Expr)
    call = statement.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert isinstance(call.func.value, ast.Name)
    assert (call.func.value.id, call.func.attr) == ("router", "include_router")
    assert len(call.args) == 1
    assert isinstance(call.args[0], ast.Name)
    assert call.args[0].id == "domain_router"


def test_domain_routers_do_not_import_the_schema_facade():
    api_dir = ROOT / "backend" / "app" / "api"
    route_modules = sorted(api_dir.glob("*_routes.py"))
    assert route_modules
    for path in route_modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "app.models.schemas" not in modules, path.name
