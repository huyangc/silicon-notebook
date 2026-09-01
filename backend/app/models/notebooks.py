from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.groups import GrantedGroupRef
from app.models.kg import KgBuildJobStatus


class NotebookCreate(BaseModel):
    name: str = "Untitled notebook"
    purpose: str = ""
    primary_domain: str = "Semiconductor"
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)
    access_scope: str = ""
    template: str = ""  # optional template id to apply preset defaults (§6.2)


class NotebookUpdate(BaseModel):
    # Notebook lifecycle states (notably the internal ``copying`` sentinel)
    # are repository-owned and must never be writable through the public API.
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    purpose: Optional[str] = None
    primary_domain: Optional[str] = None
    target_users: Optional[str] = None
    expected_questions: Optional[List[str]] = None
    source_types: Optional[List[str]] = None
    taxonomy: Optional[List[str]] = None
    access_scope: Optional[str] = None


class IndexingPipelineOptionResponse(BaseModel):
    """Sanitized deployment option; never exposes loader or availability internals."""

    pipeline_id: Optional[str] = None
    label: str
    description: str
    version: str
    overrides_chunking: bool = False
    overrides_kg_extraction: bool = False
    available: bool = True
    selected: bool = False


class IndexingPipelineResponse(BaseModel):
    pipeline_id: Optional[str] = None
    version: str
    available: bool
    missing: bool
    pending: bool
    options: List[IndexingPipelineOptionResponse] = Field(default_factory=list)
    changed: bool = False
    warning_count: int = 0
    rebuild_status: str = "idle"
    job_id: Optional[str] = None


class SetIndexingPipelineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pipeline_id: Optional[str] = None


class NotebookRef(BaseModel):
    """轻量 notebook 引用 —— 参考库挂载相关接口共用。"""
    id: str
    name: str
    tier: str = "personal"


class MountedBase(NotebookRef):
    """一条挂载边。active=False 表示边还在但当前不生效(被挂库易主 / 公共库被降级),
    前端须置灰并说明,不能假装它还在工作。"""
    active: bool = True
    inactive_reason: str = ""


class MountableNotebook(NotebookRef):
    """`GET /notebooks/{id}/mountable` 的一个候选。

    `origin` 说的是「这个候选**凭什么**能挂」,取三值之一:

    * `base`   —— 公共知识库(`tier='base'`);
    * `mine`   —— 与挂载方同 owner 的库;
    * `shared` —— 其余,即挂载方 owner 经只读共享或群组授权读得到的库。

    存在的理由是**界面上的一句事实**:群组知识共享 P1 把「读权 ⇒ 可挂载」放开之后,
    挂载选择器里开始出现别人 owner 的库,而前端只有 `tier` 可分,只能把它们归进
    「我的笔记本」——一句错话。三值由 `MOUNT_VALID_EXPR` 已有的列信息投影而来
    (`tier` 与 `created_by` 比较),不新增任何查询。

    ⚠ 刻意**新开一个模型**而不是给 `NotebookRef` 加字段:后者同时是 `MountedBase`
    的基类,而挂载边那条查询并不计算这个标志,加上去只会多一个恒为默认值的字段。

    优先级 base → mine → shared:自己 owner 的公共知识库判成 `base`,与本字段出现
    之前前端按 `tier` 分组的结果逐字一致(改的只是新准入的那批行的归属)。
    """
    origin: str = ""


class NotebookSummary(BaseModel):
    id: str
    name: str
    purpose: str
    primary_domain: str
    status: str
    counts: Dict[str, int]
    created_label: str = ""
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)
    access_scope: str = ""
    # Two-tier federation: 'base' = authoritative reference KG (analog textbook),
    # 'personal' = user notes (default). Drives tier-weighted relevance + conflict
    # precedence in ask().
    tier: str = "personal"
    # 该 notebook 是否已构建知识图谱（有任意 knowledge_objects）。
    # 驱动前端严格推理(reasoning/graph)的可用门控。
    kg_ready: bool = False
    # 该 notebook 此刻是否正在构建/重抽 KG（进程内内存标志，get_notebook 实时回填）。
    # 前端刷新/切库后据此把「构建中…」进度接回（后台 daemon 线程本就在跑）。
    kg_building: bool = False
    # 最近一次 KG 构建任务的持久化状态；仅单笔记本详情投影，列表保持 None
    # 以避免逐笔记本附加查询。
    kg_build: Optional[KgBuildJobStatus] = None
    # 该 notebook 此刻是否正在跑论文元数据补抽（进程内内存标志，get_notebook 实时
    # 回填，镜像 kg_building 的 wiring）。重启即 False——补抽本身幂等可重触发。
    paper_meta_backfilling: bool = False
    # 该 notebook 是否**存在**缺论文元数据的合规候选源(口径 = SourceStore.
    # sources_missing_paper_meta 的 WHERE,即「补全论文信息」真正会排队的那批)。
    # 驱动前端只在有活可干时显示该按钮。三值:None = 未计算(列表投影与旧后端,
    # 前端按旧行为继续显示按钮,遵循「未回填时逐字复现字段存在之前的行为」);
    # 仅单库 get() 经 EXISTS 探针精确回填 True/False。
    paper_meta_missing: Optional[bool] = None
    # 本 notebook 挂载的参考库中是否有任一已建 KG。即便本 notebook 无图,挂了有图的
    # 参考库也可进行严格推理(reasoning/graph)。未挂载 → False。
    base_kg_available: bool = False
    # 上面那个聚合布尔的**分解**:挂载的参考库里,**哪几个**已建 KG。
    #
    # 存在的理由:参考库现在可以按库取消勾选,而 base_kg_available 是 any(...) 之后的
    # 单个布尔,分不出「这次勾选的库里有没有带图的」。本库无图、用户又恰好取消勾选了
    # 唯一带图的那个参考库时,聚合布尔仍为真——前端门控(requiresKg)会放行一个这轮
    # 根本取不到图的模式,「将借用参考库《X》推理」还会点名一个本轮不参与的库。后端的
    # KG 可用性闸早已按库维度收窄(见 docs/product-and-api*.md 对应契约),
    # 界面必须能做同样的判断,故把那批 id 原样下发。
    #
    # 零新增查询:mounted_bases_row 的**每一行本来就带 has_kg**,base_kg_available 只是
    # 把它 any(...) 掉了;这里带出的是同一批行的另一种投影。
    #
    # ⚠ 与 base_kg_available 必须**自洽**:非空 ⟺ 为真。两者出自同一次读取的同一批行,
    # 因而构造性成立(test_notebook_ask_availability 反向钉住)。回填路径也与
    # base_kg_available **逐字相同**(都在 from_row 里算,故列表投影同样有值)——它不是
    # 谁的补充信号,而是同一份事实的两种形状,分叉即矛盾。
    #
    # ⚠ 刻意**不**放在 NotebookRef 上:那个模型同时是 /notebooks/{id}/mountable 的响应
    # 模型、也是 MountedBase 的基类,那两处并不计算这个标志,加上去等于在两个活端点上
    # 永久下发一个恒假字段。
    base_kg_notebook_ids: List[str] = Field(default_factory=list)
    # 本 notebook 挂载的参考库列表(0..N)。基准库不再全局唯一,也不再隐式参与检索。
    base_notebooks: List[NotebookRef] = Field(default_factory=list)
    # 该 notebook 在**任一可用问答模式下能否产出有据回答**:有可见来源、或任意 chunk
    # (含 knowhow 格子——它没有可见来源却可检索)、或已建 KG、或挂载参考库有 KG、或该用户
    # 有 confirmed memory,任一即为真。前端据此禁用"空库"的问答输入框——判定所需的隐藏
    # knowhow chunk、confirmed memory 与 base+overlay 配置前端都看不到,故由后端权威计算。
    # 仅单库 get() 精确回填;列表投影恒为默认 True(列表不消费此字段,永不误禁)。
    ask_available: bool = True
    # ask_available 的**本地那一半**:上面四个判据里去掉「挂载参考库有 KG」之后剩下的
    # 三个(任意 chunk[含 knowhow 格子]、本地可用 KG、该用户在本库的 confirmed memory)。
    # 存在的理由:ask_available 是合并后的单个布尔,分不出「有得可搜」是本地撑起来的还是
    # 参考库撑起来的;而「本次请求把参考库全部取消勾选后还剩不剩东西」恰恰只能问本地那半。
    # 少了它,后端只能退化成拿**可见导入来源数**代表本地证据宇宙,于是一个只有 Knowhow 表
    # (或只有已确认 Memory)的库会被误判成空范围:那些证据没有可见来源行。
    # 默认 **False** —— 与 ask_available 的默认 True 方向相反,但遵循的是同一条规则:
    # 「未回填时逐字复现该字段存在之前的行为」。消费侧写成「local_evidence_available 或
    # 可见来源数>0」,所以 False 就等于回落到旧判据、只增不减;默认 True 反而会让列表投影
    # 凭空放行一次空范围检索。同 ask_available:仅单库 get() 精确回填,列表投影保持默认。
    local_evidence_available: bool = False
    # 已解析但尚未抽取 KG 的 source 数,驱动前端「补抽 N 篇」
    kg_pending_sources: int = 0
    # Phase 2 只读共享:本用户对该库的访问权。"owner" = 自有(可写);
    # "reader" = 经只读共享或群组授权可读(仅读)。默认 owner 向后兼容。
    #
    # **列表与详情两条路径都回填**(群组知识共享 P1-T4)。此前只有列表回填,详情恒为
    # 模型默认的 "owner",于是工作区顶栏那整段 reader 分支对着详情响应从来没为真过。
    access: str = "owner"
    # reader 时 = 原 owner 的用户名(前端展示「来自 X」);owner 时空串。
    shared_from: str = ""
    # 这本库是**经哪几个群组**共享给我的(群组知识共享 P1)。非空 ⇒ 它属于列表的
    # 「群组」分区,前端据此标注「来自群组《X》」。
    #
    # 为什么另加字段而不给 `access` 加一个枚举值(已定裁决 7):`access` 是**权限档**
    # ——群组成员拿到的就是 reader 那一档,行为逐字相同(隐藏写按钮、Ask 走同一读
    # 守卫)。新增枚举值会让每个消费 `access` 的旧分支都要重新判一次「这个新值算不算
    # 可读」,而它们本该一个字都不改。来源(经谁共享)是**正交**的一维,所以另开一维。
    #
    # 默认空列表 = 旧行为逐字不变:只读共享进来的库、自有库都不带它。
    #
    # **列表与详情两条路径都回填**(P1-T4 更正):详情页现在是消费方——工作区顶栏要
    # 据它把只读徽章写成「来自群组《X》」,并把「退出共享」换成「由组管理员管理」
    # (那个按钮打的是成员表,对授权边是空操作,点了会假成功)。详情侧只在非 owner
    # 时发一次点查,owner 零新增查询。
    granted_via: List[GrantedGroupRef] = Field(default_factory=list)
    # 本用户对该库是否持有**内容管理权**(群组知识共享 P2,裁决 P2-3)。
    #
    # = owner ∨ 一条 `role='admin'` 的有效授权边,即 `_CAPABILITY_LEVELS` 里 "admin"
    # 那六格(来源写 / 图谱构建 / knowhow 写 / 知识治理写 / 命令目录写 / 共享管理)
    # 的判定结果。前端据此在 `access="reader"` 的库上仍然画出写入口。
    #
    # 为什么另加布尔而不给 `access` 加枚举值(裁决 P2-3,沿用 P1 裁决 7 的同款理由):
    # `access` 是**权限档**,每个消费它的旧分支都按「owner / 非 owner」二分写死;新增
    # 第三个值会让那些分支各自重新判一次「这个新值算不算可读」,而它们本该一个字不改。
    # 「能不能管内容」是正交的一维,所以另开一维。
    #
    # 默认 **False** —— 「未回填时逐字复现该字段存在之前的行为」(旧后端/旧客户端下
    # reader 就是只读)。owner 行恒 True。
    #
    # ⚠ 它是 **UI 信号不是授权判定**:权威永远是后端守卫
    # (`require_notebook_capability` / `notebook_capability_allowed`)。方向上宁可少给
    # ——投影漏判只是少画一个按钮,多判才会让用户点进一个必然 404 的动作。
    can_manage_content: bool = False
    # Sanitized per-notebook indexing selection. These fields are read-only;
    # availability is resolved from the startup-frozen host and never exposes
    # loader paths, capability names, reasons, credentials, or exceptions.
    indexing_pipeline_id: Optional[str] = None
    indexing_pipeline_version: str = "builtin.chunk.v1"
    indexing_pipeline_available: bool = True
    indexing_pipeline_missing: bool = False
    indexing_pipeline_pending: bool = False
    indexing_pipeline_stale: bool = False
    # owner 视角:本 notebook 是否已开启分享(存在有效 share_token 或 notebook_members)。
    # 驱动前端卡片右下角的「已分享」小人徽标(仿 NotebookLM);reader 看到的原库 is_shared
    # 也为 True,但 reader 卡片本身已带「来自 X」不再重复标记。
    is_shared: bool = False
    # owner 的「每笔记本文档数量上限」有效值(个人覆盖 ∪ 全局默认)。仅 get_notebook
    # 详情路径回填真实值;列表投影(list_notebooks)保持 0(前端来源面板只读详情里的它,
    # 配合来源列表 total_count 显示「文档 X / 上限」)。owner 为 admin 时前端另按角色显示
    # 「不限」,不消费此数值。
    document_limit: int = 0


class ShareResponse(BaseModel):
    share_token: str
    copyable: bool
    size: Dict[str, int]


class ShareState(BaseModel):
    """`GET /notebooks/{id}/share` —— **只读**当前的分享链接状态,绝不铸新 token。

    存在的理由(群组知识共享 P1-T4):打开「分享」弹窗本来就会 `POST .../share`,于是
    「只想共享给群组」的用户会顺带被发一条分享链接——一次纯查看的动作产生了持久副作用。
    有了这个端点,弹窗打开走 GET,链接由用户显式点「开启链接分享」时才 POST(那条
    POST 幂等,已有 token 原样返回)。

    没有 token 时 `share_token` 为空串,并且**不计算** copy-stats:那是一次真实的
    规模统计(大库上不便宜),而没有链接的库根本用不到它。`copyable` / `size` 因此
    只在 `share_token` 非空时有意义,消费方必须先判它。
    """

    share_token: str = ""
    copyable: bool = False
    size: Dict[str, int] = Field(default_factory=dict)


class SharedPreview(BaseModel):
    name: str
    owner_display: str
    source_count: int
    node_count: int
    edge_count: int
    source_titles: List[str]
    mode: str
    size: Dict[str, int]


class SharedByMeItem(BaseModel):
    id: str
    name: str
    share_token: str
    mode: str                          # "copy" | "readonly"
    size: Dict[str, int]
    members: List[Dict[str, str]]      # [{username, added_at}];仅 readonly 有值
    # 这本库共享给了几个**不同的**群组(群组知识共享 P1-T4)。总览的范围因此是
    # 「只读共享 ∨ 共享给群组」,与卡片上的「已分享」徽标同一个判据;`share_token`
    # 为空而本字段非 0 的行,就是一条只因群组共享而出现的记录(没有分享链接可发)。
    group_count: int = 0


class NotebookTemplate(BaseModel):
    id: str
    label: str
    purpose: str = ""
    primary_domain: str = "Semiconductor"
    target_users: str = ""
    expected_questions: List[str] = Field(default_factory=list)
    source_types: List[str] = Field(default_factory=list)
    taxonomy: List[str] = Field(default_factory=list)


class NotebookDeleteResponse(BaseModel):
    """202 响应体（批 3·W1 PR-3 §T-2/§5）：CAS tombstone 已提交、删除作业已排入
    后台，实际清理异步进行。前端刻意零改动——`requestVoid` 只看 2xx 就丢弃
    body，`status` 字段只为 API 契约的可读性存在，不驱动任何前端逻辑。"""
    status: str = "deleting"


class SetTierRequest(BaseModel):
    """Payload for POST /notebooks/{id}/tier."""
    tier: str   # "base" | "personal"


class SetBasesRequest(BaseModel):
    """全量替换本 notebook 的参考库挂载集合。空数组 = 取消全部挂载。"""
    base_notebook_ids: List[str] = Field(default_factory=list)


class MountedByCount(BaseModel):
    """有多少笔记本正在把本 notebook 挂为参考库(不论边当前是否生效)——删除确认
    弹窗专用(spec §6):CASCADE 会连同这些边一起清空且不可撤销,用户点删除前必须
    看到影响面。"""
    count: int = 0


class NotebookAnalytics(BaseModel):
    answers_total: int = 0
    feedback_useful: int = 0
    feedback_not_useful: int = 0
    usefulness_rate: float = 0.0
    low_rated_questions: List[str] = Field(default_factory=list)
    knowledge_counts: Dict[str, int] = Field(default_factory=dict)
    source_status_counts: Dict[str, int] = Field(default_factory=dict)
    paper_meta_counts: Dict[str, int] = Field(default_factory=dict)
