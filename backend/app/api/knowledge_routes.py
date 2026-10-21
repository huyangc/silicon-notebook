"""Knowledge-governance routes for notebook knowledge objects and edges."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    knowledge_governance_service,
    mirrored_notebook_error,
    notebook_capability_allowed,
    notebook_mirror_fence,
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
    OwnMemoryDuplicates,
    PaginatedKnowledge,
)
from app.domain.memory_kg_isolation import (
    MEMORY_MERGE_RETRY_MESSAGE,
    MemoryKnowledgeMergeRefused,
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


@router.patch("/notebooks/{notebook_id}/knowledge/{knowledge_id}", dependencies=[Depends(require_notebook_read)])
def update_knowledge(notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate,
                     user: UserProfile = Depends(get_current_user)):
    """Two authorisation paths (user ruling 2026-10-22), the same split as the
    merge route below.

    With ``knowledge:write``: as before -- the capability, the mirror fence
    (409), then the store (an object derived from someone else's Memory is "not
    found", the 404 an unknown id gets).

    With read access only: a status change alone, between approved and
    deprecated, of the caller's own Memory object supported by her own Memory
    alone (undoing or redoing a merge), decided on the row the store locked
    (``own_status_only``). Every refusal -- any other field, a shared or someone
    else's object, an unknown id, any other status -- answers the 404 this
    caller got for any ``PATCH`` before. On a mirror the read path does not
    apply (the knowledge:write fence): 404 as before."""
    if not notebook_capability_allowed("knowledge:write", notebook_id, user.id):
        return _update_own_status(notebook_id, knowledge_id, payload, user)
    mirrored = notebook_mirror_fence("knowledge:write", notebook_id)
    if mirrored:
        raise mirrored_notebook_error(mirrored)
    # M1: an object derived from someone else's Memory is "not found" (same
    # 404 body as an unknown id) — decided in the store's locking read.
    try:
        return repository().update_knowledge(
            notebook_id, knowledge_id, payload, actor_id=user.id
        )
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


@router.get(
    "/notebooks/{notebook_id}/duplicates/own-memory",
    response_model=OwnMemoryDuplicates,
    dependencies=[Depends(require_notebook_read)],
)
def find_own_memory_duplicates(
    notebook_id: str, type: str = Query("rules"),
) -> OwnMemoryDuplicates:
    """The caller's own duplicate groups: objects derived from her own Memory
    and supported by it alone, which the shared ``/duplicates`` leaves out for
    everyone, plus those of them a merge deprecated (``merged_away``, what she
    can restore). A separate route because ``/duplicates`` answers a bare list,
    so a per-viewer field would change its shape for every existing client.
    Anyone else — another member, a site admin — gets two empty lists, the same
    answer as a viewer who owns no Memory here."""
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return knowledge_governance_service().find_own_memory_duplicates(
            notebook_id, object_type
        )
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


#: What a caller without ``knowledge:write`` gets for every merge or ``PATCH``
#: refusal: the read guard's own 404 body, the one she got for any such write
#: before 2026-10-22.
_MERGE_NOT_PERMITTED = "Notebook not found"


@router.post("/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge", dependencies=[Depends(require_notebook_read)])
def merge_knowledge(notebook_id: str, knowledge_id: str, payload: MergeRequest,
                    user: UserProfile = Depends(get_current_user)):
    """Two authorisation paths (user ruling 2026-10-22).

    With ``knowledge:write`` (owner, notebook admin): as before -- the
    capability, then the mirror fence (409), then the store's M1 rules: a side
    derived from someone else's Memory is "not found" (the 404 an unknown id
    gets), the caller's own Memory answers 409 with why, except a pair of her
    own objects backed by her own Memory alone.

    With read access only: the store merges two of the caller's own Memory
    objects supported by her own Memory alone (``own_pair_only``), and refuses
    anything else in the statement that locks and classifies the pair. Every
    refusal answers the 404 this caller got for any merge before -- an unknown
    id, a shared or someone else's object, a cross-class pair -- so she learns
    nothing about the other object. On a mirror the read path does not apply
    (the knowledge:write fence, as for every knowledge write there): 404 as
    before. The only other answer is the PostgreSQL retry 409 of her own pair."""
    if notebook_capability_allowed("knowledge:write", notebook_id, user.id):
        mirrored = notebook_mirror_fence("knowledge:write", notebook_id)
        if mirrored:
            raise mirrored_notebook_error(mirrored)
        return _merge_with_write(notebook_id, knowledge_id, payload, user)
    if notebook_mirror_fence("knowledge:write", notebook_id):
        raise HTTPException(status_code=404, detail=_MERGE_NOT_PERMITTED)
    try:
        # Straight to the governance service: the facade's frozen signature
        # stays as it is (only the write path goes through it).
        return knowledge_governance_service().merge_knowledge(
            notebook_id, knowledge_id, payload, actor_id=user.id, own_pair_only=True
        )
    except MemoryKnowledgeMergeRefused as exc:
        if exc.user_message == MEMORY_MERGE_RETRY_MESSAGE:
            raise user_error(409, exc.user_message)
        raise HTTPException(status_code=404, detail=_MERGE_NOT_PERMITTED)
    except (LookupError, ValueError):
        # KeyError (unknown or someone else's object), NeedsKnowledgeWrite
        # (not her own pair), ValueError (self-merge, mismatched types)
        raise HTTPException(status_code=404, detail=_MERGE_NOT_PERMITTED)


def _update_own_status(notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate,
                       user: UserProfile):
    """The read-level ``PATCH`` (see ``update_knowledge``): straight to the
    governance service, so the facade's frozen signature stays as it is."""
    if notebook_mirror_fence("knowledge:write", notebook_id):
        raise HTTPException(status_code=404, detail=_MERGE_NOT_PERMITTED)
    try:
        return knowledge_governance_service().update_knowledge(
            notebook_id, knowledge_id, payload, actor_id=user.id, own_status_only=True
        )
    except (LookupError, ValueError):
        # KeyError (unknown or someone else's object), NeedsKnowledgeWrite
        # (not a status toggle of her own object), ValueError (invalid status)
        raise HTTPException(status_code=404, detail=_MERGE_NOT_PERMITTED)


def _merge_with_write(notebook_id: str, knowledge_id: str, payload: MergeRequest,
                      user: UserProfile):
    try:
        return repository().merge_knowledge(
            notebook_id, knowledge_id, payload, actor_id=user.id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except MemoryKnowledgeMergeRefused as exc:
        # Must precede ``ValueError`` (its base): curated copy, 409.
        raise user_error(409, exc.user_message)
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
                    payload: EdgeReviewRequest,
                    user: UserProfile = Depends(get_current_user)) -> dict:
    """Mark an edge as 'verified', 'rejected', or 'pending'.
    Rejected edges are excluded from all future graph-reasoning traversals.
    """
    # M1: a relation derived from someone else's Memory answers exactly like
    # a relation id that does not exist (404, same body).
    try:
        repository().set_edge_review(
            notebook_id, rel_id, payload.status, actor_id=user.id
        )
        return {"rel_id": rel_id, "review_status": payload.status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
