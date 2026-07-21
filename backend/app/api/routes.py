from fastapi import APIRouter

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
):
    router.include_router(domain_router)
