from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.internal_observability import public_trace_steps, sanitize_answer_payload
from app.models.common import Evidence


ADMIN_QUESTIONS_QUERY_MAX_CHARS = 200
ADMIN_QUESTIONS_DEFAULT_LIMIT = 50
ADMIN_QUESTIONS_MAX_LIMIT = 200


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
    conversations: int = Field(deprecated=True)
    questions: int
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


class AdminPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str


class AdminPasswordResetResult(BaseModel):
    id: str
    username: str


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
    conversations: int = Field(deprecated=True)
    questions: int
    reports: int
    created_at: str
    updated_at: str


# --- 用户日志页多维度改版 · 「活动」视图(P1) --------------------------------
#
# 字段名是与前端冻结的契约,真源见
# docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §3-4
# 及 frontend/app/dev/logs/activity/types.ts —— 改字段名必须两边同步。


class ActivityAsk(BaseModel):
    """一条「提问」活动条目,数据来自 ask_jobs + answers。"""

    type: Literal["ask"] = "ask"
    id: str
    notebook_id: str
    created_at: str
    asked_at: str = ""
    conversation_id: str = ""
    question: str = ""
    mode: str = ""
    status: str = ""
    answer_id: str = ""
    error: str = ""
    notebook_name: str = ""
    notebook_deleted_at: str = ""
    retained_until: str = ""


class ActivitySource(BaseModel):
    """一条「来源」活动条目,数据来自 sources(+ extraction_runs 派生诊断)。

    ``display_title`` 由路由层经 ``source_display_title()`` 合成
    (论文标题优先),不是仓储层原始的 ``title``/``file_name`` 列——所有为用户
    命名来源的路径共用同一份实现（见 `docs/product-and-api.md`）。
    """

    type: Literal["source"] = "source"
    id: str
    notebook_id: str
    created_at: str
    display_title: str = ""
    file_name: str = ""
    source_type: str = ""
    parse_status: str = ""
    status: str = ""
    # 刻意不是原始 error_message:那个串是 str(exc) 原样落库,可能带服务端绝对
    # 路径(FileNotFoundError: /…/storage/notebooks/…)。ScopedSourceDetail 出于
    # 同一理由删掉了它,而 admin 看别人的活动流正是那个模型要防的场景。派生
    # 规则与 ScopedSourceDetail.of 逐字一致。
    parse_failed: bool = False
    extraction_warning: str = ""
    parse_quality_warning: bool = False
    paper_meta_status: str = ""
    notebook_name: str = ""
    notebook_deleted_at: str = ""
    retained_until: str = ""


class ActivityReport(BaseModel):
    """一条「报告」活动条目,数据来自 reports。"""

    type: Literal["report"] = "report"
    id: str
    notebook_id: str
    created_at: str
    updated_at: str = ""
    question: str = ""
    depth: int = 0
    status: str = ""
    generation_started_at: str = ""
    notebook_name: str = ""
    notebook_deleted_at: str = ""
    retained_until: str = ""


# 按 type 判别的活动流条目联合类型 —— 与 frontend/app/dev/logs/activity/types.ts
# 的 ActivityItem 逐字对应。
ActivityItem = Annotated[
    Union[ActivityAsk, ActivitySource, ActivityReport],
    Field(discriminator="type"),
]


class ActivityCursor(BaseModel):
    ts: str
    id: str


class ActivityResponse(BaseModel):
    items: List[ActivityItem] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: Optional[ActivityCursor] = None


class AdminQuestionItem(BaseModel):
    """One cross-user question submission from Ask or Deep Report."""

    type: Literal["ask", "report"]
    id: str
    user_id: str
    username: str
    notebook_id: str
    notebook_name: str
    question: str
    status: str
    created_at: str


class AdminQuestionStats(BaseModel):
    total: int = 0
    asks: int = 0
    reports: int = 0
    active_users: int = 0


class AdminQuestionsResponse(BaseModel):
    items: List[AdminQuestionItem] = Field(default_factory=list)
    stats: AdminQuestionStats = Field(default_factory=AdminQuestionStats)
    total: int = 0
    offset: int = 0
    limit: int = ADMIN_QUESTIONS_DEFAULT_LIMIT


class AskDetail(BaseModel):
    """右栏「选中提问」详情:GET /admin/users/{user_id}/asks/{job_id} 的响应。

    ``trace``/``answer`` 的形状由既有的 ask-intent-trace / AnswerView 渲染件
    消费,这里不重新声明(前端契约里两者都是 ``unknown``)。``asked_at``/
    ``answered_at`` 的权威口径:前者直接取自 ``ask_jobs.asked_at``(单行主键
    查询,``ask_job_detail``),后者取自 ``answers.created_at``(旧 payload 重
    开时从答案行回填)——由 ``AskStateStore.ask_answer_detail`` 按 answer_id
    单行直查处理,不经会话历史(``get_conversation`` 只用于会话页,详情端点
    刻意不复用它,避免读取量随会话轮次线性增长)。
    """

    job_id: str
    notebook_id: str
    conversation_id: str = ""
    question: str = ""
    mode: str = ""
    status: str = ""
    asked_at: str = ""
    answered_at: str = ""
    error: str = ""
    trace: List[dict] = Field(default_factory=list)
    answer: Optional[Dict[str, Any]] = None
    notebook_name: str = ""
    notebook_deleted_at: str = ""
    retained_until: str = ""

    @field_validator("trace", mode="before")
    @classmethod
    def _hide_internal_trace_steps(cls, value: object) -> object:
        return public_trace_steps(value)

    @field_validator("answer", mode="before")
    @classmethod
    def _hide_internal_answer_fields(cls, value: object) -> object:
        return sanitize_answer_payload(value)


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


# --- GET /admin/extensions：已加载扩展拓扑 + 运行时开关现状 ------------------
#
# 装载拓扑白名单，恰好 6 个字段（id/version/trust/display_name/contributions/
# ui_contributions）——拓扑启动冻结，TOML `enabled = false` 或未点名的插件根本
# 没有进入 registry，因此不出现在这里；这一层依旧**没有** `enabled` 字段，真源见
# backend/app/extensions/admin_projection.py。
#
# 运行时开关是另一层，来自 `extension_runtime_toggles` 表：`runtime_enabled` /
# `runtime_updated_by` / `runtime_updated_at` 三个字段由路由层现读 DB 后按
# plugin_id 与上面的装载行合成（见 admin_routes.py::list_admin_extensions）。
# builtin 插件恒为 `None, None, None`（只读，不可开关）；已装载的 deployment
# 插件无开关行时是 `True, None, None`（无行 = 启用）。
# docs/superpowers/plans/2026-08-29-extension-runtime-toggle.md。


class AdminExtensionContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    point: str
    kind: str


class AdminExtensionUiContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slot: str
    capability: str


class AdminExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    trust: Literal["builtin", "deployment"]
    display_name: str
    contributions: List[AdminExtensionContribution]
    ui_contributions: List[AdminExtensionUiContribution]
    # 运行时开关现状（另一层，来自 DB，不是启动冻结的装载拓扑）。builtin 恒为
    # None；已装载的 deployment 插件无开关行时是 True/None/None（无行 = 启用）。
    runtime_enabled: Optional[bool] = None
    runtime_updated_by: Optional[str] = None
    runtime_updated_at: Optional[str] = None


class AdminExtensionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: Literal["1"] = "1"
    extensions: List[AdminExtension]


# --- PATCH /api/admin/extensions/{plugin_id}：运行时开关写入 -----------------
#
# 仅 `trust == "deployment"` 且已装载的插件可开关；builtin、未装载的 id 一律
# 404（路由层用「已装载 deployment 插件 id 集合」挡在 store 之前）。空路径段
# （`.../admin/extensions/` 后面什么都不跟）根本到不了这条路由——`{plugin_id}`
# 是 FastAPI 的非空路径参数，框架自己就 404 了，从不会进这个处理函数；store 自
# 己对空/纯空白 plugin_id 的校验因此在这条路由下不可达，只是纯粹的防御纵深。
# 写成功后返回刚落库的行——与 GET 合成出的三字段同名同形，前端可以直接
# 用响应更新那一行,不必重新拉一次列表。


class AdminExtensionRuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AdminExtensionRuntimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    runtime_enabled: bool
    runtime_updated_by: str
    runtime_updated_at: str
