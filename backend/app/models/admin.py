from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.common import Evidence


class PromotionCandidate(BaseModel):
    """A personal-KG node proposed for promotion into the base corpus (Track F)."""

    id: str
    notebook_id: str
    object_id: str
    object_type: str
    status: str
    reason: str = ""
    reviewed_by: str = ""
    base_match_id: str = ""
    created_at: str = ""
    # Denormalised fields populated by the repo from knowledge_objects:
    payload: dict = Field(default_factory=dict)
    evidence: List[Evidence] = Field(default_factory=list)
    source_kind: Literal["knowledge", "memory"] = "knowledge"
    memory_id: str = ""
    # Memory-backed proposals are reviewed against this immutable source revision.
    source_revision: int = 0
    # 多领域基准库(Task 7/8):这条候选要晋升进哪个公共知识库。挂 >1 个公共库时
    # 由提交方显式指定;队列里暴露出来是策展人审核"该进哪个库"的唯一依据。
    target_base_id: str = ""
    # Task 13 审查 #4:target_base_id 对应的库名,由后端 join notebooks 给出——
    # 策展人不一定是目标库的 owner,前端自己的 notebooks 列表(自有∪只读加入)
    # 覆盖不到"别人创建的公共知识库",猜不出真名。目标为空或库已不存在时是
    # 空串(list_promotion_queue 批量解析,不逐行查询)。
    target_base_name: str = ""


class PromotionApproveResult(BaseModel):
    candidate_id: str
    base_object_id: str
    base_object_ids: List[str] = Field(default_factory=list)
    merged_into: str = ""   # non-empty if deduped into an existing base object


class PromotionRejectRequest(BaseModel):
    reason: str = ""


class PromoteRequest(BaseModel):
    """POST .../promote 的可选请求体(知识对象晋升 + Memory 晋升共用)。挂载了
    多个公共知识库时必须显式指定 target_base_id,否则服务层拒绝(400)。"""
    target_base_id: str = ""


class AdminUserUsage(BaseModel):
    id: str
    username: str
    role: str
    created_at: str
    notebooks: int
    sources: int
    conversations: int
    reports: int
    last_active: Optional[str] = None
    is_online: bool = False
    role_mutable: bool = True


class AdminUserRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user"]


class AdminUserRoleResult(BaseModel):
    id: str
    username: str
    role: Literal["admin", "user"]


class AdminUserNotebook(BaseModel):
    id: str
    name: str
    status: str
    sources: int
    conversations: int
    reports: int
    created_at: str
    updated_at: str
