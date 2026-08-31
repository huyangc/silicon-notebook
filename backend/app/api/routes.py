from fastapi import APIRouter

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


router = APIRouter()
for domain_router in (
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
    # 命令目录(方案 C·C1b)追加在末尾,只是延续「新增域路由器接在末尾」的一贯写法,
    # 不是被什么约束逼的:`api_contract` 夹具落盘时对 `app.openapi()` 的输出整体
    # `sort_keys=True`(见 scripts/generate_repository_contract_fixtures.py 的
    # `_write_json`),所以 `paths` 在文件里是按路径字符串排序的,注册顺序压根不影响
    # 生成结果——插在 source_router 旁边同样不会产生无语义 diff。
    catalog_router,
    # 群组与授权边(群组知识共享 P1-T3)。同上:注册顺序不影响 api_contract 夹具
    # (落盘时整体 sort_keys),接在末尾只是延续写法。
    group_router,
    # Agentic Memory P1(T6)。同上:注册顺序不影响 api_contract 夹具,接在末尾
    # 只是延续写法。
    agent_profile_router,
    wish_router,
):
    router.include_router(domain_router)
