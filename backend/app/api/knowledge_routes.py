"""Knowledge-governance routes for notebook knowledge objects and edges."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    notebook_capability_allowed,
    repository,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.models.identity import UserProfile
from app.models.knowledge import (
    DuplicateGroup,
    EdgeReviewItem,
    EdgeReviewQueueResponse,
    EdgeReviewRequest,
    KnowledgeGraph,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    MergeRequest,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    PaginatedKnowledge,
)
from app.services.knowledge_contracts import KnowledgeGraphTooLargeError
from app.services.schema_registry import SchemaConflictError


router = APIRouter()


@router.get(
    "/notebooks/{notebook_id}/knowledge-types",
    response_model=List[KnowledgeTypeCount],
    dependencies=[Depends(require_notebook_read)],
)
def knowledge_types(notebook_id: str) -> List[KnowledgeTypeCount]:
    try:
        return repository().knowledge_types(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/knowledge", response_model=PaginatedKnowledge, dependencies=[Depends(require_notebook_read)])
def list_knowledge(
    notebook_id: str,
    type: str = Query(...),
    status: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedKnowledge:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().list_knowledge(notebook_id, object_type, status=status, offset=offset, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Editable extraction-schema registry ---------------------------------
@router.get("/object-schemas", response_model=List[ObjectSchemaModel])
def list_object_schemas(
    user: UserProfile = Depends(get_current_user),
) -> List[ObjectSchemaModel]:
    return repository().list_object_schemas(can_edit=user.role == "admin")


@router.post("/object-schemas", response_model=ObjectSchemaModel)
def create_object_schema(payload: ObjectSchemaCreate, user: UserProfile = Depends(get_current_user)) -> ObjectSchemaModel:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return repository().create_object_schema(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
@router.patch("/object-schemas/{object_type}", response_model=ObjectSchemaModel)
def update_object_schema(
    object_type: str, payload: ObjectSchemaUpdate, user: UserProfile = Depends(get_current_user)
) -> ObjectSchemaModel:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return repository().update_object_schema(object_type, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/object-schemas/{object_type}")
def delete_object_schema(object_type: str, user: UserProfile = Depends(get_current_user)):
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        repository().delete_object_schema(object_type)
        return {"status": "deleted", "object_type": object_type}
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get(
    "/notebooks/{notebook_id}/object-schemas",
    response_model=List[ObjectSchemaModel],
    dependencies=[Depends(require_notebook_read)],
)
def list_notebook_object_schemas(
    notebook_id: str,
    user: UserProfile = Depends(get_current_user),
) -> List[ObjectSchemaModel]:
    try:
        return repository().list_notebook_object_schemas(
            notebook_id,
            # ⚠ 必须与**写端点自己**的能力吃同一张表(P2-T2):这个投影驱动前端图谱
            # Schema 编辑控件的显隐,而写端点挂的正是
            # `require_notebook_capability("knowledge:write")`。此前它手写
            # `user_can_access_notebook`(owner-only),P2 把 knowledge:write 翻成
            # admin 档之后,组管理员会拿到一个「API 收得下、界面画不出来」的只读面板。
            # 走 notebook_capability_allowed 之后它随表自动跟随,不再是第二份判定。
            can_edit=notebook_capability_allowed(
                "knowledge:write", notebook_id, user.id
            ),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post(
    "/notebooks/{notebook_id}/object-schemas",
    response_model=ObjectSchemaModel,
    dependencies=[Depends(require_notebook_capability("knowledge:write"))],
)
def create_notebook_object_schema(
    notebook_id: str,
    payload: ObjectSchemaCreate,
    user: UserProfile = Depends(get_current_user),
) -> ObjectSchemaModel:
    try:
        return repository().create_notebook_object_schema(
            notebook_id, payload, created_by=user.id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except SchemaConflictError:
        raise user_error(409, "当前笔记本已存在同名类型，请换一个类型标识")
    except ValueError:
        raise user_error(400, "类型定义无效，请检查类型标识、字段、主字段和列表字段")


@router.patch(
    "/notebooks/{notebook_id}/object-schemas/{object_type}",
    response_model=ObjectSchemaModel,
    dependencies=[Depends(require_notebook_capability("knowledge:write"))],
)
def update_notebook_object_schema(
    notebook_id: str,
    object_type: str,
    payload: ObjectSchemaUpdate,
    user: UserProfile = Depends(get_current_user),
) -> ObjectSchemaModel:
    try:
        return repository().update_notebook_object_schema(
            notebook_id, object_type, payload, created_by=user.id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError:
        raise user_error(400, "类型定义无效，请检查字段、主字段和列表字段")


@router.delete("/notebooks/{notebook_id}/object-schemas/{object_type}",
    dependencies=[Depends(require_notebook_capability("knowledge:write"))])
def delete_notebook_object_schema(notebook_id: str, object_type: str):
    try:
        status = repository().delete_notebook_object_schema(notebook_id, object_type)
        return {"status": status, "object_type": object_type}
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema override not found")
    except ValueError:
        raise user_error(409, "该类型仍有知识条目，暂时不能删除")


@router.post(
    "/notebooks/{notebook_id}/schema-proposals",
    response_model=List[ObjectSchemaModel],
    dependencies=[Depends(require_notebook_capability("knowledge:write"))],
)
def propose_schemas(notebook_id: str) -> List[ObjectSchemaModel]:
    try:
        return repository().propose_schemas(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}/knowledge/{knowledge_id}", dependencies=[Depends(require_notebook_capability("knowledge:write"))])
def update_knowledge(notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate):
    try:
        return repository().update_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_KNOWLEDGE_TYPE_MAP = {
    "rules": "rule",
    "methods": "method",
    "risks": "risk",
    "cases": "case",
    "checklist": "checklist",
    "glossary": "glossary",
}


@router.get("/notebooks/{notebook_id}/duplicates", response_model=List[DuplicateGroup], dependencies=[Depends(require_notebook_read)])
def find_duplicates(notebook_id: str, type: str = Query("rules")) -> List[DuplicateGroup]:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().find_duplicates(notebook_id, object_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")



@router.get("/notebooks/{notebook_id}/graph", response_model=KnowledgeGraph, dependencies=[Depends(require_notebook_read)])
def knowledge_graph(notebook_id: str) -> KnowledgeGraph:
    try:
        return repository().knowledge_graph(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except KnowledgeGraphTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))


@router.post("/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge", dependencies=[Depends(require_notebook_capability("knowledge:write"))])
def merge_knowledge(notebook_id: str, knowledge_id: str, payload: MergeRequest):
    try:
        return repository().merge_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# Edge trust & curation (Track E)
# ---------------------------------------------------------------------------

@router.get("/notebooks/{notebook_id}/edge-review-queue", response_model=EdgeReviewQueueResponse, dependencies=[Depends(require_notebook_read)])
def edge_review_queue(notebook_id: str, limit: int = 100) -> EdgeReviewQueueResponse:
    """Return edges ranked by review priority (high centrality × low trust) desc,
    plus the queue's true total size (independent of `limit`; R3 T-A3 v4).
    Excludes already-rejected edges.

    ONE call (``review_queue_page``) instead of the v3 shape's two independent
    reads — ``items`` and ``total`` come from the same seq-gated memo entry so
    they can never describe two different KG versions.
    """
    try:
        page = repository().review_queue_page(notebook_id, limit=limit)
        return EdgeReviewQueueResponse(items=page["items"], total=page["total"])
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/relations/{rel_id}/review", status_code=200, dependencies=[Depends(require_notebook_capability("knowledge:write"))])
def review_relation(notebook_id: str, rel_id: str,
                    payload: EdgeReviewRequest) -> dict:
    """Mark an edge as 'verified', 'rejected', or 'pending'.
    Rejected edges are excluded from all future graph-reasoning traversals.
    """
    try:
        repository().set_edge_review(notebook_id, rel_id, payload.status)
        return {"rel_id": rel_id, "review_status": payload.status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
