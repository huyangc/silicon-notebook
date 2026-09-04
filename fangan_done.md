# silicon-notebook 方案已完成情况

更新日期：2026-08-31

对照依据：`silicon_notebook_fangan.md`（产品方案）。

## 总体状态

实现已经从早期“demo 表演”阶段进入**真实本机 beta 闭环**。核心链路对任意用户创建的 notebook 都用其真实上传内容工作，不再依赖单一硬编码 demo notebook：

```text
创建 notebook
-> 上传 PDF / Markdown / DOCX / PPTX / XLSX / XLSM / XLS source（异步处理）
-> 保存原始文件
-> 解析为 source elements（元素级 + location_label）
-> 生成 source summary
-> 系统绑定的 KG workload 可用时抽取 Concept / Claim / Formula / Procedure KG 对象（带 evidence 绑定与关系边）
-> 通用知识库浏览 / 状态治理 / 合并 / 冲突检测（旧候选治理端点保留兼容）
-> 混合检索（关键词 + 向量余弦，含 payload 级向量）
-> KG-native Ask（chunk / graph / reasoning，带 citation 校验）
-> Knowledge 浏览与治理
-> Ask 回答手动保存 / Agent candidate 审核 -> 私有 notebook-bound Memory
-> confirmed Memory 融入 Ask / notebook 搜索 / Deep Report
-> scoped Agent token -> Streamable HTTP MCP 使用 notebook 知识与候选记忆
-> Deep Report 两阶段规划 / 生成 / 导出
-> 用户反馈 useful / not useful
```

LLM 未配置时，摘要与回答退化为 deterministic fallback；解析仍完整执行，KG 抽取阶段记录 `error_message='no-llm'`，不再离线伪造启发式候选知识。离线 smoke 在需要验证检索/治理时会显式写入 KG/rule 对象。

本文是按时间累计的实现账本。标为「历史记录 / 已退役」的 Scenario、Case、Checklist、Article Studio 与派生规则材料只说明过去曾有实现，不代表当前 endpoint、表或 UI 仍存在；当前行为以本页顶部快照、代码和绿色测试为准。

## 1. 产品与项目基础

- 产品名和项目名统一为 `silicon-notebook`。
- 已初始化 Git 仓库，远端：`git@gitee.com:justkitt/silicon-notebook.git`。
- 第一版不使用 Docker。
- 后端统一使用本机 Miniconda Python：`/opt/homebrew/Caskroom/miniconda/base/bin/python`。
- 维护文档：`AGENTS.md`、`README.md`、`README_zh.md`、`.env.example`。

## 2. 技术架构基础

- 后端使用 Python FastAPI。
- 前端为唯一主线：Next.js / React / TypeScript，目录 `frontend/`。**原静态 `web/` fallback 目录已删除**，`scripts/dev.sh` 与 `scripts/check.sh` 已移除相关引用。
- 发行默认持久化为 SQLite：`sqlite:///.local/silicon_notebook.db`（标准库 `sqlite3`）；也可通过同一个 `DATABASE_URL` 直接选择 PostgreSQL 16。
- 原始上传文件保存到 `.local/storage`。
- repository 边界：唯一 factory 在 `SQLiteRepository` / `PostgresRepository` 间原子选择，两者仍保留“组合式 `RepositoryRuntime` 之上的兼容 facade”边界与小型 Protocol；application service 不判断 dialect。store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection。SQLite 旧库兼容由冻结 `repository_v9` fixture、schema golden 与 backup-only `scripts/verify_repository_snapshot.py` 守护；PostgreSQL 使用 checksummed migration、有界 Psycopg pool 与独立 PG16 integration/CI lane。
- 向量检索在 SQLite 使用有界 float32 矩阵/scale index，在 PostgreSQL 以 float32 `bytea` 持久化并通过 `pg_trgm`/`ILIKE` 搜索；不要求 pgvector。切换只改变 active backend，不会复制存量数据，运维 runbook 明确了停写、备份、外部迁移、一致性检查与回滚边界。
- 模型服务由部署者在 `MODEL_SERVICES_CONFIG` 指向的 TOML 中统一声明：物理 chat / embedding / rerank 服务包含协议、endpoint、模型、`api_key_env` 与唯一容量 `max_concurrency`，`.env` 只保存被引用的密钥。
- 所有模型调用按稳定 workload id 取得绑定服务并进入该物理服务的共享调度器；可用性通过 `RuntimeModelProvider.configured(workload_id)` 判定。用户不能保存或覆盖模型配置，配置路径留空时明确进入离线/确定性降级。
- **历史架构债偿还（2026-07-21，非产品功能）**：领域 FastAPI router 由 `app/api/routes.py` 组合，领域 Pydantic model 以 `app/models/schemas.py` compatibility facade 保持旧 import，七个前端 domain API module 共用 `frontend/app/api-client.ts` transport；等价性与 public/domain seam 测试取代 aggregate-private coupling。rebase 到 #315 后，exact PR head 已通过连续两次完整 warm gate，所有 lane 均不超过 60 秒；具体权威秒数记录在 PR 验证区，避免 tracked 文档改动把它们变成上一 SHA 的数据。该门槛是本机测量，CI 时长仍只作观察。workspace-state hook 拆分与 FastAPI lifespan/application lifecycle 仍未完成，保留为独立债务。

## 3. 用户系统与分享

- 当前为多账号 owner 隔离：自助注册/登录使用不透明 Bearer session；管理员可在用户总览授予/撤销其他用户的管理员角色，并共同管理全局 base tier。用户总览默认每页 20 条、可切换 20/50/100 条，并支持按数据列表头对完整用户集合做升降序排序。内置管理员与当前操作账户不可降级。
- 已完成（2026-07-22）：管理员授权管理——`PATCH /api/admin/users/{user_id}/role` 仅接受 `admin/user`，在同一写事务中重验操作者权限；用户使用总览提供二次确认的授予/撤销操作并即时更新角色显示。普通用户越权、内置管理员降级、当前管理员自降级、无效角色与不存在用户均有测试覆盖；已有 session 在下一次请求读取新角色。`scripts/check.sh`（含前端 production build）已通过。
- 分享链接已实现：小 notebook 复制到接收者账号；大 notebook 以只读成员方式加入。当前没有实时协同编辑。用户可在头像菜单自助修改密码（保留当前会话、吊销其他会话），管理员可在用户总览重置用户密码（吊销目标全部会话）；内置 `admin` 的密码仍由部署环境变量决定，两条路径都拒绝它。
- 用户记忆模式字段保留为 `manual`（不做自动记忆）。

## 4. Notebook 创建与管理

- Notebook CRUD API 完整：list / create / get / patch / delete。
- `notebooks` 表持久化，后端重启不丢失。
- 新建默认名 `Untitled notebook`，创建后直接进入来源界面。
- Notebook summary 包含 name / purpose / primary_domain / status / counts / created_label。
- **counts 为真实统计**：来源、当前知识对象类型、conversation 与 report 等统计来自当前数据库，不再依赖已退役内容表或写死 demo 数字。
- 前端集合页：tab 过滤、新建、卡片菜单、编辑、删除、grid/compact/list 预览、最近/名称/来源排序、列表表格视图。

## 5. Notebook 工作区界面

- 两列工作区：左 Source Stack / 右侧主区域；主区域为 问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report) 四个 tab，固定 Studio 右栏已移除。
- 左上角 notebook 名称可编辑保存；左栏显示来源数量、仅显示用户导入文件；网络来源检索保留为 disabled affordance。
- **按来源选择检索范围（§6.5 / §11，2026-08-03；质量修复 2026-08-04；参考库按库勾选 2026-08-04）**：左栏每个可见导入来源新增复选框及全选/清空，默认全选；用户勾选是当前 notebook 检索范围的唯一来源，同时约束问答意图预检与执行、新建深度报告。后端在入口把选择冻结为 include 硬上限，并由服务端按当前可见来源全集计算 `narrowed`，不信任客户端值；因此单篇 notebook 全选或多篇全部选中不再被误判为限定模式，不会错误清空会话历史或显示“已关闭图扩展”。全选运行私下快照当时已有的隐藏 Memory/Knowhow 参与者，不把其 id 暴露或持久化；真正收窄才排除隐藏投影。全选快照冻结可分区候选与结果，并发新增来源不会混入；执行时可见来源或隐藏参与者全集若漂移，无法安全隔离的整图通道在 I/O 前跳过。真正收窄时，chunk scale 索引用逐 ANN 行来源 sidecar 在 HNSW 进入 Top-K 前过滤，并在 hydrate/合成后复核，未选来源不能占候选席位；旧索引缺 sidecar 时只走有界来源内 FTS，重建/fold 后恢复来源内语义召回。KG、PPR 与精确查找等确定性种子结束后若证据仍完全为空，逐步推理会在首次 reflect 前确定性补一次有界原文元素检索；最终仍为零时诚实说明“本次检索未命中”，不再谎称 notebook 没有已审核知识。无法安全预过滤的当前库全图/PPR/关系/精确章节/报告整库画像通道在真正收窄或全选参与者漂移时仍跳过；事后过滤只作防御，不作为图授权。挂载参考库仍保持在参与集，并可从 source-safe KG 种子直接映射 base 原文而不走组合全图；本地全不选且未挂库时，前端置灰问答输入和新建报告，后端对绕过 UI 的请求返回 409。外库/隐藏/失效 source id 返回 422，旧客户端省略 `source_scope` 时保持整库行为；报告把公开范围持久化至 understanding 合同，在意图确认与生成前重验并重新水合私有参与者快照。**第二个维度（参考库按库勾选）**：挂载参考库不再无条件全量参与——每个已挂载参考库整库一个复选框（`base_scope`），与来源维度互相独立、同样默认全选、同样在 API 入口冻结成显式 include 快照并由服务端重算 `narrowed`。两维正交:只取消参考库勾选**不得**让 `source_scope_restricted()` 翻真，当前库的 PPR、私有 Memory、社区报告、弱支撑关系、精确章节与报告整库画像照常开着;跨库通道（集合枚举与集合地图、联邦候选检索、社区扩展、图漫游、`follow_chain`、证据装配、KG 可用性闸）各自认库维度，库级收窄统一发生在参与库解析这一个边界上，保证行与分母同谓词。社区扩展在**取兄弟实体名的入口**收窄（泄漏面包括查询词本身）;`resolve_participants` / `mount_sql.py` 不动（它与权限共用）。图漫游与 PPR 的过滤在遍历/截断之后、整图规模守卫保持 scope 盲，两条二阶代价已登记接受。409 判据改成两维同时为空，Ask 三入口与报告创建/确认/生成三处生效，本地那一维按新增的 `NotebookSummary.local_evidence_available` 判空（Knowhow/已确认 Memory/本地图谱都没有可见来源行）。答案新增只读回执 `AskResponse.retrieval_scope`（库名为授权时刻快照，检索侧从不回读，两维都没收窄时缺席）。
- **所选来源子图检索与质量门控激活（§6.5 / §11，2026-08-04；默认不可见 Shadow 2026-08-05）**：真正收窄到一篇/几篇来源时，Ask `chunk`、`reasoning`、实验 `graph` 与深度报告共用 `SelectedSourceGraphActivationService`，不再显示泛化的“限定来源下已关闭无法安全隔离的图扩展通道”。历史检索 `B` 先完整产出；来源子图 snapshot、邻居/membership、scoped PPR 及超大来源的按来源 partition 只生成独立预算的 `G`，active 也只能按 `B + G` 追加，不能驱逐、重排或重打分 `B`。全范围/全选（含单来源 notebook 全选唯一来源）在 snapshot I/O 前保持历史路径和旧响应形状。默认启用完全不可见的 `shadow`：只评估不改变答案，不进入公开 API/报告字段、轨迹、stream 或 UI，历史持久化的 `source_subgraph` 步骤也在浏览器过滤；完整状态只保留于无正文内部事件。`allowlist`、稳定 hash rollout 与 `on` 仍必须通过 canonical 五场景配对评测、受信无正文 attestation 及精确 corpus/model pin。scope 漂移、越界候选、工件不可用、图失败或 baseline eviction 都整段丢弃 `G`；mounted base 保持独立历史 lane。
- Notebook 顶栏保持紧凑：标题下不再渲染 description，description 在没有对话时进入问答欢迎态；顶部分析工具栏具备横向 overflow 保护，桌面宽度下动作标签不会被截断。
- **来源反查索引部署回填可续跑（§6.5 / §11，2026-08-05）**：SQLite v42 / PostgreSQL v20 增加 notebook 级 `source_index_backfills` 执行账本；`backfill-source-index` 每个有界 keyset 页面在同一个短事务里提交索引行、游标和计数，异常重启从最后已提交页面继续，完成代次直接跳过。`kg_mutation_seq` 漂移先以稳定码失败关闭，保持在线快速路径未发布，并在下次运行按新代次重新构建；账本不保存证据正文或异常文本。SQLite 中断/续跑/代次漂移、旧库 v41→v42 快照迁移与 PostgreSQL 对等契约均有回归测试。
- **所选来源 Shadow 一键部署准备（§6.5 / §11，2026-08-05）**：`scripts/prepare_selected_source_graph.py` 在明确停服、持有中央维护锁的前提下覆盖全部 notebook；指定的既有 env 是权威维护目标，不受 shell 同名变量重定向。脚本顺序续跑来源反查索引和 source-fact 账本，按当前 KG 版本/可见来源数/精确工件格式复验并按需重建 scale/partition 工件，再独立审计来源事实。重复运行会跳过当前有效代次与工件；无正文 receipt 只记录计数、阶段和稳定失败码。任一阶段失败都不改 env；全部数据库/工件检查成功且 repository 关闭后，脚本才在保留原文件 owner/mode/group 的前提下原子启用三个来源图 producer/artifact 开关及不可见 `shadow`，不增加任何 UI 或公开响应字段。
- source card 可打开 source detail，查看元素级文本，支持手动重解析。
- **Excel 专业分析与解析问题只读中心（§6.5.1，2026-08-31）**：用户直接上传的 `.xlsx`、`.xlsm`、`.xls` 在普通文档解析之外，于摄取期生成有界、无公式执行的分析快照；reasoning Ask 仅在问题具有分析意图且冻结范围内存在可用快照时，复用 `reasoning_agent` 做一次 8 秒内的受限计划选择，再由本地白名单执行器完成概览、聚合、排序或筛选并返回带来源定位、覆盖行数和公式缓存提示的结果卡。多数据区、超长单元格、损坏/加密文件、工作表/单元格超限等挑战结构不会静默截断或猜测，而是保留普通文本检索并自动记录独立问题、复制隔离副本；成功重解析自动标记解决并删除副本，来源/笔记本删除立即删除副本并只留脱敏摘要，保留期到期自动彻底清除。管理员用户总览保持原用户表列不变，新增「提问分析」「解析问题」两个页签；「查看提问」进入前者，后者可按用户/状态/类型筛选并在来源仍存在时跳转定位。管理员端严格只读，没有重新解析、批量重试、关闭、删除或清除接口，不会对用户笔记本执行动作。标准后端、前端、契约与生产构建验证均已通过。
- **大型来源详情有界加载（§6.2 / §6.5，2026-08-04）**：浏览器不再一次请求、持有并渲染整篇 `source_elements`；详情首屏按 40 个元素分页，单请求上限 100，可按需加载前后页。Ask/Memory 引用带目标 element 时，后端确定性返回包含它的页并保持高亮定位；本库与挂载参考库共用 active-notebook 参与集授权。旧全量 elements 端点仅保留内部与向后兼容用途。SQLite/PostgreSQL 两个适配器实现同一顺序与窗口语义，并通过分页/anchor/越权回归测试和完整 `scripts/check.sh`。
- **来源状态轮询**：上传后对非终态 source 每 ~1.5s 轮询 `GET /sources/{id}`（~3min 上限），实时展示 queued→parsing→parsed→extracting→extracted/failed；到达 extracted 自动刷新候选数与 counts。
- **主栏当前 tab**：问答 (Ask) / 知识库 (Knowledge) / 记忆 (Memory) / 深度报告 (Deep Report)；Scenario / Case / Checklist 已退役。
  - **引用标记本地化兼容（§6.5 / §11，2026-08-06）**：模型返回的半角 `[k1]` 与中文括号 `【k1】` 共用同一证据语法，半角/中文逗号复合组同样兼容；Ask 锚点、分节清洗、报告全局重编号与高风险引用审计、前端 Markdown 链接化保持一致，最终均显示为可点击编号。普通复合组遇到未知键仍整体失败关闭；按节 Ask 在绑定前删除跨节号段、保留本节合法键，维持既有隔离语义。
  - 问答：自由提问走 `/ask`（已移除写死 scenario）；支持多个 conversation/session，会话历史通过 Ask 顶栏单行 `历史 N` 入口 + 可展开会话管理面板切换/新建/重命名/删除，旁边的 `+` 直接开始新会话，不再保留重复的当前会话上下文栏，避免压缩主问答区。历史按带亚秒精度、跨 UTC offset 仍按绝对时刻正确比较的最近活动时间排序；首轮问题提交后在模型回复前就即时入历史、置为当前会话，即使 `started` 到达前切到同库其他会话仍能返回它并接回进度；切走或重连后终态会刷新轮数/推理标记，同库列表调用会追随最新请求（含跳过失败的中间请求），旧库延迟请求不会覆盖或作废当前库列表。加载 notebook/会话最新详情期间输入框与模式控制保持禁用，避免迟到详情接管新 run。欢迎区标题与 prompt chips 会根据 notebook 已导入来源的标题/摘要生成，并触发真实 ask。输入框支持 `Enter` 发送、`Shift+Enter` 换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制并恢复草稿问题；若 job id 尚未随 `started` 到达，界面先立即恢复草稿，该 run 的 controller 继续取得 id、调用后端取消，再停止本地流，且不与其他重连 job 串台。
    - 问题气泡显示网页端提交时间；回答卡同步显示与 `answers.created_at` 同源的权威完成时间。两者共用浏览器本地日期格式，均支持悬停显示、点击固定、外部点击取消和跨本地零点刷新；旧答案无需迁移即可从答案行完成时间回填。
    - **逐步推理意图闭环（§6.5 / §11，2026-07-25）**：正式界面在创建持久 Ask job 前调用 `/ask/intent/stream` 做带心跳的无语料问题理解（`/ask/intent` 保留为 JSON 兼容端点），只读取当前会话最近的用户问题，不把语料派生的助手回答回灌为意图依据，也不读取 notebook / 参考库内容或创建 conversation/job。清晰问题自动继续，但原始问题保留为第一条权威检索种子，未经人工审阅的模型规范化只能补充；会改变检索方向的歧义先展示可编辑的最终问题、必答主题、实体、比较轴、约束、排除项、前提、期望输出和补充问答，确认后审阅表述成为权威并确定性冻结，不再由第二个 LLM 重新解释。冻结合同统一用于 confirmed Memory、PPR、首轮子查询、元素/知识证据检索和答案合成；原始问题仍作为会话可见 turn，内部 `retrieval_query` 与最终 `intent` 随回答持久化，推理轨迹先显示「理解」再进入检索。首轮先执行完整权威问题，再按主题轮询确认方向；「首轮子查询」档位只约束首轮并发，超出的已确认方向进入待覆盖账目，在确定性种子通道之后、反思循环之前按合同顺序顺延执行（补种最多占步骤预算一半），预算不足时在反思循环结束后按最终未覆盖集落一条披露性轨迹步并逐轮回喂反思（按方向唯一简称匹配，模型补交即摘除、重提判重不重跑）；后续反思只能基于证据补充查询，不能替换用户意图（2026-07-30 修订：此前超出首轮宽度的方向会被静默丢弃）。无效确认在持久状态创建前返回 422；取消、切库、切会话、切模式和退出都会向尚未完成的预检传递取消信号。
  - 深度报告：两阶段后台 job，先审阅大纲再生成各节；支持实时进度、取消、删除、Markdown 与批量 zip 导出。
    - **准确性闭环（2026-07-25）**：创建报告后先做完全不读取语料的问题理解，持久化原始目标、可编辑的最终研究问题、必答主题、实体/比较轴/约束/排除项、期望输出、假设、置信度与阻断性歧义，并始终停在 `intent_ready` 等 owner 确认；模糊问题必须补齐必填答案，清晰问题也要确认最终表述，只读成员等待 owner，`auto_generate` 不可绕过。确认会原子冻结人工审阅后的问题契约，补充答案只进入内部检索问题而不污染报告标题；重复确认、重复生成和取消/阶段交接由 CAS 与同一取消事件身份保护。确认后的问题/答案成为后续检索和生成的权威输入。随后再用 notebook / mounted base 语料探测覆盖度并生成、校验大纲；每个必答主题都绑定到至少一节，人工修改大纲时保留契约并由 UI / API 阻止删除最后一个绑定，同时限制节数与检索方向。执行阶段除知识对象外，会把 parser `SourceElement` 作为一等检索结果和精确引用锚点带回；大型资料库先做有界 chunk 召回，再按候选 `element_id` 主键水合元素，避免全库扫描；Ask reasoning 同步支持元素引用。来源已接地判定为论文且解析出非空论文名时，Ask 锚点/回退引用与报告参考文献优先显示论文名，否则继续显示普通来源名/文件名。报告的 `grounded` 由服务端按实际引用锚点、证据等级和相关性计算，不再信任模型自报。最终编辑只生成摘要、意图覆盖缺口、证据局限与矛盾提示，不改写各节事实；全局参考文献按真实对象/元素身份去重，界面展示问题理解/歧义确认、每节必答问题、可编辑检索方向和原文/知识/公共库覆盖，点击引用可展开绑定原文片段。已通过完整 `scripts/check.sh`（方案契约 54 项、前端 Node 1503 项与组件 61 项、TypeScript 与 Next.js production build）；子 agent 最终复审无可执行问题。
    - **报告容量、输出预算与真实重试（§6.7 / §11，2026-08-04）**：分节撰写、全篇蓝图与最终只读终审现有独立、部署可配置的 completion 上限，高档全篇 JSON 蓝图不再静默继承全局 chat 上限；应用不声明总上下文窗口，由实际绑定 provider/model 负责 prompt + completion 兼容性。整篇报告准入与单篇节级扇出改用报告专属并发闸，并按模型服务与 PostgreSQL 池保留量继续收窄；排队不占数据库连接。全部章节为空或失败时终态为 `failed`，不再产生假完成；保留已确认大纲的失败报告可从详情页原地重新生成，认领事务会保留冻结意图/大纲并清空旧产物。SQLite/PostgreSQL 均实现同一重试 CAS，输出预算、并发、空正文终态、API 与前端按钮均有回归覆盖；完整 `scripts/check.sh`（含前端 production build）已通过。
  - **知识库（多类型浏览）**：前端从 `/knowledge-types` 动态获取对象类型，再用 `/knowledge?type=...` 浏览任意类型（Concept / Claim / Formula / Procedure 以及 legacy/custom 类型）；卡片含状态徽标 + 状态下拉（reviewed/approved/deprecated/conflict/project_specific）+ owner 内联编辑 → `PATCH /knowledge/{id}`；按状态过滤；「查重」「冲突」面板（重复组带合并按钮、冲突对展示）。
  - 回答含 citation 与 👍/👎 反馈；引用在前端以 `[1]`、`[2]` 顺序编号展示，点击引用会在答案面板内展开详情（避免浮窗越界），并可按 `source_id` / `element_id` 打开精确原文；挂载公共参考库来源经当前 active notebook 的参与集代理只读打开，不要求用户成为公共库成员。引用继续优先显示已接地论文标题，同时以独立 `source_file_name` 保留 `sources.file_name`，两者不同时在 Ask/报告引用卡显示「原始文件」，绝不使用 MinerU 临时/输出 Markdown 名；模型直接输出的数字复合引用（如 `[1, 2, 3]`）在每个编号都能映射到已知引用时也会拆成可点击引用；答案正文支持 Markdown/code/formula/table 渲染，紧邻正文的单行 `$$...$$` 会规范化为块级公式，宽公式在所属内容块内横向滚动，并提供复制按钮；chat 菜单可清空对话。
- **候选知识治理**：候选知识列表、evidence 与 approve / reject 后端能力保留；左侧 Source Stack 不再显示独立「审核队列」按钮，避免出现无效入口。
- **source detail 结构化渲染**：`formula` 元素先剥除包裹整值的 Markdown 数学定界符，再用 KaTeX 排版（失败回退可见的原始 LaTeX）、`table` 元素用 sanitized `table_html` 渲染、其余文本 + element_type 徽标。
- **图谱分析可操作性与质量解释（2026-08-11）**：图谱分析由原始指标墙改为结论优先的质量面板，先回答报告是否可用、各类型合并是否异常、主题结构是否值得查看，以及最需要复核的来源；红／黄／灰／当前状态在页内直接解释，并明确收敛率与关联度是诊断信号而不是越高越好的总分。五份预计算数据逐项说明用途，最大概念合并组与关联形成方式展开为完整诊断区块。可编辑成员可在面板内「生成分析／更新分析」，复用已有 `unified-kg/rebuild` 后台单飞任务与 `job_id` 配对轮询，不重新抽取来源，完成后自动刷新；只读成员仍可查看相同报告但不显示写动作。组件回归、TypeScript、生产构建及完整 `scripts/check.sh` 均已通过。
- 「分析」菜单当前只含晋升队列（admin）、tier 切换（admin）与边审查队列；看板、全屏知识图谱为其他顶栏动作，「图谱 Schema」已移入知识图谱视图头部：成员可查看，owner 可维护当前笔记本定义，管理员可另管全局基线。当前没有文章、思维导图、信息图或派生规则入口。

## 6. Source 上传与管理（异步闭环）

- multipart 上传：`POST /api/notebooks/{notebook_id}/sources`，支持 PDF / Markdown / DOCX / PPTX。
- **上传不阻塞**：上传请求先建记录并返回（parse_status=`queued`），解析 + embedding + 抽取经共享 KG job scheduler 异步执行。
- parse_status 状态机：`queued -> parsing -> parsed -> extracting -> extracted`（失败置 `failed`）。
- repository 通过注入 `scheduler` 回调保持同步可测：HTTP 层提交 `kg_scheduler.submit_job`，冒烟/脚本不传则同步跑完整管线。
- 记录 metadata：file_name / file_size / file_hash / source_type / parse_status / summary。
- 相关 API：list sources、get source detail、手动重跑 `POST /api/sources/{source_id}/parse`（委托 `process_source`）、delete source 与受约束 URL import。
- 重新解析保留 source 行与原始文件，替换 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用相同 cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。

## 7. 文档解析与元素级 Evidence

- **解析能力注册表与全栈可见性（§6.2，2026-08-10）**：后端以单一注册表描述自托管 MinerU、MinerU 公共云和内置解析器的顺序、支持格式、执行边界、可用性与固定不可用原因；上传准入、后端分派与来源导入 UI 共用其投影。路由保持自动，已配置自托管 MinerU 失败时不会静默外发到公共云；对外配置不暴露 endpoint、凭据、路径或原始异常。
- `SourceElement`（id / source_id / element_type / location_label / text / metadata）+ `source_elements` 表。
- parser：Markdown（heading/paragraph/list_item）、DOCX（paragraph/table_row）、PPTX、PDF、plain text fallback。
- **PPTX 升级为元素级**：按 shape / text box 逐个产出 `slide_text` 元素，并解析 `ppt/notesSlides/*.xml` speaker notes 为 `speaker_notes` 元素。
- **PDF 解析经 MinerU 适配器（`mineru_client.py`）与 GPU 解耦**：`MINERU_MODE=http` 调远端 `mineru-api`，`cli` 在隔离 Python 子进程中调用 MinerU `do_parse/read_fn`，`off`（默认）用 PyMuPDF4LLM 回退。FastAPI 后端进程不引入 torch/MinerU；MinerU URL/文件解析在远端瞬态错误默认共尝试 3 次，最终失败或产出 0 元素时降级本地解析，并在 pipeline log / source `error_message` 留下回退诊断。MinerU 输出映射为结构化元素：公式→`formula`（保留 LaTeX）、表格→`table`（HTML 存 metadata）、标题保留层级。
- **off/故障回退质量已提升**：PyMuPDF4LLM 按页产出版面感知 Markdown，保留标题、多栏阅读顺序和重建 HTML 表格，再转为现有 `SourceElement`；pypdf 仅作新解析器缺失/报错后的最后兜底。成功降级的来源保持 `extracted` 并返回安全的 `parse_quality_warning`，详情页提示潜在质量差异并提供重新解析/删除，MinerU 后续成功会清警告。
- **本机已启用 MinerU(MLX)**：本机为 Apple Silicon，已装 `mineru[core]` + `mlx-vlm`（VLM 模型 MinerU2.5-Pro 已下载），`.env` 设 `MINERU_MODE=cli`、`MINERU_BACKEND=vlm-auto-engine`、`MINERU_PARSE_METHOD=auto`、`MINERU_LANG=en`、`MINERU_TIMEOUT_SECONDS=1800`，`vlm-auto-engine` 自动走 MLX 引擎（Engram 第一页实测 24.57s，完整论文可能超过 600s）。公式/表格/版面离线可得。
- **空 PDF 止血**：PDF 解析出 0 元素时写明确提示（疑似扫描/图片型 PDF，需 MinerU/OCR），避免"假成功空结果"。
- 每个元素带 `location_label`，作为 evidence citation 锚点。
- `.env.example` 默认仍保持 `MINERU_MODE=off`，其它环境默认离线 PyMuPDF4LLM（pypdf 最后兜底）。

## 8. Source Summary

- 每个 source 解析后生成 summary，LLM 已配置走 LLM，否则 deterministic fallback；持久化并在前端展示。

## 9. 检索：关键词 + 向量混合

- **生成问题影子索引（§6.5 / §11，2026-08-10）**：新增部署级 `off/shadow/on` 模式，默认 `off` 零额外成本；显式离线 `question-index` 阶段经 `chunk_question_generation` 为原 chunk 生成问题，并用 `chunk_embedding` 独立向量化。在线只在 chunk 低召回时、于冻结来源上限内执行有界补召回；超过扫描上限直接回退 baseline。`shadow` 仅记录无正文对比计数且逐字返回 baseline，`on` 也只在 baseline 后追加原 chunk，不驱逐或重排已有结果。生成问题永不作为引用证据；迁移、级联删除/重解析、notebook 深复制及空结果幂等约束已有回归覆盖。
- Notebook 内搜索 API：`GET /api/notebooks/{notebook_id}/search?q=`，覆盖 notebook metadata、source metadata、source element 与 knowledge object payload。
- **新增 `backend/app/services/retrieval.py`**：
  - `element_embeddings(element_id, source_id, vector)` 存 JSON 向量；解析后对每个 element 调 `llm_client.embed()` 写入（未配置 embedding 则跳过）。
  - **CJK 感知分词**：`_tokens` 对中文连续串产出字符 bi-gram（单字→uni-gram），拉丁/数字保持词级，修复"整串中文变一个 token 导致中文关键词检索失效"的硬伤；该 tokenizer 被抽取的 evidence 绑定与场景 boost 复用。
  - **真正的 hybrid 融合**：`relevance = w_kw·keyword + w_sem·semantic` 加权和（默认 0.4/0.6，常量集中），按生效信号重归一化；未配 embedding 时退化为纯关键词且不被截顶。相关度门控 `RELEVANCE_FLOOR` 砍噪声。
  - **类型权重去污染**：rule 1.0 > case 0.9 > checklist 0.8 > method 0.7 > risk 0.6 > glossary 0.4 仅作 `weight` 字段（跨类型分组/tie-break），不再乘进同类型相关度排序。
  - **payload 级向量（WS4）**：新增 `knowledge_embeddings` 表，对知识对象 payload 本身建向量（approve / `PATCH /knowledge` / merge 时写入，存量 lazy 回填）；语义分 = `max(payload 向量, 证据向量)`，修正"只用证据原文向量"的存储/意义错位。
  - **结构化场景匹配（WS3）**：`score_knowledge` 接收 scenario dict，与规则 `applies_to/condition/title` 做 token 重叠，`final = relevance·(1+α·boost)` 软加权（不硬过滤）；`scenario_query` 透传结构化字段。
- 集合页搜索调用后端搜索，并加 250ms debounce，避免每键触发请求。
- 当前为 SQLite 文本匹配 + Python 余弦；尚未引入 BM25 / FTS5 / pgvector。
- Ask 的联合范围按 mode 区分：`chunk` 基线只读 active notebook 的 chunk；可选 KG overlay / PPR 才可能加入 federated KG 上下文与 base-backed chunk，`graph` / `reasoning` 走 federated KG 路径。
- **Knowhow 格子知识对象默认进入节点检索（§6.5 / §11）**：`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED` 默认改为 `true`，带行标题列的 Knowhow 投影对象会进入 reasoning/graph 的 KG-node 混合检索并保留引用直达行详情；`false` 仍可只回滚这条直接对象路径，逐格 chunk 召回不受影响。默认开启后的类型发现按 `knowhow_tables.hidden_source_id` 收窄，不会把其它自定义 Schema 类型误当作表格列；类型集合与标准化 chunk-vector→KO 旁挂做有界 single-flight 缓存，旁挂版本同时覆盖图变更序号、Knowhow chunk-vector 计数/时间和运行时向量维度，因此不会 bump KG 的纯向量修复也能刷新，KG 变更时还会显式失效，避免一次深入分析的多个子查询重复扫描隐藏格子和重建矩阵。SQLite / PostgreSQL store 均实现同义查询。已通过完整 `scripts/check.sh`（后端 6584 passed / 341 skipped、方案 harness 54 项、前端 Node 1508 项与组件 62 项、TypeScript 和 Next.js production build）。
- exact-score 的 `base` 次序只适用于知识对象命中（`federated_retrieve()`）；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。base-wins 矛盾规则是独立的答案合成策略。

## 10. 自动抽取 Pipeline

- 当前主线为 KG-native 抽取：`backend/app/services/kg_ingest.py` 调用 `kg.extract_window`，把 source 分窗后抽取 Concept / Claim / Formula / Procedure 节点与关系边，再由 `build_records()` 绑定到 `SourceElement` evidence。
- `backend/app/services/extraction_profiles.py` 当前只维护 `academic_paper` / `textbook` 两类 profile，二者对象集均为 `concept / claim / formula / procedure`；`doc_type` 按单个 source 存储。
- 系统 TOML 为 `kg_extract` workload 绑定可用 chat 服务时，`_run_extraction()` 走 LLM KG 抽取并把对象直接写入 `knowledge_objects`（status=approved）与 `knowledge_relations`；`extraction_runs.run_type='kg'`。
- 未配置 LLM 时，`_run_extraction()` 仍写入 completed run，但 `error_message='no-llm'`，不会生成启发式候选或假 KG。离线本机 beta 仍能解析、搜索、摘要和回答；需要知识召回时必须配置 LLM 或由测试/治理显式写入知识对象。
- **KG 模型故障隔离（§6.4 / §9.1）**：手动建库与完整重建均创建持久化的 `kg_build_jobs`，只在当前 notebook、本次任务内共享故障熔断状态。单次 LLM 调用受 `KG_LLM_TIMEOUT_SECONDS` 和 `KG_LLM_MAX_RETRIES` 限制；持续超时、连接失败、鉴权失败或服务拒绝会停止继续派发/重试，首个窗口确认熔断时即持久化 `stopping`，再做窗口级与来源级 drain，并把任务置为可恢复的失败状态。重建预检显式绕过且不写入 LLM 缓存。已完成 source 的 KG 结果保留；同一 source 的 object/relation 分块在一个 SQLite 事务内提交，最新 extraction run 失败的旧残留仍算未完成；terminal extraction run 提交后会失效 notebook 的待处理来源缓存。启动恢复会关闭遗留 run，并把全部 orphan `extracting` source（含创建 run 前中断者）恢复为 `parsed`。前端以 notebook/workspace/request 三重 epoch 防止跨库回写，在 durable job 仍为 running 时持续轮询，不用固定时限伪造完成；状态栏展示预检、抽取、停止、完成或中断阶段、进度与安全错误文案，并提供“继续分析未完成内容”。只有用户明确选择完整重建时才走清理语义，且会先探测模型可用性，避免模型不可用时先删除既有 KG。SQLite / PostgreSQL 离线批处理均提供显式 `kg --retry-partial`（PostgreSQL 维护命令要求停服确认并持有 advisory lock）：识别最近一次 `windows_failed>0` 且已有对象的来源，重试仍不完整或新结果为空时保留旧图，仅“零失败窗口且非空”的新图在单事务内替换。事件日志只写 started/progress/circuit-opened/stopping/succeeded/failed 的安全任务元数据。后端全量测试、前端测试、TypeScript 与 production build 已通过 `scripts/check.sh`。
- 旧 `extraction_candidates` 表与候选 API 仍保留兼容，但不再是当前自动抽取的主产物。

## 11. Curator 审核、正式知识表与知识治理（方案 v0.2）

- 正式知识统一存于 `knowledge_objects`（主线 object_type = concept/claim/formula/procedure；legacy rule/method/risk/case/checklist/glossary 与 custom 类型仍可存在），status、owner、`last_reviewed`、payload、evidence 内联 JSON；KG 关系存于 `knowledge_relations`。
- Legacy 候选审核 API（兼容保留）：
  - `GET /api/notebooks/{notebook_id}/candidates`（全部）
  - `GET /api/notebooks/{notebook_id}/candidates/{type}`（rules/methods/risks/cases/checklist/glossary）
  - `PATCH /api/candidates/{candidate_id}`（编辑 payload/status）
  - `POST /api/candidates/{candidate_id}/approve`（候选 payload 落入正式表，status=approved，并从队列移除）
  - `POST /api/candidates/{candidate_id}/reject`（status=rejected，删除对应正式记录）
- **知识治理（Tier 2）**：
  - **状态生命周期**：`reviewed / approved / deprecated / conflict / project_specific`。仅 USABLE 集合（approved/reviewed/project_specific/conflict）进入答案/检索，`deprecated` 排除；本轮修复 Ask 一跳 KG 邻居扩展也必须过滤 `deprecated`。
  - **浏览**：`GET /api/notebooks/{id}/knowledge-types` + `GET /api/notebooks/{id}/knowledge?type=...`，任意对象类型通用浏览，不再依赖 `/rules|/methods|/risks|/glossary` 旧卡片路由。
  - **审核后编辑**：`PATCH /api/knowledge/{id}` 改 status/owner/payload，并盖章 `last_reviewed`。
  - **重复合并**：`GET /api/notebooks/{id}/duplicates?type=` 同类型相似度（关键词 + 证据向量 cosine ≥0.6）成组；`POST /api/knowledge/{id}/merge`（折叠 evidence、源置 deprecated）。
  - **冲突检测**：`GET /api/notebooks/{id}/conflicts`（legacy rule 同范围、取向相反的对）。
- notebook counts 改为对正式表做真实统计（rules/cases/checklist_items/methods/risks/glossary，统计 USABLE 状态）。

## 12. Ask（当前）与 Scenario / Case / Checklist（历史记录，已退役）

- **请求内模型交互统一流式保活（§6.5 / §11，2026-08-27）**：完成全栈输出路径审查，并按任务生命周期收口，而非把所有 HTTP 机械改成流式。正式网页端直接等待模型的五类交互——Ask 意图理解、回答转 Memory 预览、Knowhow 表达优化、单行补全、格式化——统一改走共享 NDJSON 请求流：立即发 `started`，空闲每 5 秒发只含固定 `stage` + `elapsed_ms` 的无正文心跳，最终恰好一个 `final` / 稳定码 `error` / `cancelled`；统一关闭代理缓冲，断连、ASGI 帧发送失败和界面关闭/中止/重试换代都会设置 provider/retriever 共用取消事件，异常原文、prompt、身份、证据与模型正文不进入进度/错误帧。廉价确定性的资源/输入/模型可用性预检在线程池完成且先于第一帧，保留可操作的 HTTP 错误；后台标签页暂停 rAF 时前端仍用有界定时回退继续读取终态，StrictMode 探测不重复物理模型请求，Knowhow 的整行与单格入口均透传取消。Ask 意图轨迹与 Memory 预览显示实时已等待时间，Knowhow 保留既有 loading/队列态；原 JSON 端点继续兼容。持久 Ask、报告、来源解析、索引/KG、命令目录与笔记本理解仍采用 detached + 持久化 + 重连/轮询，短 CRUD/分页/status/准入保持普通 JSON，上传与 ZIP 等二进制传输也不混入 NDJSON。诊断路径白名单、OpenAPI 冻结快照与前端错误语义守卫同步更新。部署插件接口随后按同一生命周期规则补齐：`http.plugin_router` 的九字段上下文新增 core-owned `task_stream`，把同步 worker 卸载出事件循环并复用同一心跳/取消/脱敏协议；构建期 UI 的受限 `actions.api` 新增路径隔离的 `requestTask()` 消费端口。`ask.engine`/`ask.gap_consult` 继续随 durable Ask job，parser/indexing 继续后台任务，observer 仍在终态提交后，ZIP 导出不混 NDJSON。最终 `scripts/check.sh` 完整门禁退出 0：contracts 通过，后端 9,501 项、前端 Node 2,496 项、组件 694 项全绿，TypeScript 与 production build 通过。
- **部署 `ask.engine` 实时轨迹（§6.5 / §11，2026-08-27）**：补齐上一项审查中 durable Ask 传输已流式、但插件 `trace.step()` 仍只在 final 出现的缺口。Host 构造的 extension `AskMode` 统一改为 `streaming=True`，`/ask-modes` 从该 Host 真源投影匹配的 `streaming=true` / `streams_trace=true`，浏览器 fail-closed 接受这对声明并保持所有模式都走既有 `runAskStream`；同步 provider 仍在 detached worker 内执行。Ask dispatch 现在把 core-owned `on_trace` 注入 `PluginEngineTrace`，每个通过类型、长度与步数护栏的裁剪后 `plugin` 步骤只登记一次、带核心测量的墙钟间隔在数据锁外实时回调，由 durable worker 先持久化再发 NDJSON `progress`。provider 返回先冻结终止时点，再由核心做身份/证据/引用准入：通过才发布「扩展引擎执行完成」，provider 或准入失败发布「扩展引擎执行失败」；这条预算外终止步骤覆盖最后一步后的尾段，零步骤 provider 也能显示完整引擎耗时且不混入准入延迟。首个畸形/超预算调用追加且实时发送一条预算外的核心计时截断标记，后续同类调用静默，不再改写已交付旧步骤，因此 live、durable 与成功 final 轨迹 append-only 且逐字一致；显式取消保持既有的取消后写入闸门。普通 `/ask` 兼容路由继续阻塞并返回最终轨迹。聚焦的 Host/dispatch/端口/API 投影、前端 mode 解析与 Ask session 测试通过，随后 `scripts/check.sh` 完整门禁退出 0（contracts、全部后端、前端 Node、组件 695 项、TypeScript 与 production build 全绿）。
- **`ask()` 删除 demo 分叉**，全部数据驱动：
  1. 在 `claim/formula/procedure/concept` 四类 KG 对象上做混合检索，按类型权重做跨类型排序。
  2. 对 top hits 做 1-hop KG 关系扩展，且 hit 与 neighbour 都必须是 USABLE 状态。
  3. 配置 LLM 时生成带 `[k]` 标记的自然语言答案；未配置时返回 deterministic conclusion 与 `related_knowledge`/citations。
  4. citation 必须能回查到有效 `element_id`。
  5. 保存 answer 并返回 `answer_id` 与 `conversation_id`，用于反馈和多 session 会话。
- **历史记录（已退役）**：曾实现 `scenario_query()` / `case_search()` / `checklist()`；当前主线不再暴露这些 endpoint 或 tab。
- **推理模式 agentic search 实时进度（§6.5 / §11）**：新增 `POST /api/notebooks/{id}/ask/stream` NDJSON 流；后端先发带持久化 `job_id` + `conversation_id` 的 `started`，再把 `ReasoningRetriever` 的 plan / retrieve / reflect / expand / fallback / answer trace step 逐行推给前端，最后发送完整 `AskResponse`。前端收到 `started` 就即时入历史并刷新最近活动，同时在 pending answer 中实时显示一行 agent 轨迹摘要，按最新 progress 事件刷新，点击后展开完整步骤；最终回答中保留默认折叠的 `reasoning_trace` 供回看；普通 `/ask` 仍作为兼容非流式路径。Ask 生命周期已全栈接通：transport 断连、页面导航或刷新只停止当前客户端继续接收，detached Ask job 继续执行并持久化最终回答；只有用户显式点击中断、前端调用 `POST /notebooks/{id}/ask/jobs/{job_id}/cancel` 时，后端才设置 cancellation event，`ask_chunk` / `ask_reasoning` / `ask_graph` 与 LLM 流式读取路径在关键阶段停止，且不会保存被显式取消的最终回答。本次即时历史与活动时间回归已通过完整 `scripts/check.sh`，包括后端全量与 Ask 时序回归、前端节点/组件测试、TypeScript 检查和 production build。
- **`follow_chain` 类型化查询期推理（§5.9 query-time 子集 / §6.5 / §11）**：`reasoning` agent 新增两跳链路动作，通过已有 source/target 索引做有界局部抽样，仅组合 `derived_from/kind_of/prerequisite_of/precedes/part_of` 同类型关系。该能力不新增 migration/索引/历史回填；高度节点被截断且无法确认不存在直接边时 fail-closed。三节点类型与 USABLE 状态、每跳 relation quote、edge review、候选起点、方向、环路/直接边重复、最低链可信度和 `validity_scope` 也全部 fail-closed；结果只进入本轮 Ask/深度报告上下文和实时 `推导` trace，不写回 KG。两条原始 hop 各自成为 relation evidence anchor；新抽取关系尽力绑定 `SourceElement`，旧 quote-only 关系降级为 source-level 展示且不影响原检索路径；临时 `A→C` 必须标作「推断」且不伪造引用。已通过 `scripts/check.sh` 与 `cd frontend && npm run build`。
- **逐步推理档位 + Knowhow 完整枚举（§6.5 / §11，2026-07-25）**：已实现 `overview` / `standard`（默认）/ `deep` / `thorough` / `exhaustive` 五档用户控制，并把相关性 Top-N 与集合完整性拆成两套合同。五档每查询取数为 `4/8/8/12/16`；最终 `floor/aspect/cap` 为 `8/2/12`、`20/3/36`、`24/4/48`、`32/5/64`、`40/6/96`；最大 `步骤/首轮子查询` 为 `4/2`、`8/5`、`16/6`、`32/8`、`50/10`；KG/原文上下文为 `4000/12000`、`6000/30000`、`8000/50000`、`12000/80000`、`16000/120000` 字符。候选召回独立采用部署值：`CHUNK_RECALL` 默认 200，分别约束带索引 Chunk/KG 的 ANN 与词法窗；`RELATION_RECALL` 默认 200，分别约束 Relation ANN 与词法关系 ID 总窗。这些默认值不冒充请求级硬上限。意图合同新增 `ranked` / `complete` / `aggregate` / `hybrid`；Knowhow 完整/统计/混合请求按稳定游标读取，100 行表可返回 `100/100`，并在答案下方提供可展开且可跳回原行的结构化结果。五档统一为 25 行/页、最多 50 页/1,250 物理行、8 表、每表 8 列、模型单元格摘录 1,000 字符、结构化载荷 256,000 字符、正文内联 100 行、结果卡初始 20 行；游标未耗尽、表目录/行数/列元数据/所选范围不稳定或任一安全线触顶时返回 `complete=false` + `explicit_partial`，不声称“全部”。低档位不降低显式完整枚举上限。当时的完整枚举器只覆盖 Knowhow；来源元素与 KG 对象的集合枚举随后由下方「逐步推理集合枚举工具」条目补齐，其余对象集合（Memory 等）仍是相关性命中并披露边界。已通过 `scripts/check.sh` 完整门禁（后端/契约、前端测试、TypeScript 检查与生产构建）。
- **Review 修正（同一功能）**：确认时按最终编辑措辞与权威澄清答案重算 scope；结构化执行器只处理整表物理行清单/直接物理行或记录计数及其 hybrid，条件、“多少种”等去重/种类计数、分组回退并披露不支持完整。轻量 catalog 最多返回 8 个表描述且不读取内容/健康大字段，显式点名表会在截窗前被优先纳入。响应分开 per-table、batch、synthesis coverage，200/200 枚举配 100/200 分析明确为“枚举完整、分析部分”，8 表截断不污染已耗尽单表。KG/Memory/链与结构化预览/chunk/direct element 分别共享两项字符硬预算，最终证据不越界；最终门禁状态以本变更最新 `scripts/check.sh` 为准。
- **逐步推理集合枚举工具（§6.5 / §11，2026-07-29，2026-07-30 更新，`docs/reasoning-enumeration-tools-design.md` PR-2，分支 `claude/reasoning-enum-tools`）**：把「相关性 Top-N」与「集合完整枚举」的分野从 Knowhow 扩展到来源元素与知识对象。新增两个模型显式调用的 zero-LLM reflect 动作 `enumerate_elements`/`enumerate_kg_objects`：来源元素白名单 `formula`/`table`/`image`/`code_block`，知识对象白名单 `concept`/`claim`/`formula`/`procedure`（限可用状态），唯一真源为 `app/services/collection_catalog.py`。每 run 构建一次的集合地图（`[Collections in scope] …`，硬顶 600 字符）注入 plan/reflect 上下文，让模型先看计数再决定是否值得列全；地图的按源计数集合与枚举执行器（`app/services/collection_enumeration.py`）实际遍历的源集合逐字一致。覆盖率是结构化的 `TypedCollectionCoverage`（`returned_total`/`total`(可空)/`complete`/`truncated_reason`/`overflow_semantics`），执行器算出、前端徽章直读，绝不采信模型自报；`complete=false` 恒配一个可续跑游标，唯一例外 `truncated_reason=concurrent_change`（两次调用之间作用域发生变化）。预算随档位复用同一张五档表：`enum_page_size` 各档恒 50，`enum_pages_per_run` 为 `2/4/6/8/12`，`enum_rows_per_run` 为 `100/200/300/400/600`；页预算只计每个被访问分片（元素按来源、知识对象按参与的库）第二页起的额外翻页，每个分片首页免费；游标本身是 run 内部句柄，不出现在响应里。响应新增 `TypedCollectionResult`（`AskResponse.result_sets` 的 kind 判别分支之一），携带 `synthesis_rows`/`synthesis_complete` 把「已枚举清单」与「实际进入合成 prompt 的预览」分开披露。前端渲染按来源分组的清单卡（公式用 KaTeX、表格用文本摘录+跳转来源、图片仅在打开卡片后触发鉴权加载、代码块用代码块；知识对象复用既有 KG 类型标签）；卡片保留覆盖率摘要但默认收起，用户主动打开后先看前 20 条，并可继续展开全部已加载内容。跨库条目 v1 只读展示（标注「来自参考库《名》」，不提供来源跳转/图片加载）。总闸 `REASONING_ENUM_TOOLS_ENABLED`（默认 true）：关闭时两个动作都不提供、地图也不注入，回到接入前的行为。新增 SQLite `_migration_37`/`SCHEMA_VERSION=37` 与配对 PostgreSQL `0015_source_element_type_index.sql`（`idx_source_elements_source_type`），新增跨栈标签 parity 守卫 `scripts/check_enumeration_list_labels_contract.py`。默认折叠更新已通过 `scripts/check.sh` 完整门禁（contracts/backend/frontend）、前端 1,670 项 Node 测试、158 项组件测试、TypeScript 与 production build。

- **大纲便签与按节合成（§6.5 / §11 延伸，2026-07-31，`docs/reasoning-enumeration-tools-design.md` §3 PR-3，分支 `claude/outline-coevolution`）**：借鉴 DualGraph（arXiv:2602.13830）把「怎么写」与「知道什么」分离共演化：穷尽档新增第 11 个反思动作「整理大纲」（`update_outline`，总闸 `REASONING_OUTLINE_ENABLED` 默认开、仅穷尽档进 prompt/schema/动作白名单，关闭态与低档位逐字回到接入前）。模型维护 run 局部有界大纲（章节结构全量替换；同一稳定节 id 的证据 union 保留，遗漏不删，`remove_evidence` 显式撤销，8 键溢出旧键优先且未接纳新键回喂点名）；合法绑定取存活候选池与「run 内摘要曾展示 ∪ 当前大纲已持有」的交集，窗口滑动不吃旧绑定、从未展示的中段 id 也不能靠猜中通过。未接纳 key 持续作为服务端 pending，直到成功绑定、在 `remove_evidence` 点名放弃或整节删除；pending 不冻结普通额度内的后续全量结构更新，只有真正的纠错提交才守同结构约束。完整 pending 每节硬顶 56 key，prompt 每轮只展示前 8 个与剩余计数，处理后滚动露出下一批。`sufficient`/stale 终态仅在正常步骤尚有余额时最多补一次同结构纯换键纠错，第 6 次更新后也只有一次资格；`max_steps` 绝不因纠错增加，stale 熔断事实先落 trace，仍未接纳就在收尾明示。非法键丢弃、空节保留为下一步检索方向。每轮反思回喂整份大纲与剩余整理额度，大纲修订对 stale 熔断中性（纯整理躲不开熔断）。终态大纲 ≥2 个非空节且该 run 未产出集合清单/结构化整表枚举时按节合成（清单 run 保持单次合成，清单预览与覆盖披露完整进入合成上下文；被绕过时大纲披露键仍随 synthesis detail 带出）：每节只装配该节绑定证据（键基址按节偏移防串台，锚点按节解析——跨节号段引用按幻觉丢弃不误绑），被最终重排挤出选集的绑定对象随重排/配额夹取后的相关度带出参与 grounded 判定；每节用自己的切片与锚点通过 `classify_evidence`，逐节记录落 synthesis detail 的 `section_grounded` 列表（不是整篇 flag），全部有据照旧，否则只把全局结果封顶 `overview`，零节精确 grounded 也不强制误写成 `inferred`；按大纲顺序拼接 `##`/`###` 标题，任一节失败整体回退单次合成且不留假模型报警，逐节进度实时进「作答」轨迹步。答案标题字阶只作用于问答容器（深度报告/Memory/Knowhow 的 Markdown 渲染不受影响，静态+真实 DOM 双守卫）。前端补「大纲/整理大纲」标签与节进度细节行；数值上限只登记在 `docs/product-and-api*.md` 契约表。本次收敛通过 `test_reasoning_outline.py` 定向套件；完整门禁与 PostgreSQL G3 结果见本次提交验证记录。
- **本次大纲收敛验证（2026-07-31）**：`test_reasoning_outline.py` 定向套件、完整 `scripts/check.sh` 门禁与 PostgreSQL 16 独立集成门（G3）在提交当时均通过。
- **KG 弱支撑边回喂（PR-4，stacked 于 PR-3，2026-07-31，`docs/reasoning-enumeration-tools-design.md` §3.3，分支 `claude/outline-kg-gap`）**：DualGraph 共演化的另一半——KG→检索的定向缺口信号。每次**被接受**的 `update_outline` 之后，服务端在本次新绑定证据的 canonical 邻域上有界探测 `canonical_relations`，把支撑来源数（`source_count`）≤2 的关系折成便签提示追加进大纲便签，模型用既有动作（`add_subquery`/`follow_chain`/`expand_graph`）决定要不要补——零新动作、零新模型调用、零 schema 变更。总闸 `REASONING_OUTLINE_KG_GAP_ENABLED`（默认 true）叠在大纲总闸之上；run 级 `kg_gap_probed_seeds` 账目让纯换键/改标题 apply 零查询，探测异常 fail-open 记 `kg_gap_unavailable` skip 步，终态溢出纠错轮不渲染也不消费该段。数值上限登记进 `docs/product-and-api*.md` 大纲一节的契约表。已通过 `test_reasoning_outline_kg_gap.py` 定向套件；完整门禁结果见本次提交验证记录。
- **深度报告接入大纲共演化（PR-5，stacked 于 PR-3/PR-4，2026-07-31，`docs/reasoning-enumeration-tools-design.md` §3.4，分支 `claude/report-outline`）**：把大纲便签+按节合成机制接入深度报告的逐节深挖，而不是留作「v1 不支持」。三件事全部落在既有缝隙上，报告引擎不 import 任何大纲内部件：①报告「研究深度」五档（1/2/4/8/16）按阈值映射到与逐步推理相同的检索档位名（`overview`/`standard`/`deep`/`thorough`/`exhaustive`，中间值落更低档），`_deep_dive` 把映射出的 `AskRetrievalLimits` 传给 `ReasoningRetriever.run`，取代此前不论滑块位置永远按 `standard` 预算跑的行为——低档位检索预算因此变小、高档位变大，这是同名档位语义对齐的行为变化，不是回归；每节自己的 `max_steps` 仍固定为报告的 depth 值，绝不采用档位表自身的步数上限（成本按节数放大）。②到达 depth 16（穷尽档）时，`outline_wiring_active` 判据原样成立，大纲便签、`update_outline`、KG 弱支撑边回喂零改动在该节深挖里生效；报告构造 retriever 时不传 `collection_catalog`/`collection_enumeration`，集合枚举保持不可达。③深挖整理出的终态子大纲连同证据 `[k]` 反查折成有界「发现的结构」块（`outline_structure_block`：≤12 行、行 ≤80 字符、块 ≤1200 字符，超界记账 `(+N 子节略)`），作为 `discovered_structure` 附进 `report_section_prompt`，措辞教撰写模型这只是 `###` 子标题的组织建议、缺证据的子话题如实略过，且绝不回写用户确认过的 `reports.outline_json`；子大纲为空或非穷尽档时该块缺席，prompt 逐字回到接入前。节进度文案在观察到 `outline` 类型 trace 步时把 `section_status.phase` 细化为「深挖中（已整理大纲 N 节）」，复用既有 2 秒节流持久化，不新增表列/SSE。不新增 flag：报告侧激活条件 = 报告深度选到穷尽 + 既有 `REASONING_OUTLINE_ENABLED`（弱支撑边另受 `REASONING_OUTLINE_KG_GAP_ENABLED`）。已通过 `test_report_outline_integration.py` 定向套件；完整门禁结果见本次提交验证记录。
- **清单引用与原文出处修正（同一功能，2026-07-30）**：每个送达的来源元素/KG 对象条目现在至多携带一条仍存活的有界原文 `Citation`；KG 由服务端在 active notebook participant 作用域内批量解析首个有效 evidence element。进入答案合成预览的行使用隔离的 `k5001+` 引用键与反向映射，模型实际绑定且带存活 `source_id`/`element_id` 的清单锚点可确定性判 grounded；一个锚点都没绑的枚举答案不再展示无关 ranked-citation 兜底。结果卡默认收起，展开后可查看出处；本库与挂载参考库都经 active-notebook 代理精确跳转来源，参考库来源保持只读。历史 KG 出处已失效时明确显示无可用原文，不伪造引用。已通过本变更最终 `scripts/check.sh` 完整门禁。
- **指示语接地 + 来源集合枚举（§6.5 / §11，2026-07-30，`docs/reasoning-enumeration-tools-design.md` PR-2.5，分支 `claude/deixis-and-source-collection`）**：补齐「当前 notebook 的文章分析」这类问题的两个缺口。①**指示语接地**（纯 prompt 层，零新调用）：一段共用的 `prompts.SCOPE_DEIXIS_GROUNDING` 同时进入意图契约、两份规划拼写与 reflect，把「当前notebook / 这个库 / 本库 / 整个库 / the current notebook / this library / 知识图谱 / KG」接地成「用户打开的库及其挂载作用域」，要求模型解析后**剥掉**、不带进任何子查询/关键词/`exact_term`，同时保留问题本身；reflect 另逐个点名四个自由文本检索字段（`exact_term` 是字面匹配，范围词进去就是零命中探测）。刻意不做确定性词表剥离（词法路由已被否决，且会误伤真在讨论知识图谱的文档）。②**来源集合**：新增第三个零 LLM 集合，模型面**不新增动作**（用户拍板）——它是既有 enumerate 动作上的参数值 `enumerate.collection="sources"`，动作空间维持 10 个，执行器内部为独立的 `enumerate_sources`，列出作用域内**用户可见来源**的目录——显示名（接地论文优先显示论文标题）、文档类型界面词（`extraction_profiles.PROFILES`，未识别留空）、该源已存摘要的摘录；集合地图行尾新增 `| sources: N`。可见口径的真源是 SQLite `VISIBLE_SOURCE_TYPES_PREDICATE`（PostgreSQL 侧本次把三处内联收成同名常量），可见性由 `source_change_signal_rows` 把该谓词作为投影列 `user_visible` 求值（`source_type` 无索引，独立查询只能整表扫且紧跟在 signal 查询之后），`SourceStorePort.source_listing_rows` 承载来源卡投影且 `source_metadata` 改为在它之上实现（一份 SQL 两个入口）。计划 = signal 行里 `user_visible` 为真的那些（纯算术、零额外查询），地图计数与执行器遍历共用同一个 helper，故「地图说 7、清单列 8」在构造上不可能；每份文档计一行，整份清单是一个分片（首个 hydration 窗口免费，页查询上界 `1 + max_pages`），游标 `(notebook_id, source_id)` 指向尚未列出的第一份文档。遍历顺序 = 来源页签的 `(created_at, id)`（排序键随 signal 行同一次访问回来、双后端各自归一化，PG 侧先转 UTC；两侧 `list_sources`/`list_sources_page` 同补 `id` 次键，否则并列时间戳下与目录分叉；指纹只吃前两个字段，创建时间与可见性都不进摘要；元素侧顺序仍按 `source_id` 不动），所以「前 N 篇」的截断前缀就是最早加入的那几篇。schema 里 `collection` 显示为空缺省（唯一值会让照抄模板的模型被静默改道）；收尾除指纹外再对整条链已发出文档复读比对 (显示名, doc_type) 摘要，不等即 `concurrent_change`（论文元数据回填不碰 `updated_at`）。账目回喂对来源清单有一条定向豁免：附有界标题清单（≤20 条/每条 ≤60 字符/合计 ≤800 字符），否则 prompt 教的「按标题逐篇深挖」拿不到句柄。范围指示语里「知识图谱 / KG」只在领属形式下才算范围词，并写明反向豁免（文档本身讨论知识图谱时它是正当检索词）。前端清单卡新增「来源清单」arm（标题 + 类型小字 + 两行省略号收敛的摘要，本库条目可跳来源详情、跨库沿用只读围栏），trace 的 enumerate 步 summary 说「枚举来源清单」（`NEXT_ACTION` 表不变——它不是第 11 个动作），标签 parity 守卫扩到三张表。合成侧另加一条**只在目录在场时**注入的自适应粒度指导（篇数少则逐篇、多则按主题归纳），判断交模型、不设数值阈值。私有 Memory 与 knowhow 隐藏投影源两侧都不在清单内（与 PR-2 的隐私合同一致）；无图早退闸**计入**来源数：纯散文库（有文档、零元素、零知识对象）是来源清单的主力场景，此前被「请先构建知识图谱」整个挡住；零源库仍早退。已通过 `bash scripts/check.sh` 与 PostgreSQL 集成 lane。

## 13. 历史记录：Article Studio（已退役）

以下仅记录过去实现，当前 runtime 已移除对应 endpoint、表与 UI：

- 文章 API：list / create / `POST /api/articles/{article_id}/research`。
- **历史实现**：`research_article()` 曾从 title/abstract（及 element）抽取 claims，并生成 implication、validation plan 与 derived rule candidate。
- 持久化 `article_claims` / `derived_rule_candidates` 表。
- 回归保证：上传与 bondwire 无关的文章，research_article 输出 claims 来自该文章内容，不再出现写死文本（smoke 已断言）。

## 14. 用户反馈

- `answers` 表保存每次回答，`feedback` 表关联反馈。
- `POST /api/answers/{answer_id}/feedback`（rating useful / not_useful + comment）。
- 前端 AnswerView 提供轻量 👍 / 👎 / 复制操作；后端反馈接口仍支持可选 comment，但当前问答 UI 不再显示评论输入框。

## 15. 数据模型（当前 + 历史快照）

- **当前**：核心 schema 覆盖 Notebook/Source、Concept/Claim/Formula/Procedure knowledge、Ask/conversation/feedback、Deep Report、sharing 与治理；当前表包括 users/auth、notebooks/sources/elements/chunks、embeddings、knowledge/relations、answers/conversations/feedback、ask jobs/trace、reports、sharing membership 及 KG 治理/索引状态。
- **历史快照（已退役）**：旧版曾包含 RuleCard / CaseCard / ChecklistItem / ScenarioQueryRequest / ArticleSummary / ArticleResearchBrief 等 schema，以及 articles、article claims、derived-rule candidate 等表；这些不再是当前 runtime contract。

## 16. 历史记录：Demo Dataset（已移除）

- 早期曾保留 synthetic mixed 中英 demo；当前全新数据库不创建 demo notebook 或 synthetic source，只初始化内置账号。

## 17. 本机运行与验证

- `scripts/dev.sh` 同时启动 FastAPI 后端与 Next.js 前端（要求 `frontend/node_modules`，否则提示先 `npm install`）。
- 服务地址：前端 `http://localhost:3000`（占用时切 3001），后端 `http://127.0.0.1:8000`，CORS 默认放行 3000/3001。
- `scripts/check.sh`：
  - 后端 Python syntax、离线 hermetic smoke 与完整 pytest
  - 递归前端 `*.test.mjs`、`tsc --noEmit` 与 production build
  - 覆盖 KG no-LLM 边界、source cleanup、Ask/conversation/feedback、reports、sharing、检索与治理；不再声称验证已退役 endpoint
- 全部检查通过；`npm run build` 通过。

## 18. 可观测性 / 日志系统（全链路）

为解决"网页操作时卡住、不知道发生了什么"的痛点，建立统一结构化日志：JSONL 文件（`.local/logs/`，已 gitignore）+ Python `logging` 控制台简要行，对离线/未配置无副作用，写日志失败绝不影响主流程。

- **通用底座 `backend/app/core/event_logging.py`**：`EventLogger(settings, channel)` 负责 JSONL 追加 + 控制台行 + 永不抛异常；自动补 `ts/channel`，按 `LLM_LOG_MAX_CHARS` 截断；`new_id(prefix)` 生成关联 id。
- **LLM 交互日志（`llm.jsonl`）**：`LLMInteractionLogger` 基于 `EventLogger`，埋点在 `OpenAICompatibleClient`（`chat_json`/`embed`），覆盖 KG 抽取、问答、深度报告与 summary。chat 记录 prompt/响应/token/latency（截断）；embedding 只记摘要，不记录向量。
- **HTTP 请求日志（`requests.jsonl`）**：`backend/app/main.py` 新增 middleware，记录每个请求 `method/path/status_code/latency_ms/client/request_id`；超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 供前后端关联。
- **异步管线阶段日志（`events.jsonl`）**：`process_source` 对 parse/embed/extract/pipeline 各阶段两端计时打点（`kind=pipeline`，含 elements/parser_mode 等），`_set_source_status` 每次状态机跃迁 emit `kind=status`，失败记异常堆栈（`logger.exception`），可精确定位卡在哪一步、各步耗时与失败原因。
- **修复真实 bug**：`_set_source_status` 原 `params.insert(2, summary)` 误写到 `error_message` 列，导致失败时真实错误从未落库；现已修正，前端 source detail 可显示具体错误（smoke 加回归断言守护）。
- **前端可见性（`frontend/app/page.tsx`）**：`api()` 包装器 console.debug 方法/路径/耗时/request_id；轮询时显示"处理中（已 Ns）：文件: 阶段"，超时提示查看 `events.jsonl`，source 进入 `failed` 时点名文件；`error_message` 是后端写的 Python 异常串，只进 console。
- **错误人话层（`frontend/app/errors.ts` + `backend/app/api/deps.py`）**：**deny by default，信任来自出处而不是文本形态**。后端 `detail` 不再透传给用户（此前直出，用户会看到英文原文和裸状态码）。关键教训：「4xx 且 detail 含中文就原样展示」是**形态检查而非信任边界**——后端 40 处 `detail=str(exc)` 与 20 处刻意写给用户的中文文案结构上无法区分，该规则实测放行过 `403 访问被拒绝 — nginx/1.25 upstream=10.0.0.7:8000`（泄漏内网地址）。现由后端显式声明出处：`user_error(status, message)` 在响应上挂 `X-User-Message: 1`，只有带这个头的 `detail` 才可展示（标记走响应头，`detail` 的 JSON 类型保持不变，仍供 MCP / 日志 / 排查）。⚠该头必须同时进 `main.py` 的 CORS `expose_headers`，否则跨源部署静默失效而同源开发／前后端单测三处全绿。`isDisplayableUserText` 降为**第二道形态闸**（多行/标签/花括号/超长仍拒），防的是 `user_error` 被误用在拼了异常原文的串上；`trusted` 默认 false，漏传只会更保守；5xx 无论有无标记都泛化。**诊断通道**拿原始正文，压单行 + 统一截断（`truncateDiagnostic`，HTTP 与非 HTTP 路径共用上限）后只进 console。所有 fetch 失败分支走 `throwHumanizedHttpError(res, tag)`；所有 catch 分支走 `toUserMessage(error, fallback)`——它**只认品牌**（`humanizedError` 盖的 `Symbol.for` 章）不认形态，因为覆盖面最大的恰是没有 `Response` 的那些：`fetch` 自身 reject、流式 `error` 事件、`ask_job.error`、报告 `error`、来源 `error_message`、就绪快照 `error`、`model_errors[].message`——后端把它们统一写成 `f"{type(exc).__name__}: {exc}"`，其中中文的那些（`RuntimeError: 模型调用失败 upstream timeout`）正是旧形态判据漏掉的一类。品牌用 `Symbol.for` 而非模块内 `Symbol`/`instanceof`：Next.js 会把同一模块打进 server 和 client 两个 bundle，后者会产生假阴性。不上屏但要留痕的值走 `logDiagnostic(tag, value)`（渲染期禁止调用，否则随重渲染刷屏）。`errors-guard.test.mjs` 扫全量前端源码防复发，三条规则：禁裸抛状态码；`errors.ts` 之外**任何** `.message` 读取逐行登记；**任何 `.error` / `.error_message` 读取逐行登记**（前两条看不见后端诊断字段，曾致 6 个渲染点整类漏网、评审判定「6/6 通过是假绿」）。后端另有 AST 守卫禁止新增「4xx + 中文字面量 detail」的裸 `HTTPException`。
- **配置**：`config.py` + `.env.example` 新增 `EVENT_LOG_ENABLED` / `EVENT_LOG_DIR` / `SLOW_REQUEST_MS`（沿用既有 `LLM_LOG_*`）。
- **验证**：`scripts/smoke_backend.py` 新增 `check_event_logging`（JSONL 可解析、禁用不写、写失败不抛）与 `check_pipeline_event_logging`（管线阶段事件产出 + `error_message` bug 回归）；`scripts/check.sh` 纳入 `event_logging.py` 编译。
- **慢因诊断脚本**：`scripts/diag_slow.py` 保持只读/脱敏，新增 strict reasoning / PPR 路径审计，基于 DB 聚合与 scale-index manifest 输出 indexed-core 覆盖率、chunk/relation ANN 状态、delta 策略与跨 base 可能触发 active 全量向量加载的风险，用于部署机上定位大库 reasoning 卡顿。
- **检索索引调度与问答提示收敛（§16）**：手动立即构建会原子覆盖同 notebook 先前的低峰排队项，避免构建完成后又回到「空闲时建」；已有构建的真正后续任务、认领后新加入的 idle 项仍保留，worker 启动失败会恢复旧排队。idle scheduler 逐项认领，忙碌项不出队且单项启动失败不影响其余项。排队态在看板与旧回答提示中提供立即执行入口；前端以实时 `ScaleIndexStatus.exists` 覆盖回答生成时持久化的 `index_required` 快照，并在有界轮询停止后由 `index_done` 刷新当前 notebook，索引发布后历史回答中的降级提示同步消失。后端调度回归、前端纯逻辑/组件测试、`scripts/check.sh` 与 production build 已验证通过。

## 19. 历史新增（dev 分支，方案 §6/§7/§16，部分已被 KG-native 主线替代）

- **规则解释旧方案（§6.10）**：早期实现过 rule card 的 explain 方向；当前主线不再暴露 `/rules/{rule_id}/explain` 旧路由，改由通用 knowledge 详情与全屏 KG 详情展示 `出处`、相关节点和关系。
- **历史记录（已退役）：Derived Rule Candidate 审核队列（§7.5）**：过去曾有 `/derived-rules` 审核 API 与「派生规则候选」弹窗；当前 runtime 已移除。
- **创建富字段 + 模板（§6.1/§6.2）**：`NotebookCreate/Update/Summary` 增 `target_users/expected_questions/source_types/taxonomy/access_scope`（notebooks 表迁移）；6 套模板 `GET /notebook-templates`，创建按模板预填；前端集合页「从模板…」选择器 + 编辑弹窗富字段。
- **CSV / Excel 解析（§6.3）**：`parse_csv`(stdlib) + `parse_xlsx` → `table_row` 元素；上传校验/accept 扩 `.csv/.xlsx/.xlsm`。（2026-08-10 起 `parse_xlsx` 改为 MinerU 优先 + 行覆盖对账，openpyxl 仅作兜底——但兜底仍是全保真的逐格解析，因此不打降级质量警告。同批新增 `.xls`（旧版二进制 BIFF）支持：MinerU 不解析该容器、无优先分支，直接走 `xlrd` 本地解析；上传校验/accept 相应扩至 `.xls`。）
- **质量/分析看板（§16）**：`GET /notebooks/{id}/analytics`（有用率、低分提问=知识缺口、候选状态分布、知识覆盖、来源状态）；前端「看板」弹窗。已补齐 `GET /notebooks/{id}/analytics/content-overview` 的内容资产卡片：Memory 聚合严格按当前查看者和 notebook（不提供 admin 跨用户汇总），Knowhow 指标遵循 notebook 读取权限；卡片展示 Memory 总数/confirmed/candidate/最近三条，以及 Knowhow 表/行数、投影 pending/failed、过时代码和最近三张表，并只跳转到既有的 Memory、Knowhow 浏览/编辑界面。面向用户的来源计数排除隐藏的 `memory` / `knowhow` 投影来源；`size.sources`、复制阈值、存储与调度仍使用物理行，`has_unindexed_content` 在可见来源增量为零但派生内容变化时仍保留 scale-index 更新决策。
- **测试硬化**：`smoke_backend.py` 使用空 `MODEL_SERVICES_CONFIG` + `mineru_mode=off`，`scripts/check.sh` 不调用真实 LLM/embedding（即便开发机 `.env` 有密钥），全程离线。
- **架构硬化（2026-07-10，权限 / 图谱 / 异步状态 / 发布门禁）**：公共 `NotebookUpdate` 不再接受内部 `status`；深拷异常只补偿自身副本，崩溃清理由 `NOTEBOOK_COPY_STALE_SECONDS` 限定为过期 `copying` 行；KG conflict candidate 的读取/状态更新按 `(notebook_id, candidate_id)` 双重作用域，阻断跨库确认/拒绝；rejected relation 在 federated graph、PPR、scale graph 全路径排除，给 LLM 的关系方向保持 `source→target`，大图守卫覆盖 active + 全部 base；多子查询检索为每个 worker 单独传播 Context；URL 来源逐跳拒绝私网/localhost/link-local；认证解析移出 async event loop 且 session 续期节流。前端用 Ask run/workspace epoch 阻断跨 notebook/会话回写，分享/待办统一走原子 notebook opener，退出登录 abort 本地流并 remount。`Settings` 全部迁移到 Pydantic v2 `validation_alias`，非 SQLite URL fail fast；`scripts/check.sh` 禁用仓库 `.env`、运行全量 pytest + 递归前端测试 + tsc + production build，缺前端依赖不再跳过。本次完整门禁通过：后端 `2271 passed, 1 skipped`、前端 `143 passed`、TypeScript 与 Next.js production build 均成功。
- **架构模块化第二阶段（2026-07-10）**：保持 endpoint、SQLite schema 与 `SQLiteRepository` 公共 API 不变，把账号/用户模型配置/admin 用量/auth session 领域迁入 `sqlite_identity.py` mixin，并把笔记本分享令牌、深复制、成员关系与读取权限迁入 `sqlite_notebook_sharing.py` mixin；`sqlite_repository.py` 从 14,815 行降到 14,059 行，继续兼容 `_REQUEST_USER` / set/reset、`_COPY_CHUNK` 与 `_remap_json_ids` 导出。前端把 workspace API/视图模型迁入 `workspace-model.ts`，答案/引用/推理轨迹迁入 `answer-panel.tsx`，KG 类型标记迁入 `kg-type-mark.tsx`，`page.tsx` 从 6,060 行降到 5,438 行。新增后端继承/导出兼容守卫与前端源码边界测试，防止职责回流。本次完整门禁通过：后端 `2275 passed, 1 skipped`、前端 `146 passed`、TypeScript 与 Next.js production build 均成功。
- **架构渐进整改阶段 1：行为契约与文档对齐（2026-07-10）**：以当前代码和绿色测试为行为真相，修复 Ask disconnect、mode-specific federation/tier 次序、三 tab 两列 workspace、source cleanup 与退役能力文档漂移。transport 断连只停止向该客户端推送，detached Ask job 继续执行并持久化，只有显式中断才取消 worker；`chunk` 基线 active-only，KG overlay/PPR 可引入 federated KG/base-backed chunk，`graph`/`reasoning` 走 federated KG；exact-score 的 `base` 次序只适用于知识对象命中，relation hit 仍 score-only；workspace 是来源栏 + 问答/知识库/深度报告主栏两列结构。`architecture.md` 已改为稳定边界说明，文档契约测试进入常规 pytest。本次完整门禁通过：后端 `2281 passed, 1 skipped`、前端 `146 passed`、TypeScript 与 Next.js production build 均成功。阶段 2–6 当时仍为后续规划。
- **Repository composition refactor（2026-07-11～12，原阶段 2、4、6 的 Repository 部分）**：`backend/app/repositories/sqlite/` 领域 store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。application service 不再拼装主业务库 SQL，只保留业务顺序/策略/transaction seat；`SQLiteRepository` 保留为兼容 facade，公共操作只允许显式 adapter 或单跳委托，AST guard 验证真实 delegate target 与 ownership manifest，facade body/ownership debt 为 0。Ask chunk/reasoning/graph/stream、report、evaluation 使用 `backend/app/repositories/ports.py` 的可执行窄 Protocol，不再穿透 private runtime。`RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference；其他可变 runtime state 支持组合后替换。Ask/report 同步提交失败会持久化 failed、注销 cancellation entry 后重抛，既有成功顺序和 transaction checkpoint 不变。旧库 verifier 采用精确 migration/seed manifest、URI 百分号编码与 backup-only 构造；cleanup 失败只报告 retained path，原 DB/WAL 与 SHM existence/size 均受保护，live WAL 只豁免 SHM mtime。旧阶段 4 的 Pydantic 模型分文件与旧阶段 6 的 FastAPI lifespan / 统一应用生命周期仍延后为独立工作；这是一项内部架构收口，不新增产品功能或 migration。

  本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。

  2026-07-12 复验：冻结 fixture 为 `database user_version=v9 final_user_version=v10`、`PASS schema=v9 changed_tables=0`；主 checkout 的 4.7GB schema-v10 旧库为 53 表、149 个 storage 文件，`PASS schema=v10 changed_tables=0`。原 DB/WAL 的 size 与 mtime、SHM 的存在性与 size、storage manifest 均未改变；只发生了 verifier 明确允许的 SHM mtime 更新。

## 21. 文档类型抽取 profile 注册表（方案 §5 对象模型 + §6.2 模板）

- **问题**：早期抽取对所有文档硬套固定 6 类（rule/method/risk/case/checklist/glossary），只适合方案/总结；论文/课本硬套会产噪声、漏抽。当前主线已收敛到 KG-native 类型。
- **profile 注册表**（`backend/app/services/extraction_profiles.py`）：
  - `OBJECT_SCHEMAS`——当前内置 KG 类型为 `concept / claim / formula / procedure`，payload 主字段为 `name`，保留 `section_path`。
  - `PROFILES`——当前文档类型为 `academic_paper / textbook`，二者启用同一组 KG 类型。
  - `TEMPLATE_PROFILE`——仅保留 article/textbook 到 profile 的轻量映射；实际抽取主要按 source.doc_type。
  - `detect_doc_type` / `resolve_profile`——离线 bilingual 线索打分做 per-source 文档类型判别（明显胜出才覆盖模板默认，阈值：≥2 命中且领先 ≥2）。
- **接入**：`_run_extraction()` 读取 source.doc_type，调用 KG extractor；离线无 LLM 时只记录 `no-llm` run。
- **验证**：`scripts/smoke_backend.py::check_extraction_profiles` 断言当前两类 profile 与四类 KG schema；`scripts/check.sh` 全绿、离线。

## 22. 新类型通用浏览闭环 + 全栈对等规则

- **背景**：早期 knowledge UI 只覆盖少数定型 tab，会导致新对象类型「可入库但不可见」。当前主线通过动态 knowledge types 解决。
- **后端**：
  - `GET /notebooks/{id}/knowledge-types` → 该 notebook 现有对象类型 + 非 deprecated 计数 + 中文 label（`KnowledgeTypeCount`）。
  - `GET /notebooks/{id}/knowledge?type=<type>` → 任意对象类型的通用记录（`KnowledgeRecord`：headline + 按 `OBJECT_SCHEMAS` 排序的 `fields[]` + status/owner/last_reviewed/evidence），与既有 PATCH `/knowledge/{id}` 治理通用。
  - `search_notebook` 纳入 knowledge_objects（全类型）→ 新类型可被 notebook 检索命中。
- **前端**（`frontend/app/page.tsx`）：知识库 tab 改为**动态**——类型从 `/knowledge-types` 动态出现（带计数徽标）；非定型类型用通用渲染（headline + 字段表，字段名走中文 label 映射），复用状态/owner 编辑与查重。
- **同时修复**：`routes.py` 缺失的 `NotebookTemplate` import（此前 API 模块导入即 NameError，但 check.sh 只导 services 未触发）。
- **新规矩（AGENTS.md「Production code」）**：本系统中**任何面向用户的后端能力必须同变更内附带对应前端界面，不允许只实现一半**；"done" 的判定含后端端点、前端入口、`check.sh` 绿、`npm run build` 通过。
- **验证**：`smoke_backend.py` 增 knowledge_types/list_knowledge 断言；当前 TestClient smoke 确认动态 knowledge API 可用；`check.sh` 全绿。

## 23. Schema 管理 + 归纳 + 关系图 + ask 织入 + 抽取自我修正

- **全局基线 + 笔记本覆盖的可编辑注册表**：`object_schemas` 继续承载管理员维护、所有笔记本默认继承的全局基线；`notebook_object_schemas` 承载 owner 的 copy-on-write 覆盖、本库停用和本库专属类型。Ask 知识块、知识浏览与类型标签统一读取 **notebook 生效注册表**；来源的核心图谱抽取仍由独立的 Concept / Claim / Formula / Procedure 四类边契约约束，本次不把自定义类型隐式塞进该抽取器，也不迁移历史对象。删除同名覆盖表示恢复继承；有存量知识对象时拒绝删除本库专属类型。全局 `GET/POST/PATCH/DELETE /object-schemas` 的写入仍仅管理员，笔记本 `GET/POST/PATCH/DELETE /notebooks/{id}/object-schemas...` 分别使用 read / owner 权限。知识图谱里的「图谱 Schema」对成员开放查看，owner 可维护本库定义，管理员可另切全局基线视图。
- **Schema 归纳（建议态，§开放发现）**：`POST /notebooks/{id}/schema-proposals` 用 LLM 从笔记本内容提议新类型（offline 为 no-op），按 notebook 和创建者存为 `status='proposed' source='induced'`，绝不自动启用、遮蔽同名继承类型或占用其它 notebook 的同名类型；模型返回后在串行写事务内重查全局/本地注册表，避免并发写入被候选覆盖。前端在当前笔记本视图审核（批准→active / 拒绝→删除）。
- **关系边消费（§7.4 基础）**：当前主线使用 `knowledge_relations` 与 `/unified-kg`，不再依赖各对象 payload 中的 `related_rules/cases/methods/concepts` 自由文本去临时推边。
- **Object 级知识图谱可视化（§7.4）**：前端「知识图谱」改为读取 `/unified-kg?level=object`，Concept / Claim / Formula / Procedure 同屏展示；主 canvas 直接绘制节点名称、类型形状/颜色、边关系标签，并按容器尺寸响应式布局；密集全量视图用类型分区与标签降噪，左侧提供可选一种或多种类型的过滤；侧栏提供按类型分组的节点总览，选中节点会聚焦 canvas 并展示 payload、相邻关系和「出处」；问答知识对象引用会按真实来源 notebook（含挂载 base，纯 graph-BFS anchor 也保留来源）定向补载核心范围外的一跳邻域，并把原始 Concept id 有界解析为 canonical `focus_id` 后精确选中、居中，同时保留 raw object id 供 context 读取；浏览器以 active notebook 过权限，后端在有效 participant 集内解析并代理 base 读取，避免把公共库挂载误当成直接成员权限；大库 viz 未就绪时显式提示暂不可定位且不进入全量 cluster-map fallback；「出处」使用证据卡片分离来源元数据与原文正文，避免长文件名/英文段落/公式在窄侧栏中挤成细列。Concept 节点继续拉取详情，相关 Claim / Formula / Procedure 以「相关节点」展示在出处下方，并按类型分组且复用 canvas 的类型颜色/形状。
- **新类型织入 ask**：`AskResponse.related_knowledge`（通用块）召回 KG top 命中 + USABLE 一跳邻居；前端 AnswerView 不再把所有相关知识平铺在答案下方，而是用顺序引用承接证据，并在引用区提供知识图谱入口供用户继续浏览相关节点。
- **证据绑定升级**：KG `build_records()` 会把 LLM 节点 evidence 绑定到 source elements；离线 smoke 覆盖 exact/fuzzy binding、window grounding 与 ungrounded node drop。
- **顺带修复**：`routes.py` 缺失的 `NotebookTemplate` import（API 模块导入即 NameError）。
- **验证**：当前 `smoke_backend.py` 覆盖 `check_object_schemas / check_kg_record_binding / check_kg_extract_window_grounding / check_kg_store_ask_and_conversations` + API route smoke；`check.sh` 全绿。

## 24. 类型决策从「建库」移到「上传/单文件」+ 描述自动生成 + API 层冒烟

- **动机**：库类型不应在建库时选；应按**文档内容类型**选 schema，且粒度到**单个文件**（一个库可混论文/方案/复盘）。
- **建库极简化**：去掉模板/库类型与富字段预填，建库只留**名称 + 描述**。描述留空 → `purpose_auto=1`，在用户添加**首批来源**后由来源内容自动生成（LLM 配置时 1–2 句摘要，否则「N 个来源 + 类型涵盖…」启发式）；用户手改描述后置 `purpose_auto=0`，不再覆盖。
- **per-file 文档类型**：`sources.doc_type`（迁移）；上传接口增 `doc_types` 表单数组（与 files 按序对齐）；当前 profile 解析为 **source.doc_type 优先 → 空/auto 内容判别 → 默认 academic_paper**。`GET /doc-types` 暴露自动检测 + academic_paper/textbook。
- **前端**：建库改「名称+描述」弹窗；上传改**暂存式**——选文件→列清单→每文件文档类型下拉(默认自动检测)+「全部设为…」→确认上传；移除「从模板…」入口。添加来源弹窗现会中间压缩超过 48 个 Unicode 字符的文件名并保留末尾/扩展名及完整悬停提示；整个暂存批次超过笔记本剩余文档名额时，确认按钮在请求前直接禁用并显示剩余/超出数量与处理建议，操作区底部也保留明确留白。
- **API 层冒烟（夯实）**：`check_api_layer` 用 TestClient 真起 app 跑遍各路由组 + 错误码契约(404/400/422)，补上「测试从不 import routes」这个盲区（此前 `NotebookTemplate` 漏 import 即因此潜伏）。
- 备注：`/notebook-templates` 端点与 `notebook_templates.py` 现已无人使用，留作后续清理。

## 25. 冒烟脚本对齐 KG-native 当前架构（2026-06-04）

- **根因**：`scripts/smoke_backend.py` 仍导入已删除的 `app.services.extraction`，并断言旧 rule/method/risk/case/checklist/glossary 启发式候选；当前代码已改为 KG-native 抽取，离线无 LLM 时只记录 `no-llm` run。
- **脚本迁移**：
  - 删除旧 extraction.py 依赖，改测 `extraction_profiles.py` 当前 profile（academic_paper/textbook + concept/claim/formula/procedure）。
  - 增加 KG evidence binding / `extract_window` grounding / KG windowing / `store_kg` / graph / Ask / conversation 的离线 smoke。
  - API smoke 改为动态知识接口：`/knowledge-types` + `/knowledge?type=...`，不再要求已不存在的 `/rules` 等旧浏览路由。
  - 主 smoke 明确验证离线上传后 `extraction_runs.run_type='kg'` 且 `error_message='no-llm'`；需要检索/治理断言时由 smoke 显式写入 KG/rule 对象。
- **真实后端修复**：Ask 主命中已排除 `deprecated`，但 1-hop KG neighbour 查询此前没有按 USABLE 状态过滤，会把 deprecated 邻居重新带回 `related_knowledge`；现已在 `SQLiteRepository.ask()` 的 neighbour SQL 中增加 status 过滤。
- **验证**：`bash scripts/check.sh` 通过（后端 py_compile + KG-native smoke + 前端 `tsc --noEmit`）。脚本中的缺文件栈是故意触发 parse failure，以验证 pipeline `error_message` 能记录真实异常。

## 26. 大型文档摄取与检索加固 + 死代码清理（2026-06-05）

针对上传大型结构化技术手册（如 2.6MB Cadence Innovus UG）暴露的解析/成本/内存问题做的系统加固：

- **统一结构化解析**：新增 `structural_markdown.py`（markdown-it-py）——代码块整块保真、表格结构化、`<a id>` 锚点丢弃、section 面包屑；`parsers.parse_markdown` 与 `kg/parsing.parse_elements` 复用同一实现。**代码块不进 KG 抽取窗口**（代码内容不再被抽成实体），仍存为元素供检索/引用。
- **KG 窗口化贪心打包**：`make_windows` 把相邻 prose 合并到目标字符、吸收碎小节，成本随文档线性而非按小节爆炸（实测 Innovus 4330→329 窗口）；窗口数超 `kg_window_warn_threshold` 记 WARN 不截断。
- **嵌入并发化（历史实现）**：当时元素向量与知识对象向量使用独立 `embed_concurrency` 线程池；该模型并发旋钮现已退役并由 §29 的物理服务 `max_concurrency` 共享调度取代，batch/持久化分块仍只是作业形态，不代表额外模型容量。
- **抽取优先管线**：`process_source` 前台跑 KG 抽取，元素向量化在后台 daemon 线程并发；`extracted`（前端绿）只看抽取完成。`_connect` 开 WAL + `busy_timeout` 支撑并发写。
- **检索内存/性能**：`ask()` 把向量流式读成每-notebook L2 归一化 **float32 numpy 矩阵**（`vector_index`）+ `vector_cache` 版本键缓存，`query_sims` 单次 matmul。峰值内存大幅下降（实测大 KG 1.3G→约 500M），重复查询亚秒，消除大 KG 下的 OOM/卡死。`_TYPE_WEIGHT`=claim/formula/procedure/concept=1.0/1.0/0.7/0.5。
- **产品行为**：导入后不再自动生成/覆盖笔记本名字/描述；前端「＋新建」直接创建未命名笔记本并进入（去弹窗）；状态点绿色只给 `extracted`、中间态橙。
- **配置旋钮（历史记录）**：本节当时引入过 `kg_extract_workers`、`embed_concurrency` 等独立模型并发参数；它们不是现行配置，已由 §29 每个物理服务唯一的 `max_concurrency` 取代。窗口大小、batch、持久化分块、SQLite timeout 与检索数量仍属于内容/本地作业调优，不能覆盖模型服务容量。
- **死代码清理（历史记录修正）**：当时先移除了 `/case-search`、`/checklist`、`/sources/{id}/extract` 等 legacy；其后 articles / derived-rules 也已从当前 runtime 移除。当前保留的是通用 knowledge 治理、duplicates/merge/conflicts、reports 与图谱治理路径。
- **验证**：`bash scripts/check.sh` 通过（py_compile + KG-native smoke + 前端 `tsc --noEmit`）。

## 27. Agent Memory 与 MCP（方案 §19；Agent Memory 设计 §4～§13）

- **独立 Memory 层**：schema v13 增加 `memory_items`、revision、provenance、embedding/FTS、
  Agent profile/token/allowlist 表。每条 Memory 同时绑定 `created_by` 与一个 notebook；总 Memory
  页面只聚合当前用户，notebook 卡片以批量 summary query 显示当前用户数量，工作区标签为
  `问答 (Ask) | 知识库 (Knowledge) | 记忆 (Memory) | 深度报告 (Deep Report)`。共享 notebook 不共享成员 Memory。
- **手动回答沉淀**：Ask 回答提供“保存到 Memory”，先调用 preview、允许编辑，再由用户确认写入
  confirmed Memory 和可信 answer/citation provenance。同一用户重复保存同一 answer 幂等返回已有
  Memory；预览后 answer 删除则保存返回冲突。未配置或调用失败的 LLM 使用问题标题 + 清理显示引用
  后的回答作为确定性 fallback。
- **Memory 引用可追溯（§19.1 / §19.3，2026-08-04）**：既有 `memory_provenance.citations` 现在在 Memory 卡片展开为来源显示名、不同于显示名时的原始上传文件名、位置和原文摘录，不再只显示数量；notebook 内的活引用可经参与集代理打开精确 source element。跨 notebook 复制/移动后会从嵌套 provenance 的最深原始层恢复引用，但明确标为仅存档且不授予目标库跳转权限。历史已保存 Memory 无需迁移即可恢复显示；前端投影/多跳传输回归、后端保存 API 与完整 `scripts/check.sh` 已通过。
- **生命周期与权限**：Agent 只能创建 candidate；用户可编辑、确认、拒绝、弃用。Candidate 在同一
  用户、同一 notebook 下由具备 `memory:read_candidates` 的所有 Agent profile 共享；不同用户、
  不同 notebook、rejected/deprecated 均排除。用户丢失 notebook 访问权时 Memory 暂不可读/检索；
  notebook 删除按 FK 生命周期级联成员绑定的私有 Memory，前端删除确认已有不泄露成员明细的警告。
- **输入合同**：Pydantic/API、service/internal 与 MCP 共用同一 Memory normalizer；tag 原始序列先
  校验最多 20 条，再 trim/去重，空白 tag fail-closed 拒绝，避免重复/空白绕过数量上限。
- **两个检索平面**：candidate+confirmed 只进入 scoped Agent Memory 平面；正式 notebook Ask、
  notebook 搜索、Deep Report 与 `search_notebook_context` 只接收 confirmed。Memory 命中使用独立
  anchor/provenance，不伪造 source/element id。排序先判相关性，只有等分/冲突再应用
  `candidate < personal source < confirmed Memory < base KG/base source` 权威规则。
- **Agent 接入 UI 与 token**：总 Memory 页可创建/停用稳定 Agent profile，签发明文只显示一次的
  opaque token，配置默认 notebook、notebook allowlist、过期时间与最小 scope，并列出、撤销 token。
  签发回执现同时提供公开、机器可读的 `GET /api/agent-mcp/onboarding` 链接：Markdown 使用
  `MCP_PUBLIC_URL` 给出精确 MCP 地址、从 `PUBLIC_TOOLS` 派生当前工具清单，且在 warm-up 期间也
  可匿名读取；token 与链接分开交付，端点不接收、不拼入也不回显 bearer token（方案 §19.3）。
  Scope 为 `knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
  `ask:execute`、`knowhow:code`；撤销、过期、profile 停用或 notebook 权限变化会在后续调用重新校验并立即生效。
- **官方 MCP Streamable HTTP**：`/mcp` 提供十一个工具：Memory/context 七工具 `list_notebooks`、`select_notebook`、
  `search_agent_memory`、`search_notebook_context`、`get_memory`、`ask_notebook`、
  `propose_memory`，及 knowhow 四工具 `list_knowhow_tables`、`get_knowhow_discrimination`、
  `get_knowhow_row`、`put_knowhow_cell_code`（2026-07-16 随 knowhow 表 Agent 面加入，读取需
  `knowledge:read`、代码写入需 `knowhow:code`）。**以上是本条目交付当时的工具面；后续已扩展至二十四个工具
  （引用点查、来源管理、构建与库理解工具组，当前权威清单见 `mcp_server.PUBLIC_TOOLS` 与
  `docs/product-and-api.md`）。** 每个新 session 必须先显式选择 allowlisted notebook；数据工具继续校验 notebook，
  候选只能提交不能由 Agent 确认/拒绝/弃用/晋升。loopback 可用 HTTP，非 loopback/public URL 默认
  允许明文 HTTP（放宽 Host/Origin 校验并打印启动告警），设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS；
  返回私有文本按不可信 evidence 处理并做长度/结果数上限。
- **Memory → KG**：仅 confirmed 且尚未提案的 Memory 可由创建者提议；既有 admin promotion
  queue 展示脱敏后的结构化提取候选与服务端验证过的 evidence，不提供原始 Memory
  revision/provenance 浏览。编辑或弃用 proposed Memory 会在同一事务中拒绝活跃队列项、保留固定
  快照审计、清除当前 proposal 指针与 promotion 状态。批准前重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，批准
  复用 dedupe/merge，创建或合并一个或多个 Base KG 对象；API 与晋升审计保存完整
  `base_object_ids`。私有 Memory 的 owner/status 不改变，完整私有任务上下文不进入 Base KG 对象。
- **确定性评价与验证**：固定 gold 计算 Recall@5/MRR/nDCG，candidate→正式平面、跨用户、跨
  notebook 三个泄漏计数均为 0；A/B harness 覆盖 no-Memory、KB-only、KB+confirmed-Memory。
  `scripts/check.sh` 已包含官方 `mcp` client 离线 smoke，当时验证十一个工具契约（七个 Memory/context
  工具与四个 knowhow 工具；该 smoke 现按上述扩展后的工具面锁定）、session 选择隔离、candidate
  正式平面隔离和同用户同 notebook 跨 Agent 召回。本次门禁结果：后端 `2939 passed, 1 skipped`、
  前端 `189 passed`、TypeScript 与 Next.js production build 均成功。

## 28. Knowhow 新表双方向导入（2026-07-20）

- **新表导入方向**：`POST /api/notebooks/{id}/knowhow/import/preview` 与 `POST /api/notebooks/{id}/knowhow/import` 支持请求级 `orientation=columns|rows`，默认 `columns` 兼容旧客户端。
- **服务端统一规范化**：xlsx/csv/Markdown 原始矩阵在校验前按用户选择转置；属性行输入先补齐不等长行，再转成内部“列是属性”的网格。预览和正式导入使用同一方向，追加导入、存储、检索与投影契约不变。
- **前端闭环**：导入向导第一步可选“属性按列 / 属性按行”，预览显示最终规范化形态；属性按行默认建议首列为行标题，用户可改选或取消。
- **可操作失败提示**：属性行表的记录分组使用横向合并单元格时，解析器会识别展开后的重复首行并提示改选“属性按行”；空/重复表头、不支持的文件或编码、失效列设置等校验错误经安全标记进入向导，明确说明修改方式，内部异常仍不直出。
- **验证**：相关解析/API/前端契约测试、`scripts/check.sh` 与前端生产构建均通过。

## 29. 系统模型服务统一管理与全局调度（2026-07-22）

- **部署统一配置**：chat、embedding、rerank 服务改由部署 TOML 集中声明，`.env` 只保存 TOML 通过 `api_key_env` 引用的密钥。注册表共有 32 个稳定 workload；TOML 可按部署需要选择绑定，但每条已配置绑定都必须指向同种类物理服务。用户不能再保存或覆盖 endpoint、凭据、模型名与容量。配置路径留空时明确走离线/确定性降级，非空无效配置启动失败。
- **按物理服务控制全局并发**：每个服务只认一个容量参数 `max_concurrency`，共用该服务的所有在线、报告、后台与批处理 workload 进入同一个进程级调度器；不同服务容量互相独立。队列总量封顶 `10N`、单 actor 封顶 `2N`，按 interactive:report:background = `8:2:1` 调度并在同优先级内按 actor 轮转，三类排队截止时间分别为 30/300/1800 秒。一次致命错误或连续三次瞬态错误会熔断 30 秒，half-open 只放行一个恢复调用。
- **单进程容量语义**：生产脚本固定后端 `--workers 1`，避免多进程把 TOML 容量成倍放大或分裂队列、熔断和健康状态。`KG_JOB_CONCURRENCY`、批处理来源 worker、自适应窗口、embedding batch 与本地 CPU/ANN 线程都不能覆盖模型容量。
- **只读状态与维护诊断**：普通用户的「模型服务」页面只展示脱敏服务身份、workload、容量、运行/排队、熔断及最近健康状态，页面读取不探测上游；只有 admin 可显式测试单服务或全部服务。模型故障向用户返回安全 service/model 标签与 `support_id`，用户把该 id 提交给维护人员，由维护人员结合服务端日志定位具体坏掉的服务；endpoint、凭据、provider body 与 raw exception 不进入状态/UI。
- **个人配置彻底下线**：个人模型配置路由、草稿测试、保存控件与配置页面已删除。schema v24 在版本事务中不可逆地把历史 `user_profiles.model_settings` 清为 `{}`、删除旧的逐用户健康行，并按部署服务 ID 持久化当前健康状态。
- **架构边界与验证**：业务代码只按 workload 从 `RuntimeModelProvider` 取得受调度 adapter，raw transport 被限制在审查过的底层边界；repository 不再暴露模型 client 或 embedder。系统模型 registry/scheduler/provider、状态/API/UI、迁移与架构守卫的定向后端和前端测试已通过；完整离线 `scripts/check.sh` 与 production build 在本次发布最终验收阶段统一记录。

## 30. Knowhow 单行空列智能补全（2026-07-23）

- **用户闭环**：记录行详情和以行标题组织的矩阵分支都提供“智能补全空列”；前端会提交该行全部可补空列，服务端只为本次请求中当前值严格为空字符串或尚无 cell 记录的非行标题列生成建议，不自动写入。响应除逐列 suggestion/abstain、置信度、依据与同表参考外，还带稳定的检索模式/范围/状态、最终推理轨迹、服务端签发的证据 key 与库内证据卡；审阅窗分开呈现两路证据，用户逐项确认后才复用既有单元格保存或合并单元格批量保存路径。
- **双路有界取证**：`POST /api/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/complete` 把同表 schema、当前行已知列和最多 8 条相似参考行，与一次对“当前 notebook + 当前有效显式挂载参考库”的 `ReasoningRetriever` 检索合并；整行所有目标列共用一次 `top_n=12`、`max_steps=6` 的 plan→联邦检索→反思/扩展/查询期推导。补全专用策略在候选进入模型反思前排除私有 Memory 和当前整表投影，并关闭来源归属不透明的 PPR/社区扩展；不调用 Ask 答案合成，不创建对话/job，也不保存 Ask 答案。候选扫描 512 行、已知列 32 个、评分文本每格 1000 字符、检索查询 12000 字符、库内证据 24 张/24000 字符（单摘录 900）、最终 prompt 96000 字符均有硬上限。
- **接地与故障语义**：`reasoning_agent` 负责逐步检索，`knowhow_complete` 负责结构化合成；两阶段都以 system 级指令把格子和来源文本视为不可信证据。模型只能引用允许的同表 row id 或服务端 evidence key，伪造 key 被过滤，过滤后无引用的建议强制 abstain；单证据通道最高 medium，双路印证才允许 high，base/personal 冲突时以 base 为准并披露。两 workload 任一未配置返回可操作的 400；provider/检索失败、推理响应畸形，或合成响应不可解析/顶层结构不可用时会按实际 workload 记录脱敏错误并返回安全 502；单条 suggestion 畸形则过滤、降级或转成 abstain，绝不静默退成同表补全或伪造离线结果。跨库证据 Markdown 禁用链接、图片请求和 active-notebook 资产改写。
- **并发与历史一致性**：打开复核窗时冻结目标列与原始空值；接受建议使用 `expected_before` 乐观并发保护并记录 `origin="llm_complete"`，历史界面显示“智能补全”。行标题分组中的共享目标格会冻结并校验所有物理成员行、目标空值与行标题值，任一成员已变化则拒绝写入，不允许覆盖用户或协作者的新内容。
- **验证**：补全 API、双路证据映射、Memory/当前表前置过滤、严格模型故障、提示注入边界、预算、权限、并发保护、历史来源、前端纯逻辑和组件交互均有回归测试；专项后端 131 项、前端 Node 1416 项、组件 51 项通过，完整 `scripts/check.sh` 与前端 production build 已通过。

## 31. Knowhow 批量规整审阅、审计 actor 与内容感知列宽（2026-07-25）

- **批量候选与一致性**：行/整表一键规整的候选生成改为有界 worker pool，并发取 `min(3, knowhow_reformat 实时服务容量)`、状态失败回退 2；相同列与 trim 后原文 single-flight，只有成功且仍 fresh 的 leader 结果进入缓存并扇出，失败或 stale 时下一物理格可继续重试。AbortSignal 与 run epoch 阻止取消后的新请求和迟到回写，进度按物理格计数；保存仍按既有完整物理/共享单元串行，完整保留 frozen snapshot、`expected_before`、anchor/精确成员守卫、409 stale、候选保留与关窗后 reload。
- **逐项审阅与打开格子**：批量队列的有改动/已保存项可在同一弹窗进入单项 Markdown diff，提供行级增删、面向中英文/emoji/空白/标点/Markdown 控制符的行内 token 高亮、严格字符/行/Myers/token 矩阵预算与超长降级，并可切换渲染预览；返回队列恢复滚动与焦点。已保存项先安全关闭弹窗，再打开既有格子详情；共享格稳定选择 `row.position`、再 row id 最小的代表格。整体确认保存语义未改成逐项接受/拒绝。
- **审计 actor 治理**：稳定 ownership/权限/`created_by` 继续保存 user id；普通 session 审计快照统一为 `username.trim() → display_name.trim() → user.id`，Agent 保持 `profile_name`。import/create、单表复制/移动和整本 notebook copy 均拆开 identity id 与 actor label；历史页、单变更、单格历史、diff 与 Agent/MCP 代码附件展示在最多 512 个候选、每批 200 个 id 内解析旧 user id，未知/删除用户及 Agent 自由文本原样回退，无 N+1、无 schema migration、无破坏性回写，fingerprint 中的 `knowhow_cell_code.updated_by` 历史字节保持不变。
- **内容感知列宽与补全回归**：主表继续使用 fixed layout、横向滚动与 sticky 首列，通过 `colgroup` 应用 memoized 宽度；只采样可见物理行前 48 + 后 16（最多 64），每格最多 8 行/每行 120 grapheme，按 Markdown 可见文本与 CJK/全角/emoji、ASCII、标点、空白权重估算，并套 desktop/窄屏 clamp，状态列固定。本次未增加拖拽或持久化。既有“智能补全空列”仍是零新增：行详情与矩阵物理分支入口保留，只补缺省/精确空串，纯空白和已有内容不可覆盖，最多 8 条同表参考、一次有界推理检索、最多 20 项且逐项接受。
- **验证**：新增/相关前端 Node 583 项、组件 13 项、后端专项 175 项通过；分拆完整后端（排除需本机端口的生命周期文件）6579 项通过、341 项跳过，生命周期 9 项在沙箱外串行通过；`npm run build` 通过。最终以 `BACKEND_PYTEST_WORKERS=1` 运行完整 `scripts/check.sh` 退出 0，`git diff --check` 通过。

- **深度报告可信度与全篇综合（§6.7 / §11 延伸，2026-08-01）**：共享意图合同现在保留 `result_scope`/`completeness_required`，完整枚举未接入时如实披露相关性检索边界；规划、正文与界面共用完整来源计数、保守可区分资料、重复/元数据/时间边界画像。充分性不再由对象命中数授权，高风险事实句接受同句锚点审计，参考文献披露集中度。比较/综述可确认正交框架；详尽/穷尽档先完成全部章节检索，再以至多一次全局综合蓝图统一定义、条件、反证与章节 owner，随后才并行写作，主张账本、趋势资料数封顶和跨节冲突供终审只审计。所有可选结构畸形均回退旧路径，不新增 migration；SQLite 与 PostgreSQL、前端全量测试/build、删除与移动变异验证均通过。
- **深度报告可信度修复批（同一功能，2026-08-02）**：用户确认或清空的框架在生成阶段保持权威，终审上下文按节预留正文/账本预算并只裁剪完整 JSON 记录，且总量上限内始终保持语法合法；高风险扫描修正英文子串、中文单字、无单位编号、普通中文序号与英文切句误报，审计始终披露而自动降级默认关闭。资料基础改为 SQL 聚合、有限分布桶与代表页，不再把全量来源族映射复制进意图/大纲/prompt；仅对实际触达来源有界解析身份，身份未知来源在参考文献中逐份可见而不增加独立来源数，覆盖、充分性和引证集中度按未知支持最不利归入最大已确认族的上界计量。低档恢复逐节检索→撰写流水线，高档综合区分可用、无证据跳过、模型失败与校验失败并上屏；已知低档的 `not_requested`/`0/N` 纯否定回执默认静默，旧报告深度未知、高档 no-op、跳过/失败和可用账本仍可见。可信度回执和引证分布由实际报告详情路径挂载，并由组件路径与源码位置两类守卫同时覆盖；旧版 `outline_ready` 的节级 frame 与内嵌 intent 镜像不一致时，生成、持久化 understanding 与冲突审计均优先节级用户副本。来源层级徽章复用 Ask 统计，框架维度可直接删除。新增 SQLite 与 PostgreSQL 双后端行为用例直接执行六项资料画像查询及代表页排序，不再以 SQL 字符串守卫代替真库覆盖；完整 G1、前端 production build 与真实 PostgreSQL lane（240 passed / 2 skipped）通过。故意改成不存在的 PG 列、删除 `NULLS LAST`、恢复旧中文数字边界以及删除/移动前端挂载时，新增守卫均按预期变红并在恢复后转绿；不新增 migration。
- **资料基础缺席归因与全篇综合覆盖全深度（同一功能，2026-08-05）**：本条取代上面两条里「详尽/穷尽档才做全局综合」「低档保持逐节检索→撰写流水线」「已知低档静默 `not_requested`/`0/N`」三处口径。①「资料基础」缺席不再只落空 dict：收窄来源范围是有意跳过（`scope_restricted`）、聚合出错才是失败（`failed`），两者各自持久化 `unavailable_reason`，报告正文与界面给出对应说法，只有失败才发 `report_corpus_profile_failed`，两者都 fail-open；不可用标记是非空 dict，因此 `_build_corpus_map` 不能再靠 `or` 短路链判定，否则它既不触发重探又会被格式化成一份每个计数都是 0 的假画像送进规划；历史报告存的是空画像、事后无法归类，保持原失败文案。②全篇综合的判据从 `depth >= 8` 改为章节数 ≥ 2：所有深度统一走「检索屏障 → 一次全篇综合 → 并行撰写」，深度只决定检索预算，只有单节报告（跨章节一致性不成立）保留流水线且不付那次调用；已登记接受的代价是低档不再逐节流式出稿、每份多节报告多一次模型调用。六处散落的 depth 判定收敛到 `report_synthesis_requested()` 单一判据点，`_draft_section` 由调用方传入 `synthesis_requested`；前端静默规则改为「单节」，`depth < 8` 保留为历史报告兼容分支。四份 README/AGENTS/CLAUDE 与 `docs/product-and-api*.md` 契约段及上限表同步（新增「触发全篇综合的最小章节数 = 2」）。G1 退出 0；最小章节数 2→1、综合闸回退 `depth>=8`、`_build_corpus_map` 恢复 `or` 短路链、前端两种文案合并回一句、静默判据回退纯 depth 五项变异均确认报红；另有一项变异（单删 `_build_corpus_map` 的防御性 `if`）打空，查明该分支不可达后据实标注为防御并改写测试名，不留假守卫。不新增 migration。
- **报告耗时可见、资料基础覆盖参考库、分段计时与 diag 后端选择（2026-08-05）**：①`_generation_started_at` 由 `claim_report_generation` 写入，但引擎终态用内存意图合同整体覆盖 `understanding_json`，把这个 store 私有键冲掉——真机 11 份 done 报告 0 份留有该戳，界面因此对**已完成**报告（最需要看耗时的那些）永远显示不出生成耗时。两侧 store 的 `update_report` 改为在 SQL 内保留该键（PG 用 `||` + `jsonb_build_object`，SQLite 用 `json_set` + `json_extract`），从未认领过的报告不得凭空造戳。既有用例漏掉它是因为调 `update_report` 时从不传 `understanding`。②「资料基础」只统计当前笔记本，而检索跨已挂载参考库：真机最新报告画像 4 份、42 个锚点里 26 个来自挂载库，正文却说「基于当前可见的 4 份资料生成」。披露改为另行点明实际引用的参考库资料份数（由已装配引用推导、零新增查询、按来源去重不按锚点），前端资料基础卡同步；纯本地报告文案逐字不变。③报告生成新增 `report_stage_timing` 分段计时（检索/综合/撰写各一条，只带 index 与毫秒，不带标题或正文）：模型延迟本可从 LLM 日志回推，检索不能，真机一份报告 59.5 分钟墙钟对 ~27 分钟并行模型时间，差额此前无从解释。④`scripts/diag_*` 全部硬编码读 `.local/silicon_notebook.db`，在 PostgreSQL 部署上不报错而是**静默读一个陈旧 SQLite 库给出错误诊断**；新增 `diag_common.resolve_database_target()` 按 `DATABASE_URL`（进程环境优先、回退 `.env`，与 pydantic-settings 同序）判定后端，非 SQLite 时明确跳过并说明原因，诊断输出只保留 scheme、不带凭据/host。`--db` 显式指定仍照办，只有缺省值那条假设被收回。G1 退出 0，PG lane 新增用例真跑 jsonb 保留表达式；两侧 store 回退裸赋值、去掉 `.env` 回退两项变异均确认报红。不新增 migration。
- **深度报告公开分享链接（2026-08-06）**：单份已完成报告可发布成**免登录**只读页（schema v42/PG v20：`reports.share_token` + `shared_at`，只覆盖已签发 token 的部分唯一索引）。发放幂等——重复分享返回同一 token，已发出去的链接不会突然失效；撤销后与从未存在的 token 同为 404；未完成报告拒绝分享（409）。匿名端点挂**独立 router**：主 API router 带 router 级 `Depends(get_current_user)`（"零逐路由遗漏"），公开端点挂在那上面会 401 拦掉它要服务的访客——这一条**只有在真实认证环境下才暴露**，`auth_optional` 的测试夹具里缺 token 会静默回退 seeded admin，所以补了一条针对接线本身的守卫。它也因此不绑定请求用户，只能调不依赖 current-user 的仓储方法（`public_report_by_token` 只收 token）。投影是**白名单**而非脱敏：正文、问题、时间，加每条引用的标题/原始文件名/位置/摘录；`source_id`/`element_id`/`object_id`/`notebook_id` 与整个 `understanding`（意图、冻结的来源范围、可信度内部量）一律不跨出去——公开页本就打不开原文，给这些 id 只会让人拿去探测已认证接口，而资料基础披露在生成时已固化进 `content_md`。前端新增 `/r/[token]` 独立页（不 import 主应用），报告详情页加分享/取消分享按钮并即刻复制链接。真实认证环境端到端实测：匿名 GET 200、无 token 写 401、内部 id 与意图零泄漏、撤销后 404。G1 退出 0，PG lane 380 passed；把公开 router 挂回 session 守卫的变异确认报红。
- **全篇综合 facet 标签修复阶梯与逐条降级（同一功能，2026-08-10）**：生产报告因模型把蓝图 claim 的 `facet_id` 填成 facet 的取值（"ADC"/"DAC"）而非 id，整份综合蓝图被原子作废（`failed_validation`），跨节一致性协调全部丢失。facet 标签是组织性标注、后端零代码消费者，据此把它从原子判据中移出：校验先走确定性修复阶梯（精确 id → `id:value` 前缀收窄 → facet id/名称/声明取值的大小写不敏感唯一反查，声明 id 优先、两个及以上 facet 共享的拼写判歧义绝不猜测归属 → 冒号前缀反查），修不回来只清空该条主张自己的标签，绝不再因 facet 标签作废整份蓝图；证据绑定、章节归属与结构违规仍原子作废，语义逐字未动。修复/清空计数经蓝图私有键带出，engine 在蓝图进入撰写与终审链路前摘除该键（终审上下文会整只序列化蓝图，不摘会漏进 prompt），非零时发仅含计数与不透明 id 的 `report_synthesis_facet_tags` 事件。综合 prompt 同批加固：调用点从 normalized frame 提取合法 facet id 逐字枚举进指令段（不放取值反例，避免示例槽诱导照抄；位置有相对序断言钉住）。四份契约文档同步（product-and-api 双语、AGENTS、CLAUDE）。G1 退出 0；反查表置空、降级回退原子丢弃、三 facet 歧义守卫删除、id 优先跳过删除、枚举句移到 schema hint 之后、引擎调用点删参数、`row["id"]`→`row["name"]`、pop 改 get 共八项变异均确认报红后恢复全绿。不新增 migration。
- **MCP 管理面补全：来源管理、构建与引用点查（2026-08-17）**：`/mcp` 工具面从 11 个补到 **20 个**——新增引用点查 `get_cited_element`（按 `source_id`+`element_id` 把一条引用还原成原文、位置与显示标题，作用域与答案本来就能引用的一致，含已挂载参考库）、来源管理五工具（`add_source_text`/`add_source_url`/`get_source_status`/`reparse_source`/`delete_source`）与构建三工具（`build_kg`/`build_retrieval_index`/`get_build_status`）。配套三个新 scope：`sources:write`、`sources:delete`、`maintenance:execute`。**写类一律 owner-only**：token 白名单可以包含 owner 只是以只读成员身份加入的共享库，在那里落一次 Agent 写入等于把共享的读侧升级成写侧，因此过 `user_can_access_notebook`（产品里唯一的写谓词）；与之刻意分歧的是 knowhow 代码写入仍是成员面（设计 §⑥-4 明文不加 owner 轴）——删文档的爆炸半径大得多，这是拍板取舍不是疏漏。`delete_source` 另需 `sources:delete`（`sources:write` 不蕴含），且**只能删 Agent 添加的来源**：判据是 v48/PG26 新增的可空 `sources.agent_profile_id` 投影出的 `agent_created`，与证明笔记本归属的是同一次单行读取；判据取「某个 Agent 添加过」而非「本 profile 添加过」，否则轮换掉的 profile 会留下永远删不掉的来源。出处只在 INSERT 分支写入，所以同内容去重复用用户的行时该列保持为空（重传用户的字节洗不成 Agent 可删）、深拷贝显式清空（副本一律视为用户添加），列缺失时投影默认 false、fail closed。`ask_notebook` 同批扩约：可选 `conversation_id` 并回传实际记入的会话 id（同 owner+同 notebook 才接续，否则静默新建、靠比对回传值察觉），anchors 补 `source_id`/`element_id`/`knowhow`，新增 `citations` 回退列表——其中 `memory_id` 非空的行需要 `memory:read`，无此 scope 时整行必须在结果截断**之前**过滤且不计入截断计数，否则被隐藏的私有 Memory 条数可由 20-18 这样的算术还原出来。前端来源列表显示中性「Agent 添加」徽标，签发界面新增三个勾选项。守卫同批整固：`mcp_server.PUBLIC_TOOLS` 成为唯一清单，`tests/test_memory_mcp.py`、`scripts/smoke_memory_mcp.py` 与架构文档守卫全部从它派生（此前三份手抄清单可以互相认同一个陈旧值而与真实注册面脱节），文档工具表改按集合相等校验、scope 面改从 `AGENT_SCOPES` 派生，并新增两份 SOP scope 表的中英对仗守卫。历史记账（本节第 27 条与 `silicon_notebook_fangan.md`）保留「十一个工具」原文，只补一句同样由守卫派生计数的前向指针。G1 契约 lane 与 `tests/test_architecture_documentation.py`/`tests/test_memory_mcp.py` 全绿；PUBLIC_TOOLS 增/减工具、文档工具表多写假工具、`AGENT_SCOPES` 加假 scope 不改文档、只改中文 SOP scope 表、两份 README 计数说法回滚、把工具表整体移到未被覆盖的文档等变异均确认报红后恢复。
- **Agentic Memory P3：观察队列 + 用户回答偏好（2026-08-20）**：设计真源 `docs/superpowers/specs/2026-08-18-agentic-memory-design.md` §6.2/§7；本条完成该文档 §10 P3 行标。两条相互独立的线，只在 schema v55/PG v33 迁移上耦合。**C 线（MCP 开放）**：`/mcp` 工具面从 20 个补到 **22 个**——新增 `get_notebook_profile`（scope `agent_profile:read`，只读版「AI 对这个库的理解」，投影仅 `{label, value, updated_at}`，不带 `evidence` 来源 id，标注 `content_is_untrusted_evidence`/`citable: false`）与 `add_observation`（scope `agent_observation:write`，向调用者自己在该库的观察队列追加一行、`client_request_id` 幂等去重）；新表 `agent_observations`（叶表，一个出向 FK 到 `notebooks`，`(notebook_id, owner_id, agent_profile_id, client_request_id) WHERE client_request_id IS NOT NULL` 部分唯一索引承担 NULL 停车，另有非唯一的 `idx_agent_observations_scope` 支撑按 `(notebook_id, owner_id)` 的环形淘汰/读取）。`add_observation` 是**第二个**绕开 `_writable_notebook` owner-only 门的 Agent 写（第一个是 `put_knowhow_cell_code`）：爆炸半径结构上只到 token 持有者自己的覆盖层而非整库检索，因此只读成员自己的 Agent 也能用它。观察记录是**不可信**输入，只喂覆盖层巡固——巡固读到观察记录时会在 user 消息前插一条 `system` 指令声明每行是数据而非指令，且观察只有与该成员自己的提问/报告相符才能支撑一个论断；仅有观察记录不足以单独触发一轮巡固，观察也绝不移动 `usage_gaps` 的零命中计数。观察段独立 600 字符预算，不占用既有提问/报告共享的 3,000 字符段。管理走「我的」半侧：`GET`/`DELETE /notebooks/{id}/agent-observations`（读权 + 行级归属即可，不需要 `agent_profile:write`），网页面板「我的检索心得」下新增「Agent 记录」折叠小节。**B 线（B-Profile）**：新增每用户偏好文档 `user_profiles.search_profile_json`（`NULL`=未设置，同 `ui_mode` 契约），四个封闭字段（`answer_language`/`answer_shape`/`answer_detail`/`domain_terms`），逐字段 `origin: "user"|"job"` 区分用户显式设置与后台归纳、job 永不覆盖 user。v1 归纳规则唯一一条且零 LLM：按该用户最近若干次提问语言的确定性多数统计写 `answer_language`；档位众数与常用领域词登记为刻意不归纳。归纳出的 `origin="job"` 值绝不单独注入 prompt（镜像 P2 经验库「先接好管线、注入待验证」姿态），需用户显式确认（即 `origin="user"` 写入）后才生效；账户菜单新增「我的回答偏好」设置面板。渲染出的风格块注入 Ask 的规划与答案合成 prompt（不进反思循环，深度报告 v1 不接），带明确边界说明「只影响措辞与组织形态，绝不影响证据可用性或引用绑定」。部署开关 `USER_SEARCH_PROFILE_ENABLED`（默认 true）独立于 `AGENT_PROFILE_ENABLED`。正向 shadow 不变量随 schema v55/PG v33 移到 82 张业务表、112 个 unique surface，12 row slots 不变（新表只带一条出向 FK）。四份契约文档、两份 SOP、`docs/product-and-api*.md` 与 `docs/development*.md` 同步；`test_architecture_documentation.py`/`test_ui_vocabulary_guard.py` 全绿，`scripts/check_ui_vocabulary.py` 与 `scripts/check.sh` 通过。
- **Agentic Memory P4：step→anchor 归因 + reflect 内 consult_memory + 步级经验提示（2026-08-21）**：设计真源 `docs/superpowers/specs/2026-08-18-agentic-memory-design.md` §10 P4 行（偏离登记 §10.3 七条）；**零 schema 迁移**。**A——归因回收**（P2 §10.1 偏离② 的回收）：八个检索写点的 trace detail 记有界 `result_ids`（≤20，截断带稀疏标并使该动作本 run 不可归因）、synthesis 步记 `anchor_evidence_ids`（≤96，ranked 锚点协议顶；枚举号段可超→truncated→整 run 不可归因，安全方向）；「走到 I/O 无条件写（零命中写 `[]`）、skip 不写」是硬判据——键存在与否区分新老轨迹。蒸馏投影 `ActionObservation` 新增 `anchored_hits: int` + `attributable: bool` 两字段（隐私守卫判据一不认 Optional[int]），不可归因动作 anchored_hits 一律归零；老轨迹回落 `(False, 0)` 与「新轨迹零命中」严格可分；观测渲染的 `anchored=` 子句仅 attributable 时在场，全老批次蒸馏 prompt 与升级前逐字节相同。隐私守卫扩两条：运行时 id 不入观测序列化、AST 三模块共扫「id 只活在 project_run 局部」。**B——consult_memory**（第 12 个 reflect 动作 id，trace 步「回想」——「记忆」被 Memory 召回步占用）：零参数、确定性零 LLM/零新增 I/O；总闸单点 = kill switch ∧ effort ∈ deep+ ∧ 注入闸（INJECT 默认关时动作不存在，不给 deep 档白吃步预算）；内容是**差集面**——经验库未送达条目（排除集只含被动块**真送达**前缀）+ 本人覆盖层未送达 `retrieval_notes` 行（先渲染，个性化信号更稀缺）；delivered-only 记账（没送达的下次还能选、`entries` 报真实送达数、送达 0 记 `consult_memory_block_full` skip 预算照扣）；执行体整体 fail-open（瞬态库读失败记 skip 不挂 run）；报告逐节深挖 deep+ 档零改动自动生效，knowhow 补全策略位显式关闭。**C——步级提示（收窄版）**：某动作本 run **真连续** 2 次零命中（命中即清零）且库有该动作 bad 条目时推最高分那一条进 reflect 账目（每动作一次/每 run ≤2，纯内存阈值前移，未达标轮次零库读）；同受注入闸默认关；不做 state-signature 全匹配。数值上限只登记 `docs/product-and-api*.md`；四份契约文档 + deployment×2 + 设计文档同批同步。

- **群组唯一 owner 与独立工作台（群组知识共享，2026-08-20）**：schema v56 / PostgreSQL v34 为 `groups` 增加生效中的 `owner_id`；存量群组从当前管理员中确定性选择 owner（创建者仍是管理员时优先），不复活已降级或已退出的创建者，新群组的创建者成为初始 owner。只有 owner（系统管理员仅作恢复旁路）可以转让或删除群组，转让目标必须是现有成员并原子提升为管理员，原 owner 保留管理员身份；owner 不可被降级、移出或直接退出，删除路径也在群组根事务内重新验证 live owner，关闭鉴权与转让之间的竞态。账户菜单中的群组管理升级为独立页面，按知识库、成员、共享申请、设置四区组织：所有成员能直接看到本组**实际有读权**的 Notebook（管理员专属边不向普通成员披露），owner/组管理员可搜索并批量加入自己有管理权的 Notebook、撤销可见范围、切换「组管理员可管理」权限；owner 转让和删除均有独立二次确认，退出/删除后 URL 自动落到剩余群组或群组空态。页面复用现有系统的色板、按钮、边框、圆角、间距和响应式断点，桌面为左侧群组目录 + 右侧工作区，390px 窄屏四页签同屏可见。已通过 `scripts/check.sh`、前端生产 build、2326 条前端守卫、479 条组件测试及桌面/窄屏真实浏览器验收。

- **群组邀请链接（群组知识共享延伸，2026-08-21）**：组管理员可在独立群组工作台的「成员」页生成、复制、换新或撤销一条可重复使用的邀请链接；token 会跨登录/注册门保留，登录用户打开后原子加入为普通成员。重复兑换幂等且不降级既有管理员，换新/撤销/删组立即使旧链接失效，无效状态统一 404。schema v57 / PostgreSQL v35 把可空 token、签发时间和签发人审计放在群组根行，并以仅覆盖非空 token 的部分唯一索引保证一条 capability 只解析到一个群组；正向 shadow 保持 82 张业务表和 12 个 row slot，unique surface 增至 113。后端覆盖签发幂等、加入、重复加入、换新、撤销与角色保留，前端补纯 helper、接线守卫和组件交互测试；`scripts/check.sh` 与前端 production build 通过。

- **Markdown ZIP 后端摄取 + MCP 通用文件上传（§6.3 / §19.3，2026-08-24）**：`.zip` 从浏览器专属交换格式升级为 backend parser capability registry 的一等格式。原始压缩包按一个来源保存，builtin `markdown_bundle` 在后台稳定解析所有 `.md`/`.markdown`，逐元素记录 `bundle_path`，按每份 Markdown 自身目录解析相对图片并把 png/jpeg/gif/webp 字节写入既有来源资产；不解到宿主文件系统，危险/重复路径、加密/不支持压缩、无 Markdown、条目或解压总量超限整包拒绝，单图缺失/远程/损坏/不支持则保留图注/描述文字并无图降级。同一包内图片被多处引用时按归一化路径复用同一资产，成功和失败结果都缓存，避免重复消耗图片配额或放大解压负载。网页上传直接发送 ZIP 原字节，拖入文件夹继续保留浏览器 data-URI 兼容路径。MCP 新增第 23 个 core 工具 `add_source_file`：严格标准 base64 接受解析注册表支持的 PDF、DOCX、PPTX、XLS/XLSX、Markdown、CSV 与 ZIP，复用既有来源去重、文档数量上限、Agent 出处、owner-only `sources:write` 和后台解析调度；官方客户端示例新增 `--source-file`。专项后端及前端上传/配置测试通过；完整 `scripts/check.sh`（后端 9,173 项、前端 Node 2,425 项、组件 653 项、production build 与类型检查）及 `git diff --check` 通过。

- **Ask 引用图片正文内联与页内预览（§6.5 / §11，2026-08-24）**：答案引用绑定到已持久化 `asset_id` 时，鉴权界面与公开会话都在该引用所在的最小完整 Markdown 块后显示图片，而非汇总到答案末尾：段落/标题紧随块后，列表项/引用块留在内部，表格等整表结束；复合引用按正文顺序合并，同一资产只在第一次引用处出现。图片块只展示图片与「引用 [n] / 模型未直接读取图片」可信度提示，caption/图片描述仅保留为检索元数据和无障碍 alt，不重复占据正文或引用浮层；直接引用图片元素时其解析器生成的摘录也隐藏，附近带图的真实文字证据则保留。点击正文或引用浮层缩略图会进入统一的无卡片页内预览：图片直接置于压暗页面上，基于 `react-zoom-pan-pinch` 支持滚轮、按钮和双击缩放、复位、拖拽平移与触摸手势，同时保留关闭按钮、背景点击、Escape 和焦点归还，预览期间引用浮层保持挂载。公开会话投影新增不暴露内部引用 id 的 `reference_keys` 绑定与纯展示布尔值；旧公开载荷缺位置键时明确降级为未定位的 image-only 区块。MinerU 与内建解析器共用同一渲染合同：只有图片字节已落资产且元素带 `asset_id` 才能展示；孤立 Markdown 的相对/本地/远程图片在上传阶段给出非阻断警告，批内串行扫描并拒绝已移除文件的迟到结果，缺失字节不伪造图片。完整 `scripts/check.sh`、TypeScript、Next.js production build 及三项 CI 门禁均已通过。

- **生产热路径修复批 0（P0 止血，2026-08-29）**：一次生产自查发现的八处独立热路径问题的止血批（内部编号 Z1–Z8），逐条各自止血、互不依赖。**Z1** 启动路径新增连接池预算诊断：`重活维护池 + 轻活维护池 + KG 分析并发` 之和若 ≥ `POSTGRES_POOL_MAX_SIZE`（生产默认 4+4+8=16 > 10）只告警不拒启，日志给出建议值。**Z2** PostgreSQL 崩溃恢复 `recover_interrupted_jobs` 从「11 条结算语句共享一个事务」改成每条独立事务、逐语句 try/except 隔离——此前任一语句失败会回滚其余全部结算，对 `kg_cluster_scratch`/`kg_canonical_scratch` 这类可能有百万行的清扫表尤其危险（失败即让下次重启在同一处再失败一次，永远卡住）；`indexing_pipeline_stages` 的清扫改按主键分批 `DELETE`。**Z3** `unified_graph`/`kg_neighbors` 的大库闸从裸 `COUNT(*)` 改调 store 既有的 `count_active_objects`（seq-gated memo，判据数值逐位不变）——PR #621 计数缓存的同族遗漏补齐。**Z4** `/ask/stream` 里同步的 `repo.start_ask_stream()`（DB 注册 + 开始事件合成）移进 `asyncio.to_thread`，不再占用事件循环。**Z5** scale 索引 build/fold 从裸的无界 daemon 线程改为受 `SCALE_BUILD_CONCURRENCY`（默认 2）限流的进程内准入闸，并给持续失败的笔记本加指数退避（`SCALE_BUILD_FAILURE_BACKOFF_SECONDS` 起步 60、`_MAX_SECONDS` 封顶 1800），避免低峰调度器一次性起满整条 idle 队列的线程、以及失败笔记本背靠背重跑。**Z6** 治理侧孤儿聚类清扫两处修复：①「每进程每库一次」的记账移进 `finally`——此前清扫在大库上超时即绕过记账，导致每次上传都重跑注定失败的全表反连接、且中断整个增量融合（Tier2 桥接/聚类对大库静默失效），这是本条的核心行为恢复；② 单条全表 `NOT IN` DELETE 改为「keyset 页读（LIMIT 界住**扫描**行数）+ 页内按主键列表删孤儿」的两步分批，每批独立提交，单条语句代价 O(页)——键区间删除形态在真 PG 上会退化为全表 Seq Scan（实测 1M 行 201ms vs 主键列表 7.4ms），已写入 docstring。**Z7** `backfill-vectors` 端点的受理判定改走 `CheckupService` 新增的窄读口 `missing_vector_counts()`（H4/H5 事件/版本驱动 memo），不再调整套 `checkup.run()` 白付 H2/H3 全库扫描与 H7 索引探针；per-notebook 单飞检查提到计数判定之前，在飞的二次请求零查询直接幂等受理。**Z8** 笔记本全文搜索（`GET /notebooks/{id}/search` 与 MCP `search_notebook_context`）新增进程级并发闸，上限 4（与前端自身的搜索并行扇出档位一致），第 5 个及以后的并发请求等待而非拒绝，且故意不设超时——搜索结果永不因为并发争抢被收窄。诊断侧新增只读的 `scripts/diag_pg_hotpaths.py`：对生产 PostgreSQL 做 EXPLAIN(ANALYZE, BUFFERS)/行数/索引覆盖的一次性自查，会话建立后第一条语句即设为只读，默认档不含 30s 级重查询，`--deep` 才加两条 ILIKE 全文探针与两条已被 Z7 从受理路径挪出的缺向量反连接 COUNT。四份契约文档（`product-and-api*.md`、`deployment-and-configuration*.md`）与 `scripts/README.md` 同步登记；不新增 schema migration。各条目定向测试（含新增的只读会话时点守卫、崩溃恢复逐语句变异验证、backfill 零查询幂等受理验证）均已通过；完整 `scripts/check.sh` 在本批全部子项收敛后统一验收。

- **生产热路径修复批 1：Schema v39/v61 六组八条索引（2026-08-29）**：批 0 生产自查（`scripts/diag_pg_hotpaths.py`）找出的六个查询族此前零覆盖，本批补齐——PostgreSQL 侧 schema v39 六组共八条：`idx_clusters_nb_canonical`/`idx_clusters_nb_canonical_name_lower`（`concept_clusters`，服务概念详情/共提对端名/`resolve_focal`）、三条反向 FK 覆盖（`extraction_runs`/`knowledge_source_fact_elements`/`memory_items` 的 `notebook_id`，此前笔记本删除级联对这三张表退化为整表扫描）、`idx_knowledge_relations_nb_source_target_edge`（服务 `in_network_relation_rows` 双端点收窄）、`idx_chunks_source_ordinal`（服务 `chunk_section_rows`）、`idx_sources_nb_hidden_type`（partial，服务 `hidden_source_ids`）。迁移 `0039_hotpath_batch1_indexes.sql` 用普通 `CREATE INDEX IF NOT EXISTS`（在事务里跑），配套新增离线脚本 `scripts/build_hotpath_indexes.py` 用 `CREATE INDEX CONCURRENTLY`（逐条独立语句、`autocommit=True`，不占事务、不堵塞写入）供已有生产流量的库先在线建好，迁移落地即降级为 no-op 账本记录；`inspect_hotpath_indexes` 除存在性/有效性外还比对 `pg_get_indexdef`/`pg_get_expr` 的列序与谓词形态，同名但异形的手建索引一律 `UNEXPECTED`、`install` 报 `unexpected_index_definition:<name>` 拒绝而非静默通过或误删；诊断消息带上出问题的索引名与 PostgreSQL SQLSTATE，不泄漏查询文本。SQLite 侧 `_migration_61`（schema v61）落地五组共七条——同构复合索引与三条反向 FK 覆盖与 partial 索引悉数照搬；第六组 `chunks(source_id, ordinal)` 在 SQLite 上不适用：经四配置矩阵实测，`chunks(source_id, "rowid")` 一旦被规划器真正选中反而触发 `USE TEMP B-TREE FOR ORDER BY`（rowid 伪列尾巴不像真实存储列那样能让优化器确认索引序已满足），而不加这条索引时既有的 `idx_chunks_source` 本来就免费给出这份 rowid 序（二级索引同键按 rowid 排列）——建它不仅无益，被选中时反而更差，因此这一组刻意不建。写放大冗余债本批一并登记不下线：`idx_chunks_source`（0003 迁移）已被新增的 `idx_chunks_source_ordinal` 完全覆盖，生产验证新索引稳定后可 `DROP INDEX CONCURRENTLY idx_chunks_source` 下线；`knowledge_relations` 另有三条同前缀（`source_object_id` 一侧）索引重叠（`idx_knowledge_relations_nb_source`/`_nb_source_id`/新增的 `_nb_source_target_edge`），系批 0 之前就存在、本批不引入也不处理。纯增量索引，不改表/列/FK/unique surface，配对仍是 SQLite61/PG39/epoch1、84 张应用表、113 个 unique surface、12-row-slot 上界不变。单测覆盖迁移文件与模块规格的逐条 anti-drift（剥离头注释后再比对 DDL，防止注释文本让断言假通过）、fake 连接下的形态错配拒绝与诊断消息，另加真连 PostgreSQL 的 live 用例覆盖 `autocommit=True` 契约（非 autocommit 下 `CREATE INDEX CONCURRENTLY` 第一条即报 `25001` 死）与真实 `pg_get_expr` 渲染（含 `IN (...)` 被 PostgreSQL 规范化成 `= ANY (ARRAY[...])` 的比对）。四份契约文档（`development*.md`、`deployment-and-configuration*.md`）与 `scripts/README.md` 同步；`scripts/check.sh` 全绿。

- **生产热路径修复批 2（检索路径等价重写 + 搜索/体检索引，2026-08-29）**：R2 五项「同值少扫」等价重写已合入（PR #634，codex 六轮）：`annotate_edge_support` 判空早退 + 定点查询取代 8.35M 行整表物化；copystats 移出 VectorCache 进 runtime-owned 的 `CopyStatsMemo`（single-flight + per-notebook epoch）；VectorCache 三道上限（配额桶按「每库每变体一条」执法、全局 128 条、字节预算 16GiB 且超额条目一条不逐直接拒收，估算含 scipy/rustworkx 分支）；manifest 身份按 (mtime_ns,size,ino) 签名 memo。⚠ 漂移探针 run-memo 被 codex 按 product-and-api.md 的检索范围契约（验证后增删须在 unsafe I/O 前关闭通道）否决并整体回退，契约钉留档。R6（本批）：迁移 0042 两条索引——`idx_knowledge_objects_nb_payload_trgm`（notebook 域复合 partial payload 全文 GIN：btree_gin 令 notebook_id 前置、`WHERE status != 'deprecated'`，与既有 `idx_knowledge_objects_nb_name_trgm` 同形——codex 依 operations.md 已记录的「单表达式 trgm 全局位图跨 notebook 退化」教训要求；表达式与 search.py 查询逐字对应，稀有词搜索从生产实测 5.9s 降到毫秒级位图计划（3.6ms 基准测于评审前单表达式形，复合形为其严格收窄、未重测精确值），常见词保留 ordinal 走查；体积约表段 1.5×，登记为可回退写放大债；迁移装 btree_gin 并校验同名先存索引，INVALID/异形响亮失败不入账）与 `idx_source_elements_nonblank`（体检 H5 非空元素 partial，配套把 maintenance 五个资格判定站点的 PY_WHITESPACE 从绑定参数改为同值字面量单一定义点 `_NONBLANK_TEXT_SQL`——custom plan 下两种写法等价，generic plan 下唯有字面量恒用索引，live 测试用 force_generic_plan 对照钉住；SQLite 侧刻意保留绑定参数，无部分索引收益点）。三处字面量拷贝（迁移 chr() 组合 / SPECS / maintenance 字面量）与 PY_WHITESPACE 的逐码点对账钉 + 站点级防回退钉；离线脚本同通道扩到十条。

- **生产热路径修复批 2·R3（KG 审核队列/查重/概念详情，PR #638+#639，2026-08-30）**：设计真源 `docs/superpowers/specs/2026-08-30-r3-review-queue-pushdown-design.md`。PR-A（#638）——KG 审核队列端点投影瘦身（批取只投影 `payload->>'name'`，非字符串 name 从抛错改按文本打分）+ `ReviewQueueMemo` 排名 memo（seq 键 + single-flight + 双层 epoch + LRU 128 + copy-on-write carry；carry 以前值为条件——verified/pending 互转才 carry 标签，rejected 任一侧一律失效，堵住撤销拒绝续期陈旧 memo 的漏洞）；原本独立的第 5 个 `review_queue_total` counts memo 撤除，total 收敛进排名 memo 同版本存取，`review_queue_page` 单调用返回同一 seq 版本的 items+total（响应形状 `List`→`{items,total}` 不变，避免两次分别查询各自落在不同 seq 版本上撕裂）。codex 评审沿途揪出的 seq-bump 时序缺口升级成**全矩阵原子化**：`kg_mutation.py` 登记 FULL CENSUS 权威表——凡提交 `knowledge_objects`/`relations`/`clusters` 图行的写事务，bump 随行同事务提交，七处收编（`store_kg`、`complete_relations` 逐页、`update_knowledge`、`merge_knowledge`、`approve_promotion`、`delete_source` 越过两处 FS teardown、knowhow 投影双向）；再加 VECTOR-REPLACE CENSUS——`update_knowledge` 的再嵌入是七处里唯一「替换既有向量、计数中性但内容已变」的路径，经 `embed_knowledge`/`replace_knowledge_vectors` 的可选 `mark_dirty_in_tx` 回调补第二次同事务 bump；重抽取清理同批收进 `_begin_extraction_run` 写事务源头 bump（跨进程完备，替代此前的本地显式失效）。PR-B（#639）——KG-3 查重两趟取数：pass1 窄投影（id/status/name，procedure 类型额外整取 payload；deprecated 下推；ORDER BY 与旧实现逐字同）分组候选块，仅对 ≥2 成员的块 pass2 取行（不取 evidence 列，显式填 `[]`，按 pass-1 序回填）；SQLite 侧受裸 IN 契约限制仍走 IN + Python 过滤（既有 0.138s→14.155s 教训）。KG-4 概念详情 hub 成员改 keyset 分页（`ORDER BY member_object_id COLLATE "C"` + limit/after 游标，服务侧默认页 200，API `limit` 收窄到 1..200）：`member_total` 从每页都算（JOIN + deprecated 过滤的裸 COUNT）收敛为只在首页算（50k 级 hub 上单次 84ms，若 250 页逐页重付会摊到约 21s 额外 DB CPU），后续页返回 null 由前端合并保留旧值；`attached` 证据按跨页去重，避免 React key 冲突与计数虚高；中段游标查询补 `ko.id` seek 谓词，把 merge-join 全扫（104ms）收窄到 2.2ms；codex 评审再补 PG 迁移 `0043`/SQLite `_migration_64` 的复合覆盖索引 `concept_clusters(notebook_id,canonical_id,member_object_id)`，配套 `HOTPATH_INDEX_SPECS` 第 11 条与 live EXPLAIN 断言分页查询走新索引且无 Sort 节点；证据展示改渐进披露（`KgEvidenceList` 抽件：20 条起步 + 「显示更多出处」+ 概念切换重置，resetKey 加首页世代号防止 merge/rebuild 刷新与在途 load-more 竞态污染新页）；邻接水合前按批 900 剔除同簇跨页成员（`idx_clusters_nb_canonical_member` 覆盖索引探测，稠密 hub 一页 `member_set` 曾让水合重新退化成无界，且可能撞 SQLite 参数上限，输出层此前一直被 object_type 过滤挡住而不可见）；load-more 失败按 AGENTS.md Interactive feedback 契约在按钮紧邻处给出结果并 4 秒后自动清除（新首页/重试/卸载均清定时器）。登记项：`delete_notebook_kg`「保行 bump」与 `kg_analysis._state_view` 拿 `seq==0` 兼作行缺失的判据正面冲突，进程内显式失效暂时维持，统一处置留给批 3 W1 删库重造时一并解决；`idx_clusters_nb_canonical` 被新覆盖索引前缀完全覆盖，登记为可回退写放大冗余债（照 `idx_chunks_source` 先例不下线）。四份契约文档同步；`scripts/check.sh` 与 PG lane 全绿，事件序测试按新序重钉，变异自检覆盖 seq bump 原子性与 carry 前值条件。

- **生产热路径修复 W-CLI（离线/异机 scale build CLI，PR #643，2026-09-01，codex 34 轮史诗闭环，最终无意见）**：批 2 至此全收官。交付离线/异机 scale build CLI（`inspect`/`build`/`export`/`import`）+ 轻组合根（`migrate=False`/`seed=False`，防止异机跑迁移或重置生产密码）+ 跨进程 per-notebook advisory 锁（专用会话 + `claim_token` + `verify_held` 客户端超时）。评审长跑沉淀八条硬不变量（后续改这些文件必守，已写入 `docs/operations.md` / `docs/development.md` 及中文对照）：①每个毁灭性动作（swap/retire/rollback/finalize/每根拷贝的 `export`）动手前一瞬复验 claim，丢失即就地停手、逐根上报实测状态，不假设仍持有；②三根一代——`build_id`/`parent_build_id` 配对闸（同版本重发的唯一判据取活主 manifest 而非 DB 版本；旧产物双缺兜底走版本判）；③传输清单 `transfer_manifest.json`（sha256 + 字节数逐文件），`import` 对暂存副本做双向核对（清单↔暂存、清单↔根集）；④省略可选根等价于把该根原子退休进回滚状态机——「暂存目录在、live 缺」是首次拷贝的正常中间态，只认 `.old` 为已发布证据；⑤缓存换代探针三态（签名 / 确认 ABSENT / 无法判定）+ 四连看（live→`.old`→live→`.old`）+ 探针本身进锁、冷装后复探主根，companion 与 viz 两条产物同构；⑥必需 `.npy` 全探头 + companion `format_version` 前置闸，头部缺失/版本不符早失败；⑦锁会话入册管理，`close()` 连带释放仍持有的会话且注册表与关停原子；⑧信号处理器只记标志位，实际报告在信号窗口外发出，避免重入 I/O 炸掉正在进行的 rename。运维侧新增两机部署 pin 清单（迁移账本校验和、PgBouncer session 模式、连接预算、hnswlib 版本严格相等）。`fangan_done.md` 的本条目未随 PR 写入，由批 3·W1 首个 PR（本 PR）补记。

- **生产热路径修复批 3·W1（删除笔记本 job 化，PR #653/#656/#659/#663/#666，2026-09-01～09-03）**：设计真源 `docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-design_zh.md`（v5，内部四轮评审）。PR-1（#653）：40 处可见性谓词单点化 `NOTEBOOK_LIVE_SQL`，授权三谓词并入生命周期闸，行为面守卫 + 文本棘轮。PR-2（#656）：`kg_reset_epoch` 代次列（0047/68），全站点 memo 键升 (epoch, seq)，`version()` 双规则、epoch=0 零重建风暴——删库重造后 seq-keyed 缓存永不混叠。PR-3（#659，codex 25 轮）：六相位删除作业全量交付（tombstone CAS + mark/paths/quiesce/rows/files/finalize），租约围栏全相位覆盖（路径物化事务内 CAS 心跳）、FTS 影子无条件补清 + 残渣路径涂抹、资产复核生命周期探针、分析产物发布闸 fail-closed、拷贝快照钉活性根行读取、快照校验器建模启动 sweep；DELETE 端点改 202 + 后台收尾归档（详见 product-and-api 登记）。T-5a（#663，codex 22 轮）：`delete_notebook_kg` 预排水 + 原子终局——`_GRAPH_DRAIN_STEPS` 双侧登记表、ko/ksf 特例页同事务连删从属行、`kg_building` 成为 build 作业真准入闸（显式所有权 `held_by_kg_job`）、终局 REPEATABLE READ 快照钉 + in-tx 复验 + 幸存者复验重试轮、`KG_GRAPH_DRAIN_PAGE_ROWS` 数值围栏。PR-4（#666，codex 8 轮）：`sweep_stale_copies` 收编（每本一写事务 + PG FOR UPDATE 重验）、5 张无外键表的孤儿行两阶段清扫、5 棵根 + scratch 兄弟的孤儿目录清扫；核心契约「时间信号不是同步机制」——在线模式只清孤儿行与 PG scale 三根（真 advisory claim），notebooks/assets 直删根必须 `--confirm-service-stopped` 停服窗口，年龄闸只是防「没停干净」的皮带。四份契约文档随各 PR 同步；每 PR 双内评（spec+quality opus）+ codex 闭环 + `scripts/check.sh` 与 PG lane 全绿合入。

- **生产热路径修复批 3·W2（簇图代际切换，PR #668/#671/#673，2026-09-03～09-04）**：设计真源 `docs/superpowers/specs/2026-09-03-batch3-w2-generational-cluster-swap-design_zh.md`（v3，三轮内评含 PG16 实测 EXPLAIN）。修审计 WR-4/WR-5：重抽与簇切换从「DELETE+重插同事务换表」改为「写不可见新代 + 微事务翻指针」，读者永不见半态、重建期间检索不降级，生产 484GB 库上「重新合并」持锁写 9M 行的 5s 锁超时窗口结构性消失，rebuild 期间上传的增量融合不再静默丢失（链 a/链 b 双闭合）。PR-1（#668，codex 首轮无意见）：三张派生表加 `generation` 列 + `unified_kg_state` 双指针/取号器/在飞列/催收标记（0051/71），29 处读者配 published 指针谓词（LEFT JOIN 谓词必须入 ON 的红线单列守卫），索引三建五删（含两条前缀劫持索引退役 + EXPLAIN pins 双关 seqscan/bitmapscan + VACUUM 可见性图），census 普查守卫（A/B/C 三分类逐文件计数），merge_dbs published-only、拷贝代次归一，「版本身份只数 published 代」新红线。PR-2（#671，codex 19 轮）：写者切换全链路——取号 UPSERT+CAS（数据级跨进程单飞，连离线 CLI 也被闸）、释放三通道（翻转清零/finally CAS/TTL 崩溃兜底）+ 认领心跳续租（stage 边界 + LLM 分块回调，TTL 只抢真尸体）、写新代无锁无 DELETE 不 bump（两段式版本红线）、四类一把翻双 CAS（cluster_mutation_seq 同语句）、催收 keyset 分页（单时钟域闭合：PG append 行 clock_timestamp()、清标记 ts+代次双分量 CAS、排除在飞代与 deprecated、canon 名录跨页缓存）、预回收按扫描键分页（空证明不整片扫）、启动恢复先全局释放滞留认领、staged 发布同事务代际重置、finish 指针守卫、communities 族发布事务翻转 + copy-forward 重铸板块 id + FOR SHARE 发布复核。ports 棘轮 935→945 驳回有案（master 五次上调先例）。PR-3（#673，codex 4 轮）：链 b 补漏轮（至多 3 轮直调 `_kg_target_batches("incremental")` 不复述谓词，attempted 身份过滤，耗尽如实记 `kg_backfill_partial`，持久 total 同步抬）、§2.1 进程内交叉检查（build×maintenance 双向互斥 + 跨准入仲裁锁消对开双退让 + build 自身收尾 relink 显式豁免 + 四个 HTTP/MCP 出口 409）、融合吞点结构化事件（只记异常类名）。代际发布协议全文登记 architecture.md §3.5；生产收益待重启 + 一次全量 rebuild 后按 T-0 测量。

- **生产热路径修复批 3·W3（大库禁用切换索引管线，决策 D3，2026-09-04）**：修审计 WR-2 的先行方案——「保存索引管线」的发布事务（48k 次 N+1 聚合 + 整库 staging 进内存 + 单事务重写整库）在 9M 对象量级上事实不可完成，每次点击白付 30s 连接 + 整轮回滚。按计划决策点 D3 用一天级显式禁用替代一周级重构：`begin()` 在铸 generation 之前按大库判据（copy_stats 的 copyable，与社区构建大库守卫同源、seq-gated memo）拒绝任何变更（含切回内建——不可行的是整库重建机器本身），无变化的幂等保存仍走早退不受影响；路由 409 明确文案，GET 投影新增 `large_library_locked` 服务端真值，前端设置面板据此禁用控件并就地说明。重构排到有真实需求时。

## 20. 当前边界（后续阶段，未计入已完成）
- **Deep Report 正文引用图片内联（第二期）**：本期只交付 Ask 与公开会话；Deep Report 仍沿用现有引用详情图片展示，后续再复用相同的块级定位、去重和页内预览合同。
- **历史 Article 方案**：已退役，不属于当前后续承诺；当前长内容产出路径是 Deep Report。
- **深度报告来源身份缓存**：本轮只做单次有界解析，不增加 run 级来源族缓存。后续若缓存，应缓存原始身份行并在本轮触达集合上重新执行并查合并；不能直接缓存任意子集的最终 family key，否则后续出现哈希/标题桥接资料时会改变族归属。
- **v0.4 Review Mode**：review session、场景 checklist sign-off、reviewer 评论、action items、导出 review 报告。
- **v1.0 企业**：RBAC / source 级权限 / 审计 / SSO / 私有部署 / Confluence·SharePoint·Jira·Git·Slack connectors / 多 notebook 搜索 / rule version diff。
- 检索：BM25 / FTS5 / pgvector 放量、结构化硬过滤、Knowledge graph（已评估为低 ROI / 基础设施级，暂缓）。
- 扫描件 OCR、DOCX/PPTX 公式（OMML）解析；MinerU 已覆盖 PDF 的公式/表格/版面（本机 MLX 或 GPU 主机）。
- **架构渐进整改后续阶段**：阶段 3（FastAPI routers 与前端 API client）和旧阶段 4 的 Pydantic 模型分文件已由 2026-07-21 application-boundary 条目交付。剩余架构计划仅为阶段 5（前端 workspace 状态拆分）与旧阶段 6 的 FastAPI lifespan / 统一应用生命周期；阶段 2、4、6 的 Repository 部分已由 Repository composition refactor 交付（见第 19 节账本 2026-07-11 条目）。

> 已完成里程碑：v0.1 闭环、Tier 1（场景/案例/Checklist/知识库前端 + 上传轮询 + knowledge 向量召回）、PDF MinerU(MLX) + KaTeX/表格渲染、**Tier 2 知识治理（状态生命周期 + 多类型浏览 + 合并 + 冲突检测）**、**检索/抽取算法升级（CJK 分词 + hybrid 融合 + 结构化场景匹配 + payload 级向量 + 全文分窗口抽取 + 鲁棒证据绑定）**、**全链路可观测日志系统（LLM/HTTP/管线三通道 JSONL + 控制台）**。

- 已完成（2026-06-06）：大笔记本 KG 性能与合并治理——Ask 去同步 backfill/全量扫描 + notebook 级索引 + 阶段计时；node_context/concept_detail 收窄查询；unified-KG 改显式 rebuild + dirty status（摄取不再同步重建、打开图谱不自动重建）；跨文档概念合并改有界 top-k 向量候选 + 别名归一化；可选 LLM 概念合并预审。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-08-20）：手动概念判重队列不回流——候选按 canonical component 对确定性去重，rejected/deferred 稳定 seed 决策经 confirmed union 传播为整组 cannot-link，旧部署同展示对的全部重复行由一次点击按固定锁序原子收束为最新决定；拒绝全为待审状态的候选不再置图谱 dirty 或触发全库重建，确认合并会重聚，只要任一重复行已确认，反向拒绝就按图变更重建。重建在发布候选的同一事务内重新读取并应用实时决定，避免并发拒绝被旧快照复活。聚类算法版本已提升，相关后端与前端接线、状态翻转、重复行并发及发布竞态回归已补齐；完整检查结果以本次交付验证为准。
- 已完成（2026-06-06）：推理模式 agentic search 实时进度——`/ask/stream` 输出 NDJSON progress/final 事件，Ask 前端在运行中展示按事件刷新的折叠 agent 轨迹摘要，点击可展开完整步骤，并在答案中保留默认折叠的最终 trace。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-07-10）：reasoning `follow_chain`——该能力不新增 migration 或改写历史数据，查询期复用既有端点索引，对有证据、审核可用、条件兼容的同类型两跳关系做有界类型化组合；关系前提可引用、推论不入库，Ask/深度报告与流式 `推导` 轨迹均已接通。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-06-25）：用户账号系统——
  - **后端**：`auth_sessions` 表存储不透明 Bearer session token；`app/services/auth_utils.py` 封装 PBKDF2-SHA256 密码哈希与 token 生成；`app/api/auth_routes.py` 实现 `POST /auth/register`、`POST /auth/login`、`POST /auth/logout`、`GET /auth/me`；`app/api/deps.py` 提供 `get_current_user` 依赖用于路由级鉴权；`notebooks.created_by` 列实现按 owner 隔离（用户只能看/操作自己的 notebook）；内置 `user-local` 账号原地升级为 `admin`（id 不变，登录用户名 `admin`，密码由 `SILICON_NOTEBOOK_ADMIN_PASSWORD` 控制，本地默认 `admin`，每次后端启动重置；production/对外监听必须改为强密码）；管理员可经 `PATCH /api/admin/users/{user_id}/role` 授予/撤销管理员角色并共同标记公共知识库，角色变更事务内会重验操作者权限，内置管理员与当前操作管理员不可降级，已有 session 下一次请求即读取新角色；公共知识库从普通用户列表隐藏但仍参与问答上下文检索。新增环境变量：`SILICON_NOTEBOOK_ADMIN_PASSWORD`（admin 密码）和 `SILICON_NOTEBOOK_AUTH_OPTIONAL`（默认 false=强制登录；true=无 token 请求回退 admin，仅本地/测试）。
  - **前端**：首次加载展示登录/注册界面；注册用户名规则为单个字母 + 8 位数字（如 `a12345678`，存为小写）；Bearer token 写入 localStorage 并由 api() 自动注入请求头；顶栏展示当前登录用户名与退出按钮；用户使用总览提供二次确认的“设为管理员/撤销管理员”操作，并标明当前账户和受保护账户；管理员专属操作仅对当前角色为 admin 的用户可见。
  - **测试**：新增 `tests/test_auth.py`（注册/登录/会话/退出）、`tests/test_user_isolation.py`（notebook owner 隔离）以及集成场景覆盖；全部 ~990 测试通过，`scripts/check.sh` 与 `npm run build` 绿。
  - 本轮有意不包含：修改密码、共享、协作。

- **来源页签检索/分页性能专项（2026-09-01）**：一次生产自查（4.9 万 source 库）串起来源页签浅页/翻页/搜索三条独立热路径，7 个提交分三块交付。**水合**：来源页签的 `kg_extracted` 判定原以 `knowledge_objects` 行为驱动集，一页 50 source 命中 33.9 万 KO 行时对每行跑 3 次 `extraction_runs` 子查询，单条查询 3650ms，是浅页 API 3.3s 墙钟的大头；改为以页内 source id 为驱动集（VALUES CTE + KO EXISTS 半连接 + 每 source 一组 latest-run 探针），微基准（5 source×2000 KO）85.2ms→0.13ms。PostgreSQL/SQLite 双端、批量与单行两路径同构改写，判定矩阵先在旧实现跑通再切换实现钉住语义不变；两轮评审收口补齐形状守卫（spy 断言真实发出的 SQL 走 VALUES CTE 驱动而非回退到 `knowledge_objects`，PG 侧另断言 EXPLAIN 走 Values Scan + Nested Loop Semi Join，SQLite 断言 EXPLAIN QUERY PLAN 走 covering index）与「同刻两条 run 由插入序定胜负」的 tie-break 用例，其中一条删除变异只有在自持连接强制 Seq Scan+Sort 后才能被钉住（默认计划下反向索引扫描恰好隐式给出同序）。**检索**：`list_sources_page` 带 `q` 的过滤原是跨表 `OR EXISTS`，planner 只能选 hashed subplan——生产实测带 q 的 COUNT 单次 363ms，`source_authors` 21 万行、`source_paper_meta` 3.9 万行整表扫，短词与长词几乎同耗时；改写为 id 半连接三腿 UNION（title/file_name 一腿含 BitmapOr、authors 一腿、paper_meta 一腿），双端同构，配 0048 批 4 三条 notebook 前置复合 GIN trgm 索引（PostgreSQL-only，同 0042 先例，SQLite 侧只落改写不加索引）。实测长词每次用户操作 161.5ms→0.30ms（538×），短词受 trgm 三连字符下限限制单项最多 +40%，但按用户操作净计仍 3.4–4.1× 更快，已登记于迁移头注释；语义口径统一把搜索腿并入 `report_source_rows` 等报表腿的口径（按子表自身 notebook_id 收窄），水合腿 `paper_meta_for_sources` 未跟进改写，是登记在案的残留分歧。**前端**：来源翻页/搜索此前零 in-flight 反馈——`Pagination` 的 busy 属性没接，连点会叠加多个完整请求，requestId 只丢弃过期响应而服务端照算；新增 `sourcesPageLoading` 状态与请求级 `AbortController`，起手 abort 前序请求。两轮评审在同一处独立复现回归：busy 释放判据把「被后继请求顶替」与「guard/owner 否决」混成一个判据，effect cleanup 置 cancelled 后 guard 恒假，导致转圈不停 + 翻页控件永久禁用；修复为按「本请求是否仍是窗口持有者」释放，并补齐 unmount 时 abort 在飞请求、`TypeError: terminated`（undici 在 body 流读取期 abort 的异常形状，与 `AbortError`/`DOMException` 不同）判别、四个 transition 站点的守卫覆盖（第五个经结构性证明不可达，以注释代替用例）；全部经变异验证（删对应逻辑各自使守卫报红后还原）。

- 已完成（2026-08-24，§6.4 / §9.1）：KG 起始探活复用统一短输出预算，不再以探测专用小上限截断推理型模型的可见 JSON；探活和正式抽取复用共享流式 JSON 传输，持续收到 chunk 的长输出不再受请求总墙钟误伤，任务熔断能合作式停止兄弟流；流式传输请求并采集 provider 最终 usage trailer，恢复按用户 prompt/completion/total token 精确统计，明确不支持该可选参数的 provider 只探测一次后回退且不本地猜数；HTTP 成功但空白、截断或无效 JSON 的响应落成 `model_response_invalid`，界面明确显示“模型响应不可用”并保留继续分析入口，不再泛化为人工“分析已中断”。
