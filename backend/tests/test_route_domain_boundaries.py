import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.api.admin_routes import router as admin_router
from app.api.agent_profile_routes import router as agent_profile_router
from app.api.ask_routes import router as ask_router
from app.api.catalog_routes import router as catalog_router
from app.api.content_overview_routes import router as content_overview_router
from app.api.group_routes import router as group_router
from app.api.kg_routes import router as kg_router
from app.api.knowhow_routes import router as knowhow_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import memory_router
from app.api.notebook_routes import router as notebook_router
from app.api.report_routes import router as report_router
from app.api.source_routes import router as source_router
from app.api.system_routes import router as system_router
from app.api.wish_routes import router as wish_router


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
    catalog_router,
    group_router,
    agent_profile_router,
    wish_router,
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
    # Appended, not slotted next to source_router: this just continues the
    # existing habit of adding new domain routers at the end. It is not
    # enforced by anything — the api_contract fixture is written with
    # `sort_keys=True` (see generate_repository_contract_fixtures.py's
    # `_write_json`), so `paths` lands sorted by path string regardless of
    # registration order (see the comment at the composition site).
    "catalog_router",
    # 群组与授权边(群组知识共享 P1-T3),同上:接在末尾只是延续写法。
    "group_router",
    # Agentic Memory P1(T6),同上:接在末尾只是延续写法。
    "agent_profile_router",
    # 许愿墙是全局反馈域，不归属于任何单个笔记本。
    "wish_router",
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
        "get_system_model_services_status",
        "list_doc_types",
        "detect_doc_types",
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
        "test_system_model_service": "app.api.admin_routes",
        "test_all_system_model_services": "app.api.admin_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_wish_wall_endpoints_have_a_domain_owner():
    modules = _endpoint_modules()
    expected = {
        "list_wishes": "app.api.wish_routes",
        "create_wish": "app.api.wish_routes",
        "toggle_wish_vote": "app.api.wish_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_group_and_grant_endpoints_have_a_domain_owner():
    """授权边的两个端点挂在 `/notebooks/{id}/...` 下,但**归群组域所有**。

    这条钉的正是那件容易滑掉的事:URL 前缀是 notebook,策略却是群组的(双重条件、
    组管理员判定),照 URL 把它们搬进 `notebook_routes.py` 会让群组策略散成两处。

    清单是**穷举**的:少写一个端点,它搬去别的模块就没人拦得住。第二条断言按数量
    对账,保证「新增端点忘了登记」当场报红,而不是被这份不完整的清单默默放过。
    """
    modules = _endpoint_modules()
    expected = {
        "create_group_route": "app.api.group_routes",
        "list_groups_route": "app.api.group_routes",
        "get_group_route": "app.api.group_routes",
        "update_group_route": "app.api.group_routes",
        "delete_group_route": "app.api.group_routes",
        "transfer_group_owner_route": "app.api.group_routes",
        "put_group_member_route": "app.api.group_routes",
        "remove_group_member_route": "app.api.group_routes",
        "leave_group_route": "app.api.group_routes",
        "get_group_invite_route": "app.api.group_routes",
        "create_group_invite_route": "app.api.group_routes",
        "rotate_group_invite_route": "app.api.group_routes",
        "revoke_group_invite_route": "app.api.group_routes",
        "join_group_invite_route": "app.api.group_routes",
        "resolve_user_route": "app.api.group_routes",
        "list_notebook_grants_route": "app.api.group_routes",
        "create_notebook_grant_route": "app.api.group_routes",
        "delete_notebook_grant_route": "app.api.group_routes",
        "list_group_shared_notebooks_route": "app.api.group_routes",
        "delete_group_shared_notebook_route": "app.api.group_routes",
        # 成员贡献审批流(P2-T3)。三个 `/notebooks/{id}/share-requests...` 端点同样是
        # URL 前缀在 notebook、策略归群组域(申请的双重条件、组管理员审批)——与授权边
        # 两个端点同一条边界,一并登记在群组域,不能照 URL 搬进 notebook_routes。
        "create_share_request_route": "app.api.group_routes",
        "list_my_share_requests_route": "app.api.group_routes",
        # `GET /me/share-requests`(codex #519 R11 P1):URL 前缀在 `/me`(授权轴是**申请
        # 归属**,不是笔记本权限,所以刻意不挂在 notebook 维度),但策略仍归群组域——它是
        # 审批流的一部分,和上面那条按笔记本列的清单共用同一套 store 与状态口径。
        "list_my_pending_share_requests_route": "app.api.group_routes",
        "delete_share_request_route": "app.api.group_routes",
        "list_group_share_requests_route": "app.api.group_routes",
        "approve_share_request_route": "app.api.group_routes",
        "reject_share_request_route": "app.api.group_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint

    declared = {
        route.name
        for route in group_router.routes
        if isinstance(route, APIRoute)
    }
    assert declared == set(expected), (
        "群组路由的端点集合与本清单不一致——新增/改名端点必须同步登记,"
        f"否则它搬去别的域模块不会被拦下:{sorted(declared ^ set(expected))}"
    )


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
