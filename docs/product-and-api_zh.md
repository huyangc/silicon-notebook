# 产品与 API 参考

[返回 README](../README_zh.md) · [English](./product-and-api.md)

本文保留详细的产品行为与 HTTP/MCP 契约。仓库根 README 作为精简入口；实现和架构细节仍以 [architecture.md](../architecture.md) 与 [AGENTS.md](../AGENTS.md) 为准。

## 当前范围

当前仓库已进入以 KG-native 管线为核心的本机真实 beta 闭环：

- Python FastAPI 后端；SQLite 持久化路径 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端
- 由部署者统一管理 OpenAI-compatible chat、embedding 与 rerank 服务；workload 绑定及每服务 `max_concurrency` 集中写入一个 TOML
- 未配置 LLM/embedder 时全管线可离线运行（deterministic fallback）
- 干净起点：全新数据库只初始化本机用户，不预置 demo 笔记本或合成来源
- 支持 PDF、Markdown、DOCX、PPTX、CSV、XLSX 的 multipart 文件上传（经共享 KG job scheduler 异步执行）。添加来源弹窗会从中间压缩过长的待上传文件名，保留末尾/扩展名并通过悬停显示全名；有效文档上限判定会计入整个暂存批次，批次大于剩余名额时在提交前直接禁用上传，并说明剩余名额、超出数量和处理办法。
- **KG-native 摄取**：结构化 Markdown 解析 → 贪心窗口化 KG 抽取（Concept / Claim / Formula / Procedure）并发 embedding → 抽取优先状态（`extracted` = KG 就绪，不等 embedding）
- PDF/DOCX/PPTX 走 MinerU（公式/表格/版面、内嵌图片）；PDF 本机降级走 PyMuPDF4LLM 版面感知 Markdown（pypdf 仅最后兜底）
- MinerU 抽取的内嵌图片在来源正文内联展示；图注与文字保持可搜索
- **精确短语（用户检索语法）**：用**英文半角双引号**括起来的内容整体参与检索，不做分词。`什么是 "static timing analysis" 的原理` 里那段短语会作为一个不可拆的词项进入词法候选（SQLite 走带引号的 FTS5 词项，PostgreSQL 走转义后的 `ILIKE` 子串），在关键词覆盖率里也只算**一项**——整段命中才得分，散落着 `static`/`timing`/`analysis` 的文档一分不给，因此含完整短语的原文会排在前面；同时这段短语无条件获得一次精确定位探测（下文的精确标识符通道），命中的小节整体取齐。引号是**强偏好**而不是硬过滤：语义检索照常进行，不会因为某篇文档缺这段短语就把它从结果里剔除。打分侧（关键词覆盖率与 BM25/RRF 排序）会归一空白，所以文档里跨换行、多空格写的同一段短语照样算命中；**候选生成侧做不到**——FTS5 trigram 短语与转义后的 `ILIKE` 都是字面连续匹配，写成 `static   timing\n analysis` 的文档若只靠这段短语就捞不上来（要抹平它得加一列归一化的索引文本，而无索引的正则扫描是本层禁止的全库扫）。这时查询里其余词项与语义召回照常工作。识别有三条边界：只认英文半角双引号（中文排版引号 `“…”` 在散文里是普通引用，认它会把大量既有提问悄悄变成带约束的提问）、引号内至少 3 个字（SQLite 的三字符索引更短的索引不到）、一段文本里**不同**的引号内容超过 4 段时整条语法不生效（那是 JSON 之类的机器文本，引号在那里是标点不是约束）；数的是不同内容而非出现次数——推理与报告的内部检索问题会把同一段短语在目标、规范化问题和每条必答主题里各留一份。提问框与深度报告输入框在你敲下引号的当下就回执识别结果——识别到哪几段、或为什么这次没识别——不会让一次没生效的约束静默通过。规划与反思提示语在问题真的带引号时才追加一句「原样保留引号内容」，因此模型改写子查询也不会把它拆散；笔记本搜索框只是整串子串匹配，本来就不分词，那里**被识别的**那几段的引号会被去掉（未被识别的引号原样保留，仍可用来搜字面 JSON/代码）。私有 Memory 的候选生成是把整串当一个短语探测，因此每段被识别的短语会作为额外的 OR 词项进同一条有界查询，让「只含该短语、不含整句」的记忆也能进候选池；评分侧仍拿原串，短语必须整段命中。
- 混合检索：CJK 感知 bi-gram 关键词 + float32 语义检索（每 notebook 独立缓存）。SQLite FTS5 保留整句精确匹配加分，同时以安全引用的 OR 词项召回拉丁字母/数字词、重叠中文三字片段，以及 `_`/`-`/`.` 连接的完整标识符（`set_db` 这类，受「须含字母、长度 ≥4、至多 16 个」约束）；PostgreSQL 在原生 trigram 候选生成前拆分同一组有界词项，并对 `ILIKE` 分支转义 LIKE 元字符，使 `set_db` 这类词项保持字面量，不会退化成通配把 `setXdb` 也拉进候选。带索引的 Chunk/KG 路径合并有界 ANN 与词法候选窗口，带索引的 Relation 检索按方向平衡补入与词法命中 KG 端点相邻的关系并保留端点顺序。纯词法候选按 keyword-only 参与融合，不会被写入伪造的零语义分。
- 内置关系在抽取与图消费者之间共用同一套有向端点契约。违反核心类型配对的历史行仍可审计，但不能影响 graph/PPR/canonical/relation 检索；管理员定义对象类型可继续使用已知边 id 扩展。可选跨元素补全按来源代次的持久 keyset 水位推进有界页面，只使用同源索引候选并经过双阶段验证、代次复核与灰度闸，默认关闭；它不会做文档级或整书全表扫描。
- KG-native 接地问答：逐句 `[k_i]` 引用（半角 `[k1]` 与本地化 `【k1】` 共用同一绑定，半角/中文逗号的复合形式也兼容，并统一渲染为紧凑编号引用；模型直接输出的数字复合引用如 `[1, 2, 3]` 在能映射到已知引用时也可点击）、多轮会话、1-hop KG 邻居扩展，推理模式实时显示可展开的一行 agent 轨迹
- **意图优先的逐步推理问答**：正式界面启动 `reasoning` job 前，先由 `POST /api/notebooks/{id}/ask/intent` 在完全不读取 notebook / 参考库语料的条件下理解问题；它只能使用当前会话最近的用户问题，不能使用语料派生的助手回答，也不会创建 conversation 或 job。意图清晰时自动继续；因模型规范化没有经过人工审阅，原始问题仍是第一条权威检索种子，规范化表述只能补充。会改变方向的歧义暂停确认后，审阅后的表述才成为权威。冻结的主题/方向、实体、比较轴、约束、排除项、前提、期望输出和答案统一支配 Memory、PPR、证据检索与合成；首轮先执行完整权威问题，再轮询确认方向让每个必答主题都拿到一个种子；超出该档位首轮宽度的方向由一段有界补种在同一份步骤预算内顺延执行，预算覆盖不到的会在轨迹里披露并回喂给反思，而不是被丢弃。第二个规划器不得替换。无效确认在创建持久状态前返回 422，取消预检会把取消事件传给意图模型。
- **推理模式的类型化查询期推导：** agent 可调用 `follow_chain`，把有证据的两跳 `A→B→C` 临时组合成 `A→C`；首版只允许 `derived_from / kind_of / prerequisite_of / precedes / part_of`。两条直接关系各自保留可引用的关系证据；被拒绝、无 quote、类型或 `validity_scope` 冲突的路径 fail-closed；推论明确标作「推断」，且绝不写回 KG。该能力不新增 migration、索引或历史回填；查询只对既有 source/target 索引做有界抽样，高度节点无法在预算内确认时直接放弃推论。
- 两层知识库：每个 notebook 带 `tier`（`base` | `personal`，默认 `personal`）。`chunk` 基线只从当前 active notebook 读取 chunk；可选 KG overlay / PPR 才可能加入 federated KG 上下文与 base-backed chunk，`graph` / `reasoning` 使用 federated KG 路径。exact-score 的 `base` 次序只适用于知识对象命中：`federated_retrieve()` 不改相关度分数，分数更高的 personal hit 仍排在前面；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。回答合成阶段另有独立规则：当 base 与 personal 证据冲突时，以 base 立场为准并指出差异。引用携带其 tier（`AnswerAnchor.tier`），Ask 在每条引用上渲染 `base`/`personal` 标记。
- **用户系统**：自助注册（用户名规则：单个字母 + `00` + 6 位数字，如 `a00123456`，存储为小写）+ 密码登录，使用不透明 Bearer 会话 token。每个 notebook 由其创建者所有；用户库包含自己拥有的 notebook，以及主动加入的大型只读共享 notebook。首次启动时自动创建内置 `admin` 账号（登录用户名 `admin`，密码来自 `SILICON_NOTEBOOK_ADMIN_PASSWORD`，本地默认 `admin`；production/对外监听必须修改），并由它持有原有 notebook。管理员可在用户使用总览通过 `PATCH /api/admin/users/{user_id}/role` 授予或撤销 `admin` 角色；内置管理员和当前操作管理员自身不可被降级，已有会话会在下一次请求时读取到新权限。用户使用总览先对 `/api/admin/users` 返回的完整集合排序，再按页显示；默认每页 20 条，可切换为 50/100 条，并可点击各数据列表头切换升降序。总览及 `GET /api/admin/users/{user_id}/notebooks` 的问答用量字段统一为 `questions`，按归属目标用户的持久 `ask_jobs` 提交次数计数（失败/取消任务也计入），而非 `conversations` 会话容器数量；同一会话内连续提问会分别累计，共享成员的提问不会算到笔记本所有者名下。用户总数包含其在加入的只读共享笔记本中的提交；`GET /api/admin/users/{user_id}/notebooks` 刻意保持 owner-only，因此只分解自有笔记本里的提问，其合计不要求等于用户总数。旧 `conversations` 字段为 API 兼容继续返回并标记 deprecated。每个笔记本对用户上传的文档数量设有上限（默认 20，可由 `USER_UPLOAD_DOCUMENT_LIMIT` 配置）；管理员在用户使用总览调整——设置全局默认（`PATCH /api/admin/settings/upload-limit-default`）并为单个用户设置覆盖值（`PATCH /api/admin/users/{user_id}/upload-limit`，传 `null` 清除覆盖、回落全局默认）；管理员拥有的笔记本不受此限。任何管理员都可将 notebook 发布为公共知识库。公共知识库对普通用户的列表隐藏，但可在每个笔记本的参考库选择器里发现，仅对显式挂载了它们的笔记本参与检索。升级到 schema 20 不会回填挂载：所有既有笔记本挂载数清零，联邦检索对它们全部停止，直到用户自己显式挂载一个参考库。本地/测试场景可设置 `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` 跳过登录。前端在首次加载时显示登录/注册界面，顶栏展示已登录用户名和退出按钮。
- **分享链接**：owner 可发布不透明 notebook 链接；小 notebook 复制到接收者账号，大 notebook 以只读成员方式加入。写权限仍归 owner；当前没有实时协同编辑或修改密码流程。
- **报告公开链接**：单份**已完成**的深度报告可由有写权限的成员发布成免登录只读页（`POST /notebooks/{nb}/reports/{rid}/share` 发放 token，`DELETE` 撤销，`GET /public/reports/{token}` 匿名读取）。发放是幂等的——重复分享返回同一个 token，已发出去的链接不会突然失效；撤销后与从未存在的 token 无法区分，都是 404。未完成的报告不能分享（409）。匿名端点挂在**独立 router** 上：主 API router 带 router 级 `Depends(get_current_user)`，公开端点挂在那上面会 401 拦掉它服务的访客；它也因此不绑定请求用户，所以只能调用不依赖 current-user 的仓储方法（ContextVar 未设时 `current_user` 会回退 seeded admin）。投影是**白名单**而非脱敏：正文、问题、时间，以及每条引用的标题/原始文件名/位置/摘录。`source_id`、`element_id`、`object_id`、`notebook_id` 与整个 `understanding` 合同（含意图与冻结的来源范围）都不跨出去——公开页本就不能打开原始资料，给出这些 id 只会让人拿去探测已认证接口。资料基础等披露已在生成时固化进 `content_md`，无需另行下发。**渲染管线与站内共用**：正文经同一个 `remarkCitations` 把 `[k]` / 【k】标记链接化成可点编号，编号取自 key 里的序号（后端已全局重编号；公开投影会丢掉既无标题又无摘录的条目，按位置数会和正文对不上），点击跳到本页「引用出处」条目并高亮——公开页打不开原文，所以标记通往的是摘录而不是原始资料。表格/代码块沿用 `.answer-table-wrap` / `.answer-code`，宽内容在自己的内容块里横向滚动；页面必须自带 `katex/dist/katex.min.css`，否则 rehype-katex 产出的 MathML 不被裁掉，每条公式会连着逐字符的 MathML 文本渲染两遍。
- **绑定 notebook 的私有 Memory**：用户可手动把 Ask 回答生成可编辑预览，并在确认后沉淀为可复用 Memory。外层提供用户级总 Memory 页面，notebook 卡片显示当前用户的数量，工作区为 **问答**（Ask） | **知识库**（Knowledge） | **记忆**（Memory） | **深度报告**（Deep Report）。外部 Agent 可经 MCP 提交 `candidate`；它只在同一用户、同一 notebook 的获授权 Agent 间共享，用户确认前不会进入正式 Ask/搜索/报告检索。
- 可选图推理问答模式（`mode="graph"`，opt-in / 实验性）：基于 `knowledge_relations` 构建 rustworkx 内存图，做有界多跳 derivation/support 链遍历，答题时做对抗式链路校验并给出最弱环 `chain_trust` 分（默认 Ask 仍为 `chunk`）
- 深度报告（两阶段后台任务）：notebook 级「深度报告」动作把一个问题变成多节技术报告。**阶段1a 是完全不读语料的问题理解**：提取可编辑的最终研究问题、目标、必答主题、实体、比较轴、约束、排除项、期望输出、暂定假设、置信度与最多八个阻断性歧义，不调用 notebook 检索。报告始终停在 `intent_ready`；模糊问题的必填歧义必须回答，清晰问题也要由 owner 确认最终表述，只读成员只能等待 owner，`auto_generate` 也不能绕过。确认操作以数据库原子转换认领 `intent_ready → planning`，并确定性冻结用户已经看过的合同，不会再调用一次隐藏的理解模型；澄清答案只补充内部检索/写作问题，不进入报告可见标题。**阶段1b 仅在意图确认后开始**：确认后的问题和答案成为权威输入，再对每个必答主题做有界的零 LLM 覆盖探针，同时统计联邦 KG 与直接解析 `SourceElement` 命中；此后 STORM 式规划器才使用来源标题、KG 命中和 chunk 出处来改进术语、排序、专家视角和张力。语料不足只能形成缺口，不能替换或收窄用户明确要求的主题；代码会验证映射并补回模型漏掉的必答主题。大纲编辑器展示每节对应的用户问题、可编辑检索方向，以及原文元素/KG/公共库覆盖；绑定某个必答主题的最后一节不可删除，API 同步强制此约束。**阶段2（确认大纲后）**：除完整 `reasoning` 深挖外，每条已确认检索方向都会实际执行；各节并行。chunk、KG 对象、类型化关系、confirmed Memory 与直接 `SourceElement` 共用 `[k]` 绑定链路，原始 element 不再只是不可引用的提示附文：小库可直接评分 element；不可复制的大库先走有界 chunk ANN/FTS，再只按命中 chunk 的精确 `element_ids` 做有界主键 hydration，绝不全表加载 element 文本/向量。参考文献按具体证据锚点去重，不再按来源标题折叠，点击报告引用可展开其绑定的来源、位置和原文片段。Ask 与报告引用仅在来源已接地判定为论文（`is_paper=true`）且解析出非空 `paper_title` 时优先显示论文名；其余情况继续显示普通来源名/文件名。引用响应另以 `source_file_name` 携带持久化上传文件名；当它与显示标题不同时，Ask/报告引用卡显示「原始文件」，挂载公共参考库的证据同样适用。该值只可来自 `sources.file_name`，绝不能使用 MinerU 临时/输出 Markdown 名。模型返回的 `grounded` 仅是建议，后端会重新解析锚点，并要求被引证据达到相关度阈值。最终编辑器只生成执行摘要并标记未完整回答的必答主题/跨节冲突，不改写章节、不新增事实。原有（推断）/【通识】纪律、五档研究深度、`KG_JOB_CONCURRENCY` 并行、实时 `section_status`、取消和 Markdown/ZIP 导出保持不变。`ReportSummary` 与 `ReportDetail` 新增返回 `updated_at` 和 `generation_started_at`；后者在成功原子认领 `outline_ready → generating` 时写入报告内部状态。已完成报告的列表与详情用 `updated_at` 显示浏览器本地时区的精确生成时间，同时保留相对时间，并展示从 `generation_started_at` 到最终写入的总耗时。意图确认和大纲确认的等待时间不计入；旧报告缺少生成开始戳时不编造耗时。未完成报告只显示创建时间，不冒充已有最终耗时。
- **深度报告容量、输出与重试护栏**：整篇生成按 `REPORT_GENERATION_CONCURRENCY` 准入（每个后端进程默认 1 篇）；已准入报告至多同时运行 `REPORT_SECTION_CONCURRENCY` 节（默认 3），并同时受所绑模型服务容量和 `POSTGRES_POOL_MAX_SIZE - 2` 约束，在可行时为在线请求保留两条池连接；排队报告不持有数据库连接。分节撰写使用 `REPORT_SECTION_MAX_TOKENS=65536`；详尽档全篇蓝图与最终只读终审分别通过 `REPORT_SYNTHESIS_MAX_TOKENS`、`REPORT_SUMMARY_MAX_TOKENS` 使用独立的 `102400` completion 上限。这些配置是 completion 上限，不是应用侧总上下文声明，也不会预占输出；实际绑定 provider/model 必须能在对应 workload 的 prompt 下接受该上限。蓝图提示只选择承重主张，单节最多 12 条、全篇最多 60 条。若全部章节都为空或失败，报告终态为 `failed`；至少一节有效时继续 fail-open，并把失败节显式写入报告。保留已确认大纲的失败报告可原子重新进入 `generating`：重试保留冻结意图/大纲、重置 `generation_started_at`、清空旧生成产物，绝不重新理解问题或规划。认领后的排队时间计入生成耗时。
- 边可信与治理：每条边的可信信号（evidence / 同源佐证 / 类型合法性）+ 高风险边优先的审核队列；被审核拒绝的边从图推理中排除
- 知识治理：通过 `/knowledge-types` + `/knowledge?type=...` 浏览任意对象类型，状态生命周期，重复检测与合并；`deprecated` 对象从检索和 1-hop 扩展中排除。个人→基准节点晋升（propose → under_review → approve/reject），批准时去重入库，配套策展晋升队列
- 统一 KG：跨文档概念聚类（`concept_clusters`），待合并审核
- Object 级 KG 可视化：Concept / Claim / Formula / Procedure 节点，类型形状、边标签、多选过滤、按类型分组侧栏
- Notebook 集合页（网格/紧凑/列表、编辑/删除）；点击「＋ 新建」直接创建 `Untitled notebook` 并进入，无弹窗
- 第一版不使用 Docker

PostgreSQL + pgvector 仍是后续生产/团队 beta 目标，当前本机开发不需要。

## 产品流程

外层页面为 notebook 集合页（KG-native 管线）：

1. 点击「＋ 新建」——系统立即创建 `Untitled notebook` 并进入，无弹窗。
2. 上传 PDF、Markdown、DOCX、PPTX、CSV 或 XLSX 来源（multipart）。
3. 后端（异步后台作业）：结构化 Markdown 解析 → 分块 + 向量化——源处理完即可做 chunk-native 问答。
4. **KG 抽取按需触发**（见下方「KG 抽取触发」）：摄取期仅当该 notebook 已有 KG、或 `KG_AUTO_EXTRACT=true` 时才抽。`KG_JOB_CONCURRENCY` 只控制并行来源任务；每次抽取模型调用都由 `kg_extract` workload 所绑定服务的系统调度器准入，因此服务 TOML 中的 `max_concurrency` 始终是唯一模型容量上限。抽完的新源随后增量融入统一 KG。
5. 知识对象写入 `knowledge_objects` + `knowledge_relations`，并绑定元素级 evidence。
6. 混合检索（bi-gram 关键词 + float32 矩阵语义）驱动 KG-native 问答：答案含逐句 `[k_i]` 引用，支持多轮会话，并沿 KG 关系做 1-hop 邻居扩展。
7. 统一 KG 跨文档聚合概念；待合并的跨文档概念对可逐一确认或拒绝。

进入单个 notebook 后：

- 顶栏：左上角只保留可编辑 notebook 标题；notebook 描述在没有对话时显示到问答欢迎态里，顶部工具栏在桌面宽度下保持各动作标签完整。
- 左栏：用户导入来源文件，实时显示 parse-status（绿色仅给 `extracted`，其余处理中为橙色），并按后果分级显示来源异常徽标（完整性问题如解析失败标红、仅影响检索的问题如部分内容未分析标黄；待补全等中性待办状态只在来源详情显示），支持详情预览、删除，以及由问答和新建深度报告共用的检索范围复选框。来源详情每次只取并渲染 40 个 element 的有界页（API 单次上限 100），前后页按需加载；引用跳转由后端解析到包含目标 element 的页，因此打开大文档不会一次水合和挂载全部元素。所有面向用户的来源计数只计这组可见的导入来源，排除隐藏的 `memory` / `knowhow` 投影来源。网络来源检索暂不开放。
- 主栏：四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）。Ask 提供逐句 `[k_i]` 引用、三种检索模式、多轮会话、实时推理轨迹与反馈；鼠标悬停问题气泡或回答卡时在下方显示时间，点击后固定显示，点击其他位置后恢复悬停逻辑。问题采用已持久化的网页端提交瞬间，回答采用 `AskResponse.answered_at` 返回的权威答案写入瞬间（旧 payload 从 `answers.created_at` 投影）。时间按浏览器本地时间格式化：今天只显示时间；本周（周一为一周起点）内的其他日期显示星期与时间；超出本周显示日期与时间，日期和星期二选一；今年省略年份，其他年份显示年份。会话历史收进 Ask 顶栏的单行 `历史 N` 入口和可展开管理面板，旁边的 `+` 会直接开始新会话。历史按带亚秒精度的最近活动排序并显示活动时间；首轮问题一提交就会立即出现，即使在 `started` 到达前切到同库旧会话，模型仍在回答时也能重新打开。终态历史摘要按当前 notebook 独立刷新，不依赖原 run 继续占有回答区；同库列表调用会收敛到最新请求。加载 notebook/会话的最新详情期间，输入框与模式控制保持禁用。Knowledge 负责动态类型浏览与治理；Memory 只显示当前用户绑定在此 notebook 的私有记录；Deep Report 负责两阶段报告、大纲审阅、进度、导出、取消和删除。问答输入框中 `Enter` 发送，`Shift+Enter` 保留换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制。transport 断连只停止向当前客户端继续推送；导航、刷新或 transport 丢失后 detached Ask job 仍在后台运行并可保存最终回答。用户点击中断则调用 `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`，由后端设置取消事件，使 worker / LLM 路径停止，且不保存被取消的最终回答；如果点击时首个 `started` 尚不可读，界面立即恢复草稿，但该 run 的 transport 只继续读取到取得 job id，完成后端取消后即 abort。主工作区保持两列且没有固定 Studio 右栏。
- 知识图谱以全屏浮层打开：object 级 KG 节点（Concept / Claim / Formula / Procedure），类型形状，边关系标签，多选类型过滤，按类型分组侧栏（选中节点聚焦画布）。从问答知识对象引用打开时会精确定位对应节点：目标不在有界高连接度核心图时，前端按引用真实来源 notebook（包括挂载 base）叠加其有界一跳邻域，纯 graph-BFS anchor 也保留所属库 id。浏览器仍以当前 active notebook 过权限，后端只在其有效 participant 集内校验或解析对象并内部代理 base 的邻域/详情/context 读取；挂载公共 base 不会授予该 base 的直接成员权限。引用携带的原始 Concept id 由邻域接口通过单 id 聚类查询解析成 canonical `focus_id`，同时保留 raw object id 供 context 读取，不能用合成图节点 id 查询 `knowledge_objects`。大库 viz 产物仍在构建时，接口会显式返回暂不可定位而不进入全量 cluster-map fallback，前端也不会留下无法消费的 pending focus。侧栏的「出处」以结构化证据卡片展示，长标题、位置、公式与中英混排正文都限制在面板内；来源元素类型为 `formula` 的摘录会走共用块级 KaTeX 渲染，不再直接显示命令文本。
- 知识图谱视图头部还有一个只读的**图谱分析**按钮（批 1，共 4 批；见 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`），与「图谱 Schema」并列；两个后端端点都只要求 notebook 读权限，不做 admin 门控——面板本身不含任何写动作。`GET /notebooks/{id}/kg-analysis` 返回对象构成、**按对象类型分列**的合并收敛率（concept / claim / formula / procedure 分开算——四类混算会把 concept 真实收敛率稀释约 3 倍）、主题板块列表与跨板块边（供俯瞰图使用）；`GET /notebooks/{id}/kg-analysis/sources` 分页返回逐来源画像，默认按「与主体板块最不连通」在前排序，可切换为「最紧密」在前。与其它所有面向用户的来源计数同口径，来源画像也只覆盖可见的导入来源：隐藏的 `memory` / `knowhow` 投影来源的对象在**预计算**时就被排除，因此这些内部标题不会进报告，也不会把「最不连通」的排行头部占满。孤儿引用（`source_id` 指向已被删除的来源）则刻意**保留**并标记 `source_missing`——那是诊断信号，不是隐藏来源。两个端点只读 `rebuild_communities` 顺带产出的三张预计算产物表（`kg_community_edges`、`kg_source_profiles`、`kg_analysis_artifacts`），在线路径不做全表扫。这三张表与**板块划分本身**在**同一个**写事务里发布，所以报告永远不会把新一代板块与上一代账本拼在一起。每个数字都标注自己建于哪一代、落后当前多少——逐指标标注，不是整页顶一条「可能过期」的横幅。世代有**两条且相互独立**：`kg_mutation_seq`（对象与关系的写入）与 `cluster_mutation_seq`（合并结果）。合并的写路径刻意不动前者，所以从 `concept_clusters` 算出来的四份产物（簇大小直方图、最大簇榜单、跨板块边、来源画像）另带 `built_at_cluster_seq` / `cluster_seq_behind`，一次纯合并写入就会让它们变陈旧；`relation_provenance` 只读关系表、不盖这个戳，也刻意不被合并作废——重算它等于白跑一趟关系全表扫。依赖板块的那两份（跨板块边、来源画像）盖的是**它们描述的板块划分**建在哪一代合并结果上：同一轮重建过划分就是整数，只补账本的那一轮显式记 `null`——那时的划分是库里现成的，而它建在哪一代没有任何地方记。所以 `stale` 是**三值**的：某条线明确落后为 `true`，两条都对齐为 `false`，合并世代无从判断为 `null`；`null` 不等于 `false`，界面单独出一条提示而不是说「与当前一致」。产物账本对**两条**世代线都有独立于社区层的新鲜度闸，已经建过社区的库在下一次普通整理时就能补齐账本，不需要强制重建。主题板块列表本身走**同一套**三值判据，不是另写一份：它的 KG 世代那条线是 `community_seq`，合并世代那条线读的就是上面那个戳——那个戳记的本来就是「板块划分建在哪一代合并结果上」，而依赖板块的两行账本与重铸板块 id 同事务作废，所以「行在」就意味着它描述的正是当前这套划分。于是纯合并写入之后，板块那一格与建在同一套划分上的那两份产物**同时**报「对不上合并进度」，而不是因为 `kg_mutation_seq` 恰好没动就自称「与当前一致」。本视图不做任何治理动作（删除/隔离），只读出报告。
- 「分析」菜单本身只包含晋升队列（admin）、发布/撤回公共知识库（admin）与边审查队列。看板、全屏知识图谱是其他顶栏动作；图谱 Schema（知识对象类型/字段管理，仅管理员）已从顶栏移入知识图谱视图头部的「图谱 Schema」按钮，不再是独立顶栏动作；当前不再暴露已退役的内容生成或派生规则动作。现有 notebook 分析视图提供独立的 Memory 和 Knowhow 内容资产卡片：Memory 指标严格限定为当前登录用户和当前 notebook（admin 也不跨用户汇总），Knowhow 指标遵循 notebook 的既有读取权限。卡片只展示计数、健康度/最近活动摘要和跳转入口；浏览与编辑仍复用现有的 Memory、Knowhow 页面和编辑器。

知识对象类型的显示名只有一份真源：后端 `app/services/extraction_profiles.py` 的 `OBJECT_TYPE_LABELS`，由 `GET /notebooks/{id}/knowledge-types` 以 `KnowledgeTypeCount.label` 下发给前端。凡是拿得到这个 API label 的调用点——Knowledge 浏览器的类型 tab 与条目——一律直接使用它，因此用户自定义类型（例如 knowhow 表列名投影出来的类型）同样能显示正确的中文名。只拿得到 `object_type` 字符串的调用点——引用浮层与知识图谱画布/侧栏——回落到前端内置小表 `frontend/app/kg-type-model.ts` 的 `KG_TYPE_LABELS`；`kg-type-mark.tsx` 消费并 re-export 该模型供共用渲染。该表逐字等于后端常量；`scripts/check_object_type_labels_contract.py` 作为硬门挂在 `scripts/check.sh` 里，两份一旦漂移即构建失败。未知/自定义类型一律原样显示其 `object_type`，绝不 TitleCase 成臆造的英文。这两张表的键都由用户可控字符串索引，查表必须走 `Object.hasOwn(...)` 而非裸下标：`constructor`、`__proto__` 会命中原型链上继承的函数/对象，而不是「查不到」。

面向用户的文案另有一份词汇契约，真源是 `AGENTS.md`「界面词汇表」：表中每一行把一个内部词（基准库、chunk、KG、抽取、投影、晋升、schema、deprecated……）映射到界面唯一允许使用的说法。内部名保留在代码、类型、注释与架构文档里——只有渲染给用户看的字符串才改写；而**被持久化**而非被渲染的值（`Untitled notebook` 这个默认库名、协议上的 enum id）属于契约不属于文案，任何一轮措辞调整都不得顺手改动它们。`scripts/check_ui_vocabulary.py` 作为硬门挂在 `scripts/check.sh` 里执行该表，其**作用域跟着信任边界走、不跟着目录树走**：既扫描 `frontend/app` 每个源文件的渲染文本——字符串字面量加 JSX 文本节点，并先剥离注释、标识符、正则体与 `${…}` / `{…}` 插值——也扫描后端每处 `user_error(status, "…")` 的消息字面量，因为 `api/deps.py` 恰恰只给这批 4xx `detail` 打上 `X-User-Message: 1`，而 deny-by-default 的前端见到该标记就把它原样显示给用户。打标记等于声明「这是给人看的文案」，那就同样受这份词表约束；此前把守卫圈在 `frontend/app` 里，正是「仅管理员可设置基准库」「仅管理员可管理晋升队列」四条 403 一路上屏而守卫全绿的原因。裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内——它永远不上屏，detail 是诊断 / MCP 契约，这条分界由 `backend/tests/test_user_error.py` 守。任一侧命中黑名单词即构建失败。另有一条独立守卫 `frontend/app/raw-enum-fallback.test.mjs`（由 `npm run test` 递归收集，因而同样是 `scripts/check.sh` 的硬门），拒绝「兜底即原值」（`MAP[x] ?? x`，以及通过正规 API 达成同一效果的 `label(map, x, x)`）：这种查表一旦后端新增枚举值，就会把英文 id 直接渲染给用户；应改用 `frontend/app/vocabulary.ts` 的 `label(MAP, value, fallback)`，它强制传中性兜底词，使该 bug 写不出来。该检查跑在真正的 TypeScript AST 上而非正则：渲染位置的 `M[x] ?? x` 与内部归一化的 `ALIASES[v] ?? v` **语法形状完全一致**，只有上下文能区分泄漏与正常代码——正则版误报了后者，又整个漏掉了 `M?.[x] ?? x`、`getLabels()[x] ?? x` 与 `label(m, x, x)`。它自己的文件头如实写明仍然看不到的部分（先算进变量再渲染、`alert(...)` 这类非 JSX 出口），诚实标注优于假装全覆盖。若确实要原样透出**用户自己写的**字符串（自定义 `object_type`、用户自建的 schema 字段名），则显式写成 `Object.hasOwn(...) ? ... : raw`，顺带规避上面那个原型链隐患。该守卫是词黑名单而非语义检查：有两行只覆盖其无歧义的复合形态——图谱视图里裸用「节点」「边」是正当的，且「边」与「旁边」「边框」同形。`backend/tests/test_ui_vocabulary_guard.py` 存放它的正例与反例，并额外在「词汇表新增一行却既没有对应规则、也没有登记豁免理由」时失败，使黑名单无法悄悄退化成只覆盖词表的一个子集。

重新解析保留 source 行与原始文件：替换 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用同一 source-derived cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。

可见导入来源计数与物理记账刻意分离：隐藏的 Memory/Knowhow 投影来源不会出现在来源栏或面向用户的计数中，但 `size.sources`、复制阈值、存储统计和后台调度仍按物理行计数。`has_unindexed_content` 也会在可见导入来源增量为零但派生内容发生变化时保留 scale-index 更新决策。

检索索引调度提供立即执行与低峰排队两种操作。没有构建正在运行时，`when=now` 会在认领立即构建的同一临界区内覆盖同 notebook 先前的 idle 项；认领后新加入的 idle 请求仍会保留，worker 启动失败也会恢复被覆盖项。调度 tick 会逐个认领队列项，忙碌 notebook 的后续任务继续排队，单项启动失败不会丢失或阻断其余项。`AskResponse.index_required` 只记录回答生成时的降级状态；问答界面还会读取实时 `ScaleIndexStatus.exists`，有界前台轮询结束后由 `index_done` 事件刷新当前 notebook，因此索引发布后无需改写历史回答即可同步移除旧提示。

notebook 工作区隐藏集合页全局上边栏，采用偏工程风格的视觉治理。Ask、报告、Memory、Knowhow 展示的 Markdown 中，即使独占一行的单行 `$$...$$` 紧邻正文，也仍按块级公式渲染；宽块级公式只在自身内容块内横向滚动。来源详情、知识对象与知识图谱出处卡的公式视图在直调 KaTeX 前会剥除包裹整值的 Markdown 数学定界符，仍无法解析时显示原始文本，畸形公式输入不会再变成空白可视化。

### 按来源选择检索范围

来源侧栏初始全选所有可见导入来源，每行提供复选框，并提供“全选”/“清空”。当前选择同时传入不读语料的问答意图预检、问答执行和新建深度报告，约束当前 notebook 的内容块、来源元素、知识证据与关系、图路径/PPR 输出以及报告检索。范围被收窄时，隐藏的 Memory/Knowhow 投影证据也不参与，因为这些内部来源没有面向用户的复选框。这份隐藏证据里两种来源的范围不同：Knowhow 投影是笔记本级共享的，会进入每位成员的上限；Memory 投影按创建者私有，只进创建者自己的上限，且过滤就发生在那一次读取里，所以共享笔记本绝不会把一位成员的私有 Memory 暴露给另一位。已挂载的参考库是独立参与者，始终保持在范围内。

`AskIntentPreviewRequest`、`AskRequest` 和 `ReportCreate` 接受可选的顶层 `source_scope`：`{ "mode": "include" | "exclude", "source_ids": string[] }`。`include` 只放行列出的当前 notebook 可见来源；`exclude` 放行除列出来源以外的所有当前 notebook 可见来源。省略该字段保持历史整库范围，`exclude` 加空列表是前端表达“当前所有可见导入来源”的紧凑形式。API 入口会校验每个显式范围并冻结为明确的 include 列表；服务端还会按当前可见来源总数计算 `narrowed`，覆盖客户端提交的同名值：冻结“当前全选”的快照不能被误判成真的排除了来源。因此全选运行（包括只有一篇文章的 notebook）保留对话历史和正常图扩展/推理通道，只有集合确实变小时才启用受限模式的跳过。服务端会为全选运行私下快照当时已有的隐藏 Memory/Knowhow 参与者 id，真正收窄才排除它们；这些 id 不进入公开 scope 响应或持久化合同。两种模式都在来源可分区候选与结果校验中保留冻结快照，所以并发新增来源不会扩大已在运行的请求；若当前可见来源全集与全选快照不再一致，无法安全隔离的整图通道会在 I/O 前关闭。后端对外库、隐藏或已失效的来源 id 返回 422；若本地有效范围为空且未挂载参考库，问答/意图预检/报告创建返回 409。浏览器同步禁用问答输入和新建报告控件，但仍可查看既有报告。报告会把解析后的公开范围持久化在问题理解合同中，在意图确认与生成前重新校验，并为规划和写作重新水合私有参与者快照。

这个检索范围有**两个互相独立的维度**，都由问答意图预检/执行和新建深度报告共用、都默认全选：当前 notebook 每个可见导入来源一个复选框（`source_scope`），每个已挂载参考库**整库**一个复选框（`base_scope`，不展开到库内来源）。`AskIntentPreviewRequest`、`AskRequest` 和 `ReportCreate` 在 `source_scope` 之外接受可选的顶层 `base_scope`：`{ "mode": "include" | "exclude", "notebook_ids": string[] }`。`include` 只放行列出的挂载库；`exclude` 放行除列出库以外的全部挂载库；省略该字段保持「全部挂载库无条件参与」的历史行为。`notebook_ids` 只能指向当前已挂载到该 notebook 的参考库（否则 422）；与 `source_scope` 完全一样，API 入口把每一次提交——包括 `exclude` 加空列表——冻结成显式 include 快照，并在服务端重算 `narrowed`、忽略客户端提交的同名值。冻结对报告最要紧：解析后的范围持久化在问题理解合同里，并在意图确认与生成前原样重新应用，所以报告创建之后新挂载的参考库不会静默参与它。

两个维度**正交**。`source_scope` 的收窄关的是**当前库自己**的通道（PPR、私有 Memory、社区报告、弱支撑关系、精确章节查找、报告整库画像）；取消一个参考库的勾选绝不能顺带关掉其中任何一个，否则用户「少借一个库」就要为此付出当前库检索质量下降的代价。跨库通道各自认库维度：集合枚举与集合地图、联邦候选检索、社区扩展、图漫游、`follow_chain`、证据装配，以及 **KG 可用性闸**。最后这一个是**判据**而非结果过滤——本库无图、唯一带图的库被取消勾选时必须判「无图」，否则 `kg_required` 不翻真、graph 路径会跑在一份这次不许读的图上；它经与候选检索同一个 `resolve_participants` 收口解析参与库，判据是**冻结的选择**（提交过即认），不看是否收窄。会话历史是唯一同时认两个维度的闸：上一轮答案可能引用了用户刚取消勾选的那个库的内容。

库级收窄**只在一个边界**生效——参与库列表，过滤一次、被计划/遍历/分母/收尾指纹共同读取——因为枚举要求**行与计数出自同一谓词**：只过滤行而分母仍把全部挂载库算进去，会让一次真的走完的清单被永久判成 `concurrent_change`。`enumeration_active()` 不受影响：工具照常提供，只是作用域收窄。**泄漏面包括查询词本身**：社区扩展拿参考库的兄弟**实体名**当查询词，这些词会进可见轨迹、进已用查询记录并回喂反思，所以收窄必须发生在取实体名的入口，光过滤结果无效。`resolve_participants` / `mount_sql.py` 不动——它与**权限**判定共用（跨库来源详情代理、引用解析、图片资产读取），一个按请求的检索复选框没有资格收窄授权集合。

两条二阶代价已登记并接受：图漫游与 PPR 的过滤在遍历/截断**之后**，因为联邦图按 scope 无关的键在进程级缓存——被排除库的节点仍占扩散名额、仍可当中转跳，但它的内容一个字都不会进渲染出的 prompt、也不可被引用。整图规模守卫同样保持 scope 盲，所以取消勾选一个超大参考库并不会把那道守卫重新关掉。

**409「范围为空」的判据现在是两维的合取**——判据是「这次勾了哪些库」而非「挂了哪些库」——Ask 三个入口与报告的创建/确认/生成三处都生效。请求没有**收窄**的那一维按 notebook 真实证据宇宙作答；两维都没提交的请求交给既有 `ask_available` 闸。**本地证据宇宙 ≠ 可见导入来源数**：Knowhow 格子、该用户的已确认 Memory 与本地图谱都没有可见来源行，所以 `NotebookSummary` 在 `ask_available` 之外并列暴露 `local_evidence_available`（由 catalog 复用它本来就要求值的三条本地判据算出、零新增查询），空判把它与来源数取**或**，故只增不减。真收窄仍以选择为准：把来源清空就是空的，哪怕本地另有证据。

`AskResponse.retrieval_scope` 是只读回执：`{ local: { selected, total }, bases: [{ notebook_id, name, included }] }`。库名是**授权时刻的持久化快照**，绝不按当前挂载重新映射，所以重开旧答案时仍能看到那个此后已被取消挂载的库；检索侧从不回读它。两维都没收窄时该字段缺席，序列化因此与历史答案逐位一致（浏览器每次请求都会发两份范围）。它的披露面刻意比跨库来源代理更窄：只有库名与计数，没有文件路径、没有错误原文、没有来源身份。

浏览器把这两个维度并列成来源面板上的两组复选框：先是双段计数的「检索范围 · 本库 N/M · 参考库 K/L」工具条（「全选」/「清空」一并管两维），然后是「参考库」分组（每个已挂载库一行，只显示库名，不展开到库内来源），最后是「本库来源」分组——来源搜索框归在这一组的标题之下，因为它只查当前 notebook，摆在参考库之上会让人以为能一并搜到参考库里的内容。同一句双段计数还显示在问答输入框上方。两维同时为空时禁用问答输入与新建报告，判据与后端 409 逐条对齐（本地那一维读 `local_evidence_available`，不拿可见来源数代替）。`retrieval_scope` 在场时，答案卡在正文之上渲染一行默认折叠的「检索范围：…」，展开可见每个参考库本轮参没参与；缺席即不渲染，浏览器不按回执数字自行重算「算不算收窄」——那份判据只有后端一处。

浏览器的**严格推理门控同样按「这次勾了哪些库」判定，而不是「挂了哪些库」**。`NotebookSummary` 并列下发 `base_kg_notebook_ids`——它是 `base_kg_available` 的**分解**，列出挂载的参考库里哪几个已建知识图谱。零新增查询（`mounted_bases_row` 的每一行本来就带 `has_kg` 列，聚合布尔只是把它 `any(...)` 掉了），回填路径与 `base_kg_available` **逐字相同**，两者必须自洽（非空 ⟺ 为真）——它们是同一次读取的两种投影。前端据此把「深入分析 / 知识图谱」的可用性判成「本库有图 **或** 本次勾选的参考库里有带图的」，「将借用参考库「…」推理」也只点名**既被勾选、又已建图**的那几个库：读聚合布尔会在「本库无图 + 唯一带图的参考库被取消勾选」时放行一个这轮根本取不到图的模式（后端的知识图谱可用性闸早已按库维度收窄），并当着用户的面点名一个本轮不参与的库。字段缺席（版本 skew）时退回「聚合布尔 ∩ 勾选集」，只会比原判据更保守，不会更宽。这道门开始拦一个原先放行的模式之后，「取不到图谱」的提示按**成因**分成两支、出路不同：挂了已整理图谱的参考库、只是这次没勾 → 提示去来源面板重新勾选，且刻意**不给**「整理知识图谱」按钮；一个带图谱的参考库都没挂 → 保持原有的「整理知识图谱」提示与按钮。合成一支就会在用户只需点回一个复选框时，劝他跑一次整库图谱整理（真金白银的模型调用）。

顶层复选框范围是当前 notebook 的硬上限，也是检索范围的**唯一**来源。模型既不提议也不收窄或扩大它：语料盲意图规划器不输出任何来源身份，run 内也没有任何动作能改变哪些来源在范围内。全选 include 快照保留正常图通道，私下快照当时已有的隐藏 Memory/Knowhow 参与者，并冻结可分区候选和结果校验。验证后新增或删除可见来源/隐藏参与者时，在 I/O 前跳过高风险图通道；可分区检索仍按冻结上限继续。挂载参考库仍是独立参与者。带索引的 chunk/元素检索会在 scale 工件中持久化逐行来源代码；HNSW 在进入 Top-K 前应用允许来源谓词，hydrate 后还会在评分/合成前复核。旧发布索引缺少这个可选 sidecar 时仍可加载，但会先使用有界来源内 FTS，直到重建或 delta fold 写入映射。KG、PPR 与精确查找等确定性种子结束后，若证据仍完全为空，逐步推理会在让反思模型判断充分性之前确定性补一次有界原文元素检索。无法在遍历前安全应用当前来源谓词的持久化通道（当前 notebook 的全图/PPR/关系扩展、精确章节查找与报告整库画像）会在收窄或参与者全集漂移时被跳过；事后过滤不能作为授权，因为未选候选仍可能占满 Top-K 或提供不可见的图前提。按来源限定的 chunk、元素和 KG 直接检索仍正常执行，base 库 KG 种子也可直接映射回 base 原文，不经过组合全图遍历。

内部 `SourceSubgraphSnapshot` 是替换上述当前 notebook 跳过通道的读取侧准备，但现在只通过统一、受质量门控的 Ask 与深度报告激活入口消费。它在一次可重复读中只解析冻结的可见来源 id，每条 SQL 都在 `LIMIT` 前应用来源谓词；SQLite 的对象、chunk 与 cluster 读取固定从所选来源索引起步，避免先遍历被排除来源再过滤。一条关系只有在关系自身来源以及两端对象都属于所选来源集合时才会进入快照。对象到 chunk 的 membership 只来自当前代次的来源事实、规范化证据元素绑定与所选 chunk，绝不读取整库实体/chunk map。缓存身份只使用 O(1) 的 notebook KG/cluster 单调变更序列及有界的来源/run/回填状态：正式 live 投影写入会推进 KG 序列，历史修复会推进其来源账本，因此缓存命中不再重数事实、chunk 或证据绑定；构建快照时再用已经受限的事实窗口校验当前 live 代次完整性。反向索引未回填、来源删除、代次漂移或未知事实投影版本会在依赖图能力使用前 fail closed；某一行数护栏越界时只关闭依赖该分支的能力。live 或回填投影不完整时仍可用已经证实的来源事实行提供安全名称与 evidence，但事实完整性和 PPR/membership 能力保持关闭；缓存中的 payload/evidence 树递归不可变。全范围、off 与 shadow 仍保持历史响应、prompt、轨迹、候选顺序、证据预算和引用契约；只有通过 attestation 的 active run 可在 B 后追加来源内 G。

内部正整数护栏为：`SOURCE_SUBGRAPH_MAX_SOURCES=32`、`SOURCE_SUBGRAPH_MAX_OBJECTS=20000`、`SOURCE_SUBGRAPH_MAX_RELATIONS=40000`、`SOURCE_SUBGRAPH_MAX_CHUNKS=20000`、`SOURCE_SUBGRAPH_MAX_FACTS=20000`、`SOURCE_SUBGRAPH_MAX_FACT_ELEMENTS=60000`、`SOURCE_SUBGRAPH_MAX_MEMBERSHIPS=60000`、`SOURCE_SUBGRAPH_MAX_CLUSTER_MEMBERSHIPS=20000`、`SOURCE_SUBGRAPH_CACHE_MAX_ENTRIES=64`。每个数据库分支最多探测对应上限加一行以识别越界；非正值在 Settings 校验阶段直接拒绝。

所选来源原语层只通过统一 Ask/深度报告激活服务消费。邻居/扩展、关系搜索和两跳 chain 的每个结果都会再次校验 capability、关系来源、两个端点、evidence 与 review 状态；被排除来源既不能占结果名额，也不能充当中间节点。精确查找只在 snapshot 的所选 chunk 上复用既有标识符分组/子树语义；游标枚举的总数与分页只来自完整的所选 snapshot，不读取 notebook collection map。原语硬上限为：fan-out 16、扩展深度 3、扩展节点 128、chain 结果 16、关系结果 32、枚举页 100；精确查找继续使用既有 `EXACT_LOOKUP_*` 护栏。

内部受保护增强服务会在调用图 provider 前冻结历史最终 baseline。纯图 chunk 只使用调用方给出的独立 token 预算，并且只追加到隔离的增强提案；预算不足时丢弃图候选，不会重新截断 baseline 证据。baseline 中已经存在的 chunk 保持正文、来源与引用句柄、分数、相关度和位置，只有隔离副本合并生产者 provenance；off 与 shadow 的用户可见结果始终是原 baseline；active 只有通过质量门后才能发布受保护的追加提案。图失败或超时时返回同一 baseline 与 manifest；`baseline_evicted_count` 非零时丢弃整段图提案。baseline 与增强 reasoning 动作使用不可相互借用的 step 账本。shadow 关闭或图预算为零时不会调用 provider。Ask 与深度报告通过统一激活服务消费这条图通道；off 与 shadow 的调用、prompt、响应和引用仍保持历史 baseline。

小中型所选来源 snapshot 还有一条内部稀疏 PPR producer，默认为不可见 Shadow 观测启用，并由 `SOURCE_SUBGRAPH_PPR_ENABLED` 独立控制。它直接用获授权的 snapshot 节点、关系、对象到 chunk membership 和 cluster router 构建双向、列随机 CSR；来源归属、两端、evidence、review 状态与允许成员检查均发生在插边和度数归一化前。reset 中不属于这张 scoped graph 的对象/chunk id 会被忽略。min-max、Top-K 与 hydrate 命中只覆盖 snapshot 的授权 chunk，因此被排除的 B/C 来源不能影响 A 的度数、reset 归一化、分数、顺序或内存上限。transition 缓存身份由冻结 scope 及 KG/cluster/source 代次组成；LRU 最多 8 项，冷构建在 service 内 single-flight。在线护栏为：总图顶点 40,000（KG 节点 + chunk + cluster router）、逻辑无向边 100,000（CSR 重复折叠前最多 200,000 条双向 transition entry）、返回 chunk 100。构建器固定为 O(顶点 + 边)；越过构建护栏分别返回 `ppr_node_limit_exceeded` / `ppr_edge_limit_exceeded`，build/run 异常返回 `ppr_build_failed` / `ppr_run_failed`。禁止回退整库 CSR。这条 producer 只为统一 Ask/深度报告激活服务排序 G，绝不修改 B。

在大 notebook 中，按来源分区 scale 伴生产物在不打开 notebook 整库 CSR 的前提下提供同一所选来源授权语义。离线 full build 或 fold 每次只从 `knowledge_object_sources`、当前代次来源事实/元素绑定、来源自有 chunk/关系及来源受限 cluster membership 读取一个可见来源；每条分支复用对应 snapshot 的 LIMIT+1 护栏，SQLite chunk 强制从 `idx_chunks_source` 起步。它为每个来源发布一个哈希直寻 partition，并发布一份绑定主 scale manifest version、大小恒定的根 manifest。每个 partition 还绑定完整、无正文的 source/run/backfill signature，并记录所有 payload 文件的 SHA-256，因此即使计数不变，provenance 修复或可解析的文件篡改也会使旧 partition 失效；请求期身份探测仍为 O(selected)，不读取图行。partition 保存本地列随机 CSR、逐行对象类型与 chunk 身份，以及该来源拥有的跨 partition 关系端点。多来源冷读在 payload I/O 前先校验所有所选恒定大小 manifest，并把本地 transition entries 加上每条跨来源关系预留的两个 entry，按累计 60,000 nodes / 240,000 transition entries 护栏做保守预检。随后单来源请求直接复用已校验 CSR；多来源请求通过稀疏数组并出授权 identity，并只填充一次受限 cross-edge 分配。仅在关系所属来源被选、构建时 evidence 同源、两端均在所选对象并集且中央 edge registry 接受端点类型时接纳跨 partition 关系。PPR 最多执行 `SOURCE_PARTITIONED_PPR_MAX_ITERATIONS=30` 次整 CSR 迭代，先做局部 Top-K，再确定性排序最多 100 个返回 chunk candidate；禁止把全部 chunk 结果转成 Python tuple 并全排序。旧版、缺失、损坏、越界工件或任何 parent/source identity 失配都会在图使用前返回明确 unavailable reason；不存在整图或事后过滤兜底。运行时最多保留 2 个组合 partition handle，冷加载/组合 single-flight。`SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED` 控制发布，`SOURCE_PARTITIONED_PPR_ENABLED` 控制运行时 reader；两者默认开启且可独立回滚。reader 只供统一 Ask/深度报告激活服务使用，不可用时 fail closed 回 B。

版本化的所选来源 rollout gate 使用五类强制场景的 baseline/shadow 配对 observation：单篇、少量来源、至少含 10,000 chunks 的超大单篇、mounted-base evidence，以及 gold evidence 必须分别绑定不同来源别名的跨来源同名对象。两条 lane 必须使用相同 provider/model/prompt version、temperature、Top-P、seed、corpus signature、本地/基座 source id、alias-to-id 绑定与场景规模。EnergAIzer/PDAgent 案例必须执行 `expand_graph`，至少保留 20 个 baseline KG candidate，原 baseline candidate list/manifest 与 citation key 完全不变，且 baseline eviction 为零。任何越界 evidence/citation、畸形指标、scope 漂移、缺少图动作或 baseline 改变都是硬失败。Evidence Recall@20、citation coverage、citation validity、grounded-sentence coverage 与非空答案率不得下降；no-answer、erroneous-refusal 与 outline-drop rate 不得上升——逐案例与汇总都要满足；baseline 非零的结构分母不能在 shadow 变成零。shadow 延迟与数据库读取行数最多为 baseline 的 1.5 倍、峰值内存最多 2 倍，每案例最多新增 4,000 prompt tokens 和一次模型调用。不含正文的 attestation 只保存 run/corpus/model/policy identity、钉死的 canonical-golden 摘要、场景计数、无正文的逐案例与汇总指标、失败项及完整性摘要；绝不包含问题、答案、evidence/citation/source id 或 excerpt。production 不信任裸 `approved`，会重新验证每个逐案例和汇总质量/成本护栏。摘要不是签名，因此 active rollout 必须从受信部署工件读取，并同时匹配期望 corpus 与 model；任一 pin 缺失都 fail closed。`off` 完全 inert；`shadow` 无需 attestation 即可采集配对数据；`allowlist`、稳定 hash rollout 与 `on` 在默认 policy attestation 未验证批准时一律 fail closed。这道 gate 是所有用户可见 Ask/深度报告激活的强制控制点。

所选来源激活契约由 Ask 与深度报告共用，但它始终是内部检索实现细节。对真正收窄的本地来源范围，Ask 的 `chunk`、`reasoning`、实验 `graph` 与深度报告都会在历史 baseline 冻结后调用同一个所选来源激活服务。省略 scope 或全选 snapshot（包括单来源 notebook 中选中唯一来源）不进入该服务，保持历史响应形状。`SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow` 为默认值：它构建并测量 `G` 但仍返回 `B`；`allowlist`、稳定 hash `rollout` 与 `on` 必须同时通过受信、无正文 attestation 及精确 corpus/model pin。active 输出严格为 `B` 后追加 `G`；`G` 的默认独立预算为 4,000 tokens，不能驱逐、重排或重打分 `B`。snapshot/partition 失败、scope 漂移、任何终态越界候选或非零 baseline eviction 都 fail closed 回 `B`。mounted base 证据继续走独立历史 lane，绝不参加当前 notebook 的所选来源图遍历。rollout 状态不得进入公开 Ask/报告字段、推理轨迹、进度 stream 或 UI；只有无正文内部 `selected_source_graph` 事件携带它，浏览器还会过滤历史持久化的 `source_subgraph` 步骤。

无正文 `selected_source_graph` 事件仅属于运维内部 telemetry，所有用户可读 debug 日志的列表、统计、搜索、翻页和详情响应都会过滤它。

## Knowhow 表

notebook 内的 **Knowhow 表** 动作（与知识图谱并列，单开一个面板）管理 **knowhow 表**：把领域经验沉淀成一行行经验记录，列名自由命名。首个实例是半导体时序违例排查（行=违例类型；列=现象识别、根因分析、修复方法、依赖工具），但列名完全是用户自定义文本，不锁定词表。建表可以从**导入**开始（xlsx/csv/Markdown，预览时给出列→内容类型的映射建议）：新表导入会让用户选择“属性按列”（默认，首行为表头）或“属性按行”（首列为属性名），后端在预览与确认导入前自动把属性行表转置成内部统一的属性列表；追加导入与投影管线仍只处理属性列表。结构校验失败会在向导里显示安全、可操作的修改建议：尤其是记录分组使用横向合并单元格的属性行表，合并展开后也能识别并提示改选“属性按行”；重复/空表头、不支持的文件、编码错误和失效的列设置都会说明应修改什么，不再只显示“请重试”。也可以用**建表向导**从零搭建（先定列名表头，再填行）。填值两条路可自由混用：应用内经**格子编辑器**（markdown 编辑默认单栏专注、可切「并列对比」或「铺满预览」且按会话记忆、图片粘贴或拖拽即可上传、自动本地草稿（每条离开路径都会先把未保存内容同步落成可恢复的本地草稿、落不进就不离开，经 Esc／点背景／× 关闭或切换格子会先确认）、保存并下一格连续录入），或线下走 **Excel 模板往返**：按当前表头下载 `.xlsx` 模板（表头行冻结），批量填写后上传追加（提交前会预览未匹配列，以及行标题与已有行重名的提示）。

至多一列可被指定为整张表的**行标题列**（表级设置，不是逐列打标）。设置后，表中每个非空格子都会成为知识图谱节点——节点的类型就是所在列名——并用 `about` 边连回该行的行标题格；同一列里不同行出现的相同值会归并成一个节点（十行都引用同一个工具，就是一个工具节点带十条入边）。不设置行标题列，整张表就只参与检索——格子照常切成 chunk 供问答使用，但不建任何图谱节点，适合每行是一条记录而非一个具名概念的流水型表格。

投影出的格子知识对象默认进入 reasoning/graph 的图谱节点检索（`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED=true`），因此命中格子既可作为图遍历种子，引用也保留直达该行详情的跳转。把开关设为 `false` 只回滚这条直接对象路径；逐格 chunk 仍参与问答检索。默认开启后的类型扩展只认由表 `hidden_source_id` 持有的对象，不会把无关的自定义 Schema 类型扫进来；按来源收窄的类型集合和标准化 chunk-vector→对象旁挂会跨子查询做 single-flight 缓存。旁挂版本同时包含图变更状态和这批格子 chunk-vector 的计数/时间，因此不会 bump KG 的纯向量修复也能触发刷新；KG 变更还会显式驱逐缓存。

投影状态是整表完成契约，不是逐行进度捷径：整表的 chunk、embedding、知识对象/关系、变更序号与图缓存通知全部完成前，行保持 `pending`/`syncing`；只有这些收尾工作成功后才发布 `synced`。因此调用方观察到所有行均已结束时，可以立即读取完整图谱，不受后台线程调度先后的影响。状态发布还必须匹配本轮读取的表变更序号：旧任务绝不能覆盖并发新编辑留下的 `pending`，新版本由已排队的下一轮任务处理。

每列还带一个**内容类型**——仅作确定性解析提示，从不调用 LLM：**方法步骤**列解析成有序步骤列表，**工具/事物**列按列表项/换行拆分并去重成多个节点，**普通**列整格作为一个节点。格子编辑器与行详情抽屉都提供显式的**优化表达**按钮（绝不自动触发）：调用系统为 `knowhow_optimize` 绑定的服务，在保持原意的前提下规整结构与措辞，原文与建议对照展示，只有逐格确认后才会回填。

行/整表的**一键规整**先冻结完整整表快照，再以有界前端并发生成候选，禁止无界 `Promise.all`：上限取 3 与当前 `knowhow_reformat` 服务实时容量的较小值，状态读取失败时安全回退为 2。相同 `(column_id, trimmed 原始 Markdown)` 的输入共享同一个在途请求；只有请求成功且结果仍未过期才可复用。取消或关闭后不再启动新任务，并忽略迟到响应。进度按物理格子计数，局部失败可重试；整体确认后仍按完整物理/共享保存单元串行写入。每个保存单元继续校验 `expected_before`、anchor 指定、完整分组精确成员及 HTTP 409 stale 守卫；过期候选保留并可重跑，父表直到弹窗关闭才 reload。任何 stale 一经观察就立即登记待刷新，即使随后中止其余慢请求也不会丢失。队列里的有改动/已保存项可在同一弹窗进入单项原始 Markdown diff（行级增删，并对中文、拉丁文本、标点、空白做有界行内 token 高亮），也可切到渲染预览；超长内容降级为有界首尾摘要。已保存项可安全结束批量弹窗，再打开既有物理格详情；若同一批次还观察到 stale，父组件必须先等待带 epoch 守卫的详情 reload 成功，再从新行集重算目标。刷新失败、请求 epoch 失效或行/列已消失时不得打开旧目标，并通过现有可恢复的表操作错误横幅提示用户。共享/合并值选择 `row.position` 最小、再按 row id 的稳定代表格。该流程仍是一次整体确认，不扩成逐项接受/拒绝。

主表继续采用 `table-layout: fixed`、横向滚动和 sticky 首列，但通过 `colgroup` 输出内容感知宽度。纯函数只摘要表头和最多 64 条可见行（前 48 + 后 16）；每个样本格必须在换行归一化、Markdown 正则/分行和 grapheme 处理之前先截断固定 code-unit 前缀，再最多考察 8 条可见行、120 个 grapheme，折减 Markdown 控制符，并让 CJK/全角/emoji 权重大于 ASCII，最后按列类型套 min/max，状态/操作列保持固定宽度；窄屏使用更紧的范围。计算以表 identity、列和可见行为 `useMemo` 依赖，任何 render 都不得做无界 R×C 或整格扫描。本次不包含手动拖拽和宽度持久化。

Knowhow 的所有权与权限仍使用稳定 user id（`created_by`、owner 与权限检查）。面向人的审计快照（`knowhow_changes.actor`、里程碑创建者、格子代码更新者标签）统一取 session 用户 `username.trim()`，其次 `display_name.trim()`，最后才回退 user id；Agent 继续使用 `profile_name`。所有普通 Knowhow 写入口共用同一 actor-label helper，复制、导入、转移等路径则显式拆开 identity id 与审计 label。历史 id 形状的审计值不做破坏性改写；读 API 在固定上限内批量识别并解析为当前 username，未知/已删除用户或 Agent 自由文本原样返回，严禁 N+1，async 路由中的同步 identity 投影必须在线程池执行。对 `origin=agent` 的历史 change，只解析语义 `payload.before` 快照中被替换的旧人类 updater；Agent actor 与 `payload.after/current` 的 updater 始终按 profile 自由文本原样保留，即使碰巧形似 user id。wire 字段名保持兼容。尤其 `knowhow_cell_code.updated_by` 已参与整表 fingerprint，不能仅为显示批量改写存量：新写入保存 label，单格 GET/PUT 响应直接携带可读 `updated_by`，读取投影不改变历史链。

记录型表的行详情抽屉，以及行标题分组矩阵里的每个物理分支，都提供显式的**智能补全空列**。它只为该行真正未存值或存为精确空串的格子生成建议；已经保存的纯空白文本仍算现有内容，不会被静默改写。一轮请求汇合两路证据：同表最多 8 条至少填写了一个目标列的参考行（优先同一行标题分组，再按当前已填写列的相似程度与覆盖度挑选），以及一次对当前 notebook 与当前有效显式挂载参考库的有界 `ReasoningRetriever` 检索。后者复用 Ask `reasoning` 的规划、联邦检索、反思、关系扩展与有证据查询期推导，但补全专用策略会在候选进入模型反思前排除私有 Memory 与当前表自身投影，并关闭来源归属不透明的 PPR/社区扩展以及精确标识符通道（补全的查询是 JSON 信封而非问题，否则会探测信封自身的字段名）；它不会调用 Ask 答案合成、创建对话/job 或保存 Ask 答案。结构化响应会为每个请求列给出建议或明确放弃、置信度、简短依据、合法同表行 id 与服务端签发的库内 evidence key，并附最终推理轨迹和有界证据卡。模型伪造的 evidence key 会被过滤；过滤后没有任何合法表内或库内引用的建议会强制变成 abstain。personal 与 base 证据冲突时以 base 为准并披露差异。可拖动的审阅弹窗分开展示同表参考与禁用链接/图片的库内 Markdown 证据，用户逐项确认，系统绝不自动写入。接受时仍走普通格子保存，并携带 `expected_before=""` 与 `origin="llm_complete"`：生成期间若格子已被填写，就不会覆盖，正常历史与同步照常触发。`reasoning_agent` 与 `knowhow_complete` 都必须配置且以 system 级指令把证据视为不可信数据。推理响应畸形、任一 provider 不可用、检索/合成失败，或合成响应不可解析/顶层结构不可用时显式失败；单条建议畸形则过滤、降级或转成 abstain。任何路径都不能静默退成同表补全或用离线内容冒充建议。

Ask 引用命中 knowhow 格子时会直接跳转到该行的详情抽屉，而非通用来源视图。notebook 深拷贝会把 knowhow 表完整带过去——表、列、行、格子、代码附件在副本里全部重新映射 id——且不重跑 embedding，未变化的格子文本在副本里复用原向量。

外部 Agent 接入面（HTTP + MCP、判别集、代码附件）见 [Memory 与 Agent MCP](#memory-与-agent-mcp)；HTTP 路径清单见 [API](#api)。

## Memory 与 Agent MCP

从界面签发 token、配置 Codex/Claude CLI 并运行官方 client 示例的逐步操作见
[Agent MCP 与 Memory 接入 SOP](./agent-mcp-memory-sop_zh.md)。本节继续作为产品/API 权威契约；
SOP 负责首次接入与验收步骤。

Memory 必须由用户手动选择、归创建者私有，并且始终绑定到且只绑定到一个 notebook。
在 Ask 回答上点击“保存到 Memory”后，后端先生成标题、正文和标签预览，用户可编辑，
只有最终确认才写入 `confirmed` Memory。预览模型未配置或失败时，系统确定性地用问题作
标题，并用移除显示引用后的回答作正文。当该 Memory 所属 notebook 非 base 库且已开启
知识图谱抽取（与上传来源同一判定门）时，确认动作与“保存到 Memory”弹窗会显示默认勾选的
“同时抽取到知识图谱”复选框；勾选后用与上传逐字相同的抽取管线把该 confirmed Memory 抽进
该 notebook 自己的 KG，记为对用户不可见、不进任何来源列表与计数的隐藏合成源，可在每次
确认时取消；base 库除外，只经下文的晋升人审进入 KG。总 Memory 页面只聚合当前登录用户的数据；
notebook 卡片数量和 notebook Memory 标签是同一份数据的 notebook 局部视图。总数与待确认数
始终按 owner 全量统计，不随状态、搜索或 notebook 筛选变化；notebook 筛选项来自有界的 owner
聚合查询，不做逐 notebook 查询。

生命周期为 `candidate | confirmed | rejected | deprecated`。Agent 只能创建 `candidate`；
token 具备 `memory:read_candidates` 时，同一用户、当前所选 notebook 下获授权的所有 Agent
profile 都可检索它。Candidate 永远不会进入正式 notebook Ask、notebook 搜索、Deep Report
或 `search_notebook_context`；只有用户确认后才进入正式平面。Rejected/deprecated 在两个
平面都排除。检索先判断相关性，权威只在同等相关或冲突证据间生效：
`candidate < personal 原始证据 < confirmed Memory < base KG/base 原始证据`。

Candidate provenance 会保存创建它的 Agent profile id/name 与每一条提交的 evidence ref，但绝不
保存 bearer token。服务端逐条按 candidate 的 owner 与 notebook 校验，并保存 `validated` 或
`invalid` 状态及有界原因；历史未验证或无效引用仍可由 owner 查看，但绝不会标成 trusted 或成为
可晋升 evidence。Candidate 详情、审核与 provenance API/UI 都只对 owner 开放。把 Ask 回答保存为
Memory 时，后端会在写 Memory、revision、provenance 的同一个 `BEGIN IMMEDIATE` 事务内再次校验
owner/member 实时权限，因此并发撤销分享不会留下半写入 Memory。

由 Ask 保存的 Memory 会持久化回答的 citation provenance，并在每张 Memory 卡片展示来源显示名、
与之不同时的原始上传文件名、位置和原文摘录。在 notebook 内的 Memory 标签中，仍存活的引用可经
当前活跃 notebook 的参与集权限打开精确 source element。复制或移动后的 Memory 继续在嵌套
provenance 中保留原引用，但明确标成仅存档；目标 notebook 不会把这些旧 id 当成新的跳转权限。

Memory 输入在 API 与 service 两层统一归一化并 fail-closed：title/content 去除首尾空白后必须非空。
当前上限为 title 80 字符、content 40,000 字符、tag 最多 20 个且每个 80 字符、审核/candidate
reason 1,000 字符、task context 序列化 UTF-8 8,192 bytes、evidence 最多 50 条且序列化 UTF-8
32,768 bytes、client request id 200 字符。HTTP 违规返回 422；MCP/内部调用也经过同一 service 校验。
嵌套 NaN、正负 Infinity 会在持久化前被拒绝，合法 JSON null 则保持原样往返。
MCP 提案严格使用这些 Core 上限，不再叠加更窄的重复限制。
tag 原始列表会先按 20 条限额校验，再 trim/去重；空白 tag 直接拒绝。

总 Memory 页的“Agent 接入”可创建稳定 Agent profile，以及明文只显示一次的 token。
Token 有过期时间、默认 notebook、notebook allowlist，并只授予所需的
`knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
`ask:execute`、`knowhow:code` 子集；可即时撤销。后端 requirements 已包含官方 `mcp>=1.26.0` client/server
SDK。启动后，Streamable HTTP 服务位于 `/mcp`（到 `/mcp/` 的 redirect 已处理）。本机可用
loopback HTTP；默认允许远程明文 HTTP 并放宽 Host/Origin（DNS-rebinding）校验，供可信内网使用，
启动会打印明文告警（Agent token 明文过网）。公网部署请设 `MCP_REQUIRE_HTTPS=1` 强制 HTTPS
（并恢复 Host/Origin 校验），并把 `MCP_PUBLIC_URL` 设为公开的 HTTPS `/mcp` URL。
过期时间必须带明确时区偏移；浏览器把本地 `datetime-local` 转成 UTC，后端按 UTC 瞬间归一化保存。
无时区 datetime 会被拒绝，不会按服务端本地时区猜测。

Codex 推荐把签发的 token 放入环境变量，再注册服务：

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<一次性显示的 token>'
codex mcp add silicon-notebook --url http://127.0.0.1:8000/mcp \
  --bearer-token-env-var SILICON_NOTEBOOK_AGENT_TOKEN
```

当前本机 Claude Code CLI 接受 HTTP transport 和显式 Authorization header：

```bash
claude mcp add --transport http silicon-notebook http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <一次性显示的 token>"
```

Claude Code 可能把这段原始 header 保存到本机配置。应使用最小 scope、短有效期，保护
本机配置，并在使用后撤销/轮换；不要假设该 header 会做 shell 环境变量插值。

每个新 MCP session 必须先调用 `select_notebook`，再调用数据工具。精确的十一个工具是：
`list_notebooks`、`select_notebook`、`search_agent_memory`、
`search_notebook_context`、`get_memory`、`ask_notebook`、`propose_memory`、
`list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row`、
`put_knowhow_cell_code`。
服务端会在数据调用时重新检查 scope、allowlist、token 状态和 notebook 权限；返回文本是
不可信 evidence，不是可执行的 Agent 指令。

四个 knowhow 工具与 `/api/agent/knowhow/...` 下的 HTTP 端点（见 [API](#api)）共用同一套
service 函数，HTTP 与 MCP 不会在响应形状上走样。`list_knowhow_tables`、
`get_knowhow_discrimination`、`get_knowhow_row` 需要 `knowledge:read`；
`get_knowhow_discrimination` 对设有行标题列的表按行返回标题，以及每个方法步骤列的
`{column_id, column_name, text, code_status}`（表未设行标题列则返回 400），供 Agent
据此跑自己的判别逻辑挑选适用的修复方法。`get_knowhow_row` 返回一行的完整格子文本
（方法步骤/工具事物列另带 `steps`/`items`）及该行全部**代码附件**的代码本体。代码附件
是外部 Agent 针对某格方法已经写好的代码——notebook 从不生成也不执行，也从不进
embedding/chunk/索引/KG 投影——其新鲜度（`implemented`/`stale`/`none`）在读取时用格子
当前内容的 hash 与附件保存时的 hash 比对推导；判别集只带这个三态，不带代码本体，以控制
体积。读代码依然只需要 `knowledge:read`；只有写入（`put_knowhow_cell_code`，以及对应的
HTTP `PUT`/`DELETE .../code`）才需要 `knowhow:code`——一个既要读现有代码又要写新版本的
token，两个 scope 都要授予。

只有 `confirmed` Memory 可发起 KG 晋升。创建者提交后，admin queue 展示脱敏后的结构化提取
候选与服务端验证过的 evidence，而不是原始 Memory revision/provenance 浏览器。提案会固定精确的
来源 revision、脱敏候选快照和审核所见 evidence；编辑或弃用审核中的 Memory 会在同一事务中废止
旧提案并重置晋升状态，编辑后可重新提交。当前 provenance 会清除 proposal 指针，固定提案只保留在
快照与队列历史中。批准时会重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，
并在写事务内校验固定 revision 与 notebook，再复用 KG dedupe/merge 创建或合并一个或多个 Base KG 对象；批准/拒绝会记录当前登录的
admin reviewer，API 与晋升审计记录完整的 `base_object_ids`。这一过程不会改变或暴露原私有 Memory。
删除 notebook 会级联删除所有成员绑定到它的私有 Memory，因此删除弹窗会提示这一生命周期
后果，但不会泄露成员身份或数量。

仓库内固定 Memory 评测计算 Recall@5、MRR、nDCG，以及三项零容忍计数：candidate 进入正式
平面、跨用户、跨 notebook 泄漏。A/B harness 比较 no-Memory、KB-only 与
KB+confirmed-Memory 三种检索条件。

## KG 抽取触发

源解析 + 向量化完成后即可做 chunk-native 检索，因此 **KG 抽取按 notebook「按需开启」，并非每次上传都抽**：

| 上传时 notebook 状态 | 是否抽 KG | 怎么触发 |
|---|---|---|
| 尚无 KG（新库） | **不**自动抽 | 按需构建：`POST /api/notebooks/{id}/kg/build`（界面：notebook 的**「构建知识图谱」**动作；在无 KG 的库上选「深入分析」组——即 `strict` 的 `reasoning` / `graph`——时也会提示构建） |
| 已有 KG | 每个新源**自动后台抽取** | 无需手动触发——续抽以保持 KG 完整；新源随后增量融入跨文档统一 KG |

摄取期判定 = `KG_AUTO_EXTRACT 或 该 notebook 已有 KG`：

- `KG_AUTO_EXTRACT`（默认 `false`）——为 `true` 时**所有** notebook 每次上传都抽 KG。
- 否则仅当该 notebook 已有 KG 对象时，上传才抽。

即：**首次 opt-in**（构建 KG，或设 `KG_AUTO_EXTRACT=true`），之后新文档自动抽取 + 融合。整库重抽用 `POST /api/notebooks/{id}/kg/rebuild`；离线批量构建见[离线批量摄取](./operations_zh.md#离线批量摄取目录--kg)。

### KG 构建故障隔离

手动整理/全部重新分析会创建持久化、任务级的 `kg_build_jobs` 记录；同一 notebook
同时只允许一项 KG 任务运行。Notebook 与索引状态 API 会返回
`probing → extracting → stopping → finished`、来源进度和经过审查的用户提示。
前端刷新后仍能恢复该状态；失败后显示「继续分析未完成内容」。

被中断的任务落到同一个失败终态：离线批量运行按 Ctrl-C 或收到终止信号时，会合作式
停掉在飞窗口、在任务落终态之前把它们排空（守卫随该行一起释放），并记 `worker_interrupted`，因此被终止的运行不会让知识库一直显示「分析
中」。只有无法捕获的终止（SIGKILL、被 OOM 杀、掉电）才会把该行留在进行中，那种情况
由服务端启动兜底落终态。

每次 KG 模型请求使用 `KG_LLM_TIMEOUT_SECONDS`（默认 `60` 秒），瞬态错误最多重试
`KG_LLM_MAX_RETRIES` 次（默认 `2`，允许 `0..3`）。若服务持续不可达，或认证失败/
请求被永久拒绝，本次任务共享的中断控制会阻止继续发起请求，取消尚未开始的
source/window 工作，在首个窗口确认熔断时、窗口级与来源级 drain 开始前就持久化
`stopping`，再等待已经开始的调用安全退出。中断范围只限当前 notebook 的本次 KG
任务，不影响其他 notebook 或之后重新发起的任务。可用性探测会显式绕过 LLM 响应
缓存且不回写缓存，旧的成功探测不能在当前模型已经不可用时放行破坏性重建。

已完成来源的结果会保留；同一来源的 object/relation 分块共用一个 SQLite 事务，被
中断或写入失败的来源不会留下半成品；旧版本遗留但最新 extraction run 已失败的图也
仍判定为未完成。之后普通「继续分析」只处理未完成来源。只有显式「全部重新分析」
会清空已有 KG，而且会在删除前先探测模型服务。若进程重启时仍有 running job，启动
恢复会把 job 与 running extraction run 标为 failed，并把所有遗留的 `extracting`
来源恢复为 `parsed`，包括尚未来得及创建 extraction run 就中断的来源。extraction
run 进入完成或失败终态后还会精确失效该 notebook 的待处理来源缓存，避免轮询长期把
已完成来源误报为未完成。

前端用 notebook、workspace epoch 与请求 epoch 共同约束建库响应归属，并在持久化 job
仍为 `running` 时持续轮询，不再用固定时限伪造本地完成。安全结构化事件覆盖
`kg_build_started`、`kg_build_progress`、`kg_build_circuit_opened`、
`kg_build_stopping`、`kg_build_succeeded` 与 `kg_build_failed`，不记录 provider
诊断、prompt、来源正文、token 或凭据。

## 命令目录（工具手册）

工具的**命令手册**是常规摄取处理得最差的一类文档：分块会把同一条命令的说明、参数、
示例切成互不相干的片段，KG 抽取又会把参数表变成一堆游离的断言——于是一个关于命令
契约的问题，回来的是一段**关于**这条命令的散文。命令目录改为把这类来源按结构化条目
摄取（命令名、语法、参数、默认值、示例），并在人工确认后落进一张普通的 Knowhow 表。

全流程 opt-in、按来源触发，上传时什么也不跑。

**前提：来源必须已经解析完。** 成本预告与发起都要求 `parse_status` 落在
「已解析」这一档（`parsed` / `extracting` / `extracted`——后两档是解析之后的知识图谱
抽取阶段，元素早已齐了），否则返回带用户可读文案的 `409`：还在解析（或只导入了
元数据）就请等解析完成，解析失败则请重新解析或重新上传。对着还没落下元素的来源做的
成本预告会报出「约 0 个命令节」，读起来像「这份文档没什么可抽的」；对它发起识别则会
把一份手册的一小段记成完整的一次识别。

**成本预告。** `.../command-catalog/preview` 给出形状检测的计数（标识符式标题、
用法行段落、参数密集段落）以及一次抽取要跑多少节、发多少次模型调用，**零模型调用**。
它只读来源的一段有界前缀，所以 `sampled=true` 时这些数字是文档开头的**下界**而不是
普查——一个会把自己要估算的那次扫描先做一遍的成本预告没有意义。`is_manual` 只是给
调用方一个默认值，真正该拿去判断的是那些计数。

**抽取。** 每个来源一个后台任务，由覆盖 `queued` 与 `running` 的条件唯一索引守卫：
行先写、线程后起，落在那个窗口里的重复请求会被挡住，而不是排出第二个写同一份候选的
worker。每个分片一次模型调用，一个分片是一条命令的一批参数（大参数表一次问会撑爆
输出预算，所以分片是必需的而不是优化）。没有第二意见，也没有精炼轮次。

**接地校验，以及条目为什么会被拦。** 每条抽取结果落库前都要拿本节原文核对：命令名
必须在服务端给出的候选清单里、且逐字出现在原文；每个参数名必须以原始形态出现（写成
`-density` 的就得带前导短横，本节原文里写着 `-density` 而回答只给 `density` 一律拦）；
`syntax` 必须是原文某条用法行的连续拷贝；原文里找不到的 `default` 会被
清空。命令名不过关否决整条，其余只丢那一个字段。**被拦的条目同样入表**，带上原因和
一段有界的原文窗口——一次产出很少的抽取，用户唯一能自己判断「是模型错了还是这份
文档根本不是手册」的依据就是它们。

**没有短横参数的命令照样抽参数。** 一整节里一个 `-flag` 都没有，不等于这条命令没有
参数——`set_dont_use lib_cells` 这种**位置参数**正是单行文档最常见的写法。这类命令没有
可服务的参数清单，所以问法不同（照着用法行把位置参数原样抄回来，真没有才回空），但
把关的规则一模一样：名字必须逐字出现在本节原文，编出来的照样被拦并入表。

**每一批参数只按它自己那批判。** 一条命令的参数多时会分几批提取，每批只问其中一部分，
而它的回答要在两个方向上都对得上那一批：不在这一批里的参数即便逐字对得上原文也要丢掉
（同一条命令的所有参数都写在同一段原文里，光靠接地校验分不出它属于哪一批），这一批里
问了却没回来的参数则记在这条命令上。两者都在审阅面板里挨着这条命令显示。后者同时是
保留率诚实的前提：分母是「这一轮问了多少」而不是「模型愿意答多少」，所以二十个参数只
答一个是 5% 而不是 100%。一批参数覆盖率不到一半时会拆成两半重问一次——与回答过长走的
是同一个补救，因为是同一个毛病；而回答条数与所问一样多、只是答错了的那种不重问，问得
更少也救不了它。

**熔断。** 处理满十节之后，命令名整条否决率超过 20%、参数保留率低于 50%、或「完全没给出
可用回答的分片占比超过 20%」，任一成立就直接判失败并给出用户可读的理由，而不是带着一份看
起来合理的近空目录收工。三根轴缺一不可，因为它们互相看不见：一条结果完全可以选对命令名、
同时把参数全部编造出来；而一个什么都不返回的模型要按「它没在回答」如实报出来，而不是报成
「参数丢了」这个症状——那一轮会付完整本手册的钱，最后报成功、目录是空的。瞬态的模型服务
错误（限流、上游报错）不算抽取结果：它直接判任务失败，绝不会被记成「这一节本来就没有
命令」。

**模型自撰的字段有上界，也有标注。** 说明、示例和每个参数自己的说明是接地校验刻意不查的
三个字段（散文无法逐字比对），所以都在候选落库前截断：每个字段各有上界，参数说明还额外
有整行的总量上界，被总量截掉的条数与其他拦截原因一起如实报出。示例在审阅面板里显示，并
附一句「示例为模型生成，未经原文校验」。

**确认与合并。** 候选在人工确认前都是未生效的。确认时若不存在则创建名为
「命令目录：<来源标题>」的 Knowhow 表（固定六列：命令 / 语法 / 参数 / 说明 / 示例 /
出处，其中「命令」是行标题列），已存在则**只新增表里没有的命令**。`<来源标题>` 与来源在
产品其他各处（引用卡、证据卡、清单卡）显示的名字是同一个：来源已接地判定为论文且解析出
非空 `paper_title` 时用论文标题，否则用普通来源名/上传文件名——同一份手册不会在自己的
目录表上叫另一个名字。这个标题只在**首次**为某个任务创建目标表那一刻解析一次；此后该
任务的确认都按它记住的 `applied_table_id` 继续写回同一张表，哪怕后续论文元数据补抽让
标题变了，也不会改名或劈出第二张表。重新发起识别产生的新任务（自己的
`applied_table_id` 起初为空）同样会写回这张表——继承该来源最近一次任务确认写入的目标；
只有这个继承目标已被删除时，才会改按派生标题新建或查找。候选的命令在表里
已有对应行时，会原样回报进 `conflicts`，那一行**不动**——v1 刻意绝不覆盖用户可能已经
手工订正过的内容，完整的 diff/merge 属于后续任务。列按**名字**寻址，所以之后编辑目标表的
列不会把内容悄悄挪进错误的一列；表若已丢掉「命令」列，则拒绝写入而不是硬写。一次
`all_pending` 最多确认一页，剩余数量由 `pending_remaining` 如实回报。落库全部走 Knowhow
既有服务层，所以表的变更历史会像记录任何一次普通编辑那样记录它们。

每一条退出路径都会把任务行落成终态，`Ctrl-C`/`SIGTERM` 也不例外：留在
`queued`/`running` 的行会一直占住这个来源的守卫直到后端重启，进程自己兜不住的那部分
由启动兜底收尾。

**上一轮还有候选没审阅完时，重新发起识别会被拦住，跳过是唯一的显式放弃出口。**
`.../job` 只会返回一个来源最近一次任务，若这时又发起新一轮，上一轮的候选就再也够不着
了——永远留在 `candidate` 态、却没有任何页面能重新打开它。前端与 `.../command-catalog`
自己的 `409` 都会拦住这种情况（覆盖上一轮可能到达的任意终态：成功、失败或取消），
拦截提示写的就是「确认或跳过」。`apply` 只在候选与目标表冲突时才会把它挪出 `candidate`
态而不落库；一条审阅者单纯不想要、又不冲突的候选此前没有任何路可走。
`.../command-catalog/dismiss` 就是这条路——审阅面板里「跳过所选」/「跳过全部待审阅」
两个动作，选择契约、并发锁（每个笔记本一把的目录锁）与 owner-only 权限都镜像 `apply`，
只是完全不碰 Knowhow 表。

**来源被重新解析之后，这一轮的结果就作废了。** 每个任务在创建时记下当时的**来源代次**
（该来源全部元素共有的那一个落库时刻，重新解析会整批换掉它们），`apply` / `dismiss`
在动手之前先比一次：对不上就返回带用户可读文案的 `409`，并在同一次调用里把这个任务
剩下的候选整批标成 `dismissed`（原因 `source_reparsed`）。候选里的命令名、原文摘录与
出处指向的全是抽取时读到的那一代元素，原样确认进知识表写下去的就是文档里已经不存在的
内容。整批作废不是附赠的：上面那条「待审阅候选拦重跑」的守卫会读同一批候选，不清掉它们
就会两头卡死——每次确认因为过期被拒，每次重跑又因为还有未审候选被拒。同一条判据也让
重新发起识别在来源已重新解析时直接放行（顺手清掉旧候选），因为重新解析恰恰就是用户想
重跑的原因。代次刻意**不**取 `sources.updated_at`：那是有意做粗的变更信号，每次状态迁移
（重新抽取知识图谱、写摘要）都会推进而元素纹丝不动，按它判会在一次普通的重新抽取之后
谎报「来源已重新解析」，并逼用户重跑一整轮付费识别。

端点（都作用在路径里那个笔记本自己的来源上；读取需要笔记本读权限，写入需要 owner）：

- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/preview` —— 形状信号 + 成本预告，有界读取触顶时带 `sampled`；来源尚未解析完或解析失败时返回带用户可读文案的 `409`
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog` —— 发起抽取；该来源已有活跃任务（既有任务从 `.../job` 取）、抽取模型未配置、来源尚未解析完或解析失败、或上一轮还有候选没审阅完（需先确认或跳过）时返回带用户可读文案的 `409`。上一轮的候选若已因来源被重新解析而过期，则不拦，改为整批清掉再放行
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/job` —— 该来源最近一次任务：`status` 与 `progress`（`sections_total`、`sections_done`、`entries`、`rejected`、`uncovered`、`truncated_sections`、`pending_candidates`），失败时带 `failure_reason`。内部诊断列 `diagnostic` 刻意不进响应
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/cancel` —— `cancelling`（worker 会在下一个分片边界停，飞行中的一次模型调用被取消时也会停，不必等到那次调用返回）、`cancelled`（本进程没有在跑它，直接落终态）或 `not_running`
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/candidates` —— keyset 分页（`job_id?`、`state=candidate|rejected|applied|dismissed`、`cursor`、`limit`）并带各档 `counts`。`next_cursor` 是上一页最后一条的 `position` 而不是 offset：确认候选会改 `state`，offset 分页会漏行/重行。`dismissed` 候选带 `dismiss_reason`：`conflict_existing_row`（apply 发现已有同名行）、`user_dismissed`（人工显式跳过）或 `source_reparsed`（来源已重新解析，这一轮结果整批过期）
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/apply` —— body 为 `{candidate_ids}` **二选一** `{all_pending: true}`——两者同时非空/为真会返回带用户可读文案的 `422`（此前会静默偏向 `all_pending`，比调用方明确写出的 `candidate_ids` 更宽的一次写，调用方看不出自己传的选择被悄悄吞掉）；返回 `table_id`、`created`、`applied`、`rows_added`、`conflicts` 与 `pending_remaining`（一次调用最多确认一页）。来源在这一轮之后被重新解析时返回带用户可读文案的 `409`，并整批作废该任务剩余候选；重新解析**正在进行中**（还没换掉元素）时是另一条 `409`，措辞不同且**不作废任何候选**——解析可能在换元素之前就失败，那批候选仍然有效
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/dismiss` —— body 为 `{candidate_ids}` **二选一** `{all_pending: true}`，选择契约（含两者同传时的 `422`）与单页上限同 `apply`；把选中的 `candidate` 态候选标记为 `dismissed`（原因 `user_dismissed`），不碰任何 Knowhow 表；返回 `dismissed`（真正被标记的 id）与 `pending_remaining`。来源被重新解析、或重新解析正在进行中时，两条 `409` 都同 `apply`

## 检索模式（问答）

`POST /ask` 按 `mode` 分派——注册表 `backend/app/services/ask_modes.py` 是唯一真源（默认 `chunk`）。联合范围按路径区分：`chunk` 基线 active-only；可选 KG overlay / PPR 可加入 federated KG 与 base-backed chunk；`graph` / `reasoning` 走 federated KG。`federated_retrieve()` 的知识对象命中不改 score，只在完全平局时以 `base` 为第二排序键；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。这些排序信号不进入接地阈值。

| 模式 | 分组 | 需 KG | 一句话 |
|------|------|-------|--------|
| **`chunk`**（默认） | general | 否 | chunk-native 通用问答：大召回 → 选择 → 长上下文综合 → 引用绑回源 chunk。 |
| **`graph`** | strict | 是 | 对跨文档知识图谱做单趟个性化 PageRank（PPR）传播。 |
| **`reasoning`** | strict | 是 | agentic 迭代 plan → retrieve → reflect → answer（流式输出实时轨迹）。 |

### id 与显示名

上表的 id（`chunk` / `reasoning` / `graph`，以及分组 id `general` / `strict`）是**协议**：`POST /ask` 收的是它，历史会话与书签存的是它，后端注册表 `backend/app/services/ask_modes.py` 声明的也是它。它们是稳定量，不因为「名字不好听」而改。

界面上**显示**的名字是另一层，纯 UI，归前端注册表 `frontend/app/ask-modes.ts` 所有：

| 协议 id | 问答面板显示名 |
|---|---|
| `chunk` | 通用问答 |
| 分组 `strict`（选择器给出的入口，组内默认引擎是 `reasoning`） | 深入分析 |
| `reasoning` | 逐步推理 |
| `graph` | 关联追溯 |

该注册表的 `groupLabel()` / `modeLabel()` 是唯一读取口：前端任何其它文件都不得硬编码显示名，散文里提到就用模板插值。两边由 `ask-modes.test.mjs` 强制——它递归扫描 `frontend/app`，当前显示名出现在注册表之外即失败，退休名（严格推理 / 深挖推理 / 图谱多跳）复活也失败。因此改显示名只是改注册表一行，不动任何 id、请求/响应载荷或已存会话；id 集合另由 `scripts/check_ask_modes_contract.py` 跨前后端锁同步。

**`chunk` —— chunk-native，含可选 chunk×graph mix。**
- *基线：* chunk 大召回（`CHUNK_RECALL`）→ MMR / 多子查询配额多样性选择（`CHUNK_MMR_K`）→ 长上下文综合，不碰 KG。
- *mix*（仅当 `CHUNK_KG_OVERLAY_ENABLED=true` **且** 配齐 qwen3-rerank **且** 有 KG 时生效）：三路并池——(a) 向量 chunk、(b) query 种子周围的 KG 局部结构（实体 + 其 1-hop 关系，只检索一次）、(c) 这些 KG 对象背后的源 chunk——round-robin 合并 → qwen3 cross-encoder rerank → 按 token 预算装填（`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`）。每个候选会累积 producer support，不能从融合分反推来源。最终选择可通过 `CHUNK_GRAPH_RESERVE`（默认 0/关闭）为已经越过原相关度门槛的 graph-only chunk 预留席位，并通过 `EXACT_SECTION_RESERVE`（默认 4）为下面的精确标识符通道预留席位；两者都绝不扩大 item/token 预算，也不改变 oversized-first-chunk 例外，更不会挤掉对方已保住的块。答案在同一套 `[k]` 映射里同时引用 chunk 与 KG 项，接地跨 chunk ∪ KG。未配 rerank 或无 KG 时**字节等价回退**到基线。（忠实 LightRAG 的 `mix` 模式。）

**精确标识符通道（`EXACT_LOOKUP_ENABLED`，默认开）。** 有一类问题靠调排序治不好：它其实是「这是哪一节」的定位问题。手册里 `set_db` 的小节被分块切成主描述 / 参数表 / 示例，相关性打分只会留下其中最强的一块，答案就缺参数细节。因此问题里点到可精确查找的名称时，检索先多跑一条零模型通道——精确子串匹配定位小节，再把该节（含其子节）整体取齐——并按各分支既有口径并入候选。这把闸刻意比词法层的标识符抽取更窄：带 `_` 或 `.` 的名称（`set_db`、`config.yaml`）一律放行，而只用连字符连接的词必须带数字，于是型号/版本名（`GPT-4`、`v1-2`）留下，普通英文词组（`state-of-the-art`、`real-time`、`end-to-end`）被拦住。拦它们的理由很实在：这类词几乎出现在每个分析型问题里——深度报告每一节的问题差不多都含一个——而每个词都要付一次真探测，一旦命中章节标题还会把整章推进证据预算。它们仍留在词法召回里，那边多一个词项几乎不花钱。命中随后先折叠到它所属的那条命令，再分配小节名额，因此一条命令的参数表/示例子节不会把第二条被点名命令的名额吃光。这些块按 keyword-only 计分——算的是「对**名称**的覆盖率」而不是「对整句问题的相关度」，于是两个调用方对同一块证据给出同一个数——带普通 `lexical` 来源标注，并在 mix 最终选择中占 `EXACT_SECTION_RESERVE` 个席位，避免 rerank 又把参数表截掉。所有查询都有硬界（`EXACT_LOOKUP_MAX_IDENTIFIERS` / `EXACT_LOOKUP_FTS_K` / `EXACT_LOOKUP_MAX_SECTIONS` / `EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION`）；问题里没有这类名称时不会多发任何查询，那些提问完全不受影响。只作用于当前笔记本，挂载的参考库刻意不在范围内。**标题面包屑是 Markdown 解析路径才有的东西**，所以「整节取齐」只对那样解析的来源成立；MinerU 解析的 PDF/DOCX 没有面包屑，通道在那里退回到「精确返回命中的那些块」，仍然能救回普通排序会丢掉的参数表。

**`graph` —— 跨文档 KG 上的 PPR。** 经 `federated_retrieve` 取种子（KG 实体 + 其源 chunk；`RELATION_RETRIEVAL_ENABLED=true` 时再融合关系索引命中）作为 HippoRAG 式**个性化 PageRank**（`GRAPH_PPR_ENABLED`，默认开）的个性化向量，通过共享知识图谱把相关度跨文档传播；排名靠前的 chunk 喂出接地答案，`[k]` 锚点指向 KG 对象/关系。`GRAPH_PPR_ENABLED=false` 时回退为沿推理边的有界 BFS。

**`reasoning` —— 意图优先的 agentic 深挖检索。** 正式界面先调用 `/ask/intent`；这一步最多使用当前会话最近五个用户问题，不使用语料派生的助手回答，也不读取语料或创建持久 conversation/job。清晰请求自动确认，阻断性歧义以内联审阅等待补充。`/ask` 与 `/ask/stream` 在原始 `question` 之外接收已审阅的 `intent`；后端确定性冻结合同，形成唯一内部研究问题，供 Memory 检索、PPR、证据检索与答案合成共用。确认后的检索方向直接成为首轮子查询，不再执行旧的第二次问题规划；反思阶段可以按证据增加查询，但不能替换合同。超出该档位首轮宽度的方向不会被丢弃：一段确定性补种在 PPR / 精确查找 seed pass 之后、反思循环之前按合同顺序执行它们，最多占用共享推理步骤预算的一半，每条执行过的方向都产生一条普通的 `retrieve` 轨迹步。一份 run 内的注册表把每个已审阅方向映射到唯一简称(默认截断点撞出同一简称时依次加宽,仍撞则追加序号后缀),并可反查回方向原文——模型自始至终只看得到简称、也只会用简称重提;`add_subquery` 提交经这份注册表解析后,命中尚未执行的方向会按方向自己的完整契约原文执行(账目记在方向身份上,不是裸简称),命中已经执行过的方向(无论是补种执行的还是此前某轮 `add_subquery` 补上的)则判定为重复、不重跑检索。每一轮反思提示都会看到当前仍未覆盖的方向,好让模型优先把自己的预算花在这些方向上;等反思循环结束,才会按这份终态把仍未覆盖的方向记成一条 `skip` 步——不是按预算刚耗尽那一刻的旧账,若反思期间已经全部补齐则不落这一步。响应持久化确认后的 `intent`、暴露内部 `retrieval_query`，并在正式检索前以 `intent` 作为首个引擎轨迹步骤；会话里保存/显示的仍是原始问题。轨迹覆盖整轮而不只是检索段：问题理解跑在持久 job 之前，界面自行合成理解阶段的前几步（理解中 → 已理解或待澄清 → 已确认）拼在后端步骤之前，不再为它另设轨迹之外的提示条；该阶段的客户端墙钟以可选且有上限的 `intent.understanding_ms` 回传（绝不参与检索），成为持久 `intent` 步的 `duration_ms`。后端在 Memory 检索之前推送 `intent` 步；命中私有记忆时记 `memory` 步，它记录的是**召回**而非归因（归因由答案里的 `[k]` 引用承担），零命中则记一条带耗时的 `skip` 步，让候选查询与 embedding 调用的耗时留在总耗时里。答案生成之后记 `synthesis` 步——那次生成通常是整轮最长的一段，既要可见也要计入轨迹总耗时；它的引用数取绑定锚点，不取检索到的证据卡数。未携带 `intent` 的直接兼容调用保留清晰问题的旧路径，但遇到确定性无法解析的指代或纯泛化请求会 fail closed。随后委托 `ReasoningRetriever` 检索（与 `graph` 同样走 PPR 传播）、反思是否充分，按需扩图/加子查询直到能回答，并经 NDJSON stream（`/ask/stream`）输出 `reasoning_trace`。遇到显式推导问题时可调用 `follow_chain`：通过两轮有界邻接抽样复用既有 source/target 索引，再确定性检查类型、状态、审核、evidence 与 `validity_scope`；两条存储关系作为可引用前提，`A→C` 只作为带「推断」标记的查询期结论。高度节点抽样被截断且无法证明不存在直接边时，宁可不推。上面的精确标识符通道以两种方式接进这个循环，两者都零模型调用，且覆盖 `reasoning` 问答与每一节深度报告检索（报告引擎逐字复用 `ReasoningRetriever`）；`graph` 模式不接，knowhow 智能补全按策略位主动关闭它（补全的查询是 JSON 信封而非问题，否则会在每次请求上探测信封自身的字段名）。权威问题本身点名了标识符时，初检索之后无条件跑一次确定性 seed pass（记 `exact_lookup` 轨迹步，界面显示「精查」），打分用的是它实际探测到的名称本身而非整句问题——把精确命中拿去和长问题里一堆无关词打分会把它的相关度拖低到丢字符预算、甚至拖过接地判定阈值；反思模型也可以在某个被点名命令的完整定义仍未覆盖时，主动选择 `exact_lookup` 动作并给出 `exact_term`，打分方式相同。该动作与 seed pass 共用同一把名称形状闸——低选择度的任意短串会被拒绝，而不是变成全库子串扫描——每个名称一次 run 内只探测一次（seed pass 共用同一份账目），agent 主动调用每 run 至多 3 次，被跳过、重复或零收益的尝试都会带着模型能据此调整的理由回喂给下一轮反思（按理由/名称去重，同一个非法输入不会让账目无限增长）。问题里没有标识符时不发任何调用，也不多出轨迹步。严格 / KG 接地。

#### 来源范围只由用户勾选决定

检索的来源范围**只**来自[按来源选择检索范围](#按来源选择检索范围)那组可见来源复选框；模型不参与判断问题点名了哪个来源。语料盲意图规划器不输出任何来源身份，也不存在来源确认步骤：一次逐步推理从开始到结束都继承请求携带的复选框上限，run 内任何动作都改不动它。

早先的版本允许意图规划器输出 `source_refs`，由 `/ask/intent` 在有界、纯身份的来源目录里按稳定 id、显示标题或原始文件名做规范化**精确等值**匹配，配一道 `source_scope_confirmation` 审阅闸与签名预检能力，并提供 `search_evidence(query, source_refs?)` 动作在已确认 run 内继续收窄。这整套合同已移除。精确等值兑现不了简称——问「pdagent」而来源标题是「PDAGENT-BENCH: Characterizing, Grounding, and Architecting LLM/VLM Agents for VLSI Physical Design」时解析结果为零匹配——而该设计又是 fail closed 的，于是一句普通问题会以确定性 422 失败、重试无效。既然来源勾选本就在用户手里，模型再猜一遍只增加失败模式，不增加能力。

### 逐步推理档位与完整集合请求

档位在提问框里通过与深度报告「研究深度」**同一个**档位控件选择——共用一个组件，两处不会走样：一个带当前档名的 chip，点开是滑块弹层，显示该档档名与一句中性说明。界面只呈现档名与那句说明；精确上限在下面这张表（由 `frontend/app/ask-retrieval-effort.ts` 与 `backend/app/core/ask_retrieval_policy.py` 双向锁定），不铺在控件上。`answer_element_items` 与下表末三列的 `enum_*` 是这个镜像关系的例外——它们都是后端专有字段，前端没有对应消费者，只影响服务端最终合成 prompt 的组装与[集合枚举工具](#集合枚举工具)的预算。

逐步推理接受下表五个稳定的 `retrieval_effort` 协议 id，默认 `standard`。证据充分时模型可以提前停止，但不能突破任一上限。“最终 floor / aspect / cap”的计算是 `min(cap, max(floor, aspect × 实际执行查询数))`。KG / 原文上下文是真正的证据字符硬上限：原文分区包含结构化预览、chunk 与直接来源元素；KG 分区包含 KG 对象/关系、已确认 Memory 与查询期推导链；最终证据块不超过两者之和。`answer_element_items` 是最终合成 prompt 里允许纳入的直接来源元素(公式/表格/图片等)条数上限，按检索相关度降序择优而非插入序，且仍占用上面同一份原文上下文预算。`enum_page_size` / `enum_pages_per_run` / `enum_rows_per_run` 约束下文的集合枚举工具（`enumerate_elements` / `enumerate_kg_objects` 两个动作及其 `collection="sources"` 参数值）：页大小各档相同，每 run 的额外翻页次数与累计行数随档位增长，与其他上限同一套增长节奏。

| 档位 id | 界面名 | 每查询相关性结果 | 最终 floor / aspect / cap | 最大推理步骤 / 首轮子查询 | KG / 原文上下文字符 | 合成纳入的直接来源元素 | 枚举页大小 | 每 run 额外翻页 | 每 run 累计行数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `overview` | 概览 | 4 | 8 / 2 / 12 | 4 / 2 | 4,000 / 12,000 | 4 | 50 | 2 | 100 |
| `standard` | 标准 | 8 | 20 / 3 / 36 | 8 / 5 | 6,000 / 30,000 | 6 | 50 | 4 | 200 |
| `deep` | 深入 | 8 | 24 / 4 / 48 | 16 / 6 | 8,000 / 50,000 | 8 | 50 | 6 | 300 |
| `thorough` | 详尽 | 12 | 32 / 5 / 64 | 32 / 8 | 12,000 / 80,000 | 12 | 50 | 8 | 400 |
| `exhaustive` | 穷尽 | 16 | 40 / 6 / 96 | 50 / 10 | 16,000 / 120,000 | 16 | 50 | 12 | 600 |

“首轮子查询”上限只约束**首轮并发**，不代表装不下的已确认方向就不再执行。超出该宽度的已审阅方向会顺延进一段有界补种：它排在确定性 seed pass 之后、反思循环之前，与推理步骤共用同一份预算（补种最多用掉其中一半，反思循环始终留有可用份额）。步骤预算覆盖不到的方向会持续回喂给反思阶段让模型优先补齐；等反思循环结束仍未覆盖的方向,才会在轨迹里作为一条跳过步显式披露（反映的是运行结束时的最终状态，不是预算刚耗尽那一刻），绝不静默丢弃，反思期间用简称重提某个方向也会被识别为对应同一方向而非另一次检索。

候选生成不随档位放大，而由部署参数独立控制。`CHUNK_RECALL` 默认 **200**，分别约束带索引的 Chunk/KG ANN 与词法候选窗（默认去重前最多 400）；`RELATION_RECALL` 默认 **200**，分别约束 Relation ANN 与词法端点扩出的关系 ID 总窗（默认去重前最多 400），词法总窗内部仍为 source/target 两个方向预留份额。修改任一部署值都会改变实际候选窗，因此界面不会把默认值展示成请求级硬上限。

意图预检把结果范围分为 `ranked`、`complete`、`aggregate`、`hybrid`，并带显式完整性标记；用户确认时会根据最终编辑后的权威措辞与澄清答案确定性重算 scope。像“列出这个 Knowhow 表的所有方法”这样的请求不会变成更大的相关性 Top-N。确定性执行器只接收它能从物理行证明正确的语义：直接整表行/方法清单、直接物理行/记录计数，以及在此基础上的分析。条件子集、去重/种类计数（如“多少种”）、分组聚合目前回退到相关性检索，并明确说明尚不支持精确完整性。受支持的 100 行表可以返回 `100/100`。五档共用完全相同的完整枚举安全契约：

| 完整枚举阈值 | 精确上限 |
|---|---:|
| 每个游标页的行数 | 25 |
| 每次请求页数 | 50 |
| 每次请求扫描/返回的物理行数 | 1,250 |
| 每次请求的表数 | 8 |
| 每表选择的列数 | 8 |
| 交给模型的单元格摘录 | 1,000 字符 |
| 结构化结果载荷 | 256,000 字符 |
| 答案正文内联行数 | 100 |
| 每个结果卡初始显示行数 | 20 |

正文 100 行同时也是 hybrid 模型一次能看到的最大行预览，但不会删除权威结构化结果中的行。因此响应分开表达每表覆盖、请求/批次覆盖（`selected_tables/known_tables`、返回/已知行）和 hybrid 分析覆盖；例如枚举 `200/200`、分析 `100/200` 必须写成“枚举完整、分析部分”。界面初始 20 行只影响展示，其余已回传行可展开并跳回原行。轻量 catalog 最多返回 8 个表描述，不读取格内容、代码附件或健康详情；它会在应用窗口前优先放入问题中显式点名的表，因此排序第 9 张及之后的目标表仍可访问。全库聚合计数与序列和仍覆盖整个 notebook。只有游标耗尽，且枚举前后的 `mutation_seq`、基于历史的 `enumeration_seq`、行数、列元数据和所选/全局范围均稳定，覆盖率才可标完整。触及任一安全线或并发改表时，批次返回 `complete=false` + `explicit_partial`；因 8 表上限遗漏其他表时，已经单独耗尽的所选表仍可保持自己的 `complete=true`。低档位不会缩小这些完整枚举上限。当前执行器只覆盖上述 Knowhow 物理行语义；其他 Knowhow 语义仍必须披露尚不支持完整枚举。来源元素、知识对象与库内文档集合另有一条独立的有界枚举路径——见下文[集合枚举工具](#集合枚举工具)——由模型显式调用，而不是被这里的意图 scope 执行器自动触发；Memory 与其他集合仍必须披露尚不支持完整枚举。

退役 id `fast`、`global` 透明映射到 `chunk`（旧会话/书签不会 422）；其余未知 mode 返回 HTTP 422。

### 集合枚举工具

逐步推理还能通过模型调用的 reflect 动作 `enumerate_elements`（来源元素）与 `enumerate_kg_objects`（知识对象），以及在这两个动作上给出的 `enumerate.collection` 参数值 `"sources"`（改为列出库里的文档），对这几类有界集合做**列举**而非只做排序。模型面的动作空间在这个特性内维持 10 个：第三个集合是一个参数值，不是第三个动作（[下文](#大纲便签与按节合成)的大纲便签之后另外新增了货真价实的第十一个动作 `update_outline`，与这里无关）。三个集合都由零 LLM 执行器提供：模型只决定是否枚举、枚举多少；覆盖率永远由执行器算出，绝不采信模型自报。这是与上文 Knowhow 物理行执行器**并行的独立机制**——共用同一张按档位缩放的预算表，但不共用同一个意图 scope 门，且由模型显式调用而非被 `result_scope` 自动触发。

**可枚举集合。** 来源元素：`formula`（公式）/ `table`（表格）/ `image`（图片）/ `code_block`（代码块）（paragraph、heading 等高体量低语义类型刻意排除）。知识对象：`concept`（概念）/ `claim`（论断）/ `formula`（公式）/ `procedure`（过程），限定为可用状态。两张白名单各只有一处真源（`app.services.collection_catalog`），不允许再抄一份。来源清单没有子类型——库的文档目录本身就是一整个集合。

**来源清单（库里有哪几篇）。** 给出 `enumerate.collection:"sources"`（此时 enumerate 的其他参数忽略——目录没有子类型；该字段的其他取值则落回按动作本身的类型分派），按来源页签的顺序（创建时间、再 id）列出作用域内的每一份文档——所以截断时的前缀就是最早加入的那几篇，那是界面唯一支持的「前 N 篇」读法：显示名（接地判定为论文时优先显示论文标题，与引用同口径）、文档类型（就是上传时那张类型表里的界面词，未识别则不显示）、以及该来源**已存**的摘要摘录。它列的就是来源页签列的那一批——同一条「用户可见来源」谓词，所以答案里不会出现一张用户在来源页签上根本看不到的文档卡（Memory 合成源与 knowhow 表的隐藏投影源两侧都不在内）。它的用途是让「当前 notebook 的文章分析」「库里有哪几篇」这类问题先拿到目录，再按标题对值得深入的那几篇继续检索——后一步走既有的补充子查询机制，没有新增能力。这份清单不读取原文、不做模型调用，成本是每个参与库一次有界读取加上按页的批量取值。目录进入答案合成时，prompt 会同时带上一条粒度指导：篇数少到能逐篇处理就逐篇作答，篇数多则按主题归纳成几个维度并点名各维度下的文档。这个判断交给模型——它手上有精确计数，也只有它知道问题对每一篇的要求有多深——任何位置都不设数值阈值。

**清单里永远不含私有 Memory。** 一条已确认的 Memory 属于某一个人，其他通道也都是这么对待它的：Ask 只经按 owner 隔离的记忆检索通道读到它，Knowhow 智能补全按合同把它排除在候选之外。而集合清单是按笔记本的参与库作用域取数的，自身没有 owner 过滤——把 Memory 留在作用域里，就等于让共享笔记本的每个成员都能按公式/表格/图片/代码块、以及从它抽出的知识对象，把别人的确认记忆逐条读出来。因此地图计数与两个执行器都排除隐藏的 Memory 合成源及其对象，且是**无条件**的：单人笔记本与共享笔记本口径完全一致，一份清单永远只有一个含义。（Knowhow 投影源仍然在内——那是全笔记本可见的内容，本来就能从表本身看到。）这个口径刻意窄于看板上的知识计数：后者回答的是「这个库里有多少知识」，把 Memory 派生对象算进去是对的。

**集合地图（先计数后取行）。** 在这些工具真正值得调用之前，每个 run 只构建一次一行文本，形如 `[Collections in scope] elements: formula 12 (3 sources), table 5, image 0, code_block 0 | KG objects: concept 1234, claim 567, formula 89, procedure 0 | knowhow tables: 2 | sources: 7`（每个白名单 kind/type 恒在场，缺省即 0，让模型能分清「数出来是 0」与「压根没上报」；硬顶 600 字符），注入 plan/reflect 上下文，作用域为 active notebook 加其当前有效的挂载参考库。行尾的 `sources` 是用户可见的文档数（与来源页签、文档数量上限同一口径），且与来源清单的分母出自同一处，不会出现「地图说 7、清单列 8」。计数是索引辅助的，不携带来源标题、文件名或摘录文本——它是 prompt 脚手架，不是证据。地图统计的每源集合与枚举执行器实际遍历的源集合按构造逐字一致，因此不会出现「地图说 12、清单只给 8」的假部分。

同一行还会进入**写答案的那次模型调用**：作为原文证据分区最前面的一小块服务端确定性输出（固定表头 + 已封顶的地图行，整块不超过 800 字符；因为它是服务端算出来的、不是检索到的条目，所以不带 `[k]` 引用）。这是在补一个真实的缺口：reflect 提示语明确要求模型「集合远大于本轮清单额度时，别翻页，直接用计数作答」，而答案合成是另一次调用，本来根本看不到那个数——极端情况下（集合很大、又没检索到别的证据）合成压根不会触发。只要枚举工具开着且地图建成，这一块就会注入，与本轮是否真的枚举过无关；它排在所有证据块最前，避免预算吃紧时第一个被牺牲的恰好是它。

**覆盖率合同。** 每个已枚举集合都携带一个 `TypedCollectionCoverage`：`returned_total`（跨续跑链累计已返回的条数）、`total`（地图给出的已知规模，取不到时为 `null`——渲染为总数未知，绝不写成 `/0`）、`complete`、`truncated_reason`（`budget` / `payload` / `concurrent_change`）与 `overflow_semantics`（沿用共享的 `explicit_partial`）。`complete=true` 要求游标耗尽、首页前与末页后的作用域身份一致，且——跨续跑链——累计条数与已知规模相符；其余情况一律 `complete=false`。这里的「作用域身份」包含参与的库集合本身，并在收尾时重新解析一次：翻页途中挂载、卸载或使某个参考库失效，会改变「整个作用域」指的是什么，此时结果按并发变化报告，绝不说成「全部」。覆盖率之外，结果还携带 `synthesis_rows` / `synthesis_complete`：已枚举的清单与真正进入答案合成 prompt 的有界预览分开统计，因此「枚举 200/200、预览 100」会披露为「枚举完整、分析部分」——与 Knowhow batch 自身的枚举/合成两轨披露同构。这份预览额度在本轮的各个集合之间**分配**而非先到先得：先给每份清单保底等分，某份装不满的余量再按顺序让给后面的。于是一轮里列了两个集合就会预览两个，而不是被第一份吃光额度、让多集合问题只按一张卡作答。

**出处与答案归因合同。** 每个送达的来源元素/KG 对象条目至多携带一条仍存活的有界原文引用；KG 对象从自身已封顶的 evidence element id 列表中取首个仍有效的元素。挂载参考库的证据由服务端在 active notebook 已授权的 participant 集内解析，浏览器只拿来源标题、位置、定位 id 与摘录，绝不调用参考库的成员专用 source 端点。真正进入合成预览的每个条目都会获得隔离的 `k5001+` id 和反向映射；模型使用该条目时必须引用对应 `[k]`，最终回答也只有这些实际绑定的清单锚点才算完成归因。只有带存活原文出处的确定性精确枚举行才能据此判 grounded，同时不会伪造语义相关度分数；孤立行仍可列出并引用其清单身份，但不能把回答判为有据。反过来，枚举答案若一个锚点都没绑定，响应不会把其他相关性检索卡片当作这份清单的兜底来源。结果卡直接显示每一项的原文引用：本库与挂载参考库条目都可经 active-notebook 代理跳到精确来源元素，参考库来源仍保持只读。若历史 KG 条目引用的元素已经全部消失，界面明确显示「暂无可用原文出处」，绝不伪造引用。
**界面展示。** 问答结果卡始终显示覆盖率徽章、清单名称与可选的单一来源范围，但条目内容默认收起。用户打开后才挂载按来源分组的元素或知识对象，并先显示既有的 20 条预览；额外的已加载条目仍通过第二个控件展开。部分结果与合成预览披露位于折叠内容内，卡片关闭时不会挂载或请求图片条目。

**预算与续跑。** 一次动作在其 run 级预算内（`enum_rows_per_run` 总行数、`enum_pages_per_run` 额外翻页次数，以及共享的 `structured_payload_chars` 结构化载荷上限）自动翻页，直到游标耗尽或触及某项上限；只有每个被访问分片（元素按来源、知识对象按参与的库）的第二页及之后才计入页上限，因为一个分片的第一页是唯一能看到它的方式。三个上限都是**每次请求**的，不是每个动作各来一份：每次动作只拿到本 run 的剩余额度，因此一轮深度检索里的多次枚举，累计返回的结构化载荷也不会超过文档写明的请求级上限。载荷上限在两个位置、按两种量法各收一次口，因为它们回答的是两个问题：执行器在遍历时按紧凑的内部行计费（这道闸拦的是「读得比该请求允许产出的还多」），响应映射再按**序列化后的行**计费——那才是真正下发与持久化的形状，而且明显更宽。被第二道闸拦停的那一份，自己报 `truncated_reason=payload`，`returned_total` 等于实际送达的条数；合成预览也按送达的那份渲染，所以 prompt 的覆盖率块头与结果卡永远报同一个数。该游标是 run 内部句柄，不出现在响应里——客户端只看到覆盖率徽章；截断时看到的是「同一轮推理内可以继续列出」，而不是一个可操作的续跑入口。`complete=false` 恒含一个可续跑的游标，唯一例外是 `truncated_reason=concurrent_change`——此时作用域在两次调用之间发生了变化，链条绝不能静默从头重来。对尚未列完的集合重复请求，若本 run 预算尚有行数/翻页/载荷余量则从该游标续跑；预算耗尽时改为按「已达本轮上限」跳过（仍披露为部分结果），而不是按「已枚举过」跳过——只有链条已经 `complete=true` 的集合才会被跳过为「已枚举过」，因为再问一次只会重复已经报过的条目。

**限定单一来源。** 内部来源 id 从不上屏，也从不给模型看——候选摘要与引用里出现的一直是来源标题——所以「只列《某某》里的公式」是按**标题**表达的。服务端在地图已经规划要走的那批来源里做确定性解析：去掉首尾空白、忽略大小写后的精确匹配，绝不做模糊或按相关度排序的匹配，并且有界，避免一个很大的挂载参考库把一次反思变成全库标题扫描。名字没有匹配、或匹配到多个来源时，这一个动作被跳过并在推理轨迹里说明原因：既不会悄悄扩成对整个作用域的枚举（那是在回答另一个问题），也不会在同名文档里替用户挑一个。若作用域内含该类条目的来源多到超过解析器允许查看的范围，它直接拒绝作答，而不是拿看过的那一部分下结论——「唯一」是整个作用域的性质，同名的第二个来源可能就落在没看到的那一段里。

**治理历史很长的笔记本上的知识对象清单。** 知识对象的翻页是对 `(笔记本, 类型, 创建时间, id)` 的纯游标读取；已停用等不可用状态的对象在读回之后过滤，用的就是地图计数所用的同一份可用状态定义。之所以放到读取之后过滤，是为了让「一页」在某类型对象大多已被停用的笔记本上仍然只付一页的代价——把状态条件写进那条查询，它没有索引可走，会被迫访问无界多的行。过滤带来的额外读取本身按动作有界；触顶时给出的是普通的诚实部分结果（`truncated_reason=budget`）并附可续跑游标，绝不会把一份被悄悄截短的清单说成完整。

**尚未构建知识图谱的笔记本。** 这些工具不需要图谱。一个只解析了来源、还没做知识图谱分析的笔记本——自动抽取默认关闭，这本就是常态——照样可以被问「有哪些公式/表格/图片/代码块」；只要作用域里至少还有一个可枚举**元素或知识对象**集合，逐步推理就照常运行。响应里仍会如实报告该笔记本没有知识图谱（「构建知识图谱」的提示继续显示），只是不再因此拒绝作答。既没有图谱、也没有任何元素/知识对象集合的笔记本，仍然直接得到「请先构建知识图谱，或挂载一个已分析的参考库」——因为对它而言这类工具都只会返回空。来源数**刻意不算**在这道闸里：任何非空库的来源数都 ≥1，把它算进来等于把这道闸拆掉，那句明确的提示会对所有这类库消失；放行之后，来源清单与另外两个一样可用。

**范围指示语不是检索词。** 「当前 notebook」「这个库」「本库」「整个库」「知识图谱 / KG」这类短语指的是用户打开的那个笔记本及其挂载作用域——那是每次检索本来就在的范围，不是能在库里找到的内容；没有任何文档会写着装着它的那个库的名字。因此意图理解、两处规划与反思都被明确要求：把这类短语解析成范围之后**剥掉**，不带进任何子查询、关键词或要精确查找的名称，同时问题本身要原样留下（「当前 notebook 里的文章讲了什么」问的是那些文档，剥掉范围词不能把它变成另一个问题）。问库本身的规模或构成，答案来自上面那行计数与这几个枚举动作，而不是去搜「知识图谱」这几个字。这一层刻意只写在提示语里，不做确定性的词表剥离——那会变成词法路由，还会误伤真的在讨论知识图谱的文档。

**计数跟着解析当场生效。** 写入一个来源的元素时，该来源的变更信号在**同一个数据库事务**里一并推进，因此新元素一提交，支撑地图的按源计数就立即失效。刚解析完的来源马上就能被数到、进入遍历计划、被列出来——不存在「元素已入库、却仍被数成 0」的窗口，也不必等解析收尾的那次状态写入。（显式点名的 `source_id` 仍然直接查询该源而不从地图推断：用户亲自点名的一个来源，值得一次索引查询。）

**往返开销。** 一次枚举动作的页查询次数由它自己的预算限住，而且是**强制**的而非默认成立：没有该类条目的来源根本不会被访问，而一个来源之所以在计划里，正是因为地图在它里面数出过条目——访问它就会产出行，行数又受条目上限约束。于是页上限与行上限一起框住了总往返次数。每个被访问分片的第一页刻意不计入页上限：把它也计入，会让真实语料最常见的形态（一百个来源各一条公式）在任何档位都无法达到完整覆盖。

**跨库条目。** 条目若属于一个挂载参考库（而非 active notebook）的集合，会标注「来自参考库《名》」。来源详情跳转与图片统一走 active-notebook 代理端点：浏览器只按用户当前打开的笔记本过权限，服务端再在它实时有效的 participant 集内解析资源。参考库来源仍保持只读，浏览器绝不调用该库的成员专用端点。服务端还可随条目带出上面所述的有界原文引用，使清单卡和答案引用详情在跳转前即可展示标题、位置与摘录。**来源清单**里的跨库条目同一口径：既标注来源库名，也经同一条代理打开——它点名的那份文档正是该代理要解析的资源。仍保留的一项诚实边界是「按纯文本兜底解析（无结构化解析）」来源的计数披露：这类来源的元素本身就不存在，元素类枚举因此完全看不到它们；覆盖率只对**已经入库的元素**声明完整性，不对作用域内的每个来源声明完整性。

**「尚不支持完整枚举」免责声明何时不再前置。** 一个没有执行器能精确服务的完整性请求，回答会前置一句免责声明。只有四条同时成立时它才被抑制：意图 scope 不是 `aggregate`、意图合同里没有任何约束/排除项/前提、至少有一张清单结果卡返回了条目，且该卡的覆盖率为「完整」。其余情况一律保留。方向刻意偏向多警告：一张卡的覆盖率只证明「某个物理集合被完整走了一遍」，证明不了那个物理集合就是问题真正要的那个（经过条件筛选/分组/去重的）子集——而后者没有确定性判据，做出来只会是猜。

将 `REASONING_ENUM_TOOLS_ENABLED` 设为 `false` 可完全禁用这一整套：不构建地图、两个动作与来源清单参数一并不提供、无图早退恢复，逐步推理回到接入前的行为，零额外查询开销。

### 大纲便签与按节合成

被门控在「穷尽」检索档位的逐步推理，可以在反思循环中维护一份有界的、由模型撰写的大纲便签；当终态大纲解析后仍有两节或以上带着存活证据时，答案就按这份大纲逐节合成，而不是一次性通读全部证据来写。总开关是 `REASONING_OUTLINE_ENABLED`（默认 true）；关闭时，或档位不是「穷尽」时，这整套机制完全不存在——没有这个动作、没有对应 schema 分支、没有 trace 步，与这个特性从未接入逐字一致。

**`update_outline` 动作。** reflect 循环新增第十一个动作 id `update_outline`，只在「穷尽」档提供。每次调用都携带整份章节结构（结构是全量替换，不是增量补丁）：至多 12 节、两层（节可带 parent）、每节标题至多 60 字符、每节至多绑定 8 个证据 key。证据另守 citation persistence：同一稳定节 id 的合法 `evidence` 与旧绑定取并集，遗漏不再等于删除；要删除旧绑定必须在该节 `remove_evidence` 中点名，且同一个 key 同时出现在两处时显式删除优先。8 键满额时旧绑定优先，装不下的新 key 会在下一轮大纲账目中点名，而不是静默挤掉旧证据，模型可先显式腾位再重试。pending 本身不会冻结普通更新：常规额度尚存时，下一份全量载荷仍可增删、重排、改标题，里面所有合法绑定都会照常合并。`sufficient` 终态只有当轮真的提交了 `update_outline`、合并后仍有未接纳 key、且正常 reflect 步骤仍有余额时，才最多得到一次纯换键纠错；stale 熔断只看是否存在未接纳 key（当轮没碰大纲也会给出同样仅一次的纠错轮），直接 `answer` 收尾不追加纠错；第 6 次常规更新耗尽自身额度后也可暴露一次纯换键资格。这些纠错提交必须保持所有章节 id/标题/parent 不变、不能执行检索；第 6 次后的首次纠错提交立即消费资格，即使因结构违规被拒也不能重试。整体 `max_steps` 是绝对上限：最后一步产生 overflow 时只在收尾披露，不会跑成 51/50；stale 熔断事实会先落轨迹，再在仍有步骤余额时进入终态纠错。仍未接纳的 key 会在收尾 trace 中明确披露，答案只使用已经接受的绑定。整节从结构中省略仍会删除该节。每个 run 常规调用至多 6 次；超出后的非纠错提交会被跳过，并告知模型大纲已定稿。若一次提交里没有任何可用的节，同样跳过并**保留**上一份大纲——一次畸形响应把已经建好的大纲抹掉是最坏的结果，这套机制绝不让一轮坏响应清空已有结构。

**证据绑定由服务端校验，不采信模型自报。** 一个绑定 key 合法，当且仅当它仍在本 run 的存活候选池中，并且曾在至少一轮候选摘要窗口里实际展示过，或已经由当前大纲合法持有。run 内的「曾展示」集合单调累积，因此早先看见并绑定的 key 后来滑进摘要省略中段也不会失效；从未渲染过的中段 id 则不能仅靠模型猜中而通过。枚举清单的条目 id 与来源 id 刻意都不在合法集合内：前者模型根本看不到（清单只回计数，不回 id），后者是因为一份文档不是一条可引用的证据。非法 key 被静默丢弃；一节如果最终没有任何合法绑定，会被记为空节而不是被丢弃——空节正是模型下一步该定向检索的方向。

**未接纳 key 持久化。** 溢出的 key 是服务端持有的 pending 状态，不是只存在一轮的诊断。它会跨纠错提交持续点名，直到成功进入该节、模型把这个 pending key 本身写进 `remove_evidence` 明确放弃，或全量替换结构直接删除整节。模型即使腾出了旧位置却漏抄待换入的新 key，收尾 trace 仍会明确披露 unresolved overflow，不会静默丢掉新证据。完整 pending 由结构上限夹在每节 56 个 key（6 次常规提交加 1 次纠错、每次最多 8 个），但每轮 reflect prompt 只展示前 8 个和剩余计数；处理或放弃这一批后再露出下一批，与 `remove_evidence` 单次最多 8 个的输入上限对齐，prompt 不随批次数增长。

**大纲是整份回喂，不是增量对账。** 因为每轮反思都是一条全新的 prompt、没有对话历史，而章节结构又是全量替换语义，所以每轮都会把当前整份大纲连同各节缺失绑定的清单一起回喂，模型看到的永远是自己上次真正交出的东西，不必凭记忆重建；即便模型抄漏某个证据 key，服务端也会按稳定节 id 做并集保底。纯粹的措辞整理不算进展：大纲修订对推理循环的空转熔断保持中性（真正的检索动作仍会照常重置熔断计数），所以来回提交两份长得不一样、内容却没变化的大纲，蒙混不过熔断计数。

**采用引导。** 「动作在场」和「动作被用上」不是同一件事：在真实语料上，模型多次把一本笔记本的文档目录**完整列了出来**，然后把剩下的轮次花在无定向的检索上，而没有把这份目录变成章节。因此当大纲仍为空、而服务端手上已经握着开大纲的结构性理由时，反思上下文会与其它几份账目并排追加一行确定性的引导。它只在下列条件**全部成立**时出现：大纲闸开着；当前大纲**为空**（模型一旦建了大纲，便签本身就接管这个位置）；本 run 的引导额度（**2 轮**）尚未用尽；且存在两条结构性理由之一——本 run 已把**来源清单完整列完**且至少有 **2** 篇文档，或已确认意图给出了至少 **2** 个必答检索方向。两条都成立时用清单那条措辞，因为它带着一个真实条数。没列完的清单一律不算：按半份目录建出来的大纲天然缺节，而模型此刻并不知道自己缺了哪几节。这一行只陈述事实、点出条数，并显式给出「判断不需要就忽略」的出口——它是一条账目，不是一条命令，模型仍然可以选择一次作答。它不触发任何额外查询或模型调用、不新增动作 id，低于「穷尽」档或总开关关闭时逐字节缺席。真的发出引导的那一轮会给既有 reflect 轨迹步加上 `outline_nudged: true`；没发出的轮次，该步的 detail 键集不变。

**按节合成。** run 收尾时，若把终态大纲的绑定对到当时存活的候选池上，仍剩两节或以上带至少一个合法绑定，且该 run 没有产出集合清单（类型化枚举或结构化整表批次——清单 run 保持单次合成：清单预览与覆盖披露只进单次合成上下文，节切片装不下它们；把这种 run 节化，等于拿 ranked 样本写散文而让手上已有的完整清单闲置），答案就按节生成——每次调用只看见该节绑定证据装配出的上下文切片（复用既有的证据装配机制）。各节的号段互不相交，每节的引用标记先按该节自己的 id 映射解析、再合并——写出另一节号段的引用只可能是幻觉（模型压根没见过那个号），因此被丢弃，而不是被悄悄记到错误的那一节头上。答案正文就是各节自己的 `##`/`###` 标题按大纲顺序拼接（沿用聊天答案自身的标题字阶，与深度报告的标题字阶保持独立）。集合地图、枚举工具预览、私有 Memory 与查询期推导链都不会进入任何一节的切片——它们都不在合法绑定目标之列，一节根本没法「要」它们——在单次合成回退路径上保持原样不动。按节合成被绕过时（可装配节不足两节，或该 run 产出过清单），大纲披露不随之消失：只要大纲规划真的跑过，收尾 synthesis detail 仍带出大纲键集（`outline_sections` 为 0、`outline_fallback` 为 false），包括被略过节的标题——单侧答案不能在悄悄丢掉「问到了没找到」的那一面时显得完整。每一节都只用自己的证据切片与锚点通过 `classify_evidence` 判定。收尾 synthesis trace detail 里的 `section_grounded` 是有界的逐节记录列表（`id`、`title`、该节 `evidence_level` 与布尔 `grounded`），不是整篇布尔值，旁边另列无据节标题。响应继续复用既有三档 `evidence_level`：全部已合成节 grounded 时照旧采用全局分类结果；否则只把全局结果封顶在 `overview`，绝不向上抬。因此零节达到精确 `grounded` 档时，如果每节其实都有相关高分引用、只是模型自报保守，整篇仍可诚实保留 `overview`，不会被强制误写成 `inferred`／「未命中笔记本依据」。任一节的合成调用失败（在自己的重试之后仍失败）会丢弃整个半成品，转而回退到普通单次合成，而不是交付一份看不出缺口的残篇；若回退成功，那次失败会从用户可见的错误横幅里摘掉（事件日志仍完整记录），因为这一轮最终是恢复过来的。

**重试成功不再显示失败横幅。** 答案合成在第一次调用失败或返回空内容时会重试一次。这次有界重试现在遵循与上面回退同一条规则，而且适用于每一条问答路径、不只是按节合成内部：第二次拿到答案时，本次调用期间记下的失败会从响应的模型错误清单里摘掉，恢复过来的一轮不会在一份完整答案上方挂着「本次回答可能不完整」。两次都失败时所有报警一条不摘——包括最后那条空内容的终态报警，因为「检索到却答不出」必须可见。摘除只针对本次调用自己记的答案合成那几条，且按**工作身份**而非位置过滤：同一轮里其它工作（证据精炼、向量、重排）记下的报警一条不动——无论它记在这次调用之前还是调用过程之中，`events.jsonl` 两种情况下都完整记录。

**只经轨迹与答案自身的标题结构可见。** v1 刻意不为大纲新增任何 `AskResponse` 字段：每次大纲更新落一条独立的 `outline` 类型 trace 步（界面标签「大纲」），每写完一节落一条轻量的 `synthesis` 类型进度步，最终答案展示各节自己的 Markdown 标题。深度报告自己的逐节深挖在穷尽档同样共用这套机制——见下文「深度报告接入大纲共演化」。报告自己的「确认后冻结」研究问题合同不受影响：那里的大纲共演化只作用于节内部的检索与撰写结构，绝不触碰已确认的节/主题绑定。

**KG 弱支撑边回喂。** 大纲机制生效时，服务端还会从知识图谱本身给模型喂一条定向检索提示：每次**被接受**的 `update_outline` 调用之后，服务端看一遍模型刚绑定为证据的那些 KG 对象，找出从它们**出发**（仅出边——反向端无索引，是登记在案的残余）、且只有一两篇来源支撑的 canonical 关系——对综述类问题而言，这正是最值得继续追查的方向。总开关是 `REASONING_OUTLINE_KG_GAP_ENABLED`（默认 true），叠在大纲总闸之上；两者任一关闭都是零额外查询、prompt 逐字不变。提示通过模型已有的动作（`add_subquery` / `follow_chain` / `expand_graph`）生效——零新动作、零新模型调用、零 schema 变更。它绝不进入答案正文或引用，只是给检索用的便签指引。数值上限——支撑来源数阈值、探测行数上限、seed 上限、每轮行数、单行/整段字符界——是契约值，统一列在下表，这里不重复。每轮 `outline` 类型 trace 步在被接受的 apply 新入队候选时会带上整数字段 `kg_gap_candidates`（终态纠错轮算出的候选只入账、不渲染——那一轮自己的便签正文写着不得执行检索）。若 canonical 关系层缺席（从未构建过）或探测本身抛出异常，该特性对当轮静默且无害地缺席——KG 提示是锦上添花，绝不能成为整个 run 失败的理由。

| 配置项 | 数值 |
| --- | --- |
| 弱支撑阈值（支撑来源数） | ≤ 2 |
| 每次 apply 的探测行数上限 | 24 |
| 每次 apply 的 seed 上限 | 96 |
| 每轮 reflect 提示行数 | ≤ 6 |
| 单行字符数 | ≤ 80 |
| 整段字符数 | ≤ 520 |

### 深度报告接入大纲共演化(研究深度 ↔ 检索档位,PR-5)

深度报告的「研究深度」滑块与逐步推理「检索档位」选择器共用同一批档名。每节深挖（`_deep_dive`）现在把自己的 `depth` 值映射到与 `ask_retrieval_limits` 相同的档位预算，而不再是不论滑块位置永远按 `standard` 预算跑（PR-5 接入前的行为）。映射按阈值判定而非固定字典，因为接口把 `depth` 夹在 `[1, 16]` 的任意整数，不只是滑块的五个停靠点：

| depth ≥ | 检索档位 |
| --- | --- |
| 1 | overview（概览） |
| 2 | standard（标准） |
| 4 | deep（深入） |
| 8 | thorough（深度） |
| 16 | exhaustive（穷尽） |

处于中间值的 depth（如 3、5、7、15）落到更低的那一档，不向上取整。每节自己的反思步数上限（`max_steps`）仍等于报告自己的 depth 值（1/2/4/8/16），绝不采用检索档位表自身的步数上限（4/8/16/32/50）：报告的成本按节数放大，把某一档的步数上限乘上每一节不是用户在滑块上同意的那个量级；两者中更紧的那个数字始终生效。

**行为变化，显式登记。** 因为此前不论 depth 取值，检索预算都固定在 `standard`，所以低档位（1、2）现在的检索预算比接入前更小，高档位（8、16）现在的检索预算比接入前更大。这是把同名档位的语义对齐（同一个档名在 Ask 与深度报告两处买到同一份相关性/上下文预算）的修复，不是回归。

**档位同样缩放大纲阶段的 0-LLM 探针宽度。** 覆盖探针（逐必答主题）与充分性探针（逐草拟节）各自最多执行按档位定的查询数——`overview` 2 条、`standard`/`deep` 3 条、`thorough`/`exhaustive` 4 条（接入前不论深度恒为 4；报告行上缺 depth 时保持历史宽度）。默认档刻意保 3：确认问题恒居每个主题查询列表之首，宽度 2 只剩 1 条主题专属探针，而充分性判定的阈值对样本量敏感。登记的后果：低档喂进同一套充足/薄弱/缺失阈值的探针命中更少，其大纲充分性结论会比历史宽度 4 时读起来更保守。探针产出只喂给 STORM 规划器与充分性 Judge 的覆盖计数，绝不进报告正文证据，所以低档换到的是规划接地的粒度而不是证据——这正是低档在大库上提速的语义。同一次规划 run 内重复的探针查询（确认问题按设计排在每个主题查询列表之首，主题/节之间也常共享检索方向）只检索一次并记忆化；memo 不跨 run 存活、失败不缓存。大纲阶段的 LLM 调用同样随档位下调：意图理解与 STORM 各档照跑，充分性 Judge 的 **LLM 精修半**只在 `deep`/`thorough`/`exhaustive` 运行——`overview`/`standard` 保留 Judge 的确定性半（大纲编辑器显示的每节 coverage 计数与充足/薄弱/缺失结论），这与 Judge 既有 fail-open 路径的产出逐字一致（LLM 半本就只降不升、模型失败时整段跳过），且人工大纲确认门紧随其后。「多视角规划大纲中」与「大纲就绪」之间现在还会上报「检查各节证据充分性」，STORM 之后的探针段从此可见，不再呈现为卡死。每节深挖还会把**节复合问题作为第一条种子**（镜像 Ask：完整权威问题恒为首条），随后接该节经用户确认的检索方向（`intent_queries`）注入推理 run，从而跳过每节一次的规划 LLM 调用——已审阅的方向集是权威，不再让第二个模型重新解读；reflect 循环及其证据驱动的补查原样保留，run 步数预算装不下的方向仍由 run 后合并兜底执行，而 run 内检索**本身失败**的方向（账目上的稀疏 `failed` 标，与合法的零命中探针不同）同样由该合并重新执行，不会静默丢证据。

**档位管的是整节，不只是 `run()`。** 检索之后还有两段此前不看滑块、按定值跑，等于 `run()` 刚兑现的档位又被送了回去。两段现在同样随档位缩放：(a) 按方向补检索的**合并**——每条已确认检索方向仍然逐条真执行，但每个方向的**取数**按该档的 `ranked_per_query_take` 与元素额度（不再是固定的 20 + 8），合并结果再按该档的 `ranked_final_cap` 与 `answer_element_items` 重新截断（相关度降序，元素 tie-break `element_id`），4 个方向不再能把概览档的报告顶到它自己的上限之外。被大纲绑定、但**本来就在选集里**的对象优先占用上限席位而不是豁免上限（豁免会让总数越过上限），只有 `outline_evidence` 那批补集在上限之外——与它们在 Ask 侧位于 `top_hits` 选集之外是同一条规则。(b) 节撰写上下文——KG 块用 `kg_context_chars`，原文块与直接原文段**共享** `chunk_context_chars`（原文段吃 chunk 用剩的额度，且最多 `answer_element_items` 条进 prompt，按相关度择优而非插入序），不再是 `ANSWER_CONTEXT_BUDGET_CHARS` / `REPORT_SECTION_CHUNK_BUDGET` 那对定值外加一份 1/3 的元素额度。共享的来源分区同样给大纲留位置：绑定的 chunk 排在最前（该渲染器逐 chunk 独立，重排是安全的），绑定的原文段按自己的实际长度预留额度（上限为分区的一半）——否则 chunk 先吃满这份共享预算，绑定的原文段拿到的就是 0。大纲绑定的**元素**优先占用条数上限的席位并排在最前（上限本身仍是闭的——一份大纲能绑的键远多于该档允许的条数），与 KG 侧对绑定对象的规则一致——绑定键横跨三个候选 id 空间，被条数上限截掉的绑定元素与被截掉的对象一样会在「发现的结构」里丢掉自己的 `[k]`。查询期推导链与 confirmed Memory 按 KG 块用剩的 `kg_context_chars` **整块**准入（两者都自带硬上限，截半块会把一个 `[k]` 标记切断）；没被准入的块也不进证据映射。大纲绑定对象的优先额度在**一次** `knowledge_context` 调用内完成（`priority_object_ids`/`priority_budget_chars`），绝不由调用方拆成两次：该块末尾的 `relations:` 行是对一次调用自己的证据集内部求的，拆开会把所有跨两半的边静默丢掉。

**大纲便签与 KG 弱支撑边回喂在 depth=16（穷尽）时自动激活。** 因为 `outline_wiring_active` 只判 `limits.effort == "exhaustive"` 与 `REASONING_OUTLINE_ENABLED`（见上文）两个条件，报告经 depth 映射到达穷尽档时，每节深挖里会原样激活同一套大纲便签、`update_outline` 反思动作，以及（`REASONING_OUTLINE_KG_GAP_ENABLED` 开启时）弱支撑关系提示——零新增开关、报告侧零专属接线。集合枚举工具在这条路径上仍不可达：报告构造 `ReasoningRetriever` 时不传 `collection_catalog`/`collection_enumeration`，枚举闸不论档位都保持关闭。

**「发现的结构」块（仅节内生效，绝不回写已确认大纲）。** 当某节深挖整理出非空大纲便签时，`_deep_dive` 把终态子大纲连同各子节绑定的证据 key 折成一段有界的「发现的结构」块（≤12 行、行 ≤80 字符、整块 ≤1200 字符；超界按顺序截断并显式记账 `(+N 子节略)`，不静默丢行），作为 `discovered_structure` 传给 `report_section_prompt`。prompt 教撰写模型：这只是一条**建议，不是合同**——可以用 `###` 子标题按此结构组织本节正文，缺证据的子话题必须如实略过，且不得越出本节自己的范围。它绝不增删改用户确认过的节，也绝不触碰 `reports.outline_json`——报告自己确认的大纲（必答主题、节绑定）不受影响。低于 depth 16，或某节深挖没整理出大纲时，该块缺席，不会向这些报告注入「发现的结构」指令。

**节级进度文案。** 某节深挖期间，它的实时 `section_status` phase 文案会从笼统的「深挖」细化为「深挖中（已整理大纲 N 节）」——一旦大纲便签持有至少一节，在观察到 `outline` 类型 trace 步时立即生效并**强制**落库（不等到下一个检索步；大纲步紧跟在刚推过节流窗的 reflect 步之后，且常常是本节最后一个推理动作，走节流的话这次写会被随后强制写的「撰写」盖掉、用户永远看不到）——不新增表列、不新增 SSE 事件，其余写入仍复用既有 2 秒节流持久化。

### 深度报告可信度与综合

这份报告合同防止把相关性排序得到的技术扫描伪装成完整、独立或全篇综合的结论。冻结的报告 understanding 使用共享意图结果，包含 `result_scope` 和 `completeness_required`。真正的报告侧集合枚举尚未接入前，范围为 `complete`、`aggregate` 或 `hybrid` 的请求必须说明它按相关性检索生成、未做完整枚举。假设仍是可见的范围默认值，但绝不算证据，也不进入检索串。

规划、报告「资料基础」披露和界面共用同一份持久化、有界画像：它由数据库聚合和一页有界代表来源生成，不再把每份来源逐行载入应用内存。画像披露可见/展示来源数量、来源类型/年份分布（含年份未知）、保守重复膨胀下界/身份不确定性，以及按已有类型和年份元数据分层选取的代表资料。完整的 source→family 映射不再复制进意图合同、大纲、轮询响应或模型 prompt；只有覆盖探针、主张或引用实际触达的来源 id 才会一次性有界解析。解析出的行只可合并非空且相同的文件哈希、已接地且相同的论文标题，无法解析或身份不确定的资料保持分开。它不承诺 DOI/arXiv/标题/文件族的完整 canonical 化，也不伪造全库精确资料族数。充分性判断依据相关的可区分资料组、相关性和已确认方向的分布；抽取对象与元素命中数只是诊断量，不是独立权威。画像缺席有两种互不相干的原因，读者必须能区分：本次运行**收窄了来源范围**时有意跳过全库聚合，而聚合本身也可能出错。两者都持久化 `unavailable_reason`（`scope_restricted` / `failed`）而不是一个空画像，报告正文与界面各自给出对应说法，只有真失败才发不含资料内容的 `report_corpus_profile_failed` 运维事件；两种情况报告都继续 fail-open。不可用标记是**非空**对象，所以每个消费方都必须显式判定而不能靠真值判断——把它当画像格式化会把每个计数渲染成 0，并把一份从未测量过的语料摘要送进规划。历史报告存的是空画像，事后无法归类，因此保持原来的失败说法。画像只统计**当前笔记本**，而检索是跨已挂载参考库的，所以披露另行说明正文实际引用了多少份参考库资料：该数由已装配好的引用推导（零新增查询）、按**来源**去重而不是按锚点计数。没有这一句，「基于 N 份资料生成」会被读成证据的全部，即使多数引用来自挂载库。

参考文献仍保留可点击的精确锚点。分组只是展示层：身份未解析的每份资料仍按自己的 source id 独立可见，不会被压成共享的「未知」条目。因此正文披露锚点数和**可见来源组数**；可信度回执另行显示更保守、仅计身份已确认资料的**可区分资料数**，并披露 Top-1 锚点占比上界和重复膨胀率。身份未解析的锚点不增加独立资料数；计算 Top-1 上界时，先将所有这类锚点按最不利情况归入已解析资料中占比最高的一族，再除以全部锚点。「引证覆盖率」明确标为**高风险断言引证覆盖率**：确定性扫描器只检查可观察形式（带单位数字、`O(...)`、显式排序/最高级、绝对比较）在同句或同表格行是否有合法 `[k]`。标题、代码、纯公式块、章节/图编号和已标记 `（推断）`/`【通识】` 的文字不计入；英文按各自句号边界审计。它不能证明引用是否语义蕴含断言。审计与披露始终执行，但证据等级降级另受 `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` 控制，默认关闭，待真机分布校准后再决定默认值；开启时，仅当未引证比例严格大于 `REPORT_HIGH_RISK_UNSUPPORTED_RATIO`，grounded 章节才会被封顶为 `overview`。

对于比较、综述、分类形请求，规划可给出由正交 facet 和条件 axis 组成的有界可选框架。用户在大纲旁查看、编辑它，确认后节级副本即为权威，意图合同内嵌副本只作兼容镜像。**所有**报告深度的章节执行都是「并行检索 → 至多一次全局综合调用 → 并行撰写 → 只审计终审」。唯一的闸是章节数：一节报告不存在跨章节一致性，因此跳过综合并保留逐节 `检索→撰写` 流水线；深度只决定检索预算。这是明确的取舍——低档位就此失去逐节流式（已就绪章节现在要等最慢检索），换取每份多节报告一次全篇综合。综合蓝图分配中心答案、共享定义、带证据键的主张（含条件/反证）和章节 owner/不重复交接。撰写者接收的是主张而非按文献顺序堆放的证据，必须先给结论、区分共识/分歧与条件，并避免逐篇复述或跨论文的不可比排序。主张账本把实际输出的主张文本及锚点绑定到同句/同表格行。这层接地是原子的：主张文本不是正文原文，或引用键非法/不出现在同一句话里，整节账本作废。可选的 `frame_assignments` 分类标签是组织性标注而非证据接地，改为逐条降级：未知 facet 键、不在该 facet 声明取值内的取值，或非对象载荷都只丢掉那一条标签，账本其余部分照常可用。声明取值之外的取值一律丢弃，绝不向最接近的声明取值靠拢。趋势主张的语气资格按所引可区分资料数确定性封顶：一份只能作为研究性方向，两份可标为发展中，三份及以上才有资格使用高置信语气；任一被引锚点缺少可确认的来源身份时，整个趋势只能标作研究性，而不能把未知锚点算成独立资料。正文强于该口径时会进入局限披露。终审读取框架、已校验蓝图、为每节预留份额且只按完整 JSON 主张记录裁剪的合法有界上下文、高风险审计和 exclusive facet 冲突，审计一致性、趋势语气和局限，但绝不改正文或添加事实。界面会在这些信号有信息量时显示综合状态（`available`、`skipped_no_evidence`、`failed_model`、`failed_validation`）和可用章节账本数：单节报告会隐藏预期的纯否定回执（`not_requested` 且账本为 `0/N`），综合还闸在 depth ≥ 8 时生成的历史报告同样隐藏（载荷里没有任何字段能给报告定代）；多节 no-op、任何跳过/失败和任一可用账本仍会显示。模型/校验失败会进入错误日志并回退独立撰写，无证据跳过不伪报为模型错误。

frame、blueprint 或 claims 账本缺失/畸形时会丢弃新增结构，回退到此前报告路径，绝不令报告失败。没有单独的 frame 校验调用；全局综合是唯一新增模型调用，在所有深度下每份多节报告各跑一次。

| 上限 | 数值 |
| --- | ---: |
| Frame facets / 每 facet values / axes | 8 / 12 / 8 |
| Blueprint 共享定义 / 主张 | 24 / 96 |
| 每节 writer ledger 主张 | 24 |
| 综合证据字符数 | 36,000 |
| 终审输入字符数 | 24,000 |
| 资料基础代表来源 / 类型或年份分布桶 | 20 / 32 |
| 单次按需来源身份解析 | ≤ 1,024 个实际触达 source id |
| `REPORT_HIGH_RISK_UNSUPPORTED_RATIO` | 0.25（严格大于才超限） |
| `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` | false（审计披露仍启用） |
| 每报告新增模型调用 | ≤ 1，任意 depth，≥ 2 节 |
| 触发全篇综合的最小章节数 | 2 |

## API

当前 beta 的关键 API：

- `GET /api/notebooks`、`POST /api/notebooks`、`PATCH /api/notebooks/{id}`、`DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `GET /api/notebooks/{id}/analytics/content-overview` —— 面向当前查看者的内容资产：`memory`（`total`、`confirmed`、`candidate`，最多三条最近 `id`/`title`/`status`/`updated_at`）与 `knowhow`（`table_count`、`row_count`、`projection_pending`、`projection_failed`、`stale_code_count`，最多三条最近表摘要）
- `GET /api/notebooks/{id}/checkup` —— 流水线体检（只读，看板高频入口）：聚合来源与索引的损坏/待办信号——空源、缺检索片段、缺检索向量、待分析来源、检索索引过期/损坏——每项含数量、命中样本与建议修复动作，健康时全为 0。看板「来源状态」「索引与构建」两块与头像旁铃铛消费它；健康的库保持中性、不打扰。
- `POST /api/notebooks/{id}/sources/reparse` —— 体检修复：批量重新解析指定来源（空源/缺片段），后台复用既有解析管线，按 notebook 作用域过滤入参
- `POST /api/notebooks/{id}/backfill-vectors` —— 体检修复：后台补齐该库缺失的检索向量（只补缺失、幂等，仅嵌入、不动解析）
- `GET /api/system/config` —— 登录后可读的非敏感浏览器配置；当前返回 `source_upload_max_bytes`（来源选择器使用的部署上限字节值）和 `source_upload_max_files_per_batch`（固定的单次请求文件数护栏）
- `POST /api/notebooks/{id}/sources` —— multipart 文件上传（异步解析/抽取）。每个文件在 multipart 流写入临时 spool 时即受限，超过 `SOURCE_UPLOAD_MAX_MB`（默认 50 MiB）返回 413；每次请求超过 20 个文件也返回 413。浏览器读取上面的两个护栏，取得前禁用文件输入，选择时即时拒绝超限文件，并在发送前复查暂存文件
- `GET /api/sources/{id}`、`DELETE /api/sources/{id}`、`POST /api/sources/{id}/parse`、`GET /api/sources/{id}/elements`、`GET /api/sources/{id}/elements-page?offset=&limit=&anchor_element_id=` —— owner∪成员口径，按来源自己所属的笔记本判权限。分页读取返回 `{items,total_count,offset,limit}`，`limit` 最大 100；anchor 有效时会把 `offset` 调整为包含目标元素的页。
- `GET /api/notebooks/{id}/sources/{source_id}/elements-page?offset=&limit=&anchor_element_id=` —— 采用当前活跃 notebook 参与集授权的有界来源详情读取。浏览器使用该端点；代理全量 element 端点继续保留向后兼容。
- `GET /api/notebooks/{id}/sources/{source_id}`、`GET /api/notebooks/{id}/sources/{source_id}/elements` —— 同样两个读取，但权限按路径里的**当前活跃**笔记本判，目标在它的有效参与集（自身 + 已生效挂载的参考库）内解析。挂载参考库不等于获得该库的直接成员权限，因此浏览器始终只用活跃笔记本过权限、由后端内部代理读取；参与集每次请求实时判定，被挂库降级/易主/深拷贝中或挂载被取消时当场 404。本库来源走的是同一条路径（参与集首项恒为活跃笔记本自身），响应如实返回来源真正所属的笔记本，供前端据此按只读渲染。写入刻意不代理——重新解析与删除仍是 `/api/sources/{id}` 上的 owner-only 操作。详情响应比 `/api/sources/{id}` 更窄：去掉 `file_path` 与原始 `error_message`（两者都可能带服务端绝对路径），改回一个如实的 `parse_failed` 布尔；跨库的隐藏合成源（memory/knowhow 投影行，集合地图刻意把它们算进作用域）直接拒绝
- `GET /api/notebooks/{id}/assets/{asset_id}` —— 图片资产（knowhow 单元格图片、来源插图）适用同一条参与集规则：路径里的笔记本是查看者的活跃笔记本，资产自己声明所属笔记本，不在活跃笔记本有效参与集内的资产一律 404。经挂载库代理来的资产用 `Cache-Control: no-store`，取消挂载即刻生效；活跃笔记本自己的资产保持原有长缓存
- 命令目录：`GET .../sources/{sid}/command-catalog/preview`（零模型调用的成本预告）、`POST .../sources/{sid}/command-catalog`（发起；该来源已有活跃任务、或上一轮还有候选没审阅完时 409）、`GET .../command-catalog/job`、`POST .../command-catalog/cancel`、`GET .../command-catalog/candidates?job_id=&state=&cursor=&limit=`（keyset 分页 + 各档计数）、`POST .../command-catalog/apply` body `{candidate_ids}` 或 `{all_pending}`（创建或追加「命令目录：<来源>」表，绝不覆盖已有行）、`POST .../command-catalog/dismiss` body `{candidate_ids}` 或 `{all_pending}`（把候选标记为已跳过，不写任何表——不冲突的候选唯一的放弃出口——见[命令目录](#命令目录工具手册)）
- `GET /api/notebooks/{id}/knowledge-types`、`GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`、`PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- Knowhow 表：`GET|POST /api/notebooks/{id}/knowhow`、`GET|PATCH|DELETE .../knowhow/{table_id}`、`POST .../knowhow/{table_id}/reproject`——另有导入（`POST .../knowhow/import/preview`、`POST .../knowhow/import`）、列/行/格编辑（`POST .../knowhow/{table_id}/columns`、`PATCH|DELETE .../columns/{column_id}`、`POST .../knowhow/{table_id}/rows`、`DELETE .../rows/{row_id}`、`PATCH .../rows/{row_id}/cells/{column_id}`）、Excel 模板往返（`GET .../knowhow/{table_id}/template`、`POST .../knowhow/{table_id}/append` 配 `mode=preview|commit`）、显式的建议式表达优化（`POST .../rows/{row_id}/cells/{column_id}/optimize`），以及带全库推理取证的单行空列补全建议（`POST .../knowhow/{table_id}/rows/{row_id}/complete`，可选 `target_column_ids`，返回 `retrieval_mode` + `retrieval_scope` + `retrieval_status` + `reasoning_trace` + `evidence` + `suggestions`）
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask/intent` —— `reasoning` 的无语料意图预检；接收 `{question, conversation_id?}`，最多读取当前会话最近五个用户问题，不创建 conversation/job，返回可编辑的问题合同和阻断性歧义；客户端断开时向模型调用传递取消事件
- `POST /api/notebooks/{id}/ask` — 接地问答（逐句 `[k_i]` 引用；`mode`：默认 `chunk` | `graph` | `reasoning`；`reasoning` 可传 `retrieval_effort`，默认 `standard`；官方网页端以带时区的 `asked_at` 传入只用于显示的提交时间；响应以 `answered_at` 返回权威持久化完成时间；集合型回答可带结构化 `result_sets` 与精确覆盖率；联合范围遵循上文各 mode 的边界）
- `POST /api/notebooks/{id}/ask/stream` — Ask 进度的 NDJSON stream（同样接受可选的带时区 `asked_at` 请求字段；先发带持久化 `job_id` 和 `conversation_id` 的 `started` 事件，再发进度/最终事件）；前端用该会话 id 在答案生成前立即入历史并支持重新打开。transport 断开连接只会停止当前客户端继续接收，后台 job 仍继续并可保存回答
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — 供重连/恢复流程读取 detached Ask job 的 `status`、`trace` 与 `answer_id`；job 必须属于路径中的 notebook 和当前用户；状态为 `done` 后，前端重新加载 conversation 取得最终 `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — 用户显式中断端点；job 必须属于路径中的 notebook 和当前用户；设置取消事件并在保存被取消的最终回答前停止 worker
- `GET /api/notebooks/{id}/conversations`、`GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Memory：`GET /api/memories`、`GET /api/notebooks/{id}/memories`、`GET|PATCH /api/memories/{memory_id}`、`POST /api/memories/{memory_id}/confirm|reject|deprecate|promote`、`POST /api/answers/{answer_id}/memory-preview`、`POST /api/notebooks/{id}/memories/from-answer`
- Agent 接入：`GET|POST /api/agent-profiles`、`PATCH /api/agent-profiles/{profile_id}`、`POST /api/agent-profiles/{profile_id}/tokens`、`GET /api/agent-tokens`、`DELETE /api/agent-tokens/{token_id}`；Streamable HTTP MCP 挂载在 `/mcp`
- Knowhow agent 接入面：`GET /api/agent/knowhow/tables?notebook_id=`、`GET /api/agent/knowhow/tables/{table_id}/discrimination`、`GET /api/agent/knowhow/rows/{row_id}`、`GET|PUT|DELETE /api/agent/knowhow/rows/{row_id}/cells/{column_id}/code`——session 或 Agent Bearer token 均可访问；读需要 `knowledge:read`，代码写入需要 `knowhow:code`（见 [Memory 与 Agent MCP](#memory-与-agent-mcp)）
- 统一 KG：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`
- 图谱分析（只读，仅需 notebook 读权限）：`GET /api/notebooks/{id}/kg-analysis`——可选 `boards`/`top_members`/`edges` 上限，返回对象构成、按对象类型分列的收敛率、主题板块列表与跨板块边，每项都标注建于哪次 `kg_mutation_seq`；`GET /api/notebooks/{id}/kg-analysis/sources`——可选 `limit`/`offset`/`order=sparse|connected`，分页返回逐来源画像。两者都只读 `unified-kg/rebuild` 写入的 `kg_community_edges` / `kg_source_profiles` / `kg_analysis_artifacts` 预计算产物表。
- `GET /api/object-schemas`、`POST /api/object-schemas`、`PATCH /api/object-schemas/{type}`、`DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`、`POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- 两层：`POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → 返回更新后的 `NotebookSummary`（tier 非法 400，notebook 不存在 404）。设置 notebook 的联合层（base = 可发布为公共知识库，personal = 默认用户笔记）；`base` notebook 只有在被其它笔记本显式挂载后才参与该笔记本的检索（`GET`/`PUT /api/notebooks/{id}/bases`，候选列表见 `GET /api/notebooks/{id}/mountable`）。
- 参考库挂载：`GET /api/notebooks/{id}/bases` → `MountedBase[]`（本 notebook 的挂载边，含置灰的失效边）；`PUT /api/notebooks/{id}/bases` body `{base_notebook_ids}` → 全量替换，返回更新后的 `MountedBase[]`（含不可挂载的 id 时 400；仅 owner 可写）；`GET /api/notebooks/{id}/mountable` → `NotebookRef[]`（可挂候选：所有公共知识库，加上本 notebook 自己同 owner 的库）。
- 边可信与策展：`GET /api/notebooks/{id}/edge-review-queue`、`POST /api/notebooks/{id}/relations/{rel_id}/review`
- 治理 / 晋升：`POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`、`GET /api/promotion-queue`、`POST /api/promotion-queue/{candidate_id}/approve|reject`
- 深度报告（两阶段）：`POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`；先做不接触语料的问题理解，并始终停在 `status=intent_ready`。`GET .../reports/{rid}` 会返回持久化的 `understanding` 与状态/进度。`POST .../reports/{rid}/intent` body `{resolved_question, answers:[{id,answer}]}` 校验所有必填歧义，并原子认领进入语料规划的唯一转换，返回 `{status:"planning"}`；重复或过期确认返回 409 且不会启动第二个任务。规划完成后停在 `outline_ready`；若原始请求含 `auto_generate=true`，则只在意图确认后自动进入生成。富 `outline` 含每节 `intent_ids`、`intent_questions`、可编辑 `sub_queries`、客观 `coverage`、视角/张力/充分性，详情继续包含 `content_md` 与实时 `section_status`。`PATCH .../reports/{rid}/outline` body `{sections}` 仅在 `outline_ready` 编辑草案；服务端保留 intent catalog，最多接受 `REPORT_MAX_SECTIONS` 节、每节最多四条非空检索方向；没有有效节或某个必答主题失去最后一个章节绑定时返回 422。`POST .../reports/{rid}/generate` body `{depth?}` 可从 `outline_ready` 或仍保有大纲的 `failed` 报告原子启动**阶段2 生成**，其他状态返回 409；重试在认领事务内保留确认意图/大纲并清空旧生成产物。生成章节含后端重算的 `evidence_level`/`grounded`；引用可携带精确 `source_id`/`element_id`。另 `GET /reports`（列表）、`POST .../cancel`、`DELETE`、`POST .../reports/export` `{report_ids}` → `reports.zip`。节级并发采用上文报告专用的数据库保护护栏，不再复用 `KG_JOB_CONCURRENCY`。

当前持久化/API 契约是 `reports` 表与 `/reports` API；已退役的内容工作室存储与路由不属于当前 runtime。

## 管理员用户活动日志（`/dev/logs`）

`/dev/logs` 共享同一条顶部范围条（被查看用户、日期范围），分两个视图 tab：**活动**（默认）与**模型调用**（原有的按天 LLM 调用流水查看器，逐位保留——`kind`/`status`/`model` 过滤、全文搜索、按天下拉、自动刷新均未改动，其 API 仍是 `/api/debug/logs/...`，仍受 `DEBUG_LOGS_ENABLED` 门控）。

「活动」视图三栏：

- **范围** —— 被查看用户自己的笔记本列表（名字 + 来源/提问/报告计数），可展开看该库的来源清单（显示名 + 解析状态徽章）。点笔记本把中栏活动流过滤到该库；点来源在右栏详情打开它（**不**过滤中栏，见下文「未做」）。
- **活动流** —— 提问 / 来源 / 报告三类条目按时间倒序混合成一条流，按 `(created_at DESC, id DESC)` 复合游标翻页（「加载更多」），默认每页 50 条（`limit`，上限 200）。原生日期选择器 + 「全部时间」驱动一个半开区间 `[since, until)`。区间边界带**浏览器 UTC 偏移**（`2026-08-04T00:00:00+08:00`），所以「一天」指的是浏览者本地日历日——与界面上一切时间的渲染时区同一个；两端各自算偏移，夏令时切换日不会差一小时。畸形边界返回 `400` 加可上屏的中文文案（`X-User-Message`），既不静默放宽窗口，也不 500。三类条目在两个数据库后端都经同一个 `parse_activity_instant`（`app/core/activity_time.py`）归一时间：SQLite 侧经 `julianday()` 按绝对时刻比较（历史行是裸 naive 文本、新行带 offset，混合格式统一按裸串=UTC 读），PostgreSQL 侧直接按 `timestamptz` 原生比较，并带与 SQLite 同形的 `COALESCE` 兜底（`ask_jobs.created_at` 在 PG schema 里可空，而 PG 的 `DESC` 默认 `NULLS FIRST`，少了兜底这行会被顶到第 1 页并让 Python 归并直接 `TypeError`）。两侧把不可解析 / NULL 的值折到**同一个哨兵时刻**：它既排到最末、又能经游标往返，`next_cursor` 因此不会发出下一页解析不了的值。三类各自独立 keyset 分页、在 Python 内按时间归并，不做三表 `UNION ALL`——大库上可诚实回报「本页覆盖到 HH:MM 为止」的有界边界，而不是一次全局排序爆炸。
- **详情** —— 选中提问显示完整问题、答案、引用与推理轨迹（复用既有 Ask 面板渲染件，只读）；选中来源显示显示名、解析状态与派生诊断；选中模型调用（仅「模型调用」视图内）复用既有 prompt/response 详情面板，未改动。

三个新增端点（均为只读，权限口径镜像 `debug_logs._resolve_owner`：被查看的 `user_id` 等于当前用户自己的 id，或当前用户是 admin，否则 403）。三者另受独立部署开关 `USER_ACTIVITY_VIEW_ENABLED` 门控（默认 **true**——「活动」是 `/dev/logs` 默认 tab，若沿用默认关闭的 `DEBUG_LOGS_ENABLED`，普通部署一打开页面就 404；两个开关相互独立，`DEBUG_LOGS_ENABLED` 只继续管「模型调用」视图背后的 `/api/debug/logs/...`）。前端没有别的途径拿到这个部署时取值，因此 `GET /system/config` 的 `SystemConfiguration` 响应把它作为 `user_activity_view_enabled` 一并下发；`/dev/logs` 据此在关闭时隐藏「活动」tab、把 `view` 默认落到 `llm`（`?view=activity` 深链同样被归一，不会打开一个三个端点全 404 的视图）。字段缺失（旧后端 + 新前端）按 `true` 处理，与后端自身默认值一致，不藏掉一个其实可用的 tab。左栏复用的 `GET /admin/users/{user_id}/notebooks` 权限口径同样是 self-or-admin 而非仅 admin，普通用户查看自己的活动才能读到自己的笔记本清单：

- `GET /admin/users/{user_id}/activity?notebook_id=&since=&until=&before_ts=&before_id=&limit=` —— 上文的提问/来源/报告混合流。
- `GET /admin/users/{user_id}/notebooks/{notebook_id}/sources?offset=&limit=` —— 该笔记本的来源清单（`limit` 默认 50、上限 200；左栏面板只取第一页，不做进一步翻页）；`notebook_id` 不属于 `user_id` 时 404。
- `GET /admin/users/{user_id}/asks/{job_id}` —— 单条提问的完整详情（问题/答案/轨迹）；`job_id` 不属于 `user_id` 时 404。

三类活动一律按 **owner-only** 归属：只分解被查看用户自己创建的笔记本（`created_by`，排除深拷贝进行中的笔记本），与用户使用总览「展开清单刻意保持 owner-only（即使合计包含共享库提交）」同一口径。隐藏合成来源（`memory`/`knowhow`）既不进来源清单、不进活动流，**也不进范围栏的每库来源计数**——那个计数现在与清单本身用同一条可见性谓词（单一真源），表头与展开清单不可能再对不上。（`GET /admin/users/{user_id}/notebooks` 同时供着这两块屏，所以用户使用总览的每库来源数对「存过 Memory 或建过 Knowhow 表」的用户会降到同一个可见来源数——此前是偏高的。）同一份笔记本清单的报告计数同样收窄到 `created_by = user_id`——与 `questions` 已有的谓词、活动流自己展开的报告条目同一口径：共享笔记本里另一位可写成员建的报告不再计入笔记本 owner 的表头（此前是偏高的，且计数会与展开的活动流悄悄对不上——那条条目从来不会出现在活动流里）。来源活动条目**不**携带原始 `error_message`（可能带服务端绝对路径），只给派生的 `parse_failed` 布尔，加 `extraction_warning`/`parse_quality_warning`/`paper_meta_status`，与 `ScopedSourceDetail` 既有的脱敏规则同形。它的显示名来自 `SourceSummary` 新增的 `display_title` 字段（论文标题优先，与引用卡同一份实现 `source_display.py::source_display_title`）——这是**新增**字段，原有 `title` 字段原样保留，既有的来源页签零回归。`SourceSummary` 另新增原始 `created_at` 时间戳：右栏详情的两个入口（范围栏清单与活动流）都按**浏览器**时区渲染它；既有的 `created_label` 是服务端预先格式化好的日历日，继续用它会让同一份来源按点开的入口不同显示成两个日期。`created_label` 保留给既有的来源页签。报告条目的耗时逐字复用深度报告的既有规则：`generation_started_at → updated_at`；缺开始戳的旧报告不显示耗时，未完成报告只显示创建时间。

以下明确**未做**（分期到后续迭代，不是遗漏）：把模型调用挂到触发它的提问下面（需要新的写侧日志上下文透传，历史日志无法回填）；把检索/解析阶段耗时挂进提问/来源详情；左栏点来源过滤中栏活动流（当前点来源只打开右栏详情）；笔记本来源清单翻到第一页之后的内容。

## 当前限制

- SQLite 检索使用关键词/FTS 兼容的 CJK 处理与有界 float32 矩阵/scale index；PostgreSQL 在相同 repository ports 后使用 `pg_trgm`/`ILIKE` 与 byte-oriented float32 向量。pgvector 仍是未来放量选项，不是运行前置。
- 大文档摄取已加固：贪心窗口化 KG 抽取（成本线性），并发 embedding 逐批落库。极大规模下可再接入 `sqlite-vec`。
- Ask 不再在请求路径里同步补齐 embedding 或全量扫描 source elements；使用已有的关键词/向量索引，在维护任务运行时仍保持响应；并输出每阶段计时（`ask_stage` 事件）。
- 统一 KG rebuild 改为显式且可观测（`GET /notebooks/{id}/unified-kg/status`）；摄取来源只标记图谱为 dirty 而非同步重建，打开图谱浮层不再自动重建（按需刷新）。
- 跨文档概念合并使用确定性别名归一化 + 有界 top-k 向量候选（可扩展到上千概念）；可选 LLM 预审（`POST /notebooks/{id}/unified-kg/merges/review`）对小批量近义词候选做高置信确认/拒绝。
- KG 抽取需要在系统模型 TOML 中绑定 `kg_extract` workload；离线 smoke 在需要验证检索/治理时会显式写入 KG 对象。
- 两层与深度推理尚属早期：图推理 Ask 模式（`mode="graph"`）为 opt-in / 实验性（Ask 面板开关仍驱动默认的 `chunk`/`reasoning` 路径）。把 notebook 标为 `base`/`personal`（经 `POST /notebooks/{id}/tier`）、边可信审核队列、晋升（个人→基准）现都已有专属前端控件（在分析工具栏）；把一个 notebook 发布为公共知识库只是让它可被挂载——tier 感知联合检索与 base 优先冲突规则只对显式把它挂为参考库的笔记本生效。
- Notebook 分享采用链接复制/只读成员方式，不是实时协同编辑；写权限仍归 owner。
- SQLite 与 PostgreSQL 都可由唯一 repository factory 原子选择，发行默认仍是 SQLite。只改 `DATABASE_URL` 不会同步既有行；存量切换/回滚必须停写、验证备份，必要时执行外部数据迁移，并在启动后做一致性检查。PostgreSQL 向量存 `bytea`，不要求 pgvector。
- `off` 模式 PDF 回退用 PyMuPDF4LLM 的分页 Markdown，保留标题、多栏阅读顺序和重建表格；只有该解析器缺失或报错时才最后回退 pypdf。公式、图片和复杂扫描件的权威高保真路径仍是 MinerU。URL/上传文件的云解析在重试后仍失败时也走同一本地回退，并以 `extracted` + `parse_quality_warning=true` 返回；来源详情会说明风险并提供重新解析/删除入口，后续 MinerU 重解析成功会清掉警告。见[用 MinerU 解析 PDF](./operations_zh.md#用-mineru-解析-pdf)。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。
