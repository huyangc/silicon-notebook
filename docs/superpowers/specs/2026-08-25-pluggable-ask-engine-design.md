# 方案：可插拔问答引擎（`ask.engine` 扩展点 + 预范围检索端口）

状态：已实现并完成专项验证（2026-08-25）；完整行为与数值合同已同步到产品/API和部署文档。
上游背景：插件化 X 路线的延后项 **X3（generic retrieval access map，闭合 P1-1/P1-2）**
与 X6（"有真实消费者才重开扩展点"）——本方案就是那个真实消费者：内网部署的自定义检索
管线要以插件形态接入公网仓库（零补丁验收标准照 X 路线原文），并让用户在界面上**选择**
用哪条管线回答。

## 一、目标与非目标

**目标**

1. 部署插件可以注册**额外的问答引擎**（一条完整的检索→合成管线），出现在高级模式的
   引擎切换里，用户逐问选择；内建三引擎（`chunk`/`reasoning`/`graph`）逐字不变。
2. 插件引擎经**核心预先套好范围谓词的窄检索端口**取证据——`source_scope`/`base_scope`/
   私有 Memory 隔离是安全边界，靠端口的 SQL 谓词兑现，不靠插件自觉。
3. 引用、持久化、公开分享投影全部由核心拥有：插件只提出「答案 + 它引用了哪些证据句柄」，
   核心校验后按既有 `kN` 纪律装配 `AnswerAnchor`/`Citation`。

**非目标（v1 登记，不是遗漏）**

- **不流式**：插件引擎 `streaming=False`，走 graph 模式同款「普通等待文案 + 完成后展示
  持久化轨迹」。流式轨迹合同（NDJSON 事件序、前端实时面板）留 v2。
- **不接意图预检**：插件引擎请求走 chunk 模式同款直达路径，不经 `/ask/intent`。
- **不给会话历史**：v1 上下文只有当前问题。多轮上下文的披露面需要单独裁决（历史答案
  含全库证据），留 v2。
- **不开 KG 端口**：v1 检索端口只有 chunk 检索 + 元素点查。KG 邻域/PPR 端口特权面大，
  等第一个真实消费者提需求再开（X6 原则）。
- **MCP `ask_notebook` 与深度报告不接插件引擎**；浏览器面独占。
- 自动界面模式（`ui_mode=auto`）隐藏引擎子切换并强制默认引擎——现行红线不动，插件引擎
  因此自动只在高级模式可见，不需要新的门。

## 二、SDK 契约（`backend/app/extension_sdk/ask.py` 扩充）

新扩展点 `ask.engine`，contribution kind = `PROVIDER`，一条 contribution = 一个引擎。

```python
@dataclass(frozen=True)
class AskEngineDescriptor:
    mode_id: str          # 必须以「本插件 id + "."」开头，见 §四 冲突规则
    label: str            # 引擎名（用户可见，部署方自撰）
    description: str      # 一句说明（引擎切换里的 desc）
    requires_kg: bool     # 镜像 AskMode.requires_kg，前端 canUseMode 消费

@dataclass(frozen=True)
class AskEngineContext:
    # 实现期修订(codex #602 R12 P2):notebook/actor 引用已从上下文删除——
    # 稳定身份 id 交给插件只会打开跨 run 关联的口子,范围与归属全部由核心在
    # 端口构造时预绑定;provider 只收当前问题与四个端口。
    question: str
    cancellation: CancellationToken

class AskEngineProvider(Protocol):
    descriptor: AskEngineDescriptor
    def answer(
        self,
        context: AskEngineContext,
        retrieval: RetrievalAccessPort,
        model: EngineModelPort,
        trace: EngineTraceSink,
    ) -> AskEngineResult: ...

@dataclass(frozen=True)
class EngineEvidence:
    evidence_key: str     # 服务端铸造、run 内唯一的不透明句柄——引用的唯一货币
    text: str             # 有界摘录（核心裁剪后）
    source_title: str     # 展示名（source_display_title 口径）
    location_label: str   # 位置界面词（页码/章节）

@dataclass(frozen=True)
class AskEngineResult:
    answer_markdown: str          # 正文；锚点写 [k1] 形式，k 序号指向 citations 下标+1
    citations: tuple[str, ...]    # 逐条是本 run 发出过的 evidence_key
```

要点：

- `EngineEvidence` **不带** `source_id`/`element_id`/`notebook_id`/`chunk` 任何可寻址
  id——插件按 `evidence_key` 引用，核心在自己的 run 账本里保有 key→(element, source)
  的权威映射。插件既不需要也不应该拿到内部 id（与公开分享投影同一条论证：给 id 只是
  让它去探测别的接口）。
- 端口/结果类型全部 frozen dataclass + Protocol，SDK 保持零 repository/settings/
  transport import（`contracts.py` 现行纪律）。

## 三、预范围检索端口（X3 的落地形态）

```python
class RetrievalAccessPort(Protocol):
    def search(self, query: str, k: int) -> tuple[EngineEvidence, ...]: ...
    def fetch(self, evidence_key: str) -> EngineEvidence | None: ...
```

实现（core-owned，`backend/app/extensions/` 新 host 模块）绑定在构造时冻结的
`(retrieval run, source scope, base scope, actor)` 上，每个方法：

1. **范围谓词在 SQL 里、LIMIT 之前**——复用 `source_scope_context` 下推的同一批既有
   接缝（chunk ANN 的来源 sidecar 过滤、降级 FTS 的有界来源内检索），私有 Memory 的
   `created_by` 归属谓词照 Memory 检索的同一条判据写在取数 SQL 里。**不新写第二份范围
   拼写**——端口是既有 scoped 候选生成的窄包装，不是并行实现。
2. **占用共享 leaf-I/O 闸**：`search` 是 leaf，按既有 retrieval run 的 semaphore 语义
   拿槽；拿槽前后各查一次取消。
3. **k 有上限**：`min(k, 部署上限)`，上限进 `Settings`（带校验），精确数值只登记
   `docs/product-and-api*.md`。每 run 的 `search` 调用次数同样有部署上限，超限抛给
   插件一个稳定错误（不是静默空结果）。
4. **embedding 走 run 的 single-flight**：同一 run 内相同 query 的向量只算一次
   （既有纪律照搬）。
5. **证据句柄 run 内铸造**：`evidence_key` 用 run 局部序号（如 `pe1`、`pe2`），与
   `k5001+` 清单命名空间同思路但独立段；核心账本记 key→(element_id, source_id,
   notebook_id, 摘录)。`fetch` 只认本 run 发过的 key。

## 四、模型端口

```python
class EngineModelPort(Protocol):
    def complete(self, prompt: str) -> str: ...
```

- 绑定新 chat workload `plugin_engine`（一个 workload，事件/日志按 `mode_id` 打点区分
  插件），走既有 model registry/scheduler/circuit breaker——部署在 model-services TOML
  里照常路由与热加载；thinking 默认关闭（照「其余 chat workload 全部关闭」现行口径）。
- 不进 LLM 响应缓存（不传 `response_validator`，照「缓存是 opt-in」纪律）。
- prompt 长度与每 run 调用次数有部署上限（`Settings` 校验，数值登记进
  `docs/product-and-api*.md`）；超限稳定错误。
- **为什么给而不是让插件自带 LLM 客户端**：引擎没有合成能力就产不出答案；让每个内网
  插件自带 OpenAI 客户端等于把核心已经解决的配置/观测/熔断问题在插件里重新发明一遍。
  端口不是 raw client——插件拿不到 base_url/key/物理路由，观测照常落核心 LLM 日志。
  插件想调自己的外部服务仍然可以（走自己的 settings，如 arXiv 样板先例），两条路不互斥。

## 五、引用准入与持久化（核心独占）

1. 插件返回后，核心解析 `answer_markdown` 里的 `[k]` 标记（复用既有解析口径，含
   `【k】`兼容），按 `citations` 下标映射回 `evidence_key`。
2. **准入 fail-closed**：任一被引用的 key 不是本 run 发出过的 → 整份结果拒绝，落成
   带稳定错误码的失败回答（用户可见「插件引擎返回了无法核验的引用」一类 `user_error()`
   文案）。不做「剥掉坏锚点继续用」——那会把插件 bug 洗成看起来有据的答案。
3. 通过准入的引用由核心从**自己的账本**装配 `Citation`/`AnswerAnchor`（标题/位置/摘录
   取核心记录，不信插件转述），按标准 `kN` 重编号；`CitationImage` 附图、Memory 保存的
   引用可追溯、公开分享白名单投影全部自动继承既有行为。
4. 持久化走既有答案落存收口（`mode` 字段存插件 mode_id）；`answered_at`、提问时间、
   会话历史即时入历史等契约不变。轨迹按 §六持久化。
5. **身份复核**：落存前按请求冻结的 notebook/question/conversation/user/job 逐字段
   复核（镜像 `ResponseDraftStage` 提交边界的同款检查）——插件不能把答案写进别的
   job/会话/笔记本。

## 六、轨迹

- `EngineTraceSink.step(label: str, detail: str = "")`：条数、label/detail 长度均有硬顶
  （数值登记 `docs/product-and-api*.md`），超出静默丢弃尾部并在末条披露截断。
- 步类型统一 `plugin`，前端 `getTraceStepDetail` 加一个通用渲染分支（label 原样、无
  特化图标）；插件是 deployment 信任档，label 内容不做语义审查，长度闸 + 部署自担。
- v1 非流式：轨迹随答案持久化后展示（graph 模式同款），实时面板判据 `streamsTrace`
  已按引擎判，不需要新逻辑。

## 七、注册与派发

1. **启动冻结**：registry 收 `ask.engine` providers，校验：
   - `mode_id` 形状 = `^<plugin_id>\.` 前缀 + 稳定 id 字符集。**前缀即冲突证明**：内建
     id（`chunk`/`reasoning`/`graph`）与退役 id（`fast`/`global`）都不含 `.`，插件 id
     之间由插件 id 唯一性隔离——不需要维护第二份保留字清单。
   - descriptor 字段非空、长度有界。
   - 违规 = 启动失败（`ExtensionDiscoveryError` 同款 fail-closed + 脱敏纪律）。
2. **`resolve_mode` 两段查**：先内建 `ASK_MODES`（含 `_RETIRED_MODES` 窄例外，逐字
   不变），未命中再查冻结的插件表；仍未命中 → `UnknownAskMode` → 422（现行行为）。
   插件后来被移出配置：新提问 422（文案点明「该引擎已停用」），历史会话前端
   `modeFromTurn` 对未知 id 已回落默认引擎——两侧都不需要新机制，登记即可。
3. **派发**：插件引擎合成 `AskMode(id, handler="ask_plugin_engine", group="extension",
   streaming=False, requires_kg=descriptor.requires_kg, user_facing=True)`；facade 按
   handler 名派发到新收口方法，该方法：建独立 retrieval run（新 run kind，非空 actor
   校验照既有纪律）→ API 层已冻结的 scope 原样下传 → 构造三端口 → 同步调用
   `provider.answer()`（请求线程内，取消经 token 协作；**不做**额外硬 deadline——内建
   reasoning 也可跑分钟级，插件是 deployment 信任档，挂死语义与解析器链一致，登记）→
   §五准入落存。
4. **可用性**：`ask_available`/空库闸对插件引擎与 chunk 同口径；`requires_kg=True` 的
   插件引擎由前端 `canUseMode` 既有判据关闭。

## 八、API 与前端

1. `GET /ask-modes`（已存在）扩为投影端点：内建三条照旧从 `ASK_MODES` 派生，插件条目
   追加 `{id, group: "extension", label, desc, requires_kg, streams_trace: false}`。
   **不下发** plugin 内部结构/endpoint/异常（UI 投影现行脱敏口径）。
2. 前端 `frontend/app/ask-modes.ts` 仍是**内建**三引擎的静态真源（`scripts/
   check_ask_modes_contract.py` 锁同步逐字保留）；插件条目改为数据驱动：workspace 提交
   后与 `/system/extensions` 同节奏取一次 `/ask-modes`，合并进引擎切换的第三分组
   （分组界面词待定，候选「扩展功能」，**必须过 `scripts/check_ui_vocabulary.py`**）。
   `AskModeId` 收窄类型只覆盖内建；状态里 mode 放宽为 string，内建逻辑仍走字面量联合。
   取数失败 = 只显示内建三条（fail-open，不挡内建问答），下次 workspace 提交重试
   （`/system/extensions` 失败重试同款）。
3. 恢复语义：`modeFromTurn` 对「在已取回插件清单里」的 id 精确恢复，否则回落默认。
4. 合同脚本 `check_ask_modes_contract.py` 增一条：前端不得出现内建三条之外的 mode
   字面量（插件段必须数据驱动，防止有人把某个插件 id 写死进前端）。

## 九、安全不变量（逐条是验收断言）

1. 范围谓词（source/base/私有 Memory）在端口 SQL 里、LIMIT 前生效；受限 run 下插件
   一次也查不到范围外行（测试：共享库两成员、私有 Memory 越权读为零）。
2. 插件拿不到 repository/`Settings`/model raw client/连接/`evidence` 之外的任何
   可寻址 id；`EngineEvidence` 字段集合被守卫钉死（新增字段过评审）。
3. 引用准入 fail-closed：未发行 key → 整份拒绝（测试含「伪造 key」「跨 run 重放 key」）。
4. 落存身份复核：篡改 notebook/job 的注入实现被 `StageBoundaryError` 同款拒绝。
5. 观测事件只含 mode_id/plugin_id/阶段/耗时/计数，无问题/标题/证据文本（照检索观测
   现行口径）。
6. 公开会话分享投影对插件引擎答案不多带任何新字段（`mode` 本就不跨出去）。

## 十、分期与守卫

- **PR-α（后端）**：SDK 契约 + registry 校验/冻结 + `ask_plugin_engine` 收口 + 三端口
  实现 + 准入 + 测试用 fixture 引擎。守卫：mode_id 前缀规则、端口 SQL 范围断言（两成员
  共享库用例）、准入 fail-closed、身份复核、架构守卫（插件实现不得 import 具体
  repository/facade——现有守卫自动覆盖新点）。
- **PR-β（前端 + API 投影）**：`/ask-modes` 插件段 + 引擎切换第三分组 + 恢复/回落 +
  合同脚本扩条 + 组件测试。全栈对等在 β 收口（α 期间无 user_facing 插件引擎注册进
  生产部署，不构成「只做一侧」）。
- **PR-γ（可选）**：arXiv 样板加一个演示引擎（端口直查 + 模型端口合成的极简管线），
  作为 SOP 可运行范例；出厂关闭口径照样板现行约定。
- 文档同步（各 PR 内）：`docs/deployment-extensions-sop*.md` 扩展点表 + 作者指南、
  `docs/product-and-api*.md` 数值上限、四份根文档按现行同步口径。

## 十一、已裁决的开放问题（实现时不再讨论）

| 问题 | 裁决 |
| --- | --- |
| 引擎分组的界面词 | 实现时过词汇守卫定稿，候选「扩展功能」；不许裸 `plugin`/`extension` 上屏 |
| 会话历史是否给插件 | v1 不给，登记 v2 |
| KG/PPR 端口 | v1 不开，第一个真实消费者提需求再开 |
| 插件引擎挂死 | 与解析器链同口径（deployment 信任档），不加硬 deadline，登记 |
| 意图预检/流式 | v1 均不接，登记 v2 |
| 模型访问 | 核心出窄 `complete` 端口（workload=`plugin_engine`），插件自带外呼仍允许 |
