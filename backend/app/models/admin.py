from typing import Dict, List, Literal, Optional

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
    # 该用户当前生效的「每笔记本文档数量上限」及其是否为单独覆盖值。
    # upload_limit = COALESCE(用户覆盖值, 全局默认);overridden=True 表示这是
    # 给该用户单独设的覆盖值,False 表示继承全局默认。
    upload_limit: int = 0
    upload_limit_overridden: bool = False


class AdminUserRoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user"]


class AdminUserRoleResult(BaseModel):
    id: str
    username: str
    role: Literal["admin", "user"]


class UploadLimitUpdate(BaseModel):
    """PATCH /admin/users/{id}/upload-limit 请求体:给某用户设/清「每笔记本文档
    数量上限」覆盖值。limit=null 表示清除覆盖、回落全局默认。"""
    model_config = ConfigDict(extra="forbid")

    limit: Optional[int] = None


class UploadLimitDefaultUpdate(BaseModel):
    """PATCH /admin/settings/upload-limit-default 请求体:设置全局默认上限。"""
    model_config = ConfigDict(extra="forbid")

    limit: int


class UploadLimitDefaultResult(BaseModel):
    """GET/PATCH /admin/settings/upload-limit-default 响应:当前全局默认上限。"""
    limit: int


class AdminUserUploadLimitResult(BaseModel):
    """PATCH /admin/users/{id}/upload-limit 响应:改动后该用户的生效上限与是否覆盖。"""
    id: str
    username: str
    upload_limit: int
    upload_limit_overridden: bool


class AdminUserNotebook(BaseModel):
    id: str
    name: str
    status: str
    sources: int
    conversations: int
    reports: int
    created_at: str
    updated_at: str


class CacheStats(BaseModel):
    """内容寻址缓存的当前状况（admin 只读）。

    `by_tag` 的键是模型名——写入时以 model 作 tag，正是为了让"换了模型服务"这件事
    能按模型精确清理，而不是等 90 天 TTL 自己到期。
    """
    enabled: bool
    admin_supported: bool          # 后端是否实现 CacheAdmin（只有 get/put 的后端为 False）
    entries: int = 0
    bytes: int = 0
    by_tag: Dict[str, int] = {}
    hits: int = 0
    misses: int = 0
    hit_rate: float = 0.0


class CacheEvictRequest(BaseModel):
    """按 tag（模型名）清理，或整库清空。

    两者必须显式二选一：不给默认的"留空即全清"，避免一次手滑把整个缓存清掉。
    """
    tag: str = ""
    clear_all: bool = False


class CacheEvictResult(BaseModel):
    evicted: int
    scope: str                     # 被清理的 tag 名，或 'all'
