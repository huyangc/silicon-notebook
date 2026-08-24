# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是面向半导体工程团队的来源可追溯 knowhow 笔记本。它把 PDF、Markdown、DOCX、PPTX、CSV、XLSX、旧版二进制 XLS 材料转成可搜索的来源元素、结构化知识、带引用回答、私有 Memory、knowhow 表和深度报告。

模型证据标记无论写成 `[k1]` 还是本地化的 `【k1】`（含逗号复合组），都会在问答与深度报告中绑定为同一个可点击编号引用。

当前目标是可供真实团队使用的本地 beta：后端采用 FastAPI，并可选择 SQLite 或 PostgreSQL repository；前端采用 Next.js。发行默认的 SQLite 快速启动不要求 Docker、GPU、数据库服务或本地模型服务；选择 PostgreSQL 时需要可访问的 PostgreSQL 服务。OpenAI 兼容的聊天、嵌入、重排和 MinerU 服务都是可选的 URL 集成；未配置时，确定性降级仍可维持核心流程。模块化扩展底座现已建立低依赖的 `domain` 契约层、启动冻结的 Extension SDK registry、实时最小授权 capability 判定，以及 Ask/Report 共用且保护 baseline 的 retrieval contributor host。Selected-source graph 与 generated-question recall 已成为前两个内建 contributor：graph lane 保留原有 attestation、rollout、scope drift、独立预算、baseline manifest 与整段 fail-closed 合同；generated-question 仍固定在 MMR/fusion 前的候选边界，`off` 零 I/O，`shadow` 返回完全相同的 baseline，只有 `on` 能在 host 接纳后追加原 chunk。一个 capability 不可用时只关闭对应 contribution，不能吞掉其他 contributor 的输出。工作流已知某特性未配置、且 host 的冻结拓扑查询答「该 invocation 上没有其他 contribution 存活」时，该 lane 休眠，在构造任何 call/context 之前原样返回 baseline；已注册但此刻不可用的 contributor 仍进入 host。没有适用 contribution 时，host 仍在任何工作前原样返回 baseline。Point-specific proposal port 与 core 权威 reader 分离：内建 proposal 从请求内存复验，其他被接纳 proposal 只走一次带 notebook/source SQL 上限的批量读取。私有 Memory 在 contribution authority 与 generated-question 扫描的 SQL `LIMIT` 前都按 actor 过滤，兼容 no-scope 请求也不例外。非链式 contribution 默认按稳定 ID 排序；ProviderChain 使用声明式 `after`/`before` DAG 和稳定 ID tie-break，禁止整数 priority。生产 ingestion 现已只走启动冻结的 self-hosted MinerU → MinerU cloud → builtin ProviderChain，旧 dispatcher 与 facade patch seam 已退役。整条链的路由在 provider I/O 前冻结；配置 self-hosted 后只能降级到 builtin，URL PDF 在一次调用内只下载一次。插件 probe 不能持久化资产；workbook 对账与 core admission 先完成，只有被接纳的 materializer 才能替换该来源的资产代。Ask reasoning 与 Deep Report 都以不可变 application-stage 合同交接真实流水边界；请求 scope、各阶段独立的新 retrieval run、取消权威、连接探针和 leaf-slot 所有权均显式传递，同时不改变检索算法和任何物理 leaf 位置。 `ReasoningRetriever.run` 的首轮（提示块、规划、初检索、PPR 与精查 seed、补种）由显式 run 状态上的阶段方法承载，拆分前录制的黄金 trace 钉住步序与各通道计数。 逐步推理的 response draft（合成、证据分类、锚点绑定）由可注入的 `ResponseDraftStage` 经冻结 envelope 产出；出厂默认实现就是原内联逻辑，编排器在其前后各复核一次 runtime 权威。 `RepositoryRuntime` 由 11 个模块级纯领域构造函数按序组合（各返回冻结 bundle），构造顺序即依赖拓扑，任何 builder 都够不到 runtime 本身。流式 Ask 的完成后处理只有 `ask.completed_observer` 一个 host。Deep Report 同样只在原子 `generating → done` 提交成功，并且 generation gate、retrieval run、scope 与 model-work 上下文全部释放之后，并为保持既有 active-job 窗口而在这些 hook 完成后才注销取消注册，进入 `report.completed_observer`。observer 不能改写正文、引用、参考文献或终态；内建 observer 保留既有 agent-profile 完成信号。Manifest 中 capability requirement 与插件依赖保持为两个独立概念。模块化架构 PR 按 `docs/` 下的交付计划执行：两路独立 subagent review 与 CI 全绿后才 squash merge。 `RepositoryFacade` 的公开面有逐方法调用者账本（`scripts/audit_facade_callers.py`），退役按账本推进，每日棘轮在出现零调用者席位时报红。

部署插件的 `trust` 是与既有 `builtin` contribution 并列的第三档信任级别：只从 `EXTENSIONS_CONFIG` 点名指向的一份 TOML 文件装载——不扫描目录、不读 Python entry points、不看第二个环境变量，被标为停用的插件条目根本不会被 import。任何发现、capability、settings 或路由挂载校验失败都在启动期 fail-closed——进程直接拒绝起来，绝不会带着半吊子拓扑跑起来；整套装载结果在进程生命周期内冻结，没有热加载，启用、停用或升级插件都必须重启进程。插件自己的 HTTP 路由（即它的 API extensions）只挂在 `/api/extensions/{plugin_id}` 之下，走与其余 API 相同的会话认证，它触达的每个 core 端口都自己对当前请求用户做授权判定。
构建期经 `SILICON_NOTEBOOK_UI_PLUGINS`（`:` 分隔的本地插件包目录列表）把部署方私有 UI 插件包装进前端构建，每次 `dev`/`build`/`start`/`test` 运行前同步进 `frontend/features/ext-*/` 与生成的 `registry.local.ts`；插件包契约见 `docs/development.md`。

Ask 完成后扩展点使用部署配置的协作式 deadline：已开始的同步 callback 会安全完成，过期后不再启动后续 contribution。这些 final 之后的护栏不能改写持久答案、引用、检索输出或 job 终态。

Report 完成后扩展点使用独立的部署 deadline。取消或失去终态认领权的旧 generation 不能伪造完成票据：SQLite/PostgreSQL 都用带状态谓词的同一原子 CAS 发布 `done`，只有提交成功才进入 observer host。

外部 Agent MCP 由一个 API-owned tool host 发布启动期冻结目录：七个固定 capability bundle 的精确 23 个内建工具，实时 token/成员权复核、owner-only 写闸、Memory candidate 审核边界、repository/model I/O 与「恰好一次 progress wrapper」不变量均保持不变。来源面通过 `add_source_file` 接受与浏览器同一注册表里的本地格式，包括 PDF、DOCX、PPTX、表格与 Markdown ZIP；`mcp_server.PUBLIC_TOOLS` 就是这份活目录，仍是工具清单的唯一真源。

已认证的深度报告批量导出现由启动冻结的 single `report.exporter` Provider 执行。默认内建 Markdown provider 只在 repository 连接释放后取得不可变、已完成授权收窄的报告视图；文件名和 `reports.zip` 外壳仍由 core 拥有，畸形批次整体拒绝。浏览器既有单篇 Markdown 下载仍只是对已授权详情的本地呈现，因此不增加请求。未来新增用户可见格式必须在同一个 PR 同步交付 backend provider 与 frontend/API parity。

## 核心能力

- 结构化来源摄取；MinerU 配置后可保留元素级证据、公式、表格和文档内图片；来源详情只加载一页有界元素并按需继续展开，不再一次渲染整篇大文档。公式证据会在来源详情与知识图谱出处卡中排版，LaTeX 渲染失败时仍显示原文，宽块级公式只在所属面板内横向滚动。
- 带紧凑引用的多轮问答，会话历史按最近活动排序（首轮生成中会话即使立即切走也可重新打开）；引用卡可经当前笔记本打开精确原文元素，即使证据来自已挂载的公共参考库；论文标题与上传文件名不同时会另外标明原始文件，绝不显示 MinerU 中间产物名。问题和回答气泡都支持悬停显示、点击固定浏览器本地时间，其中问题采用网页端提交瞬间，回答采用权威持久化完成时间；引用卡与锚点还可能带出一个「本段附图」图片区，与引证正文视觉分隔——模型没有看过这张图，Ask 与深度报告引用共用同一套展示。新导入来源会在索引前清理重复的跨页页眉页脚，包括不在页面首尾、但被解析器明确标记的 header/footer 块；候选检索与 Ask/报告最终证据组装还会防御性折叠同一来源的同文副本，避免版式噪声占满证据席位，不同来源的相同文字仍作为独立出处保留。支持 `chunk`、`reasoning` 和实验性的 `graph` 检索模式。逐步推理会在检索前做不受语料影响的问题理解：意图清晰时自动继续，存在会改变检索方向的歧义时先请用户确认，确认后的合同支配所有检索阶段。一次运行能读哪些来源完全由用户的来源复选框决定——模型不会从问题措辞去推断来源集合。超出首轮宽度的已确认必答主题种子会在步骤预算内顺延执行而非被丢弃，仍未覆盖的方向会在轨迹中披露；跨工具映射类问题会为每个被点名工具生成独立必答主题，目标侧检索配目标工具名与功能描述词。实时推理轨迹覆盖整轮——从问题理解一直到答案生成——而不只是检索阶段。
- 逐步推理与回答合成会先严格解析模型 JSON，只对完整对象形态的语法错误（如缺引号/逗号）做保守修复；截断、未知字段、类型混淆和被改写的字符串仍会失败。Ask NDJSON 流会发送不含业务内容的空闲心跳，让耗时模型调用越过常见反向代理的 idle timeout，同时保持 detached job 原有的断连语义。
- 逐步推理提供 `overview` / `standard` / `deep` / `thorough` / `exhaustive` 五档有界检索力度。明确的整表 Knowhow 清单和物理行/记录计数改走带覆盖率的游标枚举，例如返回 `100/100`；条件筛选、去重/种类计数（如“多少种”）、分组在没有确定性计划时会披露尚不支持精确完整性，安全上限与有界混合分析也只会明确标为部分结果，绝不冒充“全部”。精确阈值见[产品与 API 参考](./docs/product-and-api_zh.md#逐步推理档位与完整集合请求)。
- 逐步推理还能按需列出（而非只做相关性排序）库里的文档目录（标题、类型、已存摘要），以及全库的公式/表格/图片/代码块清单与概念/论断/公式/过程知识对象清单，每份清单都带“已列出/总数”完整性徽章和有界原文引用；进入答案合成的条目各有独立 `[k]` 绑定，可核对最终回答究竟用了清单里的哪一项。截断时明确标注为部分结果，同一轮推理内可继续列出。结果卡保留状态摘要但默认收起，用户需要时再展开条目。细节见[产品与 API 参考](./docs/product-and-api_zh.md#集合枚举工具)。在「穷尽」检索档位下，逐步推理还能在反思过程中逐步完善一份有界大纲，一旦大纲整理出两节或以上仍带着证据的内容，就按这份大纲逐节写作而非一次性通读全部证据——面向综述类、多主题类问题；稳定节 id 会在修订间保住已有证据绑定，绑定只接受模型实际见过的候选 key，八键溢出的纯换键纠错只能占用正常推理步骤上限内的机会（普通额度内的后续更新仍可调整结构）；部分或保守支撑的分节答案最多显示既有的「概览」证据档，不会误报成没有笔记本依据。当服务端手上已有该写成结构化答案的依据时——本轮已完整列出的文档目录，或多条已确认的检索方向——会用一行提示邀请模型把它整理成大纲；提示只陈述事实，并写明「判断一次即可答完就忽略」，是否建大纲仍由模型决定。细节见[产品与 API 参考](./docs/product-and-api_zh.md#大纲便签与按节合成)。
- Concept / Claim / Formula / Procedure 抽取统一受有类型的边契约约束，并提供历史非法边过滤与只读审计、统一图谱可视化、从问答引用精确定位图谱节点（包括核心视图范围外的节点），以及个人知识向公共库提交。可选跨元素关系补全使用按模式和来源代次绑定的持久 keyset 水位与同源索引候选；未完页通过有界任务及启动恢复续跑，模式切换会在同一事务内先发布新模式的可恢复游标再将旧游标标为 stale。该能力仍受灰度闸控制且默认关闭。知识图谱视图里的「重新合并」与「补上关联」两个图谱维护动作都后台执行、按笔记本单飞（共用同一个任务槽）；有任务在跑时再点会被拒绝而不是排队，任务完成后界面自动轮询刷新图谱。手动判重中拒绝待审候选会持久记住当前展示的两个概念组之间不可合并，并直接离队而不重建未变化的图；确认合并会触发重新聚类，而把已经确认的决定反向改成拒绝仍属于需要重建的图变更。重建会在发布待审候选的同一事务里重新校验实时决定，避免并发拒绝被重新生成。「图谱分析」面板会先把预计算统计翻译成报告可信度、合并质量、主题结构和来源复核信号；可编辑成员可在面板内复用同一条后台重新合并链路生成或更新数据，只读成员仍可查看报告。
- 图谱对象类型采用“管理员维护全局基线、笔记本按需覆盖”的模式。笔记本 owner 可改写继承类型的显示名、字段、说明或启停状态，也可创建仅属于当前笔记本的新类型；删除覆盖会恢复继承，只读成员只能查看当前生效定义。模型建议的候选类型在 owner 明确批准前只供审核，不会影响当前生效定义。
- 与笔记本绑定、仅创建者可见的 Memory；Ask 引用会保留并展示来源、位置、摘录和原始文件身份，同时通过受限 MCP 向外部 Agent 提供访问。该接入面涵盖检索、问答、knowhow、引用还原，以及在独立的 owner-only scope 之后的来源归档/重新解析/删除与构建触发；Agent 只能删除 Agent 自己添加的来源，而 knowhow 格子代码写入刻意保持 scope 驱动而非 owner-only，只读成员也可写。新签发 token 旁会显示公开、机器可读的接入说明链接，用户可把链接与 token 分开交给 Agent，由它自行配置 MCP。Agent 面还可读取笔记本已积累的理解（`get_notebook_profile`）并追加带不可信标记的私有使用线索（`add_observation`）——它们只喂入调用者自己的检索心得，可在理解面板的「Agent 记录」小节查看与清空；每位用户还可在账户菜单设置回答偏好（语言/组织方式/详略/常用术语），只影响措辞组织、绝不改变检索范围。该说明逐字给出部署公布的地址、写明直连后端时 `/mcp` 到 `/mcp/` 的重定向，Authorization header 用 `${SILICON_NOTEBOOK_AGENT_TOKEN}` 插值形态：像 Claude Code 这类支持插值的客户端在连接时才解析它，凭据不落配置文件；不支持插值的客户端落盘的仍是原始 header，其配置文件必须按凭据对待。长任务——`reasoning` 档的 `ask_notebook` 要跑几分钟——会在流式（SSE）传输上周期性发送 MCP progress 心跳，客户端的 idle 超时不再中途放弃一次服务端仍在正常完成的调用；客户端自己那条每次调用的上限仍需在客户端侧调高。
- 「AI 对这个库的理解」以低成本、后台巡固的方式维护一份笔记本理解摘要——这个库大致收了什么、反复出现的主题、已知的空缺，以及每位成员私有的、行之有效的问法与问过但没找到答案的方向——可在面板中查看与编辑，并悄悄融入逐步推理与深度报告自己的检索 prompt，但从不进入答案正文本身。另有一份独立的、部署级全局的检索打法库，由所有笔记本、所有用户已完成的 run 离线蒸馏而来，条目本身不携带任何笔记本或用户身份；它只影响一次检索**怎么查**，绝不影响**能读什么**，其可选的 prompt 注入默认关闭，先攒观测数据。注入打开时，deep 及以上档的逐步推理还多一个模型可主动调用的「回想」步——拉取被动块装不下的打法与本人未送达的检索心得——以及某条检索通道连续空手时的一行确定性提示；蒸馏侧现在还会把每步记录的结果 id 与答案真实引用锚点求交，按动作归因成功。见[检索策略经验](./docs/product-and-api_zh.md#检索策略经验)。
- 面向工具手册的 opt-in 命令目录抽取：零模型调用的成本预告；每个来源一个后台任务并挡住重复发起；整篇文档按有界的「段」通读（不丢内容，没有可认领命令名的段不发模型调用）；每批参数一次模型调用；结果逐字与本段原文接地校验，且每一批参数只按它自己那批判——答成别批的丢弃、问了没回来的如实报缺；没有短横参数的命令按位置参数抽取，接地规则不变。未通过校验的条目连同原因一并保留；拦截率异常时任务直接判失败并给出可读理由，而不是交付一份近空目录；确认后的命令落进一张只追加的 knowhow 表——已有同名行只回报冲突、绝不覆盖。发起前要求来源已解析完成；来源被重新解析后这一轮的候选整批作废，不会被确认成文档里已经不存在的内容。详见[产品与 API 参考](./docs/product-and-api_zh.md#命令目录工具手册)。
- 自由列 knowhow 表、Markdown 格子、支持属性按列/按行并提供可操作校验提示的表格导入、有界批量规整审阅、可读审计操作者、内容感知稳定列宽、全库推理检索驱动的显式空列补全建议、格子知识对象默认进入图谱/推理检索的确定性图谱映射、历史/里程碑，以及保存后立即显示归因的隔离代码附件。
- 意图优先的两阶段深度报告：冻结共用的范围/完整性合同，以 SQL 聚合的有界「资料基础」和引证覆盖率透明披露语料边界；身份未知来源逐份可见，集中度按保守上界计算；可选、可确认的比较框架；研究深度还决定大纲阶段证据探针的宽度与充分性判定的模型精修（低档少探、由确定性判据直接给结论、规划更快，大纲期间的探针进度全程可见），每节深挖直接采用大纲里已确认的检索方向作为种子、不再让模型重新规划一遍；所有档位的多节报告都按「并行检索 → 全篇综合蓝图 → 并行分节写作」生成，只有单节报告保持逐节流水线。分节撰写、全篇综合和最终终审分别使用部署可配置的输出预算，整篇准入与单篇节级扇出则独立限制数据库压力，不拿模型容量直接放大连接需求。有信息量的综合失败/跳过与账本可用性对用户可见并安全回退，预期的单节 `not_requested` / `0/N` 回执默认静默；没有任何有效正文的运行落为失败而非假完成，保留已确认大纲的失败报告可在详情页原地重试。终审只接收合法有界的跨节上下文、只审计不改正文。已完成报告保留浏览器本地精确完成时间、相对时间与完整生成阶段耗时；旧报告不会编造缺失时间。
- 多账号所有权、公共参考库、分享链接、复制/只读成员与群组知识共享。群组管理升级为集合层的独立工作台：唯一且可转让的群组 owner 可集中管理成员、审批和本组可见的 Notebook；组管理员可生成、换新或撤销可重复使用的邀请链接，登录用户打开链接后自动成为普通成员。转让后原 owner 保留组管理员身份。系统也支持把已完成的深度报告或问答会话发布成免登录只读链接，以及分页、可排序的管理员使用总览。
- 结构化 JSONL 日志、有界生产诊断、离线批量摄取、检索回放、迁移和回填工具。管理员日志页默认视图是按用户的活动时间线（提问/来源/报告混合、游标翻页，范围/活动流/详情三栏），原按天分文件的模型调用查看器原样保留为第二个视图。
- 检索候选保留语义、词法、PPR、KG 来源和社区等全部生产者来源；chunk/图混合选择可在不扩大回答预算的前提下，为纯图路径证据预留有界席位。
- 前端生产模块位于 `frontend/app` 与 `frontend/features`，测试和测试支撑位于 `frontend/tests` 与 `frontend/test-support`。Ask 与深度报告共用 request-scoped 检索运行时，复用 query embedding、限制报告叶子检索扇出，并记录不含用户内容的分段观测。
- KG 抽取在全局融合前持久化不可变的来源代次事实与规范化证据元素绑定；所选来源 snapshot 及按来源 scale 伴生产物只读取获授权图行，使大库里只选一篇/几篇的成本不随整库图增长。Ask 与深度报告共用质量门控激活层：历史证据 `B` 先完整产出，图证据 `G` 使用独立预算，只有受信 attestation 批准后才追加在 `B` 后。默认运行完全不可见的 `shadow`：不改变答案、公开响应、推理轨迹或 UI，只保留无正文内部事件。scope 漂移、伴生产物不可用、图失败或 baseline eviction 都 fail closed 回 `B`。全范围/全选请求（包括只有一篇来源且选中它）保持字节等价的历史路径。已有部署可离线运行一次可续跑的 `scripts/prepare_selected_source_graph.py`，为全部 notebook 完成迁移、回填、审计并发布当前伴生产物；所有检查成功后，脚本才原子写入部署 env 文件中的四个 shadow 配置。
- 界面默认是**自动模式**——上传即可提问、一键生成深度报告，只有问题真的存在歧义时才需要你补充；从头像菜单开启**高级模式**可解锁上文的检索档位、研究深度和来源/参考库范围等完整配置。详见[产品与 API 参考](./docs/product-and-api_zh.md#界面模式自动高级)。

完整产品行为和端点契约见[产品与 API 参考](./docs/product-and-api_zh.md)。

## 快速开始

### 环境要求

- Python 3.13 或更高版本
- Node.js 20 或更高版本及 npm
- git

只有当 pip 无法使用 `numpy`、`rustworkx`、`hnswlib` 等包的预编译 wheel 时，才需要 C/C++ 工具链。

### 安装

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

( cd frontend && npm install )
```

### 配置

```bash
cp .env.example .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
```

如需模型回答和知识抽取，编辑 `.local/model-services.toml`，把 workload 绑定到物理服务，设置每个服务的 `max_concurrency` 和可选的 chat 专用固定 `top_p`，并只把 `api_key_env` 指定的密钥放进 `.env`。合法的 TOML 修改会自动热加载，无需重启后端。

若要明确使用确定性/离线降级，在 `.env` 中留空：

```text
MODEL_SERVICES_CONFIG=
```

`.env.example` 是非服务配置和密钥槽位的权威清单；`model-services.example.toml` 是服务、绑定和容量模板。远程访问、CORS、模型调度、认证、MinerU 配置和升级说明见[部署与配置](./docs/deployment-and-configuration_zh.md)。

检索与模型输入上限使用具名合同，不在调用点散落数字切片。部署可调预算列在
`.env.example`；深度报告的精确护栏与 API 行为见产品/API 参考。

### 运行

```bash
npm run dev
```

浏览器打开 <http://127.0.0.1:3000>。全新数据库会创建内置 `admin` 账号，本地默认密码为 `admin`；绑定到非回环地址时必须配置非默认的 `SILICON_NOTEBOOK_ADMIN_PASSWORD`。用户可在头像菜单自助修改密码，管理员可在用户使用总览重置用户密码（内置 `admin` 的密码仍由环境变量决定）。

启动会迁移当前选中的数据存储。默认值是 `DATABASE_URL=sqlite:///.local/silicon_notebook.db`；已准备好的 PostgreSQL 16 数据库可改用 `DATABASE_URL=postgresql://user:password@host:5432/database`。

生产模式固定一个后端 worker，使进程内模型调度器成为整个部署的容量边界：

```bash
npm run start
npm run stop
```

`npm run start` 会先安装后端依赖与锁定版本的前端依赖，再在前台完成构建并用与 terminal 脱离的后台进程启动前后端，随后不检查 readiness 而直接退出。日志仍位于 `.local/logs/`；请自行校验 `/api/ready`，停服使用 `npm run stop`；已预装依赖的部署可设 `SKIP_INSTALL=1`。

目标机没有 npm/node 或 root 权限时，先用 `bash scripts/pack.sh` 构建离线包，再按 [packaging/DEPLOY.md](./packaging/DEPLOY.md) 部署。

### 验证

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

验证采用分级门禁：G0 按改动选跑目标测试；G1 `scripts/check.sh` 是编辑期及每次 PR/push 的离线门（稳定后端、契约、前端测试、负责类型检查的 production build，外加补回 Next 构建期类型检查所静默丢弃的测试文件诊断的 `npm run lint`），默认使用 12 个 backend worker、每个前端 runner 4 个测试 worker，Apple Silicon warm 目标不超过 60 秒；G2 `scripts/check_extended.sh` 追加真实索引/性能测试、冷图/索引契约与全仓语义扫描中单测 >2s 的重活半（≤2s 的轻活半已在 G1 跑），每天 18:17 UTC（北京时间次日 02:17）执行一次，也可手动触发；G3 `scripts/check_postgres.sh` 保持为独立 PostgreSQL 集成门。CI 另有 `level-1-frontend-node26`：与 G1 同触发，用同一个前端 wrapper 在 Node.js 当前大版本上重跑一遍，补上「文档承诺 Node ≥ 20、G1 只钉 22」的验证缺口。CI 各 lane 时长仅作观察。

后端普通仓储测试只在 pytest 内按 worker 复用一份当前空 SQLite schema，并使用测试专用的快速密码哈希；迁移/快照契约与认证 helper 测试仍走真实生产路径，每条测试仍拥有独立的可变数据库文件。

仅 Codex 的执行说明：`scripts/check.sh` 包含绑定 loopback 端口和管理子进程的生命周期测试，Codex 第一次运行就必须申请沙箱外执行，不得先在沙箱内试错。GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）同样应直接申请沙箱外执行；普通本地只读 Git 检查仍留在沙箱内。

数据库专项覆盖现在只面向直接 PostgreSQL 后端；已退役的 SQLite 后端实现专项测试、SQLite→PostgreSQL 导入/正向 shadow 测试和跨后端 parity 测试不再属于当前测试套件。

## 产品流程

1. 新建笔记本。系统立即打开 `Untitled notebook`，不会预先要求填写元数据。
2. 导入来源文件。弹窗会压缩显示过长的待上传文件名、保留操作区底部留白，在上传前拒绝超过部署单文件大小上限的文件、执行单次请求 20 个文件的护栏，并在批次超过剩余文档名额时提前禁用上传且说明处理办法；拖放由前端显式接管，入列时被跳过的文件（类型不支持、超大小、超批量）在弹窗内逐条列出原因，不会静默消失。同源生产代理从后端下发的同一个 `SOURCE_UPLOAD_MAX_MB` 和固定批量护栏推导整次请求的传输上限，Next.js 通用 rewrite 的默认 10 MiB 不会再覆盖应用限制。解析过程随后生成结构化来源元素和可搜索内容块。
3. 通过基于内容块的检索立即问答；知识图谱可按需构建，也可为所有上传开启自动抽取。提问在提交时上限 4,000 字（`/ask`、`/ask/stream`、`/ask/intent` 与 MCP 的 `ask_notebook` 一律拒收更长的，前端提问框同值拦住提交、不替用户裁剪）——公开分享的问答会话把每轮问题逐字发给匿名访客，那一页之所以有界，全靠提交这一侧有界。会话重命名同理上限 200 字：公开页把标题也逐字发出去。
4. 浏览和治理抽取知识、查看全屏图谱，并在需要联合检索时挂载公共参考库。
5. 把有价值的回答保存为与笔记本绑定的私有 Memory，维护 knowhow 表，或生成深度报告。
6. 通过链接分享笔记本：小笔记本复制，大笔记本只读加入；组管理员也可以把它共享给整个群组（项目／部门／领域），它会进入每位成员笔记本列表的**「群组」分区**，成员可将其挂为参考库。beta 不提供实时协同编辑。共享库的顶栏标出它的来源（「只读／可管理 · 来自群组《X》」），库名本身始终可见；你自己退出群组、删组或撤销共享之后，如果正开着一本因此读不到的库，界面会当场把你带走；被别人移出或撤销时没有推送通道，会在你切回这个标签页时复核。删除群组只收回访问权，库本身仍属于原作者。
7. 单份深度报告可另行发布为**免登录**的只读页面：链接由 owner 生成、随时可撤销，页面只含正文与「引用出处」（标题/位置/摘录），不含来源 id、不可打开原始资料。正文与站内共用同一套渲染（公式、表格、可点引用编号），编号点开跳到本页的摘录条目。研究问题原样呈现、不截断（创建时即有 4,000 字上限，前端同步显示护栏；只有该护栏上线前建的旧报告可能超限，那时会显式标注「已截断」）；引用的标题/原始文件名/摘录仍有长度上限，但一旦触顶会显式标注「已截断」，不静默丢尾。

引用本地图片文件的 markdown 来源（例如导出笔记里的 `![alt](img.png)`），现在可以直接上传保留这些相对路径的 ZIP。原始压缩包作为一个来源保存并在后台解析，其中每个 `.md`/`.markdown` 都会进入索引，匹配到的 png/jpeg/gif/webp 字节会持久化为来源资产；缺失或不支持的图片不会拖垮整包解析，图注/描述文字仍会保留。拖入文件夹继续使用既有浏览器端配对与 data URI 准备流程；单篇 Markdown 也仍可使用 `python scripts/embed_md_images.py notes.md`。图片行紧跟一个以 `**图片描述**` 起头的引用块时，块内所有引用行都会被当作这张图的描述折进图片元素，与图注一起进入检索——所以没有 alt 图注、只写了描述的图片同样能被搜到。

笔记本内部保持两列布局：左侧是用户导入的来源，主区域依次为**问答**、**知识库**、**记忆**和**深度报告**。

来源导入弹窗从后端读取脱敏的解析能力注册表，用它驱动上传校验与支持格式提示；解析路由流程本身（自托管 MinerU、MinerU 公共云、内置兜底及各自可用状态）刻意不向用户展示。路由始终自动完成：已配置的自托管 MinerU 绝不会被静默替换成公共云，浏览器上传校验与后端使用同一份注册表投影。

每个导入来源都有一个由问答与新建深度报告共用的检索范围复选框。可见来源默认全选；该选择是当前 notebook 的检索硬上限，也是决定检索范围的唯一依据——模型既不提议也不收窄或扩大它。选中当前全集（包括单篇 notebook）继续使用正常图扩展/推理路径，并快照当时已有的隐藏 Memory/Knowhow 参与者；确实排除来源才启用收窄安全规则并排除这些隐藏投影。快照冻结可分区候选和结果；可见来源或隐藏参与者漂移时，无法安全隔离的整图通道会被跳过。收窄且已有索引时，来源谓词在语义 Top-K 之前生效，hydrate 后再复核；旧索引缺少紧凑来源映射时暂时降级为有界来源内词法检索，重建或增量折叠后恢复。已挂载的参考库是同一份检索范围的第二个、互相独立的维度：每个参考库**整库**一个复选框，同样默认全选，于是「限定到自己那一篇」的提问不再被借来的 84 篇论文库淹没。两个维度绝不合并——取消某个参考库的勾选只收窄跨库检索，绝不会关掉当前笔记本自己的图扩展、PPR 或私有 Memory。全部清空来源后本地范围为空；全部取消勾选参考库后库范围为空；两者同时为空时，问答与新建报告会被拒绝。

默认构建的超大来源图伴生产物只通过 source-first 有界读取构建。reader 会校验 partition 内容；只选一篇时直接复用该来源落盘的稀疏图，选择多篇时才执行有界稀疏组合，并且绝不回退整库图。

所选来源图的激活还受离线配对质量门保护：baseline 与 shadow 必须使用完全相同的冻结模型、采样、语料与来源绑定；硬隔离或 baseline 保留失败、任一案例质量回退、成本越界都会生成不批准且不含正文的 attestation。问答与深度报告共用这条激活入口：默认 `shadow` 返回历史 baseline 并且不进入公开 API/轨迹/UI，只有通过 attestation 的 active rollout 才能在 baseline 之后追加来源内图证据。

删除来源时，界面会立即显示“删除中”、阻止重复点击并屏蔽旧列表响应。后端会先锁住 source 再清理投影，在反查索引可用时直接使用索引，把受影响对象的删除 SQL 限制为每条最多 500 个 id，并用一次数据库往返取回、删除引用图片。历史 notebook 的交互式删除不会在请求内重建整本库的反查索引；按 keyset 分页的数据库原生降级仍可能扫描旧 KG 行，因此大库应由运维人员用 `backfill-source-index` 离线预建或修复。该离线重建现按 notebook 持久化游标与计数：每个有界页面原子提交，进程重启从最后已提交页面继续，KG 代次变化时先失败关闭再重新构建。

详细产品行为、检索语义、MCP 工具和端点路径见[产品与 API 参考](./docs/product-and-api_zh.md)。

## 架构概览

```text
浏览器
  → Next.js 前端
  → FastAPI /api 与 Streamable HTTP /mcp
  → 应用服务与 repository ports
  → SQLite 或 PostgreSQL + 本地来源/索引/日志存储

可选外部服务
  → OpenAI 兼容 chat / embedding / rerank
  → MinerU HTTP、隔离 CLI 或云端降级
```

- SQLite 默认位于 `.local/silicon_notebook.db`；PostgreSQL 是可直接选择的替代后端。两者的上传文件和生成工件仍位于 `.local/`。
- 生产后端刻意保持单 worker，因为模型队列、熔断、健康和取消状态都在进程内。
- 默认 `chunk` 检索只读取当前笔记本；图谱增强和推理路径可通过显式挂载的公共库联合检索。
- 来源事实按 `(notebook, source, extraction generation)` 归属，不会因全局对象折叠而消失；重解析/替换会在全局 KG 写事务内一并换代。复制 notebook 时会统一重映射事实、证据绑定与终态账本，并生成副本本地的已完成 KG 代次，使审计和离线修复不依赖原 notebook 的运维运行历史。
- 提问时用**英文半角双引号**括起来的内容整体检索、不做分词：它作为一个不可拆词项进入词法候选，在关键词覆盖率里只算一项（散落着这几个词的文档得不到分），并额外获得一次精确定位探测。引号是强偏好而非硬过滤。只认英文半角双引号、引号内至少 3 个字、一段文本里超过 4 段**不同**的引号内容则整条语法不生效；提问框与深度报告输入框会当场回执识别到的短语，不让没生效的约束静默通过。
- 词法检索保留整句精确匹配作为排序加分，但会独立召回拉丁字母/数字词项、重叠中文三字片段，以及以 `_`/`-`/`.` 连接的完整标识符（如 `set_db`、`config.yaml`）作为整体词项，不再强制整段查询连续出现；SQLite 安全引用 FTS5 clause，PostgreSQL 应用相同的有界词项并集并转义 LIKE 元字符，使 `set_db` 这类词项保持字面量。带索引的 Chunk 与 KG 检索使用有界的 `ANN ∪ FTS` 候选；带索引的 Relation 检索还会按方向平衡补入与 FTS 命中 KG 端点相邻的有界关系候选。
- 大库的索引检索会把 ANN 后的数据库 hydration 限制在候选窗口内，并让并发推理子查询单飞加载 ANN handle。Chunk scale 索引还持久化紧凑的 ANN 行到来源映射，使收窄检索在 HNSW 进入 Top-K 前过滤，而不是让未选来源占候选席位。默认会在 `/api/ready` 放行用户流量前加载全部已发布 scale 索引、已启用的 ANN handle 和可安全复用的单索引 PPR core；跨 notebook 组合图保持按需构造，避免成倍复制千万节点图。
- Scale/viz 工件还会持久化稳定度序和按 source 的边索引，因此有界图视图只读取已选节点的邻接段。多参考库 PPR 会一次构造有序组合 CSR，同时保持原有分数与排序。重型/轻型维护任务使用互相独立的固定 worker 池，排队任务不再一条任务生成一条等待线程。冲突治理分别按对象数和关系数准入，超限时整轮拒绝，不会把截断扫描冒充完整结果。
- 检索索引区分立即构建与低峰排队；用户发起立即构建时会覆盖同 notebook 先前的排队任务但保留后来产生的后续任务，前台轮询结束后仍由完成事件刷新实时状态，因此索引发布后历史问答中的“尚未建立索引”提示会同步消失。
- 候选 Review Queue 已退出当前流程；知识治理直接作用于已存知识对象。
- DATABASE_URL 通过唯一的 repository factory 选择正式 repository 后端。运行时只有一个 active repository 后端，由 `DATABASE_URL` 集中选择。SQLite 和 PostgreSQL 都是可直接启动的后端；发行默认值仍是 SQLite。

### SQLite / PostgreSQL 切换

Shadow SQLite source open 的分类边界刻意收窄：只有 `open_fresh_live_sqlite` 抛出的非瞬态 `sqlite3.OperationalError` 才归为 source-binding identity 失败。locked、busy、interrupted open 仍按瞬态整批重试；后续 SQLite operational error 保持原有 schema/query 分类。

应用的正常 repository 路径不会双写。`SHADOW_DATABASE_URL` 只标识显式正向影子迁移
CLI 使用的 PostgreSQL 目标；单独设置它不会启动任何任务，也不会改变 active backend。只改 `DATABASE_URL` 不会复制、迁移或同步既有数据。

在 `DATABASE_URL` 仍指向 SQLite 时，运维人员可以运行受保护的单向
SQLite→PostgreSQL 影子同步：preflight 绑定并确认两端数据库身份，`start-forward` 安装
run-scoped capture/guard 并复制一致的 70 表 baseline，随后由一个受监督的前台 worker
持续应用 SQLite change log。`status` 提供脱敏的 lag/lease/poison 状态，
`verify --level full` 执行 barrier-aware 一致性校验。worker 使用数据库时钟的排他 lease，
对 PostgreSQL 瞬态失败重试，确定性 poison 会 fail-stop；清理策略至少保留已验证进度之后
的 7 天和 100,000 条事件。

本阶段**不包含** cutover、反向复制或自动修改 `DATABASE_URL`。必须保持 SQLite 为 active，
持续维护两端备份，并在另行评审的切换阶段之前把 PostgreSQL 视为禁止业务读取的影子库。
完整命令顺序与故障规则见[运维文档](./docs/operations_zh.md)。

独立的、默认 dry-run 的 `scripts/migrate_sqlite_to_postgres.py` 继续作为受控的停机快照
importer 与本地激活工具；它不是持续复制。SQLite-active 正向 shadow 只使用
`scripts/shadow_sqlite_to_postgres.py`，且两种流程绝不能指向同一个 target。

Baseline snapshot/COPY 还要求 owner-only 的真实 snapshot 目录；所有业务 SQL 全限定到 run 绑定 schema，在关键绑定处以短写栅栏复核 live SQLite capture 仍启用，采用有界 named server cursor/statement timeout，并在起始和最终验证由正式 migration 派生的完整 v30 表/列/约束/operational+GIN-index/extension catalog。Snapshot/fence 必须用指向当前 SQLite 路径的 fresh 专用连接，不得复用 repository 的线程缓存连接；open 前后以及发布/PG commit 前都要复核 resolved path 与 device/inode。最终 SQLite 栅栏只在 PG 长 proof/ANALYZE 完成后取得，并保持到 PG H0 事务提交成功。

- 在发行默认的 SQLite 后端上，搜索使用 SQLite FTS/向量存储；PostgreSQL 后端改用 `pg_trgm`/`ILIKE`。float32 向量仍存为 `bytea`，不安装也不需要 pgvector。
- PostgreSQL 要求 `pg_trgm` 必须安装在 `public` schema。可用不回显凭据的查询检查：

  ```sql
  SELECT e.extname, n.nspname
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_trgm';
  ```

  `pg_trgm | public` 表示前置条件已就绪。若查询无行，首次 migration 会自动尝试 `CREATE EXTENSION pg_trgm`；既有 `pg_trgm` 位于其他 schema 时会 fail closed。
- 大型 PostgreSQL 部署应先用 `PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py` 检查 notebook-aware 词法 GIN 索引，再给同一命令追加 `--apply` 在线安装。工具使用 `CREATE INDEX CONCURRENTLY`、可续跑，默认保留旧的全局索引；它只改候选裁剪，不改词法谓词、分数、limit 或结果顺序。详见[运维文档](./docs/operations_zh.md#postgresql-notebook-aware-词法索引)。针对超大单库，KG 名词法臂的 GiST `<->` 早停探针在索引就位后自动生效（`POSTGRES_LEXICAL_KNN_ENABLED` 默认开，设 false 回滚；索引 DDL 与回滚步骤见同一节）。
- importer 要求目标 PostgreSQL 是空的且使用 UTF-8；目标 URL 只从 `POSTGRES_MIGRATION_URL` 读取，不放在 CLI 参数中。它用 SQLite backup API 获取包含已提交 WAL 的在线一致快照，只在工作副本上升级到配对 schema，按有界 batch 流式 `COPY`，保留 ordinal，把旧 JSON 向量转换成 float32 `bytea`，逐表做内容 checksum，并逐表提交 + 记录 checkpoint，中断（崩溃、远程连接断开、重启）后从最后完成的表续跑而非整体重来；finalize（ordinal reseed、重建索引、`ANALYZE`）是幂等的。SQLite-only 的 shadow control/change-log 表会被明确排除并记录在 receipt 中。可为大目标传入会话级批量装载调优（`--maintenance-work-mem`、`--max-parallel-index-workers`）。默认 preview/apply 不会修改 `DATABASE_URL`，也不会复制 `.local/storage`。
- 在线迁移只能算演练快照：快照之后继续写入 SQLite 的数据不会被同步。对已经停服的本地部署，显式 `--activate-env ... --confirm-service-stopped` 会重新生成 SQLite 一致快照，并按无凭据 receipt 重算 PostgreSQL 全表 checksum；全部一致后才原子替换 `.env`，把旧 SQLite URL 保存在惰性的 `SHADOW_DATABASE_URL`，并创建权限受限的回退副本。CLI 不会自行停止或重启服务。随后以 `--workers 1` 启动，并在放流量前检查 `/api/ready`、登录、数量、搜索、代表性读取和一次 canary 写入。
- 切回 SQLite 不会回放 PostgreSQL-only 写入。无损回滚要求切换后尚无新写入，或已经完成并验证双向外部对账迁移。
- `scripts/batch_ingest.py` 的 `ingest`、`kg`、`index`、`all`、`embed`、`metadata`、`question-index`、`reparse`、`backfill-source-index`、`backfill-chunk-elements` 和显式离线的 `backfill-source-facts` 同时支持 SQLite 与 PostgreSQL。`question-index` 是显式 opt-in：为原 chunk 生成并独立向量化搜索问题，要求开启 rollout mode 并绑定 `chunk_question_generation` / `chunk_embedding`，生成文本绝不成为引用证据。`backfill-chunk-elements` 建立元素→chunk 反查表（`chunk_elements`），让每次问答的证据反查从「整库 chunk 扫描」变成有界点查；新写入在同一事务内维护它，历史行只由这个显式离线阶段投影，未回填的 notebook 逐字保持旧路径、结果不变。来源事实命令无模型调用、可恢复地投影历史来源事实，显式区分在线事实与历史投影，并在运维失败重试后保留真实不完整原因；无法证明来源归属的旧数据保持可见的 incomplete，`scripts/audit_source_facts.py` 会独立对账 KG 代次/版本/数量，同时只报告计数与有界 source id。PostgreSQL 直连维护只允许离线执行：先停止 API/后台 writer，再显式传 `--confirm-service-stopped`；该参数只是运维确认，不会替你停服务。数据库级 advisory lock 会阻止两个 PostgreSQL 维护 CLI 重叠。`vectors-to-blob` 仍仅适用于 SQLite，因为 PostgreSQL 向量已经是 `bytea`。所有 `kg` 抽取形态（包括 `--limit`、`--retry-partial`）都复用页面“分析”的持久 notebook job；批处理的末尾聚类/索引完成后 job 才成功。页面“继续分析”和 CLI `kg --retry-partial` 都会安全修复 partial，旧图保留到“零失败窗口且非空”的新图成功提交。 离线长跑可加 `kg --skip-model-failures`：模型不可用时只跳过当前来源（退回未分析，重跑自动重试）而不熔断整个任务，连续 `--max-consecutive-model-failures` 个来源失败仍会停止（省略则按 `max(32, 2 × workers)` 派生；显式给出不高于 `--workers` 的取值直接报错）；起始可用性探测刻意不在覆盖范围内——服务开跑时就是死的，直接快速失败；探测复用已配置的短输出预算，给推理型模型留下输出可见 JSON 的空间，空白/无效回复明确显示为模型响应失败而不是人工中断；默认关闭，开启时打印告警并在结束时报出被跳过的来源数。

KG 探测与正式抽取把任务中断信号交给共享的流式 JSON 传输，长回复只要持续收到 chunk 就保持连接，任务熔断也能及时停掉兄弟流。流式请求会向兼容 provider 请求最终 usage trailer，并把 prompt/completion/total token 精确写回既有按用户 LLM 日志；明确拒绝该可选参数的 provider 会回退普通流式传输，且不会对后续每次调用重复探测。

preview/apply/retry 的完整命令、SQLite↔PostgreSQL selector 写法、正式切换清单、storage 处理和回滚限制见[运维文档](./docs/operations_zh.md#sqlite--postgresql-切换与回滚)；按步骤执行的清单见[迁移 runbook](./docs/postgres-migration-runbook.md)；部署配置见[部署与配置](./docs/deployment-and-configuration_zh.md)。

运行时边界见 [architecture.md](./architecture.md)，贡献者约束见[开发与仓库契约](./docs/development_zh.md)。

前端 workspace 已按状态所有权拆分且不改变产品行为：`use-notebook-collection.ts` 独占 actor-scoped 集合清单、有界搜索、筛选/排序/视图/菜单、清单发布水位、访问权对账、编辑/删除态、创建 single-flight 与删除 tombstone；`use-source-library.ts` 独占来源列表/范围、详情、删除 tombstone、重解析与解析轮询；`use-ask-session.ts` 独占 Ask 草稿、对话、意图确认、持久流、重连和会话历史；`use-report-workspace.ts` 独占报告列表/详情、轮询、意图/大纲提交、分享、导出选择和报告删除 tombstone；`use-kg-workspace.ts` 组合 `use-kg-knowledge.ts`/`use-kg-schema.ts`/`use-kg-graph.ts`，共享 `use-kg-owner.ts` 里唯一的权威门；三者合起来独占 Knowledge 浏览、Schema 状态、统一图读取、合并审阅及持久 KG 构建/维护追踪。`use-root-modal-coordinator.ts` 另行独占根层弹窗的 typed presentation lease、actor/workspace/source scope、层级策略与焦点归还；它不拥有领域 payload、API、busy 或 timer。打开笔记本是 `notebook-transition.ts` 里一条带回滚的单一 transition，`page.tsx` 的 step 列表是各 owner 声明 begin/commit/settle 的唯一登记点。各 hook 都使用显式 user/notebook/workspace owner；`page.tsx` 继续作为壳层编排器并保持既有集合 composite bundle、惰性读取、请求次数和轮询节奏。Report 与 Knowledge/KG 内容仍按需读取，打开 notebook 时只保留既有维护状态恢复探针。Ask 显式停止只保留一条权威取消请求，直到服务端明确响应；浏览器不会按猜测的计时器提前释放重试权。

Workspace UI 扩展只允许随构建静态进入。首个真实 contribution 是迁入 `workspace.side_panel` 的既有 Agent Profile 入口；它渲染成来源栏固定区（来源列表之上）的一行入口，不占工作区独立一列，点击前不读取理解数据，点击后只委托既有 owner/根层弹窗。已登录且 workspace 成功提交后，每个 actor 共享一次 `/system/extensions` 元数据读取；同用户切库复用，读取失败不缓存、下一次 workspace 提交时重试（不用 timer），未登录与集合页为零请求。canonical slot 仍只有 `workspace.side_panel` 与 `source.detail_section`；精确本地 contribution、实时 availability、核心权限、归一化 UI mode 及当前 actor/notebook/workspace generation 必须同时成立才渲染。端点只返回脱敏元数据与固定 unavailable reason；插件只拿只读摘要与窄 host action，不接 workspace setter 或领域 owner 内部状态。

来源详情也进入同一 frozen primary-dialog 水位。兼容的来源目录审阅或 info 层位于顶层时，被覆盖的详情及任何其它被覆盖 dialog 都进入 inert，并从无障碍树隐藏；只有 React 提交并移除这层 inert 后才归还焦点。

贡献者安全约束：凡任务会写入仓库代码、测试、文档或配置，都必须先新建隔离的 linked git worktree 和分支；该任务期间主 checkout 只读。纯调研、状态汇报和只读审查除外。所有改动都经 PR 合入，PR 必须经 codex 评审且每一轮原始输出逐字贴回 PR；只有在评审非阻塞、CI 全绿、且 PR 上能看到针对 **PR 远端 head 提交**的评审这三条同时成立时才可合入——评审静默没触发，和它跑完判了通过，在外部看起来一模一样，所以本地状态不算证据。后端的 notebook 授权谓词只有每后端一个唯一定义点（`repositories/*/access_sql.py`），API 写端点一律按能力守卫归类；完整契约见[开发与仓库契约](./docs/development_zh.md)。

## 文档导航

| 需求 | 文档 |
| --- | --- |
| 产品行为、检索模式、Memory/MCP、knowhow、API、当前限制 | [产品与 API 参考](./docs/product-and-api_zh.md) |
| 外部 Agent 界面配置、Codex/Claude CLI 与可运行 MCP/Memory 示例 | [Agent MCP 与 Memory 接入 SOP](./docs/agent-mcp-memory-sop_zh.md) |
| 开发、接入与运维一个仓库外部署插件（后端 bundle、构建期 UI 包、安装、拒绝码表） | [部署插件 SOP](./docs/deployment-extensions-sop_zh.md) |
| 安装、源码/生产部署、模型服务、配置项 | [部署与配置](./docs/deployment-and-configuration_zh.md) |
| 日志、事故采集、MinerU、批量摄取、回放、迁移、回填 | [运维、诊断与摄取工具](./docs/operations_zh.md) |
| 验证、CI、开发流程、测试和文档契约 | [开发与仓库契约](./docs/development_zh.md) |
| 详细运行时架构 | [architecture.md](./architecture.md) |
| 按脚本查找命令 | [scripts/README.md](./scripts/README.md) |
| 离线部署包目标机说明 | [packaging/DEPLOY.md](./packaging/DEPLOY.md) |
| KG schema | [schema/README.md](./schema/README.md) |
| 产品规格完成状态 | [fangan_done.md](./fangan_done.md) |

每份拆出的专题文档顶部都提供中英文跳转。

## 当前边界

- SQLite 是发行默认值；PostgreSQL 16 已是可直接选择的后端。仓库提供经过校验的单向 SQLite→PostgreSQL 快照 importer；它不提供实时同步、PostgreSQL→SQLite 回放或 MySQL 迁移。
- Docker 不是一期默认工作流，也不是运行前提。
- 公式、图片和复杂扫描 PDF 的最高保真解析需要 MinerU；`MINERU_MODE=off` 使用本地 PyMuPDF4LLM 版面/Markdown 降级解析（pypdf 仅作最后兜底）。远端 MinerU HTTP 调用默认对瞬态失败额外重试 2 次（共最多 3 次；`MINERU_MAX_RETRIES`）。URL 云解析最终失败时，后端会下载 PDF 并用本地解析完成；来源界面会提示可能的质量损失，并提供重新解析/删除操作。MinerU 覆盖 PDF、DOCX、PPTX 与 XLSX/XLSM，每种都各自沿一条分级本地链回落——PDF 先 PyMuPDF4LLM 后 pypdf，DOCX 先 mammoth（保住标题层级、列表与表格结构）后 python-docx，PPTX 先 python-pptx（含幻灯片表格、图表标题与演讲者备注）后原始幻灯片 XML，工作簿走 openpyxl；质量警告只覆盖**有损**的那几种兜底（PDF、DOCX、PPTX），工作簿不在其中——openpyxl 兜底对单元格值全保真。工作簿的 MinerU 产出还要先过一次本地**行 + 格**覆盖对账才被采信（两个维度都要达标——按页渲染常常保住每一行却丢掉页宽之外的列），悄悄丢行或丢列时改用 openpyxl；云端上传走同一道对账，内嵌图片也只在产出被采信后才持久化。MinerU 解析不了的格式（Markdown、CSV、纯文本）一律本地解析，绝不上传 mineru.net 云端。旧版二进制 `.xls`（前 OOXML 时代的 BIFF 格式）没有任何 MinerU 分支，一律走本地 `xlrd`（该格式唯一的纯 Python 读取器）；其余旧版二进制 Office 格式（`.doc`、`.ppt`）仍不受支持，请另存为 `.docx`/`.pptx`。
- 知识抽取和模型回答需要绑定对应 workload；离线模式不会合成知识。
- 图谱问答仍为 opt-in/实验能力，默认模式是 `chunk`。
- 生成问题召回由部署者显式开启且默认关闭；`shadow` 只记录不含正文的对比计数、不改变结果，只有明确的 `on` 才可在 chunk 低召回时补入原 chunk。
- 答案、报告与 Memory 的 Markdown 里，单个 `~` 是字面量（`7~5nm`、`~3GHz` 这类区间与约等于写法照常显示），删除线必须写成 GFM 规范的 `~~文本~~`。
- Memory 只能由用户主动选择保存，并且仅创建者可见。
- 分享是复制、只读成员或群组共享，不是实时协同编辑。群组内成员可提问、写自己的深度报告；组管理员管理共享库的内容（来源、构建、授权边），而挂载配置、链接分享与删库仍归 owner，普通成员可申请把自己的库贡献给群组、由组管理员审批。
- Web/网络来源搜索仍是禁用的未来入口。

## 文档维护

根 README 只保留项目入口信息。详细行为写入上表对应的权威文档，中英文版本保持一致。安装、产品行为、架构或开发约束变化时，仍需同步更新 `README.md`、`README_zh.md`、`AGENTS.md`、`CLAUDE.md`，并更新对应的专题文档。
