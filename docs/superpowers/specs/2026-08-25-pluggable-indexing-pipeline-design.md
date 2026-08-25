# 方案：可插拔索引管线（按笔记本选择的 chunking / KG 抽取策略）

状态：已完成（2026-08-25）。PR-1 的选择、写入闸与全库 chunk 计划，以及 PR-2 的
prompt/mapper 窄端口、证据/schema 准入和 durable job 终态均已落地。SQLite/PostgreSQL
现在把本次 generation 的逐来源 chunk、KG、来源事实、抽取结果与向量先写入不可见的 durable
notebook stage；模型与 embedding I/O 全部在发布事务之外。所有可见来源完成后，核心在一个
事务里复核 running job、selection generation、精确来源/元素 snapshot、stage 完整性与引用闭包，
再原子替换可见来源产物、失效整本派生 KG、发布 `(pipeline_id, pipeline_version)` identity、
清 job authority 并写入 `succeeded`。失败、取消、启动恢复或迟到 generation 只删除 stage，
旧 chunks、KG、来源事实、抽取历史、parser 状态与 published identity 均保持不变；隐藏
Memory/Knowhow 产物不参加替换。

## 一、目标与边界（用户已拍板的三条）

1. **选择粒度是笔记本**：索引产物整库共享，「用户可选」只能落成**按笔记本选一条管线**，
   选择冻结进产物代次。不做按来源/按提问的索引选择。
2. **parser 不进用户可选范围**：解析路由自动选择、刻意不向用户展示是已登记的产品裁决
   （解析能力注册表条目），本方案不推翻。插件想扩解析格式走既有 Parser ProviderChain
   （deployment 级、非用户可选），与本方案正交。
3. **插件拥有策略，核心拥有 schema**：插件产出必须落进既有的
   elements/chunks/embeddings/KG 持久化契约——下游全部消费契约（chunk ANN 来源
   sidecar、element→chunk 反查、KG 边契约、页边界去重、引用接地、正向 shadow 复制）
   都假设这些形状，让插件另立 schema 是不归路。**embedding 不可插拔**（模型选择是部署
   配置、截断共用既有真源；插件换 embedding 等于把在线/批处理/回填三路一起分叉）。

## 二、扩展点形状

新扩展点 `indexing.pipeline`，contribution kind = `CONTRIBUTOR`；一条 contribution = 一条
可选管线，descriptor：

```python
@dataclass(frozen=True)
class IndexingPipelineDescriptor:
    pipeline_id: str        # ^<plugin_id>\. 前缀（复用 ask.engine 的冲突规则）
    label: str              # 用户可见管线名
    description: str
    version: str            # 进产物代次身份，见 §四
    overrides_chunking: bool
    overrides_kg_extraction: bool   # 至少一项为 True
```

两段策略各自的端口（插件可只实现声明的那段，未覆盖段走内建）：

1. **Chunking 策略**（纯计算，零模型零 I/O）：
   - 输入：某来源的不可变解析元素视图（有界流式遍历，顺序 = 元素序），只读。
   - 输出：chunk 提案序列——每条 `(text, element_ids, section 元数据)`，形状即
     `replace_source_chunks` 的入参契约。
   - 核心负责：提案有界性校验（条数/单条长度部署上限）、写入 `chunks` +
     **同一写事务**维护 `chunk_elements` 反查行、embedding、FTS/ANN 索引、页边界
     去重（去重发生在插件提案之后、发布之前，语义不变）。畸形提案整来源 fail-closed
     回内建 chunker + 可见 `parse_quality_warning` 同级提示，不静默混用。
2. **KG 抽取策略**（刻意收窄为「prompt 构造 + 响应映射」，不是完整抽取器）：
   - 插件提供：窗口 prompt 模板构造器 + 模型响应→对象/边提案的映射器。
   - 核心保留：窗口切分、模型调用（`kg_extract` workload、熔断、durable
     `kg_build_jobs` 单飞/探活/排空协议）、**准入**——对象/边必须落在
     `edge_schema` 四类端点 + 管理员扩展类型内，来源事实与证据绑定同事务发布、
     代次校验、fail-closed 规则逐字继承。插件提案不具权威性（「插件 value 不具
     权威性，畸形字段 fail-open」现行纪律）。
   - 为什么不给完整抽取器：窗口/成本/重试/单飞是核心花了多轮才钉住的运维契约，
     交出去等于每个插件重新踩一遍；内网「自定义 KG 生成」的真实差异在 prompt 与
     类型映射，不在调度。

## 三、选择与权限

- `notebooks` 加可空列 `indexing_pipeline`（NULL = 内建；追加迁移 + bump
  `SCHEMA_VERSION`，双后端同修，正向 shadow 只加列不加表/FK/唯一面——迁移编号以
  动工时的当前版本为准，不在本文预写）。
- 选择入口：笔记本设置区新增「索引管线」选择（只列 descriptor 的 label/description，
  不泄漏模块路径/endpoint）；能力档位 = **admin 档**（与内容六格同档：换管线是内容
  管理动作，不是 owner 独占的对外处置——与 `notebook:configure` 的安全论证无关），
  经 `require_notebook_capability` 声明，只读成员只见当前值。
- 换选流程：确认弹窗写明「将重建全库索引」→ 落列 → 标记重建待执行 → 复用
  `mode="rebuild"` 全量重抽 + 检索索引重建的既有 job 链。**重建完成前检索继续用旧
  产物**（可用性优先于纯净，登记取舍）；重建中的单飞/终态纪律照 `kg_build_jobs` 现行。
- 深拷贝：副本继承 `indexing_pipeline` 列值；目标部署缺该插件时按 §四的缺席规则处理。

## 四、代次身份与缺席语义

1. 产物身份带管线：KG 抽取记录与 `unified_kg_state`、检索 scale manifest identity 增
  `(pipeline_id, pipeline_version)` 维度；身份不匹配 = 产物过期，消费方走各自既有的
  降级路径（缺 sidecar → 有界 FTS 一类），**绝不混用两条管线的产物回答同一问题**。
2. `pipeline_version` 变更语义 = `CLUSTER_ALGO_VERSION` 同款：插件改策略语义必须
  bump，核心据此判定重建需要。
3. **插件缺席 fail-closed**：配置移除插件后，选了该管线的笔记本——读路径不受影响
  （产物是核心 schema，照常可查）；**新的**解析/增量抽取/重建 409，文案点明「该索引
  管线已停用，改回内建将重建」，附一键改回。不做静默回落内建（那会在同一库里混出两代
  管线的产物，正是 §四.1 要防的）。

## 五、安全与观测不变量

1. chunking 端口只见单来源元素视图，拿不到 repository/连接/其他来源/其他笔记本。
2. KG 策略端口只见窗口文本与既有类型注册表投影，模型调用在核心手里。
3. 观测事件只含 pipeline_id/阶段/计数/耗时，无正文（现行口径）。
4. 提案有界性上限全部进 `Settings` 校验，数值只登记 `docs/product-and-api*.md`。

## 六、分期

- **PR-1**：扩展点 + chunking 策略 + notebook 列/设置 UI + 代次身份 + 换选重建流
  （全栈对等一次交付）。
- **PR-2**：KG 抽取策略段（prompt/映射端口 + 准入接线）。
- 每 PR 文档同步照现行口径；样板插件是否加演示管线视 3a 的 PR-γ 结论跟随。

## 七、已裁决的开放问题

| 问题 | 裁决 |
| --- | --- |
| parser 用户可选 | 不做（维持已登记裁决） |
| embedding 可插拔 | 不做（配置真源分叉风险） |
| 换选粒度 | 笔记本级；重建期间用旧产物 |
| 插件缺席 | 读不受影响、写 409 + 一键改回内建，不静默回落 |
| 完整 KG 抽取器外放 | 收窄为 prompt+映射策略，调度/准入恒归核心 |
