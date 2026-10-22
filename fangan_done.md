# silicon-notebook 方案已完成情况

更新日期：2026-10-21（新增 §62 MCP 读取本人的问答记录与深度报告：四个只读工具并入读取档、发起方读时推导；此前 2026-10-19 新增 §61 剩余待办收尾：reasoning 原文段引用卡复用装配期读取；全局问答参与笔记本被删后对所有者留存与全局 run 事件标记；全局问答 👍/👎 进提问分析与知识分析看板；§60 MCP 提问的全部笔记本写法与限时回答链接、模型 JSON 按严格 schema 对齐；此前 2026-10-15 新增 §59 技术债整改程序：全局回答读法、取样通道批量读、Knowhow 面板怪癖、门面退役；此前 2026-10-08 新增 §55 知识图谱里的 Memory 只属于本人；§56 会话公开分享先告知个人记忆，公开页每次打开复核用到的库；§57 晋升对象由公共知识库自己承载出处；§58 挂载只对能读被挂库的人生效；此前 2026-10-07 新增 §54 检索各腿按本次运行的来源天花板取数；此前 2026-09-30 新增 §52 笔记本拷贝不带 Memory；§53 成员退出与 Memory 的彻底删除；此前 2026-09-29 新增 §47 当前库保底席位与原文段下限；§48 全局问答引用核对：部分失败照常交付；§49 KG 对象定义的来源归因与 KG 详情按查看者过滤；§50 全局问答集合枚举引用按冻结参与集；§51 集合枚举与按篇读取原文认本次勾选的来源）

对照依据：[产品方案](silicon_notebook_fangan.md)。章节号指向原方案；“扩展”表示交付时延伸能力，不冒称原方案已有独立条款。

## 总体状态

本页只登记完成状态、方案对应关系与交付依据。实现细节、端点、数值限制、配置和操作步骤分别由[产品/API][product]、[架构][arch]、[部署][deploy]、[运维][ops]和[开发规范][dev]维护；本页不再复制这些合同。方案中的早期对象与界面设计不等于当前产品承诺。

当前已交付的主链路是：真实资料导入 → 元素与摘要 → KG 抽取和检索 → 有引用的问答、知识治理与深度报告；另有私有 Memory、Agent MCP、Knowhow 表、群组共享与全局问答。内置问答引擎为 `chunk` / `reasoning`；旧 `graph` 等模式只是兼容别名，见[引擎注册表](backend/app/services/ask_modes.py)。

**记录口径：**“已交付”沿用原账本的实施结论，不代表所有部署已启用、质量实验已放量或已完成生产验收。旧测试计数、性能测量及评审过程通过各节“交付记录”链接保留在固定提交 `403b796f3c1620f93a4c35e92f035e68432290d4`，只证明当时状态。本次精简不新增完成项；无独立验证记录的条目不补造通过结论。历史设计仅用于追溯，当前行为以所属权威文档与代码为准。

未完成工作统一见[待办账本](fangan_todo.md)与[当前架构债务](architecture.md#6-已知架构债务与整改顺序)。原编号和二级标题保留，便于既有链接继续定位。

## 1. 产品与项目基础

**已交付；方案 §1、§10。** 项目名、仓库、开发入口及中英说明已建立。当前安装方式见[部署][deploy]，开发约定见[开发规范][dev]；早期远端地址、特定开发机路径和本机安装状态仅保留在交付记录中。

[历史交付记录][h1]。

## 2. 技术架构基础

**已交付；方案 §10。** FastAPI + Next.js 主线、SQLite/PostgreSQL repository 组合边界、存储与模型服务接缝已落地。当前开发/生产默认部署 PostgreSQL，SQLite 为可选后端，见[部署：选择数据库](docs/deployment-and-configuration_zh.md#31--选择-sqlite-或-postgresql)。

Repository composition（2026-07-11～12）及 application boundary（2026-07-21）是已交付的内部重构；当前组件职责与剩余生命周期债务见[架构][arch]。不再把当时的 schema 版本、门禁秒数或阶段 5 未完成状态当作现状。

[历史交付记录][h2]。

## 3. 用户系统与分享

**已交付；方案 §3、§13、v1.0。** 多账号隔离、会话、管理员授权、密码管理及笔记本链接分享已落地；群组交付记录另见第 31 节。权限和分享方式见[产品/API][product]。

**统一认证代码已交付，生产切换未验收（2026-10-10 简化）。** 外部 `auth.provider` 与 w3-auth 示例已实现：统一认证返回的工号与本站用户名精确一致（区分大小写）即直接登录，否则由用户选择凭密码关联老账号或新建账号；插件开关即开启，停用插件即回退到本地登录，不再有阶段、迁移或退役状态机。不把插件交付等同于真实 IDaaS 联调或生产切换完成。边界见[外部认证](docs/product-and-api_zh.md#外部认证)、[认证部署](docs/deployment-and-configuration_zh.md#外部认证部署)和[启用与回退](docs/operations_zh.md#统一认证的启用与回退)。回归入口：[认证宿主](backend/tests/test_auth_provider_host.py)、[认证存储契约](backend/tests/auth_store_contract.py)。

[历史交付记录][h3]。

## 4. Notebook 创建与管理

**已交付；方案 §6.1、§13。** Notebook CRUD、持久化、真实统计、集合浏览及排序已落地。标题/描述现在分别记录自动与手动归属，自动刷新不会覆盖用户已编辑的字段；早期“永不自动改名/描述”的记录已被后续实现取代，详见[标题与描述](docs/product-and-api_zh.md#笔记本标题与描述)。回归入口：[元数据存储](backend/tests/test_notebook_metadata_storage.py)、[增量复用](backend/tests/test_notebook_metadata_cache.py)。

[历史交付记录][h4]。

## 5. Notebook 工作区界面

**已交付；方案 §6.5、§6.5.1、§9、§11。** 工作区为来源栏与主区两列，主区包含问答、知识库、记忆、深度报告。下列交付各自只保留索引：

| 交付项 | 状态/日期 | 当前说明 |
| --- | --- | --- |
| 来源及参考库选择、引用精确定位、大型来源详情分页 | 已交付，2026-08-03～04 | [来源范围](docs/product-and-api_zh.md#按来源选择检索范围) |
| 所选来源子图、可续跑反查索引及 Shadow 部署准备 | 已交付准备与质量门控，2026-08-04～05；不代表已放量 | [检索合同][retrieval]、[运维][ops] |
| Excel 专业分析与解析问题只读中心 | 已交付，2026-08-31 | [Excel 分析](docs/product-and-api_zh.md#excel-专业分析与解析问题自动归档) |
| 来源状态、结构化渲染、知识治理与图谱分析面板 | 已交付，图谱面板更新于 2026-08-11 | [产品流程][flow] |
| 会话管理、问题理解、进度/中断、引用本地化及反馈 | 已交付，详见第 12、14 节 | [产品流程][flow]、[检索合同][retrieval] |
| 报告意图确认、大纲审阅、生成/取消/重试与导出 | 已交付，准确性闭环 2026-07-25、容量与重试 2026-08-04 | [深度报告][reports]；后续综合与分享见第 31 节 |

当前入口以产品流程为准；Scenario / Case / Checklist 与固定 Studio 右栏均属已退役历史。

[历史交付记录][h5]。

## 6. Source 上传与管理（异步闭环）

**已交付；方案 §5.1、§6.3。** 文件/受约束 URL 导入、异步处理、状态展示、重新解析和删除已形成闭环。重新解析保留来源及原文件并重建派生数据；删除另移除来源及文件。格式列表、状态机与清理边界见[产品/API][product]，批量摄取与恢复见[运维][ops]。Markdown ZIP 与 MCP 文件导入的后续交付见第 31 节。

[历史交付记录][h6]。

## 7. 文档解析与元素级 Evidence

**已交付；方案 §5.1～§5.2、§6.3。** 来源元素携带位置与证据锚点；解析能力注册表统一上传准入、解析分派与界面投影。MinerU 和本机后备解析器的格式、降级披露与图片边界见[产品/API][product]及[解析运维](docs/operations_zh.md#用-mineru-解析-pdf)。

元素级 PPTX/备注、结构化 Markdown、PDF 版面回退、空 PDF 失败提示与工作簿行/格覆盖对账均已交付；旧本机 MLX 安装记录不作为部署要求。回归入口：[Office](backend/tests/test_parsers_office.py)、[PDF](backend/tests/test_parsers_pdf_python.py)、[Markdown](backend/tests/test_parsers_markdown.py)。

[历史交付记录][h7]。

## 8. Source Summary

**已交付；方案 §6.3、v0.1。** 来源解析后生成并持久化摘要，模型未配置时使用确定性回退。当前摘要与笔记本信息刷新的关系见[标题与描述](docs/product-and-api_zh.md#笔记本标题与描述)，模型配置见[部署][deploy]。

[历史交付记录][h8]。

## 9. 检索：关键词 + 向量混合

**已交付；方案 §6.5、§11、§19.2。** Notebook 搜索、关键词/向量混合召回、按本次参与集的联邦 chunk 检索与 Knowhow 对象召回已落地。可选生成问题索引于 2026-08-10 交付，启用与 Shadow 仍受部署配置约束。

相关性、来源授权、跨库合并、知识对象/关系的排序区别及大库降级只在[检索合同][retrieval]维护；PostgreSQL 词法路径另见[运维][ops]。全局对等检索的后续交付见第 35～44 节，不沿用早期“chunk 只搜当前库”或“仅 SQLite 文本匹配”的历史描述。

[历史交付记录][h9]。

## 10. 自动抽取 Pipeline

**已交付；方案 §6.4、§9.1。** KG 抽取以 Concept / Claim / Formula / Procedure 为主产物并绑定证据；无模型时记录 `no-llm`，不伪造候选。持久化 KG 构建作业、模型故障隔离、恢复与部分失败重试已落地；2026-08-24 又统一探活/抽取流式传输及安全失败分类。

当前触发和故障语义见[KG 抽取](docs/product-and-api_zh.md#kg-抽取触发)，恢复流程见[运维][ops]。旧候选表/API 只是兼容面，不代表自动抽取仍产出早期六类候选。

[历史交付记录][h10]。

## 11. Curator 审核、正式知识表与知识治理（方案 v0.2）

**已交付；方案 §6.7、§12、v0.2。** 通用知识类型浏览、状态/owner 编辑、证据、同类查重合并、冲突检测、概念与关系治理已落地；不可用知识不得经邻居扩展重新进入回答。Legacy 候选审核保留兼容。

2026-08-20 补齐手动概念判重队列不回流、重复决定收束与重建边界。当前治理合同见[产品/API][product]，治理及索引所有权见[架构][arch]；第 31 节还保留大库队列优化的交付索引。

[历史交付记录][h11]。

## 12. Ask（当前）与 Scenario / Case / Checklist（历史记录，已退役）

**当前 Ask 已交付；方案 §5.9、§6.5、§8、§11。** 以下里程碑仍服务当前问答/报告链路：

| 交付项 | 交付日期/依据 | 当前说明 |
| --- | --- | --- |
| 真实语料作答、citation 校验、持久化任务与实时推理轨迹 | 2026-06-06 起 | [产品流程][flow]、[架构][arch] |
| `follow_chain` 有界查询期推理 | 2026-07-10 | [检索合同][retrieval]；推论不写回 KG |
| 意图确认、五档检索与 Knowhow 完整枚举 | 2026-07-25 | [检索合同][retrieval] |
| 元素/KG/来源集合枚举、覆盖率与清单引用 | 2026-07-29～30 | [集合枚举](docs/product-and-api_zh.md#集合枚举工具)、[设计记录][enum-design] |
| 大纲便签、按节合成、KG 弱支撑回喂及报告接入 | 2026-07-31 | [大纲合同](docs/product-and-api_zh.md#大纲便签与按节合成)、[设计记录][enum-design] |
| 请求内模型交互流式保活、部署引擎实时轨迹 | 2026-08-27 | [产品流程][flow]、[部署问答引擎](docs/product-and-api_zh.md#部署问答引擎askengine) |

枚举完整性与分析覆盖是独立结果，精确预算不在账本复抄。历史 Scenario / Case / Checklist endpoint 和 tab 已退役；Graph Ask 也已退役为兼容别名，不能据旧记录重新宣传为可选引擎。大纲/枚举回归入口：[大纲](backend/tests/test_reasoning_outline.py)、[集合](backend/tests/test_collection_enumeration.py)。

[历史交付记录][h12]。

## 13. 历史记录：Article Studio（已退役）

**已退役；对应历史方案 §7、v0.3。** Article Studio、article claims、derived-rule candidates 及其 endpoint、表、界面只保留交付历史。当前长内容产出路径是[深度报告][reports]，不以曾经实现推定旧能力仍存在。

[历史交付记录][h13]。

## 14. 用户反馈

**已交付；方案 §16.2、v0.1。** 回答持久化并支持 useful / not_useful 反馈与复制；后端可选 comment 不代表当前问答界面有评论输入框。当前操作与分析入口见[产品/API][product]。

[历史交付记录][h14]。

## 15. 数据模型（当前 + 历史快照）

**当前模型已交付；方案 §5、§10、§19。** 现行模型覆盖账号、Notebook/Source、KG、Ask/会话/反馈、报告、共享、Memory、Knowhow 与治理。表组织与存储边界统一见[架构][arch]，不把迁移版本号或逐表列表复制进账本。

RuleCard / CaseCard / ChecklistItem / ScenarioQueryRequest / ArticleSummary 等早期模型与 Article 表属于历史快照；是否仍有兼容对象应按当前合同判断。

[历史交付记录][h15]。

## 16. 历史记录：Demo Dataset（已移除）

**已移除；对应历史方案 §15。** 早期 synthetic demo 不再作为新数据库内容；全新数据库不创建 demo Notebook 或合成来源。当前初始化与运行方式见[部署][deploy]。

[历史交付记录][h16]。

## 17. 本机运行与验证

**开发与验证入口已交付；方案 §10、§16。** 本机启动、标准门禁、离线 smoke、前端测试/构建及 PostgreSQL 独立验证渠道均已有入口，具体命令与环境要求只在[开发规范][dev]和[脚本手册][scripts]维护。

各节旧测试数与耗时均为当时提交的记录，不能冒充本次精简或当前部署的验收结果。

[历史交付记录][h17]。

## 18. 可观测性 / 日志系统（全链路）

**已交付；方案 §16、运维延伸。** 模型交互、HTTP 请求、管线阶段日志及关联 id、安全错误展示、慢因诊断、索引调度状态与操作反馈已落地。早期 `error_message` 写列错误、排队覆盖/认领与实时索引提示问题均有交付记录。

现行日志字段、正文许可与脱敏、保留策略、诊断命令和管理员日志页以[运维][ops]、[部署][deploy]及[管理员活动日志](docs/product-and-api_zh.md#管理员用户活动日志devlogs)为准；旧“把原始异常直接显示”的描述不是现行合同。回归入口：[事件日志](backend/tests/test_event_logging.py)。

[历史交付记录][h18]。

## 19. 历史新增（dev 分支，方案 §6/§7/§16，部分已被 KG-native 主线替代）

**历史交付汇总；方案 §6、§7、§16。** 本节原有条目按现状分为：

| 历史条目 | 当前归属/状态 |
| --- | --- |
| 规则解释、Derived Rule Candidate 审核队列 | 旧路由和派生规则队列已退役；知识详情/出处由通用知识界面承接 |
| 建库富字段与模板 | 曾交付；模板入口和端点随后退役，见第 24 节 |
| CSV/Excel 导入、质量看板及内容资产统计 | 已交付，现行边界见[产品/API][product] |
| 离线 smoke、权限/异步状态/URL 及发布门禁硬化 | 已交付，当前规则见[开发规范][dev] |
| 架构模块化、阶段 1 行为对齐、Repository composition | 已交付；当前职责及债务见[架构][arch] |

Repository v9→v10 兼容复验、主机数据测量和旧三 tab 界面只是当时记录，不作为当前 schema 或工作区状态。

[历史交付记录][h19]。

## 21. 文档类型抽取 profile 注册表（方案 §5 对象模型 + §6.2 模板）

**已交付；方案 §5、§6.2。** 文档类型 profile 与对象 schema 注册表已落地；当前来源类型是 academic_paper / textbook，核心 KG 抽取对象为 concept / claim / formula / procedure。定义由[领域注册表](backend/app/domain/extraction_profiles.py)维护，service 模块只保留兼容导出。文档类型按文件确定，见第 24 节与[产品/API][product]。

[历史交付记录][h21]。

## 22. 新类型通用浏览闭环 + 全栈对等规则

**已交付；方案 §6.7、§9.1。** 动态知识类型、通用字段渲染、全类型检索和治理形成前后端闭环；新增类型不再仅可入库而不可见。现行合同见[产品/API][product]，全栈对等完成规则见[开发规范][dev]。早期 NotebookTemplate import 缺失修复及 API smoke 记录保留于交付历史。

[历史交付记录][h22]。

## 23. Schema 管理 + 归纳 + 关系图 + ask 织入 + 抽取自我修正

**已交付；方案 §5、§6.4、§7.4、§11。** 全局 schema 基线、Notebook 覆盖、本库专属类型、建议态 schema 归纳审核，以及统一关系图、对象详情和 Ask 知识引用已落地。自定义 schema 不会隐式扩大核心四类 KG 抽取合同；证据绑定与无依据节点丢弃仍是抽取要求。

当前 schema 权限、图谱定位与参与集代理读取见[产品/API][product]，运行时边界见[架构][arch]；旧自由文本关系推边与旧卡片 API 不作为当前实现。

[历史交付记录][h23]。

## 24. 类型决策从「建库」移到「上传/单文件」+ 描述自动生成 + API 层冒烟

**按文件类型决策与上传预览已交付；方案 §6.1～§6.4。** 建库不再要求用户选择库类型；上传时可逐文件指定文档类型，未指定时按内容判别。模板入口及 `/notebook-templates` 于 2026-09-07 清理。

本节曾记录的“首批来源自动描述”已被当前独立自动/手动字段和全来源刷新机制取代，见[标题与描述](docs/product-and-api_zh.md#笔记本标题与描述)。暂存上传、文件名显示与配额反馈见[产品/API][product]。API 层 smoke 已补齐路由导入及错误码覆盖。

[历史交付记录][h24]。

## 25. 冒烟脚本对齐 KG-native 当前架构（2026-06-04）

**已交付，2026-06-04；方案 §6.4、§6.5、§16。** smoke 由早期六类启发式候选迁至 KG-native profile、证据绑定、抽取窗口、图谱、Ask 与会话；无模型断言 `no-llm`，检索场景显式写入测试对象。同时修复不可用一跳邻居回流。

当前验证入口见[开发规范][dev]与[脚本手册][scripts]；当时门禁的检查组成不等同于今天的标准门禁。

[历史交付记录][h25]。

## 26. 大型文档摄取与检索加固 + 死代码清理（2026-06-05）

**已交付，2026-06-05；方案 §6.3～§6.5、§11。** 结构化 Markdown、贪心抽取窗口、代码块保真但不进 KG 抽取、有界向量缓存及大型文档成本/内存治理已落地。旧病例/Checklist/Article 路由清理属于退役记录。

当时独立模型并发旋钮已由第 29 节物理服务调度取代；“不自动更新标题/描述”又被第 4 节现行合同取代。本节性能数字仅是旧样本测量，当前摄取和检索实现见[产品/API][product]与[架构][arch]。

[历史交付记录][h26]。

## 27. Agent Memory 与 MCP（方案 §19；Agent Memory 设计 §4～§13）

**已交付；方案 §19.1～§19.5。** 独立、创建者私有且 Notebook 绑定的 Memory、手动回答预览/保存、candidate 审核生命周期、引用追溯与 Memory→KG 提案均已落地。Agent 候选面与正式 Notebook 检索面隔离；共享 Notebook 不共享成员私有 Memory。

Agent profile、opaque token、scope/allowlist/过期与撤销、公开 onboarding 和官方 Streamable HTTP MCP 已交付。工具集后续持续扩展，此处不保留会过期的工具总数与清单；以[Memory/MCP 合同][memory]及[Agent 接入 SOP](docs/agent-mcp-memory-sop_zh.md)为准。

原 gold 评价与官方 client smoke 的验收记录可追溯；当前回归入口：[Memory 召回](backend/tests/test_memory_retrieval.py)、[MCP](backend/tests/test_memory_mcp.py)、[晋升](backend/tests/test_memory_promotion.py)。

[历史交付记录][h27]。

## 28. Knowhow 新表双方向导入（2026-07-20）

**已交付，2026-07-20；方案 §6.3 / Knowhow 表扩展。** 新表支持属性按列或按行导入，预览与提交共用转置/规范化，向导显示实际形态并提供可操作错误。追加导入与存储语义保持。当前合同见[Knowhow 表][knowhow]。

[历史交付记录][h28]。

## 29. 系统模型服务统一管理与全局调度（2026-07-22）

**已交付，2026-07-22；方案 §10。** 部署 TOML 统一声明物理服务与 workload 绑定；每个服务按唯一容量共享调度。个人模型配置已下线；模型状态为脱敏只读，管理员可显式探测。

具体 workload、容量、优先级、健康状态、单进程要求与配置迁移只在[部署][deploy]和[架构][arch]维护。回归入口：[模型服务 API](backend/tests/test_system_model_services_api.py)。

[历史交付记录][h29]。

## 30. Knowhow 单行空列智能补全（2026-07-23）

**已交付，2026-07-23；方案 §6.5 / Knowhow 表扩展。** 单行空列补全联合同表证据与库内推理检索，先生成建议，经用户逐项确认后保存；伪造/无依据引用不得变成高置信建议，保存使用乐观并发保护。

当前取证、权限、失败和历史记录合同见[Knowhow 表][knowhow]。回归入口：[补全](backend/tests/test_knowhow_completion.py)。

[历史交付记录][h30]。

## 31. Knowhow 批量规整审阅、审计 actor 与内容感知列宽（2026-07-25）

**Knowhow 已交付，2026-07-25；方案 §6.5 / Knowhow 表扩展。** 批量规整采用有界候选生成、逐项 diff 审阅与取消保护；审计显示名与稳定身份分离；表格使用内容感知列宽。当前行为见[Knowhow 表][knowhow]，回归入口：[规整](backend/tests/test_knowhow_reformat.py)。

本节曾累计了多个无关领域的长记录。下表保留交付索引，正文已归各自权威文档；没有把历史未完成清单重新标为完成。

| 交付日期 | 里程碑与方案对应 | 当前说明/追溯 |
| --- | --- | --- |
| 2026-08-01～10 | 深度报告可信度、资料基础、全深度综合、耗时/覆盖披露与 facet 修复；§11 扩展 | [深度报告][reports]；后续全深度合同取代最初仅高档执行综合 |
| 2026-08-06 | 完成报告公开分享；§13 扩展 | [报告分享](docs/product-and-api_zh.md#报告公开分享护栏) |
| 2026-08-17、24 | MCP 来源管理/引用/构建及通用文件上传；§6.3、§19.3 | [Memory/MCP][memory] |
| 2026-08-20～21 | Agentic Memory P3 观察队列/回答偏好，P4 锚点归因、回想与步级提示；§19 扩展 | [理解与经验][experience]、[设计记录](docs/superpowers/specs/2026-08-18-agentic-memory-design.md) |
| 2026-08-20～21 | 群组唯一 owner、独立工作台及邀请链接；§3、§13 扩展 | [群组共享](docs/product-and-api_zh.md#群组知识共享) |
| 2026-08-24 | Markdown ZIP 后端摄取、图片资产与 MCP 文件导入；§6.3、§19.3 | [图片/压缩包](docs/product-and-api_zh.md#引用附图本段附图)、[ZIP 回归](backend/tests/test_parsers_markdown_bundle.py) |
| 2026-08-24、09-07 | Ask/公开会话与报告正文引用图片内联；§5.2、§6.5 | [引用附图](docs/product-and-api_zh.md#引用附图本段附图) |
| 2026-08-29 | 热路径批 0、批 1：故障恢复、热点查询与索引；§10、§16 | [架构][arch]、[运维][ops]、本节交付记录 |
| 2026-08-29～30 | 热路径 R2 等价重写、R3 审核队列/查重/概念详情；§10、§16 | [PR #634](https://github.com/huyangc/silicon-notebook/pull/634)、[#638](https://github.com/huyangc/silicon-notebook/pull/638)、[#639](https://github.com/huyangc/silicon-notebook/pull/639) |
| 2026-09-01 | W-CLI 离线/异机 scale 构建；§10、§16 | [运维][ops]、[PR #643](https://github.com/huyangc/silicon-notebook/pull/643) |
| 2026-09-01～03 | W1 删除笔记本作业化；§6.1、§10 | [删除运维](docs/operations_zh.md#笔记本删除作业)、[设计记录](docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-design_zh.md)（PR #653/#656/#659/#663/#666） |
| 2026-09-03～04 | W2 簇图代际切换；§10、§11 | [架构][arch]、[设计记录](docs/superpowers/specs/2026-09-03-batch3-w2-generational-cluster-swap-design_zh.md)（PR #668/#671/#673） |
| 2026-09-04 | W3 大库禁用索引管线切换；§10 | [产品/API][product]；显式限制已交付，不代表大库切换重构完成 |
| 2026-09-04～05 | W4 写路径收尾及 scale 图侧有界构建；§10、§16 | [设计记录](docs/superpowers/specs/2026-09-04-batch3-w4-misc-design_zh.md) |
| 2026-09-07 | 待办对账：backmatter 过滤、批量合并、ANN 配对、窗口/并发及模板清理；§6.4、§11 | [待办](fangan_todo.md)、本节交付记录 |
| 2026-09-07 | 多领域基准库 A1–A9 收尾；§11 | [对账记录](docs/superpowers/specs/2026-07-19-multi-domain-bases-followups.md) |
| 2026-09-07 | workspace 状态拆分三片；§9、§10 | [架构][arch]、[实施计划](docs/superpowers/plans/2026-09-07-todo-closeout.md)（PR #686 等） |
| 2026-09-07 | 待确认中心展示本人进行中的提问；§9 扩展 | [当前合同](docs/product-and-api_zh.md#待确认中心的进行中的提问) |
| 2026-09-01 | 来源分页/搜索/水合及前端在途反馈性能修复；§6.3、§9 | [列表分页](docs/product-and-api_zh.md#列表分页)、本节交付记录 |

本节末尾重复登记的账号、KG 治理、实时推理、`follow_chain` 与模型流式探活分别合并到第 3、10～12 节。历史 Article 能力已退役；Review Mode、企业扩展、解析缺口及剩余架构工作不在本节宣称完成。

[历史交付记录][h31]。

## 32. Reflect 实验退役与 Legacy 保留（2026-09-13）

**V2 已退役，Legacy 保留，2026-09-13；方案 §6.5、§11 的实验记录。** Reflect V2 实现、开关与专用探针已删除；通用权限、取消、覆盖披露、模型失败观测及 Legacy 改进保留。

当时有限样本没有显示稳定质量优势，不能把耗时对照外推为生产 P95、普遍质量结论或缓存命中率。完整实验数与限制见交付记录；现行推理合同见[检索模式][retrieval]。

[历史交付记录][h32]。

## 33. 插件提供的 reflect 动作 `ask.reflect_action` 与首个真实消费者（2026-09-14）

**已交付，2026-09-14；方案 §6.5、§11 的部署扩展。** `ask.reflect_action` 宿主与 arxiv-search 首个消费者已落地；动作由模型在宿主事实闸之后选择，外部证据可参与 Ask 合成。报告/Knowhow 未因此接入，样例插件仍需部署者显式启用。

当前合同见[Reflect 插件动作](docs/product-and-api_zh.md#reflect-插件动作askreflect_action)，配置与作者规则见[扩展 SOP](docs/deployment-extensions-sop_zh.md)；设计/交付见[设计记录](docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md)和[PR #714](https://github.com/huyangc/silicon-notebook/pull/714)。回归入口：[宿主](backend/tests/test_reflect_action_host.py)。

[历史交付记录][h33]。

## 34. MCP `ask_notebook` 的 reasoning 档接上网页端的问题理解与澄清回合（2026-09-14）

**已交付，2026-09-14；方案 §6.5、§19.3。** MCP reasoning 接入与网页同源的问题理解：清晰问题继续，阻断歧义返回澄清视图与会话绑定句柄，确认后再执行；不静默裁掉必答歧义。当前响应、预算与会话约束见[Memory/MCP][memory]和[Agent 接入 SOP](docs/agent-mcp-memory-sop_zh.md)。

[历史交付记录][h34]。

## 35. 联邦 KG 的 1-hop 扩展节点过逐库来源天花板（2026-09-20）

**已交付，2026-09-20；方案 §11 / 全局问答扩展。** 联邦 KG 一跳扩展节点与关系证据按所属库的冻结来源范围裁剪。交付当时尚无生产写入方；第 41 节 `global_run` 接入后已成为全局问答链路的保护，不能继续称为当前不可达。

当前边界见[全局问答][global]，回归入口：[来源范围子图](backend/tests/test_peer_ceiling_subgraph.py)。

[历史交付记录][h35]。

## 36. `knowledge_context` 的 canonical 折叠范围认参与集覆盖（2026-09-20）

**已交付，2026-09-20；方案 §11 / 全局问答扩展。** `knowledge_context` 的 canonical 折叠按本次参与集解析；真实访问鉴权仍使用真实成员关系，控制错误不得被普通降级吞掉。前置交付时的“零生产变化”是历史时点，第 41 节已接入写入方。

当前参与集/鉴权边界见[架构][arch]、[全局问答][global]；回归入口：[参与集守卫](backend/tests/test_participant_override_guard.py)。

[历史交付记录][h36]。

## 37. 对比题兄弟实体名过逐库来源级闸（2026-09-20）

**已交付，2026-09-20；方案 §11 / 全局问答扩展。** 对比题兄弟实体/社区名称在生成检索词前接受所属库来源范围过滤，避免仅过滤最终结果却泄出名称。SQLite 与 PostgreSQL 保持对应实现；前置功能现由第 41 节全局入口启用。

当前合同见[全局问答][global]，回归入口：[对比来源范围](backend/tests/test_comparison_peer_ceiling.py)。

[历史交付记录][h37]。

## 38. KG 命中的定义 / 引文 / 引用过来源天花板（2026-09-20）

**已交付，2026-09-20；方案 §5.2、§11。** KG 命中的定义、引文与引用重查受来源范围约束；仅清空初始 hit 的 evidence 不再被当作足够隔离。此项修复既有线上行为。

当前证据与授权合同见[检索模式][retrieval]及[全局问答][global]，回归入口：[知识上下文范围](backend/tests/test_knowledge_context_source_ceiling.py)。

[历史交付记录][h38]。

## 39. 联邦 chunk 通道的对等模式：没有主体库时三件套分叉（2026-09-20）

**已交付，2026-09-20；方案 §11 / 全局问答扩展。** 无主体库的对等检索取消当前库保底，保留每条引用的真实归属，使用全局预算与平等证据策略；单参与库的全局提问也遵循同一范围复核。交付时“生产不可达”的前置状态已被第 41 节接入取代。

当前合同见[全局问答][global]，回归入口：[联邦对等模式](backend/tests/test_peer_mode_federation.py)、[引用归属](backend/tests/test_peer_mode_citations.py)。

[历史交付记录][h39]。

## 40. 对等模式下 `AskService` 各步骤与三条 active-only 检索腿的处置（2026-09-20）

**已交付，2026-09-20；方案 §6.5、§11 / 全局问答扩展。** 对等 run 的 Ask 步骤与检索通道已收束，避免名义 active 库取得私有 Memory 或额外检索特权；表格分析按冻结范围收窄。全部 peer 工作簿分析及部分单库检索腿联邦化仍是独立待办。

当前启用入口见第 41 节，完整限制见[全局问答][global]，回归入口：[对等 Ask 步骤](backend/tests/test_peer_mode_ask_steps.py)。

[历史交付记录][h40]。

## 41. 全局问答改由单库引擎作答：`global_run` 的四件事（2026-09-20）

**已交付，2026-09-20；方案 §11、v1.0 多 Notebook 检索扩展。** 全局问答复用 `AskService.ask`；`global_run` 统一安装参与集、逐库冻结来源范围、detached 对话与联邦运行计划，结束/异常/取消均释放。全局任务与会话独立存储，并纳入管理端提问分析、活动与用量投影。

这也是第 35～40 节前置能力从测试安装进入真实全局入口的接入点。当前合同见[全局问答][global]；代码与回归：[global_run](backend/app/services/global_run.py)、[入口测试](backend/tests/test_global_run.py)。

[历史交付记录][h41]。

## 42. 全局问答的两种引擎与 reasoning 的问题理解预检（2026-09-20）

**已交付，2026-09-20；方案 §6.5、§11 / 全局问答扩展。** 全局支持 chunk 与 reasoning，reasoning 提交前有问题理解预检；暂不接部署扩展引擎。新任务返回标准 AskResponse，历史 payload 只读兼容，运行轨迹可见。

全局档位限制、预检权限与响应形态见[全局问答][global]。回归入口：[引擎对等](backend/tests/test_global_ask_engine_parity.py)、[意图路由](backend/tests/test_global_ask_intent_route.py)。

[历史交付记录][h42]。

## 43. 全局问答的回执口径与引用冻结复核（2026-09-20）

**已交付，2026-09-20；方案 §5.2、§11 / 全局问答扩展。** 多轮检索回执聚合真实成功/降级状态；答案交付前复核冻结来源、可见性与适用的检索指纹。失败时整份答案按公开合同作废并提示重试，不冒充已重新检索的原答案。（2026-09-29 起不再整份作废，见第 48 条。）

当前回执、引用冻结及权限复核时点见[全局问答][global]，回归入口：[全局问答](backend/tests/test_global_ask.py)。

[历史交付记录][h43]。

## 44. 全局检索的预算口径与共享执行器（2026-09-20）

**已交付，2026-09-20；方案 §10、§11 / 全局问答扩展。** 联邦任务共享进程级执行器与公平窗口；阶段时限和实际数据库调用时限分开计量，大库参与腿不冷加载索引。失败原因、回执与关闭联邦时的退化行为已有明确合同。

当前数值、原因码与预算只在[全局问答][global]及[部署][deploy]维护。回归入口：[并发/预算](backend/tests/test_global_ask_parallel.py)。

[历史交付记录][h44]。

## 45. 全局会话的公开分享：同一个公开页、按库集合的实时复核（2026-09-20）

**已交付，2026-09-20；方案 §13 / 全局会话分享扩展。** 全局会话复用公开会话页与受控图片通道；每次公开读取按分享者身份复核被引用库集合，失权时整条链接不可读。公开投影不暴露内部库标识、回执与轨迹。

当前水位、撤销、历史兼容与授权合同见[会话公开分享](docs/product-and-api_zh.md#问答会话公开分享护栏)、[全局问答][global]。回归入口：[分享 API](backend/tests/test_global_ask_share_api.py)。

[历史交付记录][h45]。

## 46. 检索策略经验库按笔记本分区：本库自己的打法、界面可见、注入默认开（2026-09-22）

**已交付，2026-09-22；方案 §19 的 Agentic Memory 延伸。** 检索策略经验按 Notebook 分区，由本库成员共享，先取本库条目再用独立全局分区补齐；理解面板可查看、整理与清空本库经验。同步 HTTP、MCP 与 durable 问答均接入完成记账。

同日交付的模型绑定启动校验已由提示升级为默认拒启：非空配置缺失工作负载绑定或含未知／已退役 id 时拒绝启动；热重载拒绝坏配置并保留旧注册表。非严格模式与空配置离线模式仍受支持，条件见[部署][deploy]，交付依据见 [PR #779](https://github.com/huyangc/silicon-notebook/pull/779)。

注入默认开启，蒸馏与注入仍独立开关。经验只作检索提示，不是可引用证据；本机连通性试跑不等于真实 A/B 质量验收，后者仍在待办中。当前权限、隐私、预算与配置见[检索策略经验][experience]及[部署][deploy]；回归入口：[蒸馏](backend/tests/test_retrieval_experience_job.py)、[注入](backend/tests/test_retrieval_experience_injection.py)、[隐私](backend/tests/test_retrieval_experience_privacy_guard.py)。

[历史交付记录][h46]。

## 47. 当前库保底席位与原文段下限（2026-09-29）

**已交付，2026-09-29；方案 §19 两层知识库的延伸（来源范围与引用可信度修复计划 PR-C）。** 挂了参考库时，当前笔记本在每一处机械切分里都保有 `CHUNK_FEDERATION_ACTIVE_RESERVE` 的席位：`chunk` 模式的 MMR / 配额融合 / mix 最终切分，`reasoning` 单次合成、按节合成（只在本节绑定段之间）与深度报告逐节撰写的原文段前缀；全局问答没有主体库，同样的席位按最佳命中顺序逐库分配。判据、席位数、相同正文一席、「无参考库条目则惰性」只有一处定义；未挂参考库时席位恒惰性。reasoning 合成里先于原文段装配的结构化块不再把精确与保底段落挤出 prompt：下限按原文段的真实渲染长度计算，只有结构化块照旧渲染会把保底前缀挤出去的运行才会重新渲染；下限的精确前缀部分对单库同样生效（比例为 0 时也是），其余运行逐字节不变。保底只约束机械切分，不覆盖证据精炼与大纲绑定。

合同、数值与回滚开关见[检索模式问答][retrieval]、[全局问答][global]与[部署][deploy]；回归入口：[mix 席位](backend/tests/test_mix_reserve_seats.py)、[reasoning 前缀](backend/tests/test_reasoning_library_reserve.py)、[原文段下限](backend/tests/test_reasoning_passage_floor.py)、[当前库 id 贯通](backend/tests/test_active_notebook_id_threading.py)。

## 48. 全局问答引用核对：部分失败照常交付、检索时刻登记（2026-09-29）

**已交付，2026-09-29；方案 §5.2、§11 / 全局问答扩展，取代第 43 条的作废口径。** 全局回答的终态引用核对不再整份作废：每条引用与锚点逐条判定，未通过的标注「原文已改动 / 资料已删除 / 无法核对」，答案下方一句如实说明，正文照常交付。核对只判引用完整性，权限与来源范围只归检索层。

对照依据是检索时刻的指纹快照：联邦原文段、mix 分支的知识图谱叠加段、文档概览、集合枚举、KG 对象、推导链与表格分析都经同一道接缝登记（读过全文的生产者进程内哈希，只持指针的一次有界按 id 读），首个真实快照获胜，只按 id 读到的快照不替换已有条目；本 run 登记过、随后被删的元素报告为「资料已删除」。检索时已经不存在的元素不生成引用卡，单库与全局一致。

当前合同见[全局问答][global]与[运维事件][ops]；回归入口：[逐条判定与说明句](backend/tests/test_global_ask_citation_check.py)、[真实引擎的健康运行与竞态](backend/tests/test_global_citation_producers_e2e.py)、[构造点静态登记](backend/tests/test_citation_attestation_guard.py)、[单库悬空元素](backend/tests/test_reference_liveness.py)。

## 49. KG 对象的定义按来源归因，KG 详情按查看者的可读来源过滤（2026-09-29）

**已交付，2026-09-29；方案 §5.2、§11。** 关掉第 38 节登记的残余口：`node_context` 的定义带上归属（`definition_basis` / `definition_source_id` / `definition_element_id`），`defines` 证据有序、有界地回落到第一条范围内证据，概念簇融合描述只在能证明全部成员来源都在范围内时使用（用户裁决 Q1，严格；超出成员上限的簇在绑定的范围下不用），流程步骤与章节路径先归因再显示。来源范围只在它能排除东西时才绑定这次重查，全选、未变化、没有读不到的隐藏来源的运行与不带范围的运行逐字节相同。知识图谱面板的对象详情、概念详情与邻域补全按查看者的可读来源过滤（用户裁决 Q4 / M1）：另一位成员的私有 Memory 抽出的对象、出处、定义与簇标签不再出现在这三个读取里。

当前合同与数值上限见[检索模式][retrieval]与[产品/API][product]的 KG 详情一节；回归入口：[对象上下文](backend/tests/test_node_context.py)、[知识上下文范围](backend/tests/test_knowledge_context_source_ceiling.py)、[范围绑定裁决](backend/tests/test_ceiling_binds_verdict.py)、[查看者规则](backend/tests/test_kg_viewer_scope_rules.py)、[逐身份判定](backend/tests/test_kg_viewer_scope_identities.py)、[两用户端到端](backend/tests/test_kg_viewer_scope_routes.py)。实施计划：[来源范围与引用可信度修复计划](docs/superpowers/plans/2026-09-29-scope-ceiling-remediation.md)。

## 50. 全局问答里集合枚举的引用卡按本次冻结的参与集给出（2026-09-29）

**已交付，2026-09-29；方案 §11 / 全局问答扩展。** 全局（对等）运行里，集合枚举行的引用卡改按本次运行冻结的参与集判定成员资格——即全局问答解析并授权过、各带一份逐库来源天花板的那组库——不再借用命名锚点的挂载表：选中却没挂在锚点上的库，其清单行现在有可点开的依据；挂在锚点上却没被选中的库，既不列出也不出引用卡。单库运行仍按真实挂载谓词复核，与此前逐值相同；两条分支由参与集守卫分别钉住。同一轮里集合枚举与其引用全面按来源天花板取数（计数、清单、续跑游标与引用卡只看勾选或冻结的来源）。

当前合同见[全局问答][global]与[检索模式][retrieval]；回归入口：[集合枚举来源天花板](backend/tests/test_collection_enumeration_source_ceiling.py)、[参与集守卫](backend/tests/test_participant_override_guard.py)。

## 51. 集合枚举与按篇读取原文认本次勾选的来源（2026-09-29）

**已交付，2026-09-29；方案 §11 扩展。** 提问时只勾选部分来源、或冻结范围之后来源发生变化时，集合枚举与 `read_document` 不再整体关闭：集合地图、来源 / 元素 / 知识对象清单、计数、续跑游标与引用卡只看勾选的来源，清单在轨迹、回喂账目、合成分区标题、reflect 提示词与结果卡上如实标注「（仅勾选的来源）」；按篇读取在读原文前再按来源天花板与参与集核对一次，范围外的文档与「清单里没有这个标题」无法区分。全选且未漂移的默认提问与不带范围时完全相同：天花板是否约束由每个检索 run 判定一次，并显式传给每个枚举与目录入口。全局问答不再提供「仅当前笔记本」范围、不注入锚点库的理解块；Knowhow 完整枚举在全局、收窄或漂移时让路。原待办「集合枚举的引用不过来源天花板」「`read_document` 延后项：收窄时整套枚举不提供」「全局模式下『参考库』措辞没有对应物」随之关闭。

当前合同见[检索模式][retrieval]中的集合枚举工具一节；回归入口：[推理侧](backend/tests/test_reasoning_enumeration_source_ceiling.py)、[执行器与目录](backend/tests/test_collection_enumeration_source_ceiling.py)。

## 52. 笔记本拷贝不带 Memory（2026-09-30）

**已交付，2026-09-30；方案 §19 私有 Memory 的延伸（来源范围与权限整改计划 PR-E5，用户裁决 M2）。** 深拷贝——给自己拷一份与经分享链接投递的一份——一律不带任何成员的 Memory 及其派生内容：Memory 来源、元素与向量、派生的知识对象、关系、事实与向量、任一端是 Memory 对象的关系、「Memory 的簇」（含 Memory 成员或 canonical id 铸自 Memory 对象），以及 Memory 借给保留行的证据条目。成簇过时或含 Memory 的笔记本，副本以「待重建」开始；分享预览与分享弹窗的数量不含 Memory。没有 Memory 且成簇最新的笔记本，拷贝与此前完全相同。此前拷贝留下的无主 Memory 来源由启动后的后台清扫删除，体检项 H12 只读显示剩余数量。

当前合同见[产品/API][product]的深拷贝一节与[运维][ops]的无主 Memory 来源一节；回归入口：[拷贝排除](backend/tests/test_notebook_share_copy.py)、[判据契约](backend/tests/test_memory_sql_contract.py)、[孤儿清扫](backend/tests/test_memory_orphan_sweep.py)。

## 53. 成员退出与 Memory 的彻底删除（2026-09-30）

**已交付，2026-09-30；方案 §19 私有 Memory 的延伸（来源范围与权限整改计划 PR-E5，用户裁决 M5）。** 成员退出共享笔记本时，只有本人的退出、并且在告知条数并得到确认之后，才永久删除本人在该库的记忆；确认前可以导出为文件或转移到自己的笔记本。每种结果都由服务端如实计数（退出合同 v2：204、带删除条数的 200、需重新确认的 409、带已删与剩余条数的 409 / 503 未完成）。收回访问权的操作一律不删记忆；删除整本笔记本则删除其中的一切。硬删除、批量删除、弃用、转移移动、退出与无主来源清扫共用同一条移除：剥离合并进共享对象的证据（共享对象保留），整簇删除 Memory 的簇及点名它们的审阅候选，并标记待重建。

当前合同见[Memory 与 Agent MCP][memory]；回归入口：[清除场景](backend/tests/memory_purge_cases.py)、[退出路由](backend/tests/test_memory_exit_routes.py)、[PostgreSQL 交错](backend/tests/postgres/test_memory_exit_interleavings_pg.py)。

## 54. 检索各腿按本次运行的来源天花板取数（2026-10-07）

**已交付，2026-10-07；方案 §11、§19 的延伸（来源范围与权限整改计划 PR-E2）。** 各检索腿在取数时就按本次运行冻结的来源天花板取舍，不再先取后滤：概念漫游（PPR）的名额只给天花板内的段落，越过被拒候选的遍历有上界；推导链每一跳都过证据边界，无界内证据即整条丢弃；弱支撑提示与 mix 叠加只保留界内支撑的节点与样本关系；回答上下文的关系行与「×N源」只计界内来源；所有提问人共用的关键词与关系向量缓存不再含 Memory 派生内容；插件引擎的越界命中直接丢弃；晋升到公共库的对象按证据快照呈现，不再现读推广者私有库。收窄范围时精确标识符查找恢复可用，天花板下推进探针，单库与全局逐库一致——待办账本里登记的逐库精确臂「探测窗口先取后滤」残余随之关闭。全选且未变化、没有他人 Memory 的运行与不带范围时逐字节相同。默认天花板（PR-E1）为个人记忆通道关闭的提问临时关掉的全图、PPR、关系与精确查找四个通道随之恢复，各自按天花板挡住提问人本人被扣下的个人记忆；各腿统一在读取前只取一次本次运行的判词。

当前合同见[产品/API][product]的「按来源选择检索范围」与 Memory 晋升两节、[开发规范][dev]的 id 清单绑定一节；回归入口：[服务层腿](backend/tests/test_retrieval_leg_ceilings.py)、[候选层腿](backend/tests/test_candidate_leg_ceilings.py)、[PPR 名额](backend/tests/test_ppr_source_ceiling.py)、[store 与证据](backend/tests/test_store_evidence_ceiling_plans.py)、[真实存储上的精确查找](backend/tests/test_exact_lookup_ceiling_store.py)、[PostgreSQL 计划钉子](backend/tests/postgres/test_store_evidence_ceiling_explain_pins.py)。实施计划：[检索层权限整改计划](docs/superpowers/plans/2026-09-29-retrieval-permission-remediation.md)。

## 55. 知识图谱里的 Memory 只属于本人（2026-10-08）

**已交付，2026-10-08；方案 §19.1「成员的 Memory 互不可见」与 §19.4 Memory → KG 治理的延伸（来源范围与权限整改计划 PR-E4，用户裁决 M1）。** 由成员 Memory 派生的知识对象与关系在结构上只属于该成员：建图时不做共享概念簇的种子或成员，不进规范关系、提及桥、共提、社区及其摘要和簇分析；增量融合、冲突检测与手工合并不碰它们（合并 409，按 id 写入对他人等同不存在，通用贡献申请拒收、批准时复核）；含成员 Memory 的笔记本不能发布为公共知识库；落盘的图谱预览、检索索引与来源分区伴随工件不含任何 Memory；知识列表、计数、图谱与邻居视图、KG 搜索和搜索框知识部分按查看者读取，查看者只多看到本人的 Memory 对象，没有 `memory:read` 的令牌一条也看不到；Memory 来源永不分块，整本索引重建、Knowhow 转移与同步导入各自拒绝。存量由迁移 PostgreSQL 0067 / SQLite v87 清理，就绪后后台一轮检查并重建（体检 H9–H11 只读提示），升级前可用普查脚本估算重建范围。

当前合同见[Memory 与 Agent MCP][memory]中的「知识图谱里的记忆」与[运维][ops]的记忆隔离一节；回归入口：[建图排除](backend/tests/test_rebuild_streaming.py)、[写侧拒绝](backend/tests/test_memory_kg_isolation_write.py)、[store 读者](backend/tests/test_memory_kg_store_readers.py)、[两用户端到端](backend/tests/test_kg_viewer_scope_routes.py)、[工件构建](backend/tests/test_viz_index_build.py)、[迁移](backend/tests/test_memory_isolation_migration.py)、[隔离重建](backend/tests/test_memory_isolation_rebuild.py)、[段落写入守卫](backend/tests/test_memory_chunk_write_guard.py)、[读者登记守卫](backend/tests/test_memory_reader_guard.py)。

## 56. 会话公开分享先告知个人记忆，公开页每次打开复核用到的库（2026-10-08）

**已交付，2026-10-08；§13 公开分享的延伸（来源范围与权限整改计划 PR-E7 的 E7-5/E7-6，用户裁决 M4，审计 D-2/D-3/D-4）。** 公开或更新笔记本会话与全局会话的分享链接前，服务端按这次恰好要公开的那些轮次数出作者本人不同个人记忆的条数（直接引用的记忆、记忆锚点，以及来自作者个人记忆投影的证据），并给出相对当前已公开水位的新增条数；条数大于 0 时必须以相同的确认值公开，否则 409 且不发链接、不推进水位，不含个人记忆的会话与此前完全相同；弹窗只显示服务端的数字。公开页把作者个人记忆的引用标为「作者的个人记忆」。全局会话的公开页每轮复核它检索过与引用过的全部库；笔记本会话与报告的公开页每次打开都按创建者复核引用到的挂载参考库，任一失效即与撤销的链接相同，分享时已失效则不发链接。报告的分享披露先行交付，见同一节合同。

当前合同见[产品/API][product]的会话与报告公开分享护栏、[全局会话分享](docs/product-and-api_zh.md#全局会话分享)；回归入口：[分享披露场景](backend/tests/conversation_share_disclosure_cases.py)、[公开页复核场景](backend/tests/public_page_mount_recheck_cases.py)、[PostgreSQL 孪生](backend/tests/postgres/test_conversation_share_disclosure_pg.py)。

## 57. 晋升对象由公共知识库自己承载出处（2026-10-08）

**已交付，2026-10-08；§6 晋升（贡献到公共知识库）的延伸（来源范围与权限整改计划 PR-E8，审计 B-12；呈现方式按用户 2026-09-29 裁决「显示为可见来源」）。** 批准晋升时，公共库为每个原件（推广者的来源；个人记忆晋升时是那条记忆）建立或复用一个可见的「晋升自：<原标题>」来源（个人记忆为「晋升自个人记忆：<记忆标题>」），每条证据成为它的一个元素（通用晋升取原元素现文，读不到取保存的摘录；个人记忆晋升只保留成员批准的摘录；原件是成员个人记忆来源的条目一律丢弃；没有文本的丢弃），证据改指它们，原来源与所属笔记本只作展示键保留；合并进既有公共对象时只改写新进来的条目。于是挂载公共库的笔记本（挂载库只开放可见来源）与全局问答都能召回并引用晋升对象，引用卡显示原件标题、打开公共库的「晋升自」来源，推广者删除私有笔记本也不受影响。这类来源在详情显示类型「收录」，列表徽标为「已收录」或「不在图谱中」；不占上传文档名额；可删除（摘掉它的证据条目，只删失去全部证据的条目，被合并的原生条目保留）；不可重新解析，抽取入口拒绝它，不进任何文档管线；批准与删除并发时批准等待并重新写入来源。存量由迁移 PostgreSQL 0068 / SQLite v88 改写并把受影响的库标记待重建，双后端结果一致，SQLite 迁移用冻结副本且与运行时规则钉成相等。

当前合同见[Memory 与 Agent MCP][memory]中的晋升段落与[运维][ops]的「公共知识库里晋升对象的出处」一节；回归入口：[批准与问答场景](backend/tests/promotion_provenance_cases.py)、[SQLite](backend/tests/test_promotion_provenance.py)、[PostgreSQL 孪生与计划钉子](backend/tests/postgres/test_promotion_provenance_pg.py)、[迁移场景](backend/tests/promotion_provenance_migration_cases.py)、[SQLite 迁移](backend/tests/test_promotion_provenance_migration.py)、[PostgreSQL 迁移](backend/tests/postgres/test_promotion_provenance_migration_pg.py)。

## 58. 挂载只对能读被挂库的人生效（2026-10-08）

**已交付，2026-10-08；「读权 ⇒ 可挂载」的延伸（来源范围与权限整改计划 PR-E6，用户裁决 M3，审计 N-6）。** 参与集随查看者解析：一条有效的挂载边，只对挂载人本人、自己本来就能读被挂库的人，以及公共库与 `everyone` 授权库的所有人生效。主人把私有库挂到共享笔记本后，成员提问时它在所有检索通道（段落、关键词、精确标识符、知识图谱、PPR、关系、弱支撑、叠加、社群、推导链）上零贡献，来源/资产代理与 MCP 取证对它一律 404，笔记本摘要里看不到它的名字与图谱标志；主人一切照旧；成员另获读权后即刻生效。查看者由 `current_viewer_id()` 给出（检索运行的 actor → 请求用户 → 无人），报告与离开后接回的运行在工作线程上按运行的 actor 判，不取线程里的请求上下文（全局问答只检索勾选的库、不读挂载，M3 在它上面不适用）；默认来源天花板按其 owner 解析参与集；无 actor 的后台路径只剩公共库与 `everyone`。联邦关系图、PPR 图与规模 PPR 合成图的进程缓存键在查看者有效集合窄于全部有效边时带上该集合的指纹（与全局问答覆盖的指纹分属不同命名空间，指纹段不计作配额桶）：集合相同的人共用一份，含私有库的图不与读不了它的成员共用；每次运行的参与集与键是同一份快照，挂载人免掉参照读。公开页按分享创建者复核挂载（会话与报告两路）。

当前合同见[产品/API][product]「挂载只对能读被挂库的人生效」与[开发规范][dev]的 `mount_sql` 条目；回归入口：[端到端](backend/tests/test_mount_viewer_e2e.py)（[PostgreSQL 孪生](backend/tests/postgres/test_mount_viewer_e2e_pg.py)）、[调用点守卫](backend/tests/test_mount_viewer_guard.py)、[store 矩阵](backend/tests/test_mount_viewer_store.py)、[片段契约](backend/tests/test_mount_sql_contract.py)、[公开页复核场景](backend/tests/public_page_mount_recheck_cases.py)。

## 59. 技术债整改程序：全局回答读法、取样通道批量读、Knowhow 面板怪癖、门面退役（2026-10-15）

**已交付，2026-10-15；2026-10-12 起的技术债整改程序（计划、用户裁决与评审存于程序台账，不入库）里下列几项的完成记录，原登记在[待办账本](fangan_todo.md)。** 该程序其余已合入项的合同分别由各自 PR 与权威文档承载，本节不复述。

- **历史全局回答 payload 归一（PR #851）。** 全局作业的答案以两种形状落库：现行引擎的 `answer` 与引擎切换前的 `response`（流式步骤在顶层 `trace`）。读法统一为投影函数族——现为四个（`global_answer_text` / `global_answer_citations` / `global_answer_trace` 与新增的 `global_answer_payload`），先读新形状、再回落旧形状；检索经验学习链的 reasoning 采样 SQL（PostgreSQL 与 SQLite 各一处）原先只读 `answer.reasoning_trace`，现已按 `COALESCE(answer.reasoning_trace, trace, [])` 与投影同口径（真实旧行的 mode 不会是 reasoning，生产上命中 0 行，无行为变化）。原待办的二选一取了「守卫」：[读取形状守卫](backend/tests/test_global_answer_shape_guard.py)按名登记每一处读取答案路径的 SQL 字面量与 Python 站点，要求两种形状都读或写明理由，登记按名字而非行号。**一次性迁移不做**：旧行零迁移继续靠投影读。
- **`read_document` 取样通道的 I/O 放大（PR #859，延后项 (b)）。** `source_elements_page` 新增仅限关键字的 `positions=`，一次取一组位置（单批不超过 100 个，超出报错而非截断），契约在 `app/domain/source_positions.py`；服务层先读首页拿总数，其余位置分批读取并逐批与首页总数比对。PostgreSQL 每批一条 READ COMMITTED 语句，存在性、计数与取数在同一快照里完成，并按代价在两种形状间选：逐 offset 的索引窗口读，或对 `idx_source_elements_source_created` 走一遍并用 `row_number` 编号再按主键回表（head-walk）；SQLite 每批一条 `UNION ALL`，每一项逐字就是窗口读的语句。用户可见的 `SourceOverview` 与改前逐字节相同，计数和元素来自同一快照，旧实现在计数与取行之间的漏检窗口随之消失。延后项 (a)「同名文档无法消歧」仍在待办。回归入口：[SQLite](backend/tests/test_source_elements_positions.py)、[PostgreSQL 孪生与计划钉子](backend/tests/postgres/test_source_elements_positions_pg.py)。
- **Knowhow 面板拆分后记录的怪癖（PR #901，承接 #860 / #863 / #868 / #871 的 S4 拆分与 #874 的删表按钮修复）。** 拆分期间只录不修的七条行为在此修完，每条一次提交、各带修前红修后绿的用例：引用跳转改指概念表时路由沿用旧表数据；切换笔记本多发一次旧表详情请求；引用改指同一张表的另一行时网格空白且白发一次行代码请求；切换笔记本后导入向导仍开着；三条复位路径清的状态集合不一致；行抽屉的 Esc 监听每次渲染重注册；关抽屉多一次提交。同一 PR 还修了删表流程的两个既有问题（删除进行中用户移到别的表时被踢回列表、失败提示写到别的表上；旧笔记本的表清单可被写到新笔记本上），并让删除在切换笔记本后只释放自己的锁、不改新笔记本的状态。拆分遗留的导入/管理向导 CSS 字面量注释在 PR #871 改指 `knowhow-panel-styles.tsx`（两个 styled-jsx id 随字面量改变，编译后的 CSS 逐字节不变）。回归入口：[复位路径](frontend/tests/component/knowhow-panel-resets.component.test.tsx)、[删表](frontend/tests/component/knowhow-panel-delete-table.component.test.tsx)、[面板边界守卫](frontend/tests/guards/knowhow-panel-boundary-guard.test.mjs)。面板焦点陷阱（S7，PR #910）随后补齐：面板复用 `use-dialog-focus.ts` 的焦点约定（新增可选的焦点归还，以及「上层对话框让位」和「容器自身也是 Tab 边界」两条），打开时焦点进入面板、Tab / Shift+Tab 只在面板内循环，`page.tsx` 在面板打开时给顶栏与笔记本视图加 `inert`，关闭（含切换笔记本卸载）后焦点回到打开它的元素；「Esc 只关最上层」不变。回归入口：[面板焦点](frontend/tests/component/knowhow-panel-focus.component.test.tsx)。
- **reflect 回喂「已展开过的节点」按展开顺序（PR #903）。** `legacy_action_ledger_note` 原先直接遍历 `set`，展开两个以上节点时同一问题的 prompt 字节随进程 hash 种子变。`visited` 改为插入有序的字典当有序集合，账本按首次展开顺序渲染；同模块其余进模型文本的集合均只做成员判断或经 `sorted()`。回归入口：[跨 hash 种子的子进程用例](backend/tests/test_reasoning_visited_ledger_hash_seed.py)。
- **运行期诊断目录可配置（PR #902，C10 → C11）。** lifespan 原把 `.local/diagnostics` 写死为相对仓库根的路径，测试与脚本进入 lifespan 会把 `runtime.json`、`thread-dumps.log` 写进检出目录。新增 `DIAGNOSTICS_DIR`（默认值与旧路径逐字节相同，相对路径锚在仓库根，空值回退默认），`diag.py incident` 新增 `--diagnostics-dir`（显式参数 > 已配置的 `DIAGNOSTICS_DIR` > `<--local>/diagnostics`）；测试进程、冒烟脚本、契约夹具生成器与影子台各自指向一次性目录。回归入口：[目录隔离](backend/tests/test_diagnostics_dir_isolation.py)、[诊断公共件](backend/tests/test_diag_common.py)。
- **门面（`RepositoryFacade`）退役与协议绑定（S2 批 A / C / D / E / F / B：PR #857 / #870 / #862 / #872 / #906 / #907）。** 只被测试调用的 facade 单跳委托逐批退役，生产经运行时组件（`_runtime.<组件>`）调用，测试改为直调组件，参数逐字不变，生产行为与性能不变：批 A 退 18 个，批 D 退 8 个，批 C 退 7 个（并把 scale builder 弱引用接缝 `incremental_fuse_source` 改接线到生命周期服务，避免被删后融合被静默跳过），批 E 退 6 个身份方法，批 F 退 15 个分享/成员方法并把测试专用的 `add_relations` 挪到测试夹具。随之新增的守卫：facade 公开面双向零松弛、动态引用检查（弱引用接缝、字面量 `getattr` / `hasattr`、经 `._runtime.<组件>.<成员>` 的接缝成员必须仍在组件类上）、协议绑定检查——被当作协议交出去的 facade 不得缺协议声明的方法。协议绑定的既有缺口从 44 条（AskStateStorePort 20、McpMemoryRepository 4、NotebookRepository 20）收到只剩 `NotebookRepository.rerank`（见待办）：靠把 `ask_state_repository()` 的返回类型收窄为只含 `answer_memory_source` 的协议、把 `IdentityRepository` / `NotebookAccessRepository` / `NotebookSharingRepository` 各拆出 facade 实际实现的只读基类、删掉 facade 从未实现的两处协议声明。批 B 最后退 10 个 KG 治理方法（审阅路由走 facade 保留的 `approve_promotion_as_reviewer` / `reject_promotion_as_reviewer`），审计里只测试调用的 facade 方法收到只剩按裁决保留的 `rebuild_notebook_kg` / `relink_notebook_kg`。`ports_protocol_method_count` 975 → 967，`allowed_names` 327 → 262。当前台账见[门面退役台账](docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md)，守卫见[架构边界脚本](scripts/check_architecture_boundaries.py)与其[基线](scripts/architecture_boundary_baseline.json)。
- **测试里的死代码。** `backend/tests/test_diag_unified.py` 的 `_command_matrix` 无任何引用，已删除。

## 60. MCP 提问的全部笔记本写法与限时回答链接；模型 JSON 按严格 schema 对齐（2026-10-15）

**已交付，2026-10-15；方案 §19.3 与模型调用扩展。** 技术债整改程序移交（§59）之后合入、此前未入账的四项。合同由所属权威文档承载，本节只登记完成状态与回归入口。

- **MCP `ask` 接受 `notebooks=["*"]`（PR #913）。** #830/#831 把省略 `notebooks` 定为默认笔记本后，原「全部授权笔记本」的写法随之消失；用户裁决保留省略 = 默认笔记本，另加显式的 `"*"`：白名单内、token 主人当前仍可读的全部笔记本，一律走全局问答，范围由服务端既有的 `all` 模式解析，不由 MCP 展开 id。`"*"` 与其他 id 混传、解析超过上限等边界均返回可操作的拒绝。当前合同见[Memory 与 Agent MCP][memory]；回归入口：[全局问答 MCP](backend/tests/test_global_ask_mcp.py)、[全局问答准入](backend/tests/test_global_ask_admission.py)。
- **MCP 回答的限时链接（PR #915）。** `ask` / `get_ask` 新增 `delivery="link"`：已回答时只返回确定性生成的纯文本摘要和一个限时匿名链接（`/c/alnk-…`，每次打开按签发 token 的当前权限复核，过期且行仍在时返回 410），供即时聊天工具这类不渲染 Markdown 的客户端使用；默认 `inline` 的行为与响应不变。存储为新表 `answer_links`（SQLite v94 / PostgreSQL 0074），Agent token 新增「允许签发限时回答链接」开关。当前合同见[Memory 与 Agent MCP][memory]与[问答会话公开分享护栏](docs/product-and-api_zh.md#问答会话公开分享护栏)，配置见[部署][deploy]，接入步骤见 [Agent MCP SOP](docs/agent-mcp-memory-sop_zh.md)；设计与实施计划：[设计](docs/superpowers/specs/2026-10-15-mcp-answer-link-design_zh.md)、[计划](docs/superpowers/plans/2026-10-15-mcp-answer-link-plan_zh.md)。
- **模型 JSON 校验对齐与 JSON Schema 能力协商（PR #914）。** 模型回复通过共享 JSON 边界后仍可能被各消费方读歪（数字 id 覆盖字符串 id、容器被当成正文、动作越出词表等），改为在各自的消费方收窄并补用例；每个模型服务在首次调度使用时协商一次 JSON Schema 能力，支持的服务走严格 wire schema 加本地校验，不支持或无法确认的保持普通 JSON。当前合同见[模型 JSON 恢复与流保活](docs/product-and-api_zh.md#模型-json-恢复与流保活)与[部署][deploy]。
- **JSON hint 与严格投影、消费方对齐（PR #916）。** 对 #914 的逐条复核：每个 hint 与其提示词、投影出的严格 schema 和消费方三方对齐——修掉一处真实拒收（Agent profile 的 `retire:false`）、提示词里「省略」与严格 schema 的矛盾（改为「写 null」）、根字段枚举误收空串；并按约定简化回复形状（KG 抽取的边改按节点数组下标引用、refine 改为 `{"drop":[…]}`、reflect 收成单一 `query` 等）。hint 文本是模型缓存键的一部分，升级后第一次建图会对每个窗口重新调用模型。回归入口：[hint 投影快照](backend/tests/test_model_schema_hint_snapshots.py)。

## 61. 剩余待办收尾（2026-10-19 起）

2026-10-19 起按「先做能直接做的、拍板项逐条过」推进的收尾项，每项一条。用户 2026-10-19 对待办账本里几条待拍板项的裁决，不做的项登记在[待办账本](fangan_todo.md)「整改计划刻意不做、只登记」的「产品与守卫裁决」组。

- **无图披露步文案对齐界面词汇表（PR #920）。** 笔记本没有可用知识图谱时，推理轨迹里的披露步由「本笔记本尚未构建知识图谱,本轮只用原文检索与集合清单」改为「本笔记本尚未整理知识图谱,本轮只用原文检索与集合清单」（半角逗号不变），与[界面词汇表](docs/ui-vocabulary.md)「建图 → 整理」一致。[产品/API][product]「尚未构建知识图谱的笔记本」一节里逐字冻结的那句、仓库黄金 fixture 与既有用例同批改写；界面词汇守卫中逐字全串的登记例外 `GRANDFATHERED_TRACE_SUMMARIES` 随之删除，守卫对该句改回「构建」重新报红。回归入口：[界面词汇守卫](backend/tests/test_ui_vocabulary_guard.py)、[推理检索](backend/tests/test_reasoning_retrieval.py)、[问答黄金契约](backend/tests/test_ask_repository_golden.py)。
- **reasoning 原文段引用卡复用本 run 已做过的批量读取（PR #921）。** 原登记在[待办账本](fangan_todo.md)第二节。`_draft_reasoning_response` 的原文段腿原先对进了 prompt 的那批 chunk 再做一次 `tier_map` / `citation_source_info` / `knowhow_refs_for`，而装配合成上下文的 `chunk_context` 已经各读过一次（按节合成每节一次，每节都注入的精确块与几节共用的段落还会逐节重读）。现在一次 reasoning 作答共用一份 `CitationLookups` 备忘（问过的每个键都记，没命中记空）：各节装配、重试、按节失败后的单次回退与引用卡都经它读取，只问备忘里还没有的键，问的键总是旧读取的子集。所以同一个键一个 run 至多读一次，每个读取方法的调用次数和行数都不多于旧路径；仅检索输出的装配不带备忘，读取不变。`chunk_context` 刻意不把 knowhow 读取放宽到多元素段落的首元素：放宽后，被分区追加截掉的那段也会多读一行。KG-only run 这条腿零调用。引用卡、锚点、tier、knowhow 与附图对无图 / 有图 / 联邦多库 / Knowhow / 按节合成 / 按节回退逐字节等于旧路径。`_draft_reasoning_response` 天花板 597 → 564。回归入口：[复用对照、部分命中与行数对照](backend/tests/test_reasoning_citation_lookup_reuse.py)。
- **空笔记本只挂无图参考库也能提问（PR #922）。** 联邦原文通道早已给参与集里每个库各发一条召回腿，但 `NotebookSummary.ask_available` 的参考库判据只认「挂载参考库有可用知识图谱」，于是「当前笔记本零证据 + 只挂了一个无图参考库」被前端锁死输入框、直连 Ask 被拦成 409（与枚举工具开关无关）。参考库那一半现在多一条原文判据：联邦通道这次会搜的挂载库里有可见来源的 chunk。它只数对查看者生效的挂载（M3），只数可见来源（参考库的 Knowhow / Memory 投影不进跨库通道），开关与参与上界取自联邦通道自己的同一个纯函数。前端 `isAskBlocked`、路由 409 预检读的都是这一个字段。它只在单库详情、且本地无证据时求值，是一条与挂载数无关的语句；列表投影不变。合同见[产品/API][product]「无图笔记本」一节的 HTTP 可用性段；回归入口：[SQLite 矩阵与 HTTP 端到端](backend/tests/test_notebook_ask_availability.py)、[PostgreSQL 孪生与计划钉](backend/tests/postgres/test_notebook_ask_availability_pg.py)。
- **全局问答「检索范围为空」提交即拒绝（PR #924）。** 原先八个库全空时这次 run 照样跑完、交回一份没有证据的答案，用户读到的是「没查到」。用户裁决：提交时拒绝，判据同单库。现在 `GlobalAskService` 在解析范围、两次读权复核与冻结来源天花板之后（`_resolve_run_scope`，所以 `start()` 与 `reasoning` 的问题理解预检都受它约束），所有参与库的冻结可见来源都为空时以 409 拒绝，不建会话与作业、不花模型调用；任一库冻结清单非空即放行。组合口径 `local_universe_non_empty` 与单库 `_require_non_empty_scope` 共用，全局的本地证据半边恒为假：全局检索宇宙就是冻结的可见来源天花板，只有 Knowhow 格子、已确认 Memory 或知识对象的库全局检索不到，不得放行（规格评审实测放行后 cited 为空）。判据只看查看者本次可读（MCP 再与白名单取交集）的参与集，点名读不到的库仍是 404；零新增查询。MCP 返回 `unavailable` 与同一句中文；界面提示条对这句拒绝不给「重新加载」。当前合同见[全局问答][global]；回归入口：[空范围准入](backend/tests/test_global_ask_empty_scope.py)及其 [PostgreSQL 孪生](backend/tests/postgres/test_global_ask_empty_scope_pg.py)、[MCP 端到端](backend/tests/test_global_ask_mcp.py)。
- **全局问答的推理动作目录不再提供只查命名锚点库的两条通道（PR #923）。** 原登记在[待办账本](fangan_todo.md)「全局（对等）模式的元素检索臂联邦化」条目的附带部分（联邦化本身另开 PR）。对等（全局）运行里 `search_elements` 与 `ppr_retrieve` 的生产者早已整条关闭，但两个动作仍留在 reflect 的动作目录里，模型选到就白花一轮反思。现在由两把逐动作的 run 级闸（判据 `element_search_offered` / `ppr_retrieve_offered`，各管一个动作，元素臂联邦化后只翻前者）把它们从 prompt 说明、schema 枚举与白名单三处一起摘掉，prompt 里提到它们的对比句、偏好句与 consult_memory 示例清单同步改写；模型硬吐其中之一时走既有非法动作兜底，反思步记 `invalid_action:<动作名>`。登记时引用的那步「概念漫游:跨文档检索,得到 0 段原文」其实来自首轮 PPR 播种而非这个动作，所以首轮播种与它的预取读 PPR 那把判据，对等运行不再播种（PPR 在那里恒返回空，证据零影响）。经验库注入块与 consult_memory 的回想行同步按本 run 的 reflect 白名单过滤（`core_reflect_actions`，与 `allowed_actions` 同一个函数）：推荐了本 run 不提供的动作的行在选择前滤掉、不占名额、不算送达，无图闸、对等闸与档位闸一并适用。单库运行的 prompt、schema 与轨迹逐字节不变（有图、无图各由一份摘要钉住；全闸组合矩阵逐字节对账），动作全在场的单库有图 run 注入块逐字节不变（摘要钉住）。当前合同见[全局问答][global]与[检索模式][retrieval]的无图动作段；回归入口：[对等模式 reflect 动作](backend/tests/test_peer_mode_reflect_actions.py)、[经验行按提供的动作过滤](backend/tests/test_experience_offered_actions.py)。
- **本机热门禁目标放宽到 100 秒（PR #925）。** 原「G1 本机热门禁 ≈77 s，目标 80 s，余量在被吃掉」一条：用户裁决不把 T3-b1（全仓 AST 正向扫描类守卫的真实树扫描）挪到每日 G2——PR 上违反这些守卫的改动继续当场报红——而是把本机 warm gate 目标从 80 秒放宽到 100 秒，60 秒仍是长期愿景。`scripts/check.sh` 传给 `scripts/check_timing_summary.py` 的 `--target` 与该脚本默认值同改为 100，开发文档中英同步，对应用例与文档守卫随之改写。G2 每日扩展门仍不受该时限约束。
- **问答两种模式永久保留，高级界面默认改为 reasoning（PR #925）。** 原「问答方法归一」路线的第 ④ 步「退役 chunk 流水线」取消，通用问答（chunk）与深度分析（reasoning）长期并存；产品决定写入[产品与 API 合同][product]。前端 `DEFAULT_ASK_MODE` 改为 `reasoning`（全局问答仍沿用本浏览器上次显式选择的引擎，没选过才用它），「高级模式」名字不变；MCP 自 #731 起就默认 reasoning，无需改动；省略 `mode` 的 `POST /ask` 仍跑 chunk（兼容客户端不进 reasoning 的意图闸，已写入产品文档）。路线 ①–③ 与 ④ 的前置（`read_document` #724、引用卡与精确席位 #728、无 intent 跟进改写）早已合入，不再另记。
- **全局问答：参与笔记本被删后会话对所有者照常可读，全局 run 的事件带「全局」标记（PR #待定）。** 原登记在[待办账本](fangan_todo.md)「全局问答的删库后可读性与事件标记未做」的 (c)(d)。用户裁决（2026-10-19）：对所有者保留、引用降级，留存期与笔记本内问答的删库留存同一保留期；公开分享照旧逐次复核；事件给全局 run 加专门标记。现在删除笔记本的最终事务（写 `retained_user_activity` 的同一个事务、同一个到期时刻）给 `resolved_notebook_ids` 含该库的全局作业盖 `global_ask_jobs.retained_until`（SQLite v95 / PostgreSQL 0075，后一次删除不延长前一次的窗口）；所有者读取时已不存在的库不再参与「参与库整体可读」，只对盖过戳的轮次放宽——仍存在但本人读不了的库照旧整轮 404（取更保守的一侧，不泄露当前不可读库的内容），未盖戳的缺库轮次、非所有者与带白名单的 Agent token 也照旧 404。作业响应带读时字段 `retained_until` / `deleted_notebook_ids`，前端在读入口把已删库的引用标成不可点的「来源已删除」、去掉其附图，不对已删库发任何读请求；每轮下方写明保留期限。`retained_until` 是强读闸（所有者读取、会话列表、公开链接、管理员总览/活动流/提问分析、学习链取样），到期后由启动恢复或下一次删库物理清理，清空的会话一并删除、自动标题移到剩下最早一轮。全局 run 里的每条事件经 `EventLogger` 补 `scope: "global"`（`global_ask_run` 装槽，`notebook_id` 原样保留），`diag_retrieval_latency.py` 把全局 run 的 run 总计单列。合同见[全局问答][global]「参与笔记本被删除之后」；回归入口：[双后端留存契约](backend/tests/global_ask_retention_cases.py)（[SQLite](backend/tests/test_global_ask_retention.py)、[PostgreSQL](backend/tests/postgres/test_global_ask_retention_pg.py)）、[前端降级](frontend/tests/component/global-ask-retention.component.test.tsx)。
- **已确认意图 run 的首轮词法臂改由问题理解产出关键词（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第二节「中文问句检索英文语料首轮空」的三条次因之一：正式界面永远带着已确认意图，`ReasoningRetriever` 跳过 `plan()`，而首轮原文播种的词法臂只读 `plan()` 里 `expand_query` 的高/低层关键词，结果高级界面与简化界面的 run 都只剩语义臂。按用户裁决（2026-10-19），不重跑 `plan()`、不加模型调用：问题理解那一次调用在词法臂本 run 能跑时多产出与 `expand_query` 同形的 `high_level_keywords` / `low_level_keywords`（复用同一条语料语言双语化规则 `keyword_language_rule`；单库取本库语言，全局问答在参与集覆盖下取并集），连同 `keyword_basis` 摘要随合同往返；确认后按 `plan()` 同一拼法喂给词法臂。三个入口（笔记本 `/ask/intent`、MCP `ask` 调用内理解、全局问答理解预检）共用 `AskService.preview_reasoning_intent`。编辑规则取最保守的一种：改过确认措辞、回答过澄清、或合同的问题/子问题/检索方向与 `keyword_basis` 对不上，确认时整组作废、词法臂不跑（即此前行为），不做确定性分词兜底。词法臂关闭时理解 prompt / schema 逐字节不变、合同只多带两列空列表与空 basis，深度报告的理解从不要关键词；`plan()` 路径的合并结果不变。首轮词法召回改为与向量召回同池并发、各占召回闸一个席位（闸宽按 N+1 条召回算，上界仍是 8 // P），合并顺序不变——此前它在向量臂收齐后串行发起，确认意图路径默认多付的一次 FTS 因此不再叠加在首轮墙钟上，`plan()` 路径同样受益。语言探测抽成 `probe_corpus_langs` 两处共用（参与集座位 fail-soft 登记随之改名），`subjectless_run_active` 不再为回答「是否无主体」而构建尚未构建的懒天花板（默认天花板恒非无主体），问题理解仍零天花板读取。对其它 `plan()` 关键词读者的盘点：仓库里只有首轮播种词法臂读它；深度报告带确认方向的节同样跳过 `plan()`（没有方向的节照跑 `plan()`、照有词法臂），但报告理解是整份报告一份、关键词对不上逐节方向，刻意不接，报告理解 prompt 逐字节不变。回归入口：[契约、编辑规则与三入口](backend/tests/test_intent_keyword_arm.py)、[推理检索词法臂](backend/tests/test_reasoning_retrieval.py)、[提示投影快照](backend/tests/test_model_schema_hint_snapshots.py)、[默认天花板入口](backend/tests/test_default_ceiling_entrypoints.py)。
- **`read_document` 同名篇按序号消歧（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第二节（PR-A 延后项 (a)）。动作参数就是标题，花名册里同名的第二篇过去只能被 skip。现在仅当本 run 的花名册（所有来源清单按 `source_id` 折叠后的整体，与解析域同一份，跨库不按库分别判定）里出现同名文档时，反思轮账目把这几篇列成「标题（第 N 篇）」（按首次列出顺序发号、发号后永不改，每一篇都带），模型原样回传即读到对应那篇；动作参数形状不变，无同名时账目与解析逐字节不变。发号簿是 `_ReasoningRunState.roster_numbering`（`_RosterNumbering`，惰性 `sync(enum_chains)` 增量吃新行），账目、标题解析、已读账目、读取轨迹摘要、`enumerate_elements` 来源名参数（占位串先翻成花名册里那篇的 id）共读同一份；较早清单链续跑插队、或后到真实标题恰等于已发占位串都不改号（后者两者都命中 → 歧义 skip）。同名按账目可见形态判（折叠空白、截 60 字、trim + casefold），60 字之后才不同的两篇也各得序号；占位串发号时避开当时已知的所有真实标题（撞了换「（同名第 N 篇）」「…·k」）；多篇无名文档仍共用一个占位串、不编号（保住账目「最多占一个标题位」的 codex #724 R2 规矩）。不带序号的裸标题仍是歧义 skip，文案改为教模型连序号一起抄。`_roster_placeholders` / `_ambiguous_read_summary` 在 `services/reasoning_retrieval.py`，用例 `tests/test_reasoning_document_read_duplicate_titles.py`。
- **全局问答的表格分析臂联邦化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第二节「全局（对等）模式的表格分析臂只覆盖名义 active」（D0-5 已关掉泄漏那一半）。用户裁决（2026-10-19）：联邦化、工作簿总数封顶。对等模式下表格臂不再读鉴权级座位 `ask_engine_participant_notebooks`（它必须保持真实挂载谓词），改走新入口 `chunk_federation.federated_library_sources`：与原文段落通道同一个 `_bounded_participants`（参与集、上界、回退开关）和同一份逐库可见来源读取（同一个 run 级 memo）；每库仍按冻结天花板收窄（D0-5 规则保留，只减不增）。所有参与库的工作簿合并成一份目录、只做一次规划、只执行一张表；目录与单库共用同一份字节预算，按「笔记本 id 排序 + 问题摘要定轮转起点 + 逐轮每库一份」填充，工作簿逐份读入、预算一满即停读，所以规划次数、目录长度与 manifest 读取份数都以单库上界为顶（实测 8 库 × 64 份时读 183 份、prompt 36.7KB，与单库饱和值相同），装不下的份数在规划提示、结果卡与轨迹里如实披露；只有一个库提供工作簿时目录与单库逐字节相同。两个及以上的库提供工作簿时目录每项带所属笔记本名（计入同一预算），合成表格块与轨迹摘要也标库名，结果卡在表格来自别的库时按引用卡口径标「来自「库名」」；逐库来源读取沿用原文通道的取消检查与单库预算，单个库失败或超时只丢该库并记入轨迹，原文通道本 run 已准备失败的库不再重读。表格意图闸按用户裁决把「比较」「对比」加进分析词、把一组封闭的「…表」名词加进表格上下文（单库同样生效）。单库（含挂载）路径逐字节不变，由规划提示摘要、manifest 读取序列与结果摘要钉住。合同见[产品/API][product]「全局问答」与「Excel 专业分析与解析问题自动归档」两节；回归入口：[联邦表格臂与单库逐字节钉](backend/tests/test_spreadsheet_federation.py)、[PostgreSQL 孪生](backend/tests/postgres/test_spreadsheet_federation_pg.py)。
- **全局问答的 👍/👎 进管理端提问分析与笔记本知识分析看板（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第二节。用户裁决：统计侧跨表聚合，不镜像写 `feedback` 表；只加一条索引迁移（SQLite v96 / PostgreSQL 0076 的局部表达式索引 `idx_global_ask_jobs_feedback`，只收录有评分的全局作业，写入路径与存储形态不变）。看板的 👍/👎 与有用率同时读 `feedback` 表与归入本库的全局评分——一条全局评分对它检索到或引用到的每个库各计一次（两者皆空归入参与库减被跳过的库），与问答后学习链同一口径 `global_ask_attribution.attribution_sql`；数字后注「（其中全局问答 N 条）」，提问数标为「笔记本内提问」（只数本库笔记本内回答，全局回答归入另行处理）；「低分提问（知识缺口）」只列笔记本内提问原文，末尾注「另有 N 条来自全局问答」，全局提问原文不上屏、响应不含其他参与库的 id 或库名。管理端提问分析汇总卡片新增「有用」「需改进」，按当前筛选统计、一条全局问答只算一次，注「其中全局问答 N 条」；深度报告与删库后的留存投影不带评分（`feedback` 行随删库删除）。两处都只读有评分的行：看板每条已评分的全局作业解析一次 payload，管理端按索引条件取已评分 id、不解析 payload；列表页查询的 SQL 逐字不变。合同见[产品/API][product]的全局问答「记录口径」、提问分析与 `GET /api/notebooks/{id}/analytics`；回归入口：[SQLite](backend/tests/test_global_ask_feedback_counts.py)、[PostgreSQL 孪生与计划钉](backend/tests/postgres/test_global_ask_feedback_counts_pg.py)、[看板组件](frontend/tests/component/analytics-answer-quality.component.test.tsx)。
- **全局问答的原文元素检索臂逐库联邦化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)「全局（对等）模式的元素检索臂联邦化」（其附带部分即上面 PR #923 那条）。用户裁决（2026-10-19）：做，形态照抄全局关键词臂。对等（全局）运行里 `retrieval_candidates.retrieve_elements` 不再整条返回空，而是对每个参与库各跑一次单库同款元素检索（`_retrieve_elements`，天花板为该库冻结天花板 ∩ 可见清单，声明为上下文天花板，恒走有界的段落回表路线），借语义腿同一套联邦扇出（`chunk_federation.federated_element_candidates` → `_run_supplement_arm`：共享执行器、公平窗口、整条臂一份逐库预算、参与集同 `_bounded_participants` 的 8 库上限）；合并按轮次、轮内按分数（不偏名义 active），去重，总数以单库元素上限封顶（每次 8 条）；每条元素打上所属库（`RetrievedElement.notebook_id`，单库路径恒空串）。补召回腿不开熔断（`_CHUNK_SUPPLEMENT_LEG`：它的全文超时只遵守、不打开共享的按库 FTS 熔断）、失败只记 `arm="element"` 的 skip 事件，不进覆盖回执、不上横幅（腿内经真实通道失败的模型调用——查询向量、ANN、唯一全文臂——只写 `model_error` 事件，回答横幅 sink `_ASK_MODEL_ERRORS` 在腿内换成丢弃列表）；交出去的那批元素按读到的全文`attest_read`（producer `element_search`）登记检索时刻证据。引用卡（`element_context` / `element_citations`）的 `notebook_id` 与 `tier` 按元素所在库，结果边界 `filter_retrieval_items` 的元素分支改按元素自己的库判天花板；单库元素一律回退传入的库，逐值不变。reasoning 侧 `element_search_offered` 改为按元素通道是否可用判定（检索层 `element_arm_available`，同一句话供生产者短路），对等运行默认重新提供 `search_elements`，`_element_search_skip` 的 peer_mode 跳过与 `_first_round_empty_fallback` 的补腿闸同读这一把；对等运行的 `search_elements` 外层不取扇出槽（联邦腿在叶子上取）。引用核验守卫的元素条目由「全局不可达」改登记为 read-registered（`_ELEMENT_ARM_PROOF`），出处守卫改为 stamped-downstream。开关 `GLOBAL_ASK_ELEMENT_ARM_ENABLED`（默认 `true`）关掉即回到联邦化之前。当前合同见[全局问答][global]；回归入口：[对等模式元素臂](backend/tests/test_peer_mode_element_arm.py)、[真引擎端到端](backend/tests/test_global_citation_producers_e2e.py)、[检索时刻证据竞态（含 PG 孪生）](backend/tests/test_producer_attestation_races.py)。
- **公共库晋升来源标题改为「收录自」，存量读时映射，纳入词汇守卫（PR #待定）。** 原登记在整改计划 §13.2 第 2 条（「晋升自」用词）。用户裁决（2026-10-19）：合成来源标题「晋升自：<原标题>」／「晋升自个人记忆：<记忆标题>」改为「收录自：<原标题>」／「收录自个人记忆：<记忆标题>」，存量标题不改库内数据、读取时映射，并去掉“标题是数据不是界面文案”的豁免。新写入由 `app/domain/promotion_source.py` 的 `PROMOTION_TITLE_PREFIX` / `MEMORY_PROMOTION_TITLE_PREFIX` 生成；封版迁移（SQLite v88 / PostgreSQL 0068）仍写旧措辞，所以旧前缀留作 `LEGACY_*` 常量，迁移用例改钉它们。读取汇聚点是同一个 `presented_source_title`（只替换标题开头的前缀，且只对类型为 `promotion` 或来源 id 带 `src-promo-` 的来源生效，用户自己上传、恰好叫「晋升自：…」的文档与正文里的「晋升」都不动），经 `source_display_title`（显示名、枚举、检索候选、MCP 来源输出）、`SourceSummary` 的 validator（来源列表/详情/代理读取详情）、`source_titles`、笔记本搜索的来源与元素命中、活动记录（含删库后留存的快照）、分享预览的来源标题和直接元素检索回退，两个后端同形；引用卡按标题前缀取原件标题的逻辑新旧前缀都认。来源列表筛选与笔记本搜索（来源/元素两条腿）搜「收录自」同样命中存量：查询词含「收」或「录」时多加一条按映射后标题匹配的分支（`app/repositories/promotion_title_sql.py`，只对晋升来源，两后端同形），其余查询的 SQL 与计划逐字不变（PG 以 30000 来源实测 EXPLAIN 对比：同一 Seq Scan、同缓冲区；含「收录」的查询 9.3→11.1 ms 且从漏掉变为命中）；搜索命中的摘要回退用映射后标题。界面词汇守卫新增第七条通道，扫前缀常量的值并按名字集合判扫描面（`LEGACY_*` 不扫），写回「晋升自」即红。[产品/API][product]与运维文档（`docs/operations.md` / `_zh.md`）中英同步，[界面词汇表](docs/ui-vocabulary.md)（仅中文）去豁免并说明读时映射。
- **挂载对系统管理员不设旁路：核实已与读守卫同口径，只改文档（PR #待定）。** 原登记在整改计划 §13.2 第 1 条（#828 遗留：`mount_sql` 的读权支不含系统管理员旁路，疑与路由层口径不一致）。核实结论：路由层同样没有管理员旁路。读守卫 `require_notebook_read` → `user_can_read_notebook` → `access_sql.NOTEBOOK_READ_SQL` 与挂载查看者支用的是同一个 `read_access_clause`，都不读 `users.role`；全局问答勾选集与 MCP 读权复核也走 `user_can_read_notebook`。实测管理员直接打开、列出、提问他人私有库一律 404。用户裁决（2026-10-21）：不加旁路，只更正文档。[产品/API][product]「挂载只对能读被挂库的人生效」中英同步补一段「系统管理员没有挂载旁路」，整改计划 §13.2 第 1 条与 PR-E6 落地记录标为已决并附证据链。代码与行为不变，不写发布说明。
- **跨环境同步设计文档 §11 的四条未决问题按用户裁决落地（PR #待定）。** 原为[同步设计文档](docs/incremental-sync-design.md)「§11 未决问题」前四条，待办账本未登记。用户裁决（2026-10-19）：(1) 群组按名字匹配足够、不建显式映射表，导入从不建组——本次导入新建镜像的群组授权要用到的组在目标端缺失、目标端重名或包内重名时，导入在认领前（dry run 同样）拒绝并报出组名，只被已有镜像授权引用的组不挡导入；原「本包建过的组」进度标记及其取代时的转移、报告的 `groups_created` 一并删除。(2) Knowhow 编辑历史（`knowhow_milestones`/`knowhow_changes`）默认不同步，导出端 `SILICON_NOTEBOOK_SYNC_KNOWHOW_HISTORY` 打开才带；包在 `manifest.knowhow_history` 声明，导入只走包带的表，关时目标端已有历史不删不改（全量对账也不碰），父行消失时随外键级联；历史完整与否按链记在 `sync_imports.report_json`，关后再开的窗口标「不完整」并提示跑一次 `sync export --full`。(3) 导入器给带过来的每条记忆在 provenance 顶层写 `sync_origin=<源环境>`（不加迁移；不用「来源回答在本环境不存在」判据，因为本地记忆删对话后同样悬空），记忆面板「来源与依据」据此显示不可点的「来源回答：在 <源环境> 中」。(4) 用户名先精确、再按 ASCII 忽略大小写匹配，忽略大小写后多候选或两个源用户落到同一目标用户则拒绝并报出。评审跟进：组只带 `(id, name)`、成员行恒空，身份映射与建号只针对会被写入的行引用的用户；包格式升到 3（本构建仍读 2，两端须一起升级）；历史不完整时认领前预检二级唯一键；续跑时点名旧构建建过的组。当前合同见[同步设计文档](docs/incremental-sync-design.md) §4 与 §11「已决」、[运维手册][ops]跨环境同步一节、[部署配置][deploy]；回归入口：[全量导入](backend/tests/test_sync_import.py)、[增量导入](backend/tests/test_sync_incremental_import.py)、[身份映射纯函数](backend/tests/test_sync_manifest.py)、[PostgreSQL 全量](backend/tests/postgres/test_sync_import_pg.py)与[增量](backend/tests/postgres/test_sync_incremental_import_pg.py)泳道、[前端记忆来源行](frontend/tests/unit/memory-model.test.mjs)。
- **表格分析一次规划执行多张表（PR #待定）。** 全局问答的表格分析臂联邦化之后一次规划仍只执行一张表，「比较 A、B 两张报价表」只出一张结果卡。用户裁决（2026-10-19）：一次规划支持执行多张表，最多 2–3 张（上限取 3，作配置项 `SPREADSHEET_ANALYSIS_MAX_TARGETS`，默认 3、可配 1..3），仍只做一次规划调用，单库与全局都受益。规划回复改为 `targets` 列表（JSON hint 与严格投影同步：`targets[].filters` 沿用标量单元格合同，投影快照更新），输出上限按项数放大为每项 500 token；每项单独校验，非法的一项只丢它自己、不拿别的表顶替；同一张表可以做几项不同分析（用户裁决 2026-10-21），各占一个名额、各出一张卡，只有算的东西完全相同的项（同表、同操作、该操作实际读的参数与筛选都相同）才去重，这几张卡另带参数读法 `analysis_label`（卡片与合成块标题行 `plan=` 据此区分同表同操作的两张卡，只做一项的表不带）；超出上限的按规划顺序截掉并在最后一张卡上如实披露、逐项写出未做的分析；一项可用都没有时与单个规划非法时一样回退本地概览；旧的单对象回复仍按一项读取。每张执行的表一张结果卡、按规划顺序，全局问答里各卡各标所属库；没有合并对比卡。合成表格块总预算不变：先放每张表的标题行、再放每张表的表头行（宽表头挤不掉后面表的标题行，预算变大也不会少列表），再逐轮每表一行，单表时与旧的贪心顺序逐字节相同；表格键 `k6001` 起按规划顺序编号。顺带修掉一处既有撞号：插件带回的库外证据与表格结果同在 6000 号段、都从 `k6001` 起，两者同时出现时合并映射会用库外证据覆盖表格回执——现在库外证据接在表格键之后编号，没有表格结果时不变；同一工作簿两张工作表的来源级引用也不再被去重成一条。单张表时结果卡、轨迹步、合成表格块与可引用映射逐字节不变（基线提交上钉的摘要，旧单对象回复与一项列表回复都对上）；规划提示只改了指令句，问题行与目录逐字节不变。合同见[产品/API][product]「Excel 专业分析与解析问题自动归档」；回归入口：[多表规划、预算分配与单表逐字节钉](backend/tests/test_spreadsheet_multi_table.py)、[多卡渲染与来源库标注](frontend/tests/component/answer-knowhow-results.component.test.tsx)。
- **SQLite 长期保留：双后端并存写进架构文档，能力对等矩阵可校验（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「工程卫生」（没有双后端能力对等矩阵，做之前先决定 SQLite 的支持范围）。用户裁决（2026-10-19）：SQLite 长期保留，支持离线、单机与测试，PostgreSQL 仍是规范定义；2026-07-22 写下、从未执行的退役计划状态头标为「已放弃」，「不做退役」登记在待办账本「产品与守卫裁决」组。[架构文档][arch] 新增 2.2.2 节：产品决定、支持范围、新功能的双后端义务（存储层改动同一 PR 两侧都做、PostgreSQL 先定口径、只为性能的结构可只做 PostgreSQL 但要登记），以及按代码逐项核实的能力对等矩阵（词法检索、向量、JSON 索引、并发写、跨进程协调、读预算、计划钉、大库 scale 构建、增量同步、两侧专属运维与迁移方向，每行附 `文件:符号` 出处）。[开发文档][dev] 中英同步写入双后端义务，[部署文档][deploy] 中英同步写明 SQLite 支持范围，根 README 当前边界一句随之改写。新增 G1 守卫 `test_dual_backend_parity.py`（不连数据库）：schema 版本在 SQLite 迁移器、PostgreSQL manifest 与 shadow 配对三处同值且两侧迁移连续编号；每个 bundle 座位两侧实现端口全部成员；两侧类级公开成员集合之差恰等于登记的后端专用辅助成员；矩阵「出处」逐条可解析、能力名不重复（只证明出处存在，改变被引用符号行为的 PR 要人工复核对应矩阵行）。矩阵也写明同步包格式与后端无关、笔记本级内容可跨后端双向引入，整库迁移与正向 shadow 只有 SQLite → PostgreSQL。代码与行为不变，不写发布说明。回归入口：[双后端对等守卫](backend/tests/test_dual_backend_parity.py)、[架构文档契约](backend/tests/test_architecture_documentation.py)。
- **知识分析看板的「提问」计入归入本库的全局回答：全局问答作业的物理归因表（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第二节（#935 登记）。用户裁决：给全局问答作业加物理归因、作业完成时写入、存量回填、建索引，使计数成为索引查询，标签改回「提问」。现在 SQLite v98 / PostgreSQL 0078 新增 `global_ask_job_notebooks(job_id, notebook_id)`（复合主键、`job_id` 外键级联、`(notebook_id, job_id)` 索引）与 `global_ask_jobs.attributed`（默认 0），以及只收录「已完成且未归因」作业的部分索引 `idx_global_ask_jobs_unattributed`。两端选同一个子表模型而非 PG `text[]` + GIN（偏离建议，理由：SQLite 没有可索引的数组，同一模型让双后端共用的 `GlobalAskStore` 只写一份，btree 精确命中不需复核，删作业（留存清理、删会话）级联带走行）。`GlobalAskStore.save` 在把作业落为 `done` 的同一事务里写 `attributed = 1` 与归因行，内容取自 `global_ask_attribution.job_attribution`——完成钩子 `_note_completed` 改调同一个函数，失败/取消/中断不写。存量由服务就绪后的一次后台回填自动补写（与 `memory-orphan-sweep` 同位置，启动日志写出 `attributed=0` 计数），`scripts/backfill_global_ask_attribution.py`（`maintain global-attribution`）留作手动补跑：它以 `migrate=False, seed=False` 组装、不迁移不 seed（不会改写 admin 密码），库未到 v98 / 0078 即拒绝（退出码 3）。两条路径共用 `run_backfill`：按 id 分页、每页一个事务，PG 上 `FOR UPDATE SKIP LOCKED` 先拿锁、从不等待，所以不会与删库的 `stamp_retained_turns` 成环（#931 同类），被跳过的行留给下次并在 `pending_after` 报出，`40P01` 整页重试；读不回 `GlobalAskJob` 的 payload 只报告、继续走旧判据。看板 `answers_total` 加上归入本库、已完成、未过留存期的全局作业（新字段 `answers_global`，前端「提问 N（其中全局问答 N 条）」）：已归因的走子表索引，未回填的走 `attribution_sql`（PG 与参与库 GIN 求交），两臂互斥，回填前后数字相同。实测（全站 5000 个作业）：本库参与 50 时 PG +1.2 ms（未回填）/ +0.3 ms（回填后）、SQLite +34 ms / +0.04 ms；本库参与 1500 时 PG +23 ms / +3.2 ms、SQLite +34 ms / +0.33 ms；写入路径 PG +0.05 ms。👍/👎 计数与学习链取样刻意不改读新表：取样要读运行中/已停止作业的轨迹（新表只收已完成作业），评分计数已由「有评分 ∩ 参与库」两条索引限定，改读新表测不出收益却多一条回填期双路径。对拍用例逐行钉住归因行与 `attribution_sql`（每个作业 × 每个库）。合同见[产品/API][product]全局问答「记录口径」与 `GET /api/notebooks/{id}/analytics`；运维见[运维文档][ops]「全局问答归因回填」；回归入口：[SQLite](backend/tests/test_global_ask_attribution.py)、[PostgreSQL 孪生、锁交错与计划钉](backend/tests/postgres/test_global_ask_attribution_pg.py)、[共享用例](backend/tests/global_attribution_cases.py)、[看板组件](frontend/tests/component/analytics-answer-quality.component.test.tsx)。
- **整本深拷贝 `NotebookCopyService.copy_notebook` 拆分，零行为变化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「结构」的第二梯队大函数拆分（其余三个函数仍在那一条里）。621 行的方法按阶段抽成私有 helper：哨兵与挂载、来源四表、Knowhow 业务表与资产、元素与分块、知识对象、副本本地生成代、来源事实三表、关系、分块与向量派生行、失败补偿、发布后的投影调度；跨阶段的重映射表与「本次写过哪棵目录」两个旗标放在一份 `_NotebookCopyRun` 上按同一对象传递，id、时钟与分块大小仍在开头各读一次。`copy_notebook` 本体 83 行，天花板 621 → 83；新 helper 最长 87 行，不进热函数表。拆分前先提交了两端（SQLite / PostgreSQL）共用世界的表征：12 个场景的完整观测（对 store / catalog / 调度 / 文件系统的每次调用与参数、id 铸造与取时钟、本线程每条 SQL、整库逐表增删差分、存储目录差分、日志与返回值）在拆分后逐字节不变，随后按 AGENTS.md 退役，长期回归由同一组场景（另补一个「源目录不存在、目的地目录已存在」的补偿场景）的行为断言承担。回归入口：[SQLite 场景](backend/tests/test_notebook_copy_characterization.py)、[PostgreSQL 孪生](backend/tests/postgres/test_notebook_copy_characterization_pg.py)、[世界与断言](backend/tests/notebook_copy_characterization_cases.py)。
- **Python 依赖带哈希锁定，运行依赖与测试依赖分开（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「工程卫生」的「Python 依赖未锁定」。用户裁决（2026-10-19）：加锁文件。`backend/requirements.in`（运行）与 `backend/requirements-dev.in`（pytest、pytest-xdist、matplotlib，`-c requirements.txt` 约束到运行锁）保留版本范围，`scripts/lock_python_deps.sh` 用钉死版本的 pip-tools 在 CPython 3.13 下 `--generate-hashes` 生成 `backend/requirements.txt` 与 `backend/requirements-dev.txt`；首份锁沿用 `3132d4667` 那次 master CI 实际装上的版本，唯一例外是 `pymupdf4llm`：自 PyPI 于 2026-10-19 发布 2.0 以来的全新安装（含 master CI）装到的都是这个没有模块的空壳占名包，`parsers.parse_pdf_python` 的 fail-open 兜底因此一直把非 MinerU 的 PDF 静默退回 pypdf 纯文本（本 PR 之前就已存在）；现在 `.in` 限制 `<2`，锁钉 1.28.2 及 PyMuPDF / pymupdf-layout / onnxruntime 等七个依赖，后端环境约增 200 MB。哈希覆盖 PyPI 每个文件，macOS 与 Linux 共用一份锁（两平台的依赖标记只在未请求的 extra 上不同），3.14 也能按同一份锁安装；只支持 macOS / Linux，Windows 用 WSL。CI 三个 job 与每日三个 job 改为 `pip install --require-hashes -r 两份锁`，缓存键随两份锁的哈希变；`scripts/prod.sh` 按运行锁 `--require-hashes` 安装；`scripts/pack.sh` 按运行锁 `pip wheel --require-hashes` 并写 `wheelhouse/locked-wheels.txt`（就地编译的 hnswlib 记本机文件哈希），`packaging/install.sh` 离线按它、在线按运行锁带哈希安装。升级只走 `--upgrade-package` / `--upgrade` 显式命令。离线门禁 `backend/tests/test_python_lockfiles.py` 比对每份锁记下的 `.in` 指纹并逐条核对钉版范围、哈希、来源与两份锁的共享包，改了 `.in` 忘了重锁即红，并沿已安装包的 `Requires-Dist` 检查传递依赖是否都在锁里；`test_third_party_imports_resolve.py` 导入 `backend/app` 与 `scripts/` 的全部第三方模块（含函数内懒加载）并核对用到的名字与属性、要求发行包在锁里，`test_parsers_pdf_python.py` 不打桩用真 pymupdf4llm 转换一份 PDF。
- **前端 ESLint 进门禁，存量记基线只降不升（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「工程卫生」：devDependencies 早有 `eslint` 与 `eslint-config-next`，但没有配置文件，`npm run lint` 实际是 `tsc --noEmit`，门禁与 CI 都不跑 ESLint。用户裁决（2026-10-19）：用 Next 自带规则集加 react-hooks 规则，存量先记基线、只拦新增。现在 `frontend/eslint.config.mjs`（flat config，经 `FlatCompat` 载入 `next/core-web-vitals` + `next/typescript`）把所有规则一律定为 error，`rules-of-hooks` 与 `exhaustive-deps` 都是 error；`npm run lint` 是 `eslint --max-warnings 0 .`，原来的 tsc 改名 `npm run typecheck`（`pretypecheck` 接替 `prelint` 同步插件投影），`next build` 设 `eslint.ignoreDuringBuilds` 不再自己 lint。存量 66 条违反按「文件 + 规则」记进 `frontend/eslint-suppressions.json`（ESLint 的 bulk suppressions：超出与过期都失败），`eslint-baseline-guard.test.mjs` 零松弛钉住每条规则的条数与文件数、要求条目文件都存在、`rules-of-hooks` 永不进基线。基线之前先零行为变化地清了一批：未用的 import / 类型 / 局部绑定与参数 24 处、一处 `prefer-const`、三条失效的 disable 注释（其中两条放错了行，从未生效）、九条 `rules-of-hooks` 误报（测试 helper `useKnowhowPanelSuite` 只注册 vitest 钩子，改名 `registerKnowhowPanelSuite`）。`check_frontend.sh` 把 lint 作为独立一组并行跑（冷跑约 5 秒），CI 经同一条 `check.sh --lane frontend` 自动覆盖。做法与降基线流程见[开发文档][dev]「前端 ESLint 门」。后端 lint 不在本项范围，另记一条留在待办账本。`npm run lint` 改名会让仓库外仍按旧用法做类型检查的脚本静默变绿，所以写了一条给管理员看的发布说明，部署扩展 SOP 中英文也加了改名提示。
- **退出群组前告知会看不到的笔记本与记忆，允许先导出（PR #待定）。** 原登记在整改计划 §13.2 第 8 条（本人退出群组：记忆不删，但本人看不到）。用户裁决（2026-10-21）：退出确认弹窗告知「退出后有 N 个笔记本、共 M 条个人记忆将看不到」，允许先导出；记忆不删除，重新加入后仍可见。核实：退群（`group_store.remove_member`）只删一行 `group_members`，两个后端都不碰记忆。新增 `GET /groups/{id}/membership/leave-preview`（只答本人、非成员 404、无系统管理员旁路），两个后端 `memory_store.group_exit_memory_preview` 一条语句：本人记忆按笔记本计数，读权谓词 `read_access_clause` 此刻为真、而 `access_sql.read_access_after_group_exit_clause` （同一组片段拼成，只在两条 `group_members` 子查询里剔除本人在该组那一行）为假的才列出——经别的群组、成员关系、个人授权、everyone 授权或本人是主人仍可读的都不算。契约用例对世界里每一行组成员关系逐格比「谓词预测」与「真删掉那一行之后的 `NOTEBOOK_READ_SQL`」，两后端同一套场景（`tests/group_leave_preview_cases.py`，SQLite / PG 孪生）钉住预览等于退出后读权真翻转、从记忆列表真消失的那几本，且退出后行数不变、以组管理员身份重新加入后原样可见，以普通成员身份（邀请链接）重新加入时只授权给组管理员的那本仍看不到。群组页「设置」里的「退出群组」由行内确认改为确认面板（与退出共享同一套落点、焦点与无退路契约）：逐本列出名字与条数，每本一个「导出」复用 `GET /notebooks/{id}/memories/export`，导出进行中不能确认；M 为 0 时如实说明、不出现导出入口；在途的退出与失败原因按群组记、在途的导出按笔记本记在群组页那一层；告知读不到不放行确认，回 404 时说「你已经不在这个群组里了」并刷新清单；退出失败的原因落在面板里，面板已取消时落在那个组的「退出群组」按钮旁。导出文件的说明行补上「退出群组不会删除它们」。[产品/API][product]中英同步，整改计划 §13.2 第 8 条标为已决，写发布说明（internal）。
- **后端 ruff 进门禁，存量记基线只降不升（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「工程卫生」的「后端无 lint 门禁」（最早附记在「前端无 ESLint 门禁」一条里）：后端没有 pyflakes / ruff 一类的 lint，门禁与 CI 都不跑。用户裁决（2026-10-22）：照前端 ESLint 门的思路，ruff 加只降不升的存量基线。现在 `ruff==0.16.10` 钉在 `backend/requirements-dev.in`（测试锁只多了 ruff 一项），仓库根 `ruff.toml` 只开 lint 规则与 import 排序：pyflakes（`F`）、`E9`、`E722`、`PLE`、flake8-bugbear 里指向疑似 bug 的 26 条（可变默认值、`finally` 里跳转、重复/空异常元组、`pytest.raises(Exception)`、无意义比较等），以及 `I`（用户 2026-10-22 裁决：开 `I`、不开 `UP`；`UP` 6324 条几乎全是注解改写，`UP042` 还会改 `str()` 结果）；`B008`/`B904`/`B905`/`B007`/`B009`/`B010`/`B023` 刻意不开。`I` 的排序器设置照现有写法定（`src = ["backend", "scripts"]` 让 `app`/`tests`/脚本同级模块算第一方，`line-length = 120` 只给排序器用），存量 1395 条（1009 个文件）只记基线、一个 import 都没重排；排序器不跨 `sys.path.insert` 这类语句移动 import，新违反只要求新写的 import 放对位置。`scripts/check_backend_lint.py` 在 contracts lane 里检查 `backend/app`、`backend/tests`、`scripts` 下 git 看得见的全部 Python 文件（不排除任何文件，耗时不到 1 秒），按「文件 → 规则 → 条数」对比 `scripts/backend_lint_baseline.json`：超出与过期都失败，ruff 版本或受检文件集合不符也失败；`--prune` 只降不升。`backend/tests/test_backend_lint_gate.py` 零松弛钉住每条规则的总条数与文件数、钉死 `ruff.toml` 与受检范围、按规则钉住点名受检规则的 `# noqa`（禁止不带规则码的、文件级的豁免与一切 `# isort:` 指令），`F821` 等永不进基线；过期报错与 `--prune` 会打印上限该降多少，`--prune` 也清掉已删文件的条目，改名用 `--rename OLD NEW` 搬条目；行级豁免的条数由门禁按 git 列出的同一批文件统计、与守卫两处钉住；守卫按去掉注释后的有效命令行核对 contracts lane 恰好跑一次门禁（注释掉、`|| true`、挪到 `exit` 之后都红）；没装 ruff、没有 git、仓库外路径与符号链接都给出可读报错（退出码 2）。基线之前先零行为变化地清了一批：11 处 `F821`（都是注解里的未定义名，补导入；`ports` 那处因 repositories 不许新增指向 services 的导入边改注 `object`；`scale_index_builder` 两个引用随后 `del` 掉的名字的 lambda 换成 `functools.partial`）、48 个未用 import（只删全仓别处既不再导出、也不作 monkeypatch 或字符串目标的，fixture、子模块、`try` 内与 `__init__` 的一律不动）、未用局部变量、重复导入与一个被同名 staticmethod 覆盖的死方法、无占位符的 f-string、一处重复字典键、三处源码里的不可见字符改写成转义、两处漏写 `assert` 的无效比较（B015）补上断言；`AskService._run_reasoning_stage` 的函数长度上限随之降到 513。存量 2068 条进基线（`I001` 1395 条；`F811` 402 条几乎都是导入 pytest fixture 再按参数注入，`F401` 242 条多为再导出与副作用导入）。做法与降基线流程见[开发文档][dev]「后端 lint 门」。纯开发工具改动，不写发布说明。
- **本人可以手工合并本人的两条记忆派生知识对象（PR #待定）。** 原登记在整改计划 §13.2 第 9 条（#824 起任何涉及调用方自己 Memory 派生对象的合并都 409）。用户裁决（2026-10-21）：两条对象都只属于同一人、且两份证据的每一条都出自他本人在本库的记忆来源时允许合并；判据复用 `memory_sql` 的 `memory_derived_object`、`foreign_memory_object_excluded`（别人的与孤儿记忆照旧 404）与 `own_memory_source`，规则仍只在 `app/domain/memory_kg_isolation.py` 的 `memory_merge_refusal` 一处，`SAME_OWNER_MESSAGE` 删除。结果保留目标对象的记忆来源，所以仍只对本人可见、证据只含本人记忆条目；跨类、或任一方证据夹着普通来源 / 他人记忆 / 缺来源 id 的条目照旧 409「…不能与共享的知识对象合并」，系统管理员无旁路。「删 / 改一条记忆只删到它自己的对象」由两处维持：删除、转移、成员退出本就先摘证据（`strip_sources_evidence_on`），编辑后重抽（`ingest_memory_source`）新增同一次摘除（经 `memory_evidence_detach` 接线，在源锁下、清旧产物之前）；这次摘除同时修复了一个既有缺口：隔离上线前留下的混合合并对象，原先会在编辑记忆时被按证据引用连带删除。反方向按用户裁决（2026-10-21）：拥有合并结果的那条记忆被编辑、转移或删除而清掉合并结果之前，把直接并进来、仍是弃用、且其记忆仍在的对象恢复为 approved，重新进入检索；记录在被并走对象自己身上（本人记忆对之间合并时，被并走对象的 payload 写入隐藏键 `_merged_into` = 目标 id；以 `_` 开头的键不展示、不进检索；再被并进别处时改为最新目标，手动改状态或内容时清掉；共享合并不写，行与证据逐字节不变），不需要迁移——证据全被去重或为空的被并走对象也能恢复。两条入口共用 `GovernanceStore.detach_memory_merges_on`：PostgreSQL 上先用一条按 id 排序的语句锁住「本来源自有对象 ∪ 摘证据候选」（交叉合并 A(S)→B(T)、D(T)→C(S) 后同时删除 T 与 S 不再死锁，双连接用例钉住），再按 `plan_merge_follow_actions` 处理标记指向被清对象的弃用对象（目标仍在用 → 恢复；目标也已被并走 → 改指下一层；目标是手动弃用的 → 只去标记），对其记忆来源与对象行用 `SKIP LOCKED`、不在拆除事务里等；拿不到锁的（持锁者也可能是关系补全、抽取发布、元素替换或随后回滚的事务）在提交后用会等待的短事务补跑，补跑在请求线程上、等待上限为 `SET LOCAL lock_timeout` 3 秒，超时就把这本笔记本交给一次后台兜底（等 60 秒），再漏的由启动后台扫描（`heal_memory_merges`，自愈判据「弃用、标记指向的对象已不在、自己的记忆仍在」）处理；兜底在只读连接上先取记忆来源（`idx_sources_nb_hidden_type`）、再按来源取对象（`idx_knowledge_objects_source`，PG 用 LATERAL 逐来源探测），代价随记忆量，两后端零候选大库计划钉住；兜底恢复前先复核别的来源的对象是否仍带着它的全部证据条目，有就改指过去，所以不会恢复出两份（两后端用例钉住）；合格的改指目标只有带门闸、仍在用的对象或弃用且自带记录的链上一层，主人手动弃用的不算（直接恢复），改指时给目标补写门闸；后台兜底进轻活维护池、进程内按笔记本去重；`_` 开头的 payload 键在图谱节点、本人叠加层、概念详情、晋升队列的出口统一经 `visible_payload` 去掉，前端图谱详情另有兜底过滤；证据为空的候选一律恢复；本该只去记录的那一步，事务内被跳过、且请求内补跑也没完成时，后台兜底会把它当作恢复处理；后台兜底运行期间又有补跑超时时记「再跑一轮」标志、本轮结束后从头再读，期间新产生的候选不会被吞掉（PG 用例钉住）；改指目标在等锁期间被删除时这一步原样保留、交给兜底下一轮复核（两后端用例钉住）；要等下次启动的窗口只剩补跑与随后的后台补跑都失败、或进程在提交与补跑之间（或后台补跑期间）被杀；证据为空的被并走对象同样恢复（两后端用例）；编辑路径恢复非空时在同一事务里标脏。被并走方自己的记忆被删时不恢复；记忆已不在（孤儿）的不恢复；主人手动弃用的不恢复；a1 → a2 → a3 链每清掉一层恢复一层。只有本版本起的合并自动恢复：#824 之前留下的本人记忆合并没有记录、无法与手动弃用区分，需本人 PATCH 改回 approved（用户裁决 2026-10-22）。合并同时在目标上写门闸 `_absorbs`（PATCH 保留，残留只多一次读）：被清掉的对象都不带它时整段跳过跟随者读，所以没有任何合并的普通记忆删除与编辑在反向索引已认证时只多一条走索引的语句（前置锁），未认证时多两条（再读一次本来源自有对象 id），计数钉住。PostgreSQL 上合并先按 id 序对源对象的主来源与其证据引用的记忆来源取 `FOR KEY SHARE` 再锁两个对象，与拆除 / 重抽同一「先来源、后派生行」锁序——并发删除会等合并提交后摘掉合并写进的证据（线程用例钉住）；加锁前后源对象证据变了则 409「要合并的知识对象刚被修改，请稍后重试」、一行不写。共享对象合并多一条按主键探测、不锁任何行的语句（计划钉）。界面上的唯一合并入口「查重」按既有合同不列记忆派生的行，所以本项只改接口行为，按更新说明 README 不写说明；UI 入口已裁决另开 PR，见 [fangan_todo](fangan_todo.md)「知识图谱」。[产品/API][product]中英同步。回归入口：[SQLite 写侧](backend/tests/test_memory_kg_isolation_write.py)、[PostgreSQL 孪生与锁序/计划钉](backend/tests/postgres/test_memory_kg_isolation_write_pg.py)、[两后端路由场景](backend/tests/memory_promotion_isolation_cases.py)。
- **统一图谱重新合并 `KnowledgeLifecycleService.rebuild_unified_kg` 拆分，零行为变化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「结构」的第二梯队大函数拆分（剩下的两个函数仍在那一条里）。458 行的方法按阶段抽成私有 helper：输入未变的跳过闸、取号、checkpoint 维护与欠账催收和预回收、阶段边界（删库检查点、认领心跳、日志与进度）、概念聚类、LLM 合并复核、概念描述与写簇、其余三类对象、待审候选刷新、翻转与催收与收尾状态、无条件的 scratch 清理与认领释放、派生尾部（规范关系、共提桥接、viz、自动索引、社区）；跨阶段状态放在一份 `_UnifiedKgRebuildRun` 上按同一对象传递（阶段产出的字段起始为空、经 `ready` 读取，阶段错序会响亮失败；聚类结果只存一份），入口版本、认领与 run id 各取一次，概念向量不进这份对象、仍在聚类阶段结束时释放，对象本身不持有指回自己的回调、调用返回即按引用计数释放。`rebuild_unified_kg` 本体 65 行，天花板 458 → 65；新 helper 最长 69 行，不进热函数表；搬动的三处函数内 import 按 isort 顺序排好，后端 lint 基线里本文件的 I001 少一条（11 → 10，`BASELINE_CEILING` 同步 1395 → 1394）。「任务槽管道不进这一遍」的源码守卫改为覆盖本体加全部 `_unified_rebuild_*` helper。拆分前先提交了两端（SQLite / PostgreSQL）共用世界的表征：17 个场景（首次重建、跳过路径三种、带中途决策/写入/并发追加的强制第三代、fresh、维护失败、认领被拒、欠账先结、删库中止、待审刷新前被抢占、翻转作废、派生尾部失败、空图两种、笔记本不存在、模型未配置）的完整观测——对 store / scale / 事件日志 / 模型客户端与本服务协作方法、`kg_merge` 与合并复核函数的每次调用与参数返回值、读写连接进出、id 铸造与取时钟与 run id、本线程每条 SQL、每条提示词、整库逐表增删差分、存储目录差分、日志与返回值——在拆分后逐字节不变，随后按 AGENTS.md 退役，长期回归由同一组场景的行为断言承担。拆分前后各跑一批变异：拆分前 42 个（补了「空图再刷新」场景、异常属性与一条暂缓决策后全部被抓），拆分后同一批重新定位再加 5 个拆分形状特有的（共 47 个）全部被抓；快照退役后只靠行为断言再跑一遍，先漏 8 个，补了阶段心跳、翻转后清缓存、各类型 canonical 前缀、描述复用签名、两个 LLM 阶段同一 checkpoint 版本、实时决策方向六条断言后剩 2 个输出等价的（暂缓决策在刷新事务里还会被再过滤一次；run id 只是唯一键）。评审后又补两个「取号之后准备阶段失败」场景（预回收失败、checkpoint 版本读取失败），钉住「取号之后每一步都在 finally 覆盖内、认领必释放」：把准备阶段挪出 try 的变异在两端都变红；行为层只保留断言真正读的观测（调用、提示词、本线程日志、进度、变化的表名）。回归入口：[SQLite 场景](backend/tests/test_unified_kg_rebuild_characterization.py)、[PostgreSQL 孪生](backend/tests/postgres/test_unified_kg_rebuild_characterization_pg.py)、[世界与断言](backend/tests/unified_kg_rebuild_characterization_cases.py)。
- **非服务进程的 CLI 一律是 schema guest；新增显式的 `database migrate`（PR #待定）。** 维护 CLI 原先经 `open_cli_repository` / `open_maintenance_cli_repository`（即 `create_application_repository`）或直接 `SQLiteRepository(Settings())` 打开库，等于 `migrate=True, seed=True`：每跑一次都会对可能在役的库执行待跑的迁移 DDL，seed 还会用运维 shell 里的 `SILICON_NOTEBOOK_ADMIN_PASSWORD` 无条件重写 admin 密码哈希。现在只有服务启动与新命令 `bash scripts/cli.sh database migrate --confirm-service-stopped`（`app.scripts.migrate_database`，两后端都要停服确认，持离线维护锁，只迁移不 seed，不新建库）迁移 schema。其余入口——`batch`（全部阶段，含 `backfill-images`）、`maintain` / `kg` 各命令、selected-source 准备、Knowhow Markdown 回填、全局归因回填、检索回放、反向出题、reflect rig 的 `search`/`report`、eval 辅助脚本——统一经 `maintenance_cli` 的 guest 入口组合（`create_application_repository(owns_schema=False)`；无插件宿主的工具走 `create_bare_schema_guest_repository`），组合前在裸连接上做一次集中的版本校验（`app.repositories.schema_currency`：SQLite `user_version`，PostgreSQL 账本正好 `1..N` 且 checksum 一致），版本不对一律退出码 3，并用同一条文案同时给出原因与解决办法；scale 构建的账本预检改用同一校验与文案（退出码保持 2）。selected-source 准备的执行态第 1 步改为「先跑 `database migrate --env-file <同一文件>`」（迁移命令的 `--env-file` 与准备命令同一约定：只认该文件、忽略 shell 变量），原先直接 `SQLiteRepository` 构造的工具仍只接受 SQLite；直接开库的 `maintain delete-leftovers` 与 `maintain promotion-targets` 也过同一道闸；全局归因回填保持无插件宿主的 bare 组合。PostgreSQL 检查走 `_SafeDiagnosticConnection`、`connect_timeout` 取连接池获取超时并设 `statement_timeout`，连不上时（URL 写法、凭据、主机）只打印一条不含用户名/密码/主机/库名的固定文案、退出码 2；SQLite 探测不留 `-wal`/`-shm`；停服维护命令拿到维护锁后再核对一次。三个只作用于新目标或副本的转换工具（`migrate-postgres`、`shadow start-forward`、`merge`）在运维文档里点名为例外。发布说明 `admin-start-service-before-command-line-maintenance`。守卫是 AST 扫描（不用 seed 路径上的进程角色断言，理由见守卫 docstring）：服务进程与登记过理由的临时库工具之外，任何仓储构造必须字面量传 guest 关键字。合同见[运维文档][ops]「schema 归属」一节与[部署文档][deploy]「运行」；回归入口：[SQLite 逐 CLI 矩阵与迁移命令](backend/tests/test_cli_schema_guest.py)、[PostgreSQL 孪生](backend/tests/postgres/test_cli_schema_guest_pg.py)、[CLI 表](backend/tests/cli_schema_guest_support.py)、[守卫](backend/tests/test_schema_guest_guard.py)。
- **仓储层反向 import services 的 ①–③ 三条：backend factory 与 PostgreSQL wrapper 上移到 services，零行为变化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「仓储层反向 import services 仍有 7 条」的 ①–③，按整改计划 S3 的既有裁决执行：`backend/app/repositories/factory.py` → `backend/app/services/repository_factory.py`（它导入 `SQLiteRepository`），`backend/app/repositories/postgres/repository.py` → `backend/app/services/postgres_repository.py`（它导入 `RepositoryFacade` 与 `services.checkup`），不留 shim（shim 本身仍算一条边）。两个文件 `git mv` 搬动，内容只改了 factory 里对 PostgreSQL wrapper 的懒加载路径，以及「后端不可用」判据里的模块名（`app.repositories.postgres` 包缺失或 wrapper 模块缺失时仍报 `RepositoryBackendUnavailableError`，嵌套依赖缺失仍原样抛出）。选 SQLite 时仍不加载 PostgreSQL wrapper 与 psycopg，冷导入核对过；原有的懒加载用例复用已导入的 factory，看不出 factory 顶部的急切导入，现补一条从头重新导入 factory 的用例，并补「wrapper 模块或 adapter 包缺失 → 后端不可用」两条参数化用例。全仓导入路径、monkeypatch / `sys.modules` 字符串目标、守卫登记（`check_architecture_boundaries.py` 的 `FACADE_CLASS_FILES`、`audit_facade_callers.py` 的 wrapper 路径、schema guest 守卫的 `SCHEMA_OWNERS`、调用方边界 fixture、`repository_callers.py` 的理由表）与文档路径同步改写；`docs/superpowers/` 下的历史计划按 AGENTS.md 不改。基线 `repository_service_import_ceiling` 零松弛下调：other 5 → 4、postgres 2 → 0。剩下的 ④–⑦（filesystem scale artifact store → `services.kg`）仍在待办账本。后端 lint 基线随改名搬条目，顺带排好被改到的 import 块，`I001` 上限 1394 → 1390（文件 1009 → 1006）。[开发文档][dev]中英同步写明 factory 与两个 wrapper 都在 services、services 可以依赖 repositories、反过来不行，[架构文档][arch]同步。纯结构改动，不写发布说明。回归入口：[backend factory](backend/tests/test_repository_factory.py)、[schema guest 守卫](backend/tests/test_schema_guest_guard.py)、`scripts/check_architecture_boundaries.py`。
- **逐步推理问答编排器 `AskService._run_reasoning_stage` 拆分，零行为变化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「结构」的第二梯队大函数拆分（剩下 `_draft_reasoning_response` 一个仍在那一条里）。513 行的方法按阶段抽成私有 helper：建立本轮状态（确认后的问题理解只解析一次、intent 步）、Knowhow 完整枚举与它的结构化答案、Memory 召回、未配置模型与无可检索来源两条短路、关联报告与记忆那两步（仍经 `_record_report_and_memory_steps` 一处）、类型化检索阶段与选定来源图谱（参与者守卫登记的 fail-soft 站点随之改指 `_reasoning_retrieval_phase`）、草拟阶段（取消检查、权威复核、`ResponseDraftInput` 装配、模型错误回填与草拟后的站外补充）；原来的四个闭包（推送 intent、带取消检查的轨迹推送两种、短路带回的轨迹）成了 `_ReasoningStageRun` 的方法，跨阶段状态放在这份对象上按同一对象传递：阶段产出的字段起始为专用哨兵、经 `ready` 读取，阶段错序会响亮失败（不用 `None` 作哨兵，因为「没有 Knowhow 批」等产出本身就是 `None`）；表格分析臂追加的轨迹与草拟阶段读到的是同一个列表对象；各自在跨阶段时保持同一对象。两个请求级 ContextVar 的 set/reset 与最终提交留在编排器本体。`_run_reasoning_stage` 本体 67 行，天花板 513 → 67；新 helper 最长 95 行（检索阶段），不进热函数表。拆分前先提交了两端（SQLite / PostgreSQL）共用世界的表征：37 个场景（同步与流式作答、带任务与记忆命中、澄清后的意图、仅检索输出三种、无图有文档、无可检索来源两种、未配置模型三种、Knowhow 完整/计数/混合三种与不可枚举/旧目录形状/越界、检索 fail-soft 与两种照抛、三个取消点、作答模型失败与超时、规划失败、站外补充、注入草拟阶段两种、表格分析臂两种、收窄范围下检索快照带候选账本与外部证据、选定来源图谱在检索返回后失败、类型闸）的完整观测——对本服务协作方法与 `execute_*_stage` / `kg_in_scope_for` / 结构化枚举 / `_egress_question` 的每次调用与参数返回值、每次取消检查、ContextVar 的 set/reset、每条提示词与回复、每一步推送、事件日志、本线程每条 SQL、整库逐表增删差分、日志与返回值——在拆分后逐字节不变（SQLite 换 hash seed 与 -n0/-n2/-n4/-n6、PostgreSQL 换 hash seed 都逐字节复现），随后按 AGENTS.md 退役，长期回归由同一组场景的行为断言承担。变异三批：拆分前 35 个（4 个在原世界里输出等价，补了收窄范围快照与「未配置且完整性不可用」两个场景后全部被抓）；拆分后同一批重新定位再加 11 个拆分形状特有的（阶段错序、表格臂追加到副本、草拟或检索挪出 ContextVar 窗口、短路不返回、短路提交丢 job_id 等）共 47 个，快照在时抓住 45 个（一个锚点重复当轮未施加；`ready` 放过未产出字段的那个只在阶段错序时才可见），其中 7 个只有快照抓得到；快照退役后只靠行为断言，补了「记忆召回时停止」「交给检索的推送先查停止」两个场景、权威复核点序列、选定来源图谱拿到的段落上限、结构化答案步的 detail、站外补充期间的模型报警不进答案、绕过 `_engine_scope` 直调时每个查询只 embed 一次（另加一个直调场景）与 `ready` 拒读未产出字段的单测后，47 个全部被抓。热路径对照：同步作答 ABAB 四轮中位 21.65 → 20.99 ms、tracemalloc 峰值 180.3 → 177.1 KiB，不劣。回归入口：[SQLite 场景](backend/tests/test_reasoning_stage_characterization.py)、[PostgreSQL 孪生](backend/tests/postgres/test_reasoning_stage_characterization_pg.py)、[世界与断言](backend/tests/reasoning_stage_characterization_cases.py)。
- **仓储层「调用方边界合同」整套删除：#383 之后无断言，用户裁决删除（PR #待定）。** 用户裁决（2026-10-22）。`backend/tests/fixtures/repository_contract/caller_boundaries.json` 记录的是 `facade_imports` / `independent_private` / `independent_sql` / `sqlite_connect` 四类调用方边界，唯一核对它的断言 `test_caller_contract_matches_live_semantic_boundaries` 在 #383（提交 `62e51b0c9`，2026-07-28）被删之后，再没有任何测试读它：`test_repository_dependency_contract.py` 里的 `_contract()` 与 `ENTRY_FIELDS` 无调用方，`--rebaseline-callers` 在 master 上已经跑不通（`scripts/measure_sync_capture_cost.py` 的 SQLite import 没有登记 reason，报 AssertionError；全量算下来 119 条没有 reason），fixture 与现场收集结果严重脱节。整套移除：fixture、生成器的 `--rebaseline-callers` 模式、`repository_callers.py` 里的四张 reason 表（`SQL_REASON_BY_PATH` / `FACADE_IMPORT_REASON_BY_PATH` / `PRIVATE_REASON_BY_PATH` / `SQLITE_CONNECT_REASON_BY_PATH`）与只为它服务的 `collect_caller_contract` / `_entries` / `product_sql_sites` / `sqlite_connect_sites` / `facade_import_sites` 等；仍有活断言依赖的 `production_source_index()` 与 `private_repository_sites()`（`test_repository_dependency_contract.py`、`test_maintenance_cli_wrappers.py`）及其 helper 原样保留，生成器的其余模式（`--rebaseline` / `--rebaseline-surface`）不变。纯测试与脚本侧删除，`backend/app` 无任何引用，零行为变化，不写发布说明。回归入口：[依赖合同](backend/tests/test_repository_dependency_contract.py)、`scripts/check_architecture_boundaries.py`。
- **「查重」给本人单列「我的记忆里的重复」，只读成员也能合并与恢复本人的记忆对象（PR #待定）。** 原登记在[待办账本](fangan_todo.md)「知识图谱」（用户 2026-10-21 裁决另开 PR，依赖上一条）。用户裁决（2026-10-22）：合并与状态 `PATCH` 的鉴权放宽到读权（只限本人记忆支撑的对象）、镜像库上不开读权路径（这一条是实现方据同步设计推断的保守口径，经用户确认）、分区放在共享组下方单独成段、默认展开单独分页、不进合并建议与模型复核、两个分区的写入结果都就地显示。后端新增 `GET /api/notebooks/{id}/duplicates/own-memory?type=`，返回 `{groups, merged_away}`（单独端点：`/duplicates` 返回裸数组，加字段会改它的形状）：`groups` 是调用者本人的、未弃用的、证据只出自她本人在本库记忆来源的记忆派生对象按与共享查重同源的规则成组（`find_duplicates` 拆出 `_duplicate_blocks` / `_duplicate_groups` 两者共用），`merged_away` 是同口径、已弃用且仍带 `_merged_into` 的本人对象；只按 `viewer_identity` 读、不构建整库查看者范围，与共享路径同形的两遍窄读（`GovernanceStore.own_memory_duplicate_seed_rows` 只取 id/状态/名称，「证据全出自本人记忆」在语句里判定，两后端 `_only_own_memory_evidence_sql`；`own_memory_duplicate_member_rows` 按 `id_binding` 绑定的 id 只为成员与 `merged_away` 读 payload），差分用例对 400 对象语料（各种证据形状）钉住与被替换的整行读法逐字节一致；他人、系统管理员、没有 `memory:read` 的令牌得两个空列表；两者都不缓存。共享查重逐字节不变：基线与本分支对 1500 对象语料（5 种类型、记忆来源与弃用态、两个查看者 × 7 个类型参数，178 组）的输出 `cmp` 相同。合并与 `PATCH` 路由改挂读守卫：有 `knowledge:write` 的照旧（能力 → 镜像 409 → 存储层规则）；只读成员直接走治理服务（facade 签名不变）——合并经 `merge_objects_in_transaction(own_pair_only=True)`（两方都须已批准，免得先合并再恢复把冲突、已复核等状态抬成已批准；只读成员的本人组因此只列已批准的对象，有写权者口径不变），`PATCH` 经 `update_object_in_transaction(own_status_only=True)`（只许在 approved 与 deprecated 之间改本人对象的状态，照 #957 清掉 `_merged_into`、保留 `_absorbs`），都在加锁读回的行上判定、任何写入之前抛 `NeedsKnowledgeWrite`；她的每一种拒绝（未知 id、共享对象、他人记忆、跨类、带共享证据、自并、类型不同、改内容或负责人、改成别的状态）都回与以前相同的读守卫 404 响应体；PostgreSQL 上非本人对与非本人对象在任何加锁之前由一次不加锁的读取拒绝（主来源与证据都查；持锁用例钉住）；镜像库上读权路径不开（只读成员 404、管理者 409）；请求体校验的 422 先于这些判定，文档写明。前端：只读成员也有「查重」，但只请求本人分区；镜像上不显示（`workspaceCapabilities.canMergeOwnMemory`）；本人分区末尾列「合并时弃用的条目」，每行一个「恢复」；两段的写入结果改为就地显示并按时收起，不再走页顶提示——那一组（或那一行）还在当前页时显示在它里面，因写入离开列表时显示在它原来的位置：成功「已合并，原条目已弃用」/「已恢复」，写入成功但该段没能重读时提示重新查重，409 原文并重拉该段，404「这条已不在了」并重拉该段；两段的查重请求各自落定，某一段读取失败时在原位如实说明，不显示「未发现重复。」；成功路径的请求序列不变（写入 → 列表 → 类型 → 该段查重）。端口方法数 979 → 981。[产品/API][product]中英同步，写发布说明（feature）。回归入口：[两后端路由场景](backend/tests/memory_promotion_isolation_cases.py)（SQLite 与 PostgreSQL 孪生）、[两遍窄读差分](backend/tests/own_memory_dedup_narrowing_cases.py)、[读者计划钉](backend/tests/test_memory_kg_store_readers.py)与 [PostgreSQL EXPLAIN 钉](backend/tests/postgres/test_memory_kg_readers_explain_pins.py)、[查重组件](frontend/tests/component/duplicate-group-list.component.test.tsx)、[知识领域 hook](frontend/tests/component/use-kg-knowledge.component.test.tsx)。
- **删除整本笔记本先保留一天，成员可以导出或转移自己的记忆（PR #待定）。** 原登记在整改计划 §13.2 第 7 条（用户 2026-10-21 裁决、B1–B12 裁决 2026-10-22）。`DELETE /api/notebooks/{id}` 改为把笔记本放进一天删除窗口（`notebooks.status='pending_delete'`，SQLite v100 / PostgreSQL 0081 加 `delete_after` / `delete_requested_at` / `delete_requested_by` / `status_before_delete` 四列与部分索引 `idx_notebooks_pending_delete_due`）：`pending_delete` 并入 `NOTEBOOK_LIVE_SQL`，这一天里对所有人不可用；只开五个入口——导出本人记忆（窗口谓词 `export_access_exists_clause`，文件头写明截止）、主人撤销（`POST …/cancel-delete`，只认主人）、告知投影（`GET /api/me/notebook-deletions` 与铃铛条目，只告知主人与有记忆的读者、计入未读）、`GET /notebooks/{id}` 的 404 说明码、把本人已确认的记忆移动到自己的另一本笔记本（`POST /notebooks/{id}/memories/transfer`，固定 move，转走的不因撤销回来）。T1 / T2 / T3 三个转移各是一次同事务 CAS，截止由删除扫尾新增的驱动 C 推进成 `deleting` 并插作业行，之后完全复用六相位、留存投影与 #931 标记；注入时钟贯穿。前端：确认弹窗新文案、列表顶部「即将永久删除的笔记本」区块（撤销 / 导出 / 转移，结果落在控件旁，读不到时 fail-closed）、铃铛分组、说明框、退群面板的「即将删除」标签。合同见[产品/API][product]「删除整本笔记本：一天窗口」，规格 [2026-10-22-notebook-delete-grace-window_zh.md](docs/superpowers/specs/2026-10-22-notebook-delete-grace-window_zh.md)；发布说明 `notebook-delete-keeps-one-day`。回归入口：[双后端共用场景](backend/tests/notebook_delete_grace_cases.py)、[SQLite 与 HTTP](backend/tests/test_notebook_delete_grace.py)、[PostgreSQL 孪生与双连接竞态](backend/tests/postgres/test_notebook_delete_grace_pg.py)、[计划钉](backend/tests/test_notebook_delete_grace_plans.py)、[生命周期矩阵](backend/tests/test_notebook_lifecycle_visibility.py)、[前端区块](frontend/tests/component/notebook-deletions-block.component.test.tsx)。合入当天（2026-10-22）共享场景在两端确定性变红（热修 PR #待定）：用例把仓库时钟钉在 2026-10-22T08:00Z，而删除作业的过期判定（扫尾驱动 A 的 `list_stale` 与 `mark_running` 的抢占分支）取的是主机时钟、`updated_at` 却由仓库时钟写入——宿主时钟一走过这一刻，刚推进的作业行就被当成过期再提交一次（0e2910a77 先把 T0 挪到 2099 止血）。两端现在都从仓库时钟（`self.now()`）取截止，生产上两者本是同一个主机时钟、行为不变；新增用例把仓库时钟拨到主机时钟前后 30 年钉住它（[SQLite](backend/tests/test_notebook_delete_jobization.py)、[PostgreSQL](backend/tests/postgres/test_notebook_delete_jobization_pg.py)），导出文件头的截止改由 `DEADLINE` 推出，共享场景在 T0 取 2025-01-01 至 2099-01-01 六个日期下两端全绿。
- **主界面外壳的列改成 minmax(0, 1fr)：笔记本列表在 1024 / 768 / 375 宽下不再横向溢出到 1102px（PR #待定）。** 删库宽限期那次截图时发现的既有布局缺陷。`.app` 是 `display: grid` 却只写了行，唯一的一列是隐式的 `auto`，不会窄于子项的 min-content；`.notebook-grid` 的 `repeat(auto-fill, minmax(250px, 1fr))` 加 `max-width: 1200px` 在算固有尺寸时按 1200 定出 4 条轨道，min-content 为 4×250 + 3×18 = 1054px，外壳被撑到 1054 + 48 = 1102px，顶栏右侧入口和「＋ 新建」被推出视口（卡片里的长名称早有 `overflow-wrap: anywhere`，不是元凶）。修在外壳的列定义上，不在外层加 `overflow-x: hidden`。外壳按视口排版后，下面几处版式问题同批处理（工具条一条是被 1102px 掩盖的；顶栏一条群组页、私有记忆页在 375 宽下本来就露出来，只是笔记本列表页被撑宽才没露；列表视图一条是评审探针在 768 宽量到的）：761~850px 及搜索框展开时工具条一行放不下、按钮被挤到字折行（现为整组换到第二行，动作组 `flex: 1 1 0` 使 1024 宽展开搜索仍留在同一行，带字按钮不折行）；≤760px 顶栏本就设计成可换行，但外壳第一行定死 56px，换行后的入口溢出顶栏、在群组页压住「返回主页」（现为 `minmax(56px, auto)`）。列表视图的各列下限之和（≤1100 时 777px）在 768~840 宽超出内容区，评审探针量到 `scrollWidth` 801 / `clientWidth` 753；原来 `.notebook-list { overflow-x: auto }` 只写在 ≤760 的 @media 里，现提到基础规则，表格在自己内部横向滚动，整页不再被撑出滚动条。同一区域还有一处既有毛病：不带空格的超长标题在标题列里不换行、会压到右边的「来源」列上，给标题格补 `min-width: 0`、`.notebook-list-title strong` 补 `overflow-wrap: anywhere`（与网格卡片一致），长标题现在在本列内折行；768 / 375 宽实测标题右缘 ≤ 本格右缘 ≤ 下一列左缘，表格内部仍滚动（777 / 769 对 720 / 327），整页 `scrollWidth == clientWidth`。Browser pane 实测：三个宽度修复前 `scrollWidth` 均为 1102，修复后均等于 `clientWidth`；私有记忆、群组、工作区三个视图在 768 / 1024 下全部元素盒逐个相同，1440 下笔记本列表全部可见元素逐个相同；独立页不挂 `.app`，不受影响（独立页 admin/usage、agents、updates、wishes、manual、ask 在 1440 / 1024 / 768 / 375 四个宽度下与修复前逐个元素盒相同；另在 320 / 600 / 761 / 800 / 850 / 1200 宽下笔记本列表 `scrollWidth == clientWidth`；网格与列表两种视图在 1440 / 1024 / 900 / 800 / 768 / 375 宽下均 `scrollWidth == clientWidth`，列表在 800 / 768 / 375 宽下表格内部 `scrollWidth` 794 大于自身 `clientWidth`，只在表格内滚动）。375 宽下群组页与私有记忆页的差别只有一处：顶栏换成两行后第一行从 56px 长到 95px，页面其余内容整体下移 39px，元素之间的相对位置不变。回归入口：[外壳列与工具条换行守卫](frontend/tests/guards/app-shell-column-guard.test.mjs)。
- **恒真的 PG 断言改成能咬住，陈旧守卫引用从注释和文档里清掉（PR #待定）。** `test_scale_build_lock.py` 里「锁会话关掉空闲回收」一条读回 `idle_session_timeout == "0"`，而本机和 CI 的库默认值本来就是 0，`_disable_idle_session_timeout` 不生效这条也照样绿。改成与删库宽限期的转移占位用例同一修法：先 `ALTER DATABASE … SET idle_session_timeout = '30min'`、finally 里 RESET，PG 14 以下跳过 ALTER 与读回；变异（去掉 `try_scale_build_lock` 里的该调用）读到 `'30min'` 变红。同批清理仍在引用已不存在的 `test_repository_callers_static.py`、`INDEPENDENT_PRIVATE_SITES`、`independent_private` 跟踪的注释、文档字符串与 `docs/large-notebook-latency-analysis.md`（`deps.py`、`knowhow/api.py`、`knowhow/transfer.py`、两个 `knowledge_store.py`、`backfill_promotion_targets.py`、`test_knowhow_transfer_service.py`）：说明「为什么这样写」的保留原因，只剩一条引用的整段撤掉，不改任何代码行。同一批还清掉另一类同源陈旧注释：已退役的 `test_repository_surface_manifest.py`、`EXPECTED_PATCH_DELTAS`、`LINE_NUMBER_INSENSITIVE_FILES`、`*_ALLOWED_IMPORTS` 等「按行号钉死、所以必须追加在文件尾 / 用局部 import」的说法（`test_notebook_assets.py`、`test_notebook_share_copy.py`、`test_knowhow_*.py`、`test_multi_domain_bases.py`、`test_memory_transfer_service.py`、`test_notebook_copy_service.py`、`app/api/deps.py` 等），只保留「追加在尾部 / 局部 import」这个事实本身，同样不改任何代码行。
- **启动器取消用例改握手判据；伴随服务启动期的取消不再被 bash 吞掉，启动器消失也算取消（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「其它残余」的「`test_launcher_forwards_cancellation_while_companions_start` 高负载下超时」。用例超时的真因不是负载：本机两轮扩展门里失败的恰好是 4 个 SIGHUP 参数，那两轮的门禁进程继承了 SIGHUP=SIG_IGN（`nohup` 或脱离终端的 harness），而 POSIX 规定非交互 shell 不能 trap 启动时已被忽略的信号，启动器因此收不到取消，等满 5 s。让 pytest 自己忽略 SIGHUP 即可 4/4 确定性复现；同样满载下默认处置 5/5 全绿。用例改为：启动器经 `preexec_fn` 以默认信号处置启动；新增 `runner-ignores` 参数钉住这一点；所有等待都以握手结束（控制器按改名发布 pid，或启动器自己退出），墙钟只留 20 s 兜底。按随机时刻打断启动器的探针另外找到两处产品竞态（macOS `/bin/bash` 3.2），都会让取消拖到伴随服务全部起完才生效：(1) 取消恰好落在 fork 控制器与进入 `wait` 之间时，trap 被挂起到 `wait` 的子进程退出；(2) 收尾发给控制器的 TERM 若落在 fork 之后、exec 之前，会被子进程里那份启动器 trap 接住并在 exec 时丢掉。修法：`extension_services_start` 改用 `kill -0` 加 0.05 s 前台 `sleep` 轮询控制器；`extension_services_cleanup` 每个轮询间隔重发 TERM 直到控制器退出；控制器只响应第一次取消、吸收后续重复（回滚期间第二次 Ctrl-C 也被吸收，强退用 `kill -9`，前台运行时也可用 SIGQUIT）。评审追加：启动器经 `--launcher-pid $$` 把自己交给控制器，控制器在启动期的各个等待里发现父进程变化或启动器 pid 消失即按取消处理，回滚并删除回执——覆盖启动器被 SIGKILL、以及挂起的 trap 落在 `&` 与 `$!` 之间导致收尾拿不到控制器 pid 的情况；控制器在启动就绪后即退出，已交接的会话不受影响。复审时又查出第三条路径：bash 3.2 上挂起的 trap 常在 fork 控制器之后、`$!` 赋值之前执行（打在 fork 附近的取消约两成落在这里），收尾拿不到 pid，启动器退出后控制器继续跑——探针里的假控制器不自检，曾在本机堆出约 900 个孤儿进程（全部来自本轮探针与压测，master 现有用例 3 轮 0 残留）。修法：fork 前记下 `$!`，收尾发现 `$!` 变了就把它当控制器照常停掉（400 次泄漏 92 次 → 600 次 0 次）；读 `$!` 经 helper 临时解除 nounset（bash 3.2 在首个后台作业之前对 `$!` 报 unbound）。咬住这条回归的是压测脚本（泄漏计入失败、退出码 1；去掉兜底时 400 次泄漏 92 次）和各用例自己的 `assert not alive(...)`；启动器用例 fixture 收尾时清掉本检出目录残留的控制器并判错，只是防止残留进程累积的堆积兜底，不是回归守卫（带 WAIT_SERVICE 的用例在 finally 里已先 `kill_published`），`forked` 模式经 `exec -a` 保留控制器路径，兜底也看得见它。行为契约不变：取消照样转发，伴随进程照样收尾；代价是启动完成与取消收尾各多至多一个 0.05 s 轮询间隔。竞态 (1) 打不中确定性窗口，由不进 CI 的压测脚本 `scripts/stress_launcher_signals.py` 守护（运行方式与检出率见 [开发文档](docs/development_zh.md)「压测脚本」）。新增确定性用例：控制器漏接第一次 TERM 时启动器继续转发；控制器已起、尚未写回执就取消；回滚开始时再来一次 TERM 不打断回滚；启动器被 SIGKILL 后控制器回滚、停掉伴随服务、删掉回执；父进程判据与 pid 判据各有一例只靠它才能判出；不传 `--launcher-pid` 时父进程变化不算取消（手动 `nohup … start &` 退出登录不被回滚）；启动器确实把自己的 pid 传给控制器。用例：[启动器生命周期](backend/tests/test_extension_service_launchers.py)、[伴随服务控制器](backend/tests/test_extension_services.py)。
- **Memory 召回在没有可用向量时不再 embed 问题（PR #待定）。** #963（`_run_reasoning_stage` 拆分）评审时发现：每轮逐步推理问答把研究问题 embed 两次，第一次在 `MemoryRetriever._hits`，它在 run 级 embedding memo 装上之前直接调 `embed_query`，而且不管候选行有没有向量都先 embed。本人在本库没有记忆是常态，这次串行往返完全白付，还会推迟后面所有轨迹的推送。现在 `_hits` 先解码候选行的向量，只有至少一行解码结果不是 `None`（与打分时 `has_vector` 同一判据）才 embed；没有任何候选行带存储向量（无记忆，或记忆都还没有存储向量）时不 embed；已确认记忆被编辑后状态回到 pending、但旧向量行仍在，那种情形照样 embed，词法臂照常跑，命中逐字节不变（对照旧实现的两后端用例；解码出空向量的旧 JSON 行仍会 embed，因为它会改融合分母）。逐步推理表征用例 `QUESTION_EMBEDS_PER_RUN` 由 2 改为 1（钉它的世界里没有本人记忆）。`memory_world` 场景补钉了有带向量本人记忆时研究问题 embed 两次（变异：恒不 embed、调用方返回空、调用方换上必抛的 embedder 三种都报红）。两侧共用一次 embed 是第二步，受 key 归一与 `EMBED_RUNTIME_DIM` 截断两条约束，登记在[待办账本](fangan_todo.md)「检索」。
- **逐步推理问答的草拟阶段 `AskService._draft_reasoning_response` 拆分，零行为变化（PR #待定）。** 原登记在[待办账本](fangan_todo.md)第四节「结构」的第二梯队大函数拆分；它是四个函数里的最后一个，那一条随之移除。566 行的方法按阶段抽成私有 helper：草拟输入的重绑定（证据元组的列表快照、本轮读到的关联报告、推送、取消与请求级模型报警表）、相关知识与知识图谱证据卡（按命中所属笔记本的类型定义排版、去重、截断）、合成的输入（作答客户端、集合地图块与结构化证据侧）、按节合成的尝试、它的采用或单次合成（回退成功时摘掉分节阶段那段报警）、检索基线清单、各类引用卡、证据等级（按节封顶）、清单答案的引用清空与证据附图、合成轨迹步、结论与完整性 / Knowhow 覆盖披露、`AskResponse`；原来的分节进度闭包成了`_ReasoningDraftRun` 的方法，跨阶段状态放在这份对象上按同一对象传递：阶段产出的字段起始为专用哨兵、经 `ready` 读取，阶段错序会响亮失败；轨迹、引用列表、计数与基线两个回填字典、已装入的站外证据集合都在跨阶段时保持同一对象（清单答案没有绑定标记时引用列表被换成新的空列表，与原来同义）。函数体逐字搬动，新增的只有阶段之间的读写。`_draft_reasoning_response` 本体 80 行，天花板 566 → 80；新 helper 最长 94 行（单次合成），不进热函数表。拆分前先在编排器拆分留下的两端（SQLite / PostgreSQL）共用世界上补了草拟阶段自己的分支（世界多了两跳 `part_of` 推导链、本人的已完成深度报告、可来源于其上的第二个笔记本；脚本模型能读候选摆大纲 / 顺链、只引匹配的证据号、让某一节失败或不引）：31 个草拟场景（集合清单行被引、两行引同一段只出一张卡、清单答案无绑定标记时清空引用，完整清单抑制完整性披露与有谓词 / 聚合范围 / 空清单三种保留，按篇原文取样被引，元素与推导关系锚点按自身相关度定级，按节合成写成 / 部分有据封顶 overview / 回退并摘掉该节报警 / 回退也失败保留全部报警 / 选集丢失的绑定仍进证据池，一节大纲与清单旁的大纲只合成一次，关联报告读入、只有报告时也触发合成、绕过大纲，证据附图（清单行与原文段卡不带、只在报告分区里的段落带全），作答客户端未配置（有 Knowhow 批时「分析未运行」），无排序命中的合成失败，相关知识截断与去重，联邦命中按其所属笔记本的类型定义排版，草拟阶段自身三个取消点，直调与类型闸），把逐字节快照装回——除编排器那份所录之外还录草拟阶段对本服务方法、`evidence_context` / `schemas` / `model_clients` 端口与调用时导入的模块函数的每次调用（按集合顺序读的类型定义作多重集合比较）——拆分前后逐字节不变（SQLite 换 hash seed 0–13 与 -n0/-n2/-n4/-n6、PostgreSQL 换 seed 0/5/9 都逐字节复现），随后按 AGENTS.md 退役。变异：拆分前 53 个全部被抓，其中 13 个只有快照抓得到；拆分后同一批重新定位再加 11 个拆分形状特有的（阶段错序、回写遗漏、分节报警起点丢失、`ready` 放过未产出字段、进度步写到轨迹副本）共 64 个，快照在时抓住 62 个（报警起点丢失在原世界里等价、`ready` 只在阶段错序时可见）；退役后补了记忆被引、两节绑一段原文得到引用卡、分节回退保留草拟前的报警、无候选账本的收窄运行、推导链默认 tau 下定为有据、收窄运行的基线清单（选集段落顺序、轨迹步数、合成装配）、站外条目与表格行定位被引、分节计数与精确命中预留与引用读取备忘、只在报告里的段落附全图与 `ready` 拒读的单测，64 个全部只靠行为断言被抓。热路径对照：同步作答 ABAB 四轮中位 20.34 → 20.02 ms，tracemalloc 峰值 182.2 → 182.2 KiB；单独调草拟阶段 0.808 → 0.823 ms（结构开销估算约 9 µs/次：68 次 `ready` × 38 ns、约百次字段读与 28 处回写、13 次 helper 调用、建 run 4.1 µs），草拟峰值 60.2 → 58.6 KiB。回归入口：[SQLite 场景](backend/tests/test_reasoning_stage_characterization.py)、[PostgreSQL 孪生](backend/tests/postgres/test_reasoning_stage_characterization_pg.py)、[世界与断言](backend/tests/reasoning_stage_characterization_cases.py)。
- **启动器在启动成功的那一刻被杀，也按取消回滚（#978 codex P2 后续，PR #待定）。** #978 的控制器只在各个启动等待之间检查启动器：启动器在最后一次检查之后、会话转为 ready 之前被 SIGKILL 时，控制器照样按成功返回，`main` 的 finally 只删掉回执，新起的会话就在没人认领的情况下继续运行。同样的缺口还在复用会话的复检成功出口和「未配置任何服务」的返回前。现在 `start()` 的每个成功出口都先问一次启动器监视（判据不变：传了 `--launcher-pid` 才监视，pid 不在或父进程变了即判定消失），已消失就走取消路径：退出码 130、回滚本次启动的会话、复用的会话不动、删除回执。确定性握手用例让控制器在「读到 ready」「复用探活成功」「空配置 spawn 返回」之后停住，测试 SIGKILL 启动器、确认控制器已被重新挂到别的父进程后再放行；三处检查各去掉一处，对应用例即红。压测脚本的假控制器不经过 `start()`，覆盖这条路径需要另造假 supervisor，成本不划算，未加。用例：[伴随服务控制器](backend/tests/test_extension_services.py)。
- **KG 流式整表读的 PG 用例，造数据阶段不再被池里的语句超时打断（PR #待定）。** #981 的 level-3-postgres-integration 上 `test_kg_heavy_streams_statement_timeout_pg.py` 两个参数同时在 `_seed` 里 `QueryCanceled`（外键检查 `FOR KEY SHARE`），同批其余用例全绿，该 PR 没碰存储层。根因：`_seed` 在 `postgres_database.write()` 的连接上用 `generate_series` 各插 12000 行宽行（约 700 字符的 id）进 `knowledge_relations` / `knowledge_objects`，这条连接沿用测试池的 2 秒 `statement_timeout`，CI 负载高时插入加外键检查超过它；与被测的「慢消费者在 500 ms 紧超时下存活」无关。修法：`_seed` 开头 `SET LOCAL statement_timeout = 0`，只作用于这次 write 事务（与 `test_memory_isolation_migration_pg.py` 造数据的写法一致）；被测部分（之后 `connect()` 连接上的 `SET statement_timeout = '500ms'` 与 `_timeout_ms` 前提断言）不动。把 seed 连接的超时压到 50 ms 能复现同一处 `QueryCanceled`，换成本修法后变绿。
- **模型服务状态面板列出未绑定的工作负载（PR #待定）。** 原登记在[待办账本](fangan_todo.md)「二、产品功能待办」的「Ask / Deep Report」：面板卡片按物理服务组织，`MODEL_BINDINGS_STRICT=false` 放行时缺绑定的工作负载不出现在任何一张卡片上，只能翻启动日志的 `model-bindings:` 告警。现在「未绑定」的判定只有一处——`model_registry.binding_report`（离线标志、三类全部未绑定工作负载、其中特性开关开着的子集、被忽略的陈旧 id）；启动告警改为渲染它（日志逐字节不变，原用例原样通过），`GET /api/model-services/status` 对管理员另带 `unbound_workloads[]{id,label,feature_enabled}`、`stale_binding_ids[]` 与 `stale_binding_hidden_count`（陈旧 id 与日志同一把脱敏，抽成 `displayable_ids`），对其他用户恒为空；「测试全部」的回包同样带，前端单服务测试合并时沿用当前快照的缺口。每次读现场注册表、一次快照的服务行与缺口只取同一代注册表，热重载补齐后随之清空；离线模式与启动期一样不点名任何工作负载；严格档下能起来即为空。面板在卡片列表上方为管理员单列一块（「未绑定」「已忽略的绑定」，特性开着的标「（功能已开启）」），没有缺口不渲染。严格档拒启、日志格式与任何工作负载的路由都不变。纯配置计算、不读写存储，故后端用例单后端。合同见[部署与配置](docs/deployment-and-configuration_zh.md)「模型服务」两段；回归入口：[与启动告警并排比对的放行/严格/离线/陈旧 id/热重载用例](backend/tests/test_model_status_binding_gaps.py)、[HTTP 端到端只给管理员](backend/tests/test_system_model_services_api.py)、[面板组件](frontend/tests/component/model-service-panel.component.test.tsx)、[解析与合并](frontend/tests/unit/model-service-status.test.mjs)。
- **`scripts/dev.sh` 的退出收尾对齐 `scripts/prod.sh`，两个启动器共用一份收尾（PR #待定）。** 来自 #978（启动器取消握手）评审登记的一条（待办账本里没有对应条目）。dev.sh 原先在 Ctrl-C / TERM / HUP 时只对 uvicorn 与前端各发一次 TERM 就退出，不等、不兜底，前端还是 `npm run dev`（TERM 停在 npm 中间层）。现在收尾抽成新文件 `scripts/launcher_cleanup.sh`，由 dev.sh 和 prod.sh `source`（不进 `extension_services.sh`，那份由配套服务取消握手 #978/#979 独占；`packaging/start.sh` 的第三份不同形，未动）：两个子进程同时发 TERM，轮询等 `START_CLEANUP_GRACE_SECONDS`（默认 10 秒，正整数，非法值在启动任何东西前以 2 退出），仍在运行的发 KILL 并打印 `startup child PID <n> did not stop after SIGTERM; sending SIGKILL`，`wait` 回收后才停配套服务；退出码 130 / 143 / 129 两个启动器相同，某个服务自己退出时仍返回它的退出码并同样停掉另一个。**宽限等待期间再按一次 Ctrl-C / 再收到 TERM、HUP 不再杀死启动器**（原先 `trap - EXIT INT TERM HUP` 把信号恢复成默认处置，第二个信号直接杀掉 bash，配套服务不停、回执残留、退出码 -2——uvicorn 打印 "CTRL+C to force quit" 时用户照做就会触发）：现在这三个信号只置一个升级标记，宽限等待立即结束、直接 KILL 残留子进程，配套服务照常停止，退出码仍是第一个信号的。终端的 Ctrl-C 发给整个前台进程组，会同时打断启动器自己的 `sleep`，而启动器带 `set -e`，这条失败命令曾让收尾在 SIGKILL、回收与配套服务停止之前就中止（codex 评审发现，原用例只向启动器 pid 发信号所以没抓到）；现在收尾整个在 `set +e` 下执行。又发现 bash 3.2 不会对「正在处理其处理函数的那个信号」再运行 trap，所以第一次 Ctrl-C 之后的第二次 Ctrl-C 的 trap 永远不跑——改为把被信号杀死的 `sleep`（状态 > 128）也当作升级（终端的信号总会杀死它）；只发给启动器 pid 的重复 SIGINT 仍要等宽限期结束，已写进文档。又补了既有窗口：信号 trap 落在 `( exec … ) &` 与 `PID=$!` 之间（bash 3.2 上常见）时，子进程曾因 pid 还没进变量而成为孤儿；现在每次 fork 前后用 `launcher_fork_mark` / `launcher_fork_done` 括起来，收尾发现 `$!` 比 fork 前变了就把它当作对应子进程处理（与 `extension_services.sh` 对控制器的做法同形）。两个子进程都改成 `( exec … ) &` 启动，前端直接 exec `frontend/node_modules/.bin/next dev`，所以 `$!` 就是服务本身。绕开 `npm run dev` 会丢掉 `predev` 钩子（`npm run sync:ui-plugins`，即 UI 插件同步），所以 dev.sh 在 `frontend/` 下先以同样 exec 子进程的方式跑 `node scripts/sync-ui-plugins.mjs`（占用 `FRONTEND_PID`，同一个收尾覆盖同步窗口内的取消；失败则不启动前端、停后端、以其退出码退出；同步期间后端先退出则以后端退出码退出并停掉同步），并用用例把 dev.sh 与 `frontend/package.json` 的钩子集合钉在一起。刻意没有加 uvicorn 的 `--timeout-graceful-shutdown`：prod.sh（以及 backend.sh）现行做法就没有，退出时限完全由宽限期负责。用例（假检出目录里的假 uvicorn / next / node，握手判据、20 s 只作兜底）：TERM / HUP / INT 下两个子进程都收到 TERM、都退出、不出现 KILL，后端的父进程就是启动器；一个无视 TERM 的前端在宽限后被 KILL 且只 KILL 它；dev.sh 与 prod.sh 各自在宽限等待里收到第二个 INT / TERM / HUP（prod.sh 用假 `rm` 停在交接之前）后立即 KILL、配套服务 stop 已执行、回执已删、退出码为第一个信号的；子进程自己失败时返回其退出码并停掉另一个；同步先于 next、同步失败、同步期间取消与同步期间后端退出；信号落在 fork 与赋值之间的子进程仍被停止（用 `kill` 夹在 fork 与 `PID=$!` 之间确定性复现）；两个启动器都 source 共享脚本且每次 fork 都有标记；非法宽限值。变异：去掉 KILL 兜底、不先 TERM、不等宽限、只给后端发信号、去掉升级处理、宽限循环不看升级标记、升级后跳过配套服务清理、去掉 `$!` 找回、去掉后端的 `exec`（后端子 shell 是两条命令，ppid 用例抓得到）、给前端或同步的子 shell 追加第二条命令、去掉同步、同步失败仍启动 next、同步改前台、同步等待不看后端、prod.sh 不再 source 共享脚本，都有用例变红。注意：`( 单条简单命令 ) &` 在 bash 3.2 上本来就不 fork 子 shell，所以只去掉前端或同步那一处的 `exec` 是不可观察的等价改动，不是被拦住的变异。用例：[启动器生命周期](backend/tests/test_extension_service_launchers.py)，假子进程在[检出目录工具](backend/tests/launcher_checkout_testkit.py)，共享收尾在 [launcher_cleanup.sh](scripts/launcher_cleanup.sh)。

## 62. MCP 读取本人的问答记录与深度报告（2026-10-21）

**已交付，2026-10-21；方案 §19.3 的 Agent MCP 扩展。** 用户反馈通过 MCP 看不到用户在笔记本里的问答，希望 Agent 能读到对应的问答信息与深度报告。合同由所属权威文档承载，本节只登记完成状态与回归入口。

- **四个只读工具并入 `read` 档（PR #待定）。** `list_conversations`、`get_conversation`、`list_reports`、`get_report`，内部能力名 `history:read` 映射到既有的 `read` 档：没有新档位、新开关或新迁移，已签发的 `read` 凭证立即获得，调用记录里显示为「读问答记录和报告」；Agent 接入页「读取」档说明与更新说明同步写明。MCP 公开工具由 17 个变为 21 个。
- **只读凭证主人本人的记录。** 单库与全局会话并列出，一次读一轮；全局会话按整条判（它任一轮用到的笔记本不在白名单内或主人已无权读取就整条不给，列表连标题都不露），不满足一律与「不存在」同一条 `not_found`；报告只读本人创建的，须同时传 `list_reports` 给的 `notebook_id`。`get_ask` 语义不变（仍只重读经 MCP 发起的任务），读浏览器里的问答用 `get_conversation`。
- **新增 `submitted_via` 读时推导。** 单库会话摘要与每轮多出发起方字段（取自 `ask_jobs`，不加迁移，找不到任务行为空串），HTTP 会话列表与详情的响应因此只增不改。
- **报告读取的取舍。** 正文与摘录同网页上作者本人所见，标识符更严（他人记忆的条目不给 `ref`、来源与元素 id 置空，永不输出记忆记录与内部字段）；失败小节的原始错误不经 MCP 输出。`history:read` 与 `memory:read` 同属 `read` 档，由测试钉住。
- 当前合同见[Memory 与 Agent MCP][memory]，接入步骤见 [Agent MCP SOP](docs/agent-mcp-memory-sop_zh.md)，面向用户的说明见[用户手册](docs/user-manual_zh.md)；实施计划：[计划](docs/superpowers/plans/2026-10-21-mcp-history-tools-plan_zh.md)。回归入口：[会话工具](backend/tests/test_mcp_conversations.py)、[报告工具](backend/tests/test_mcp_reports.py)、[档位同档不变量](backend/tests/test_agent_history_capability.py)、[全局会话可见性](backend/tests/test_global_ask_agent_history.py)。

## 63. 问答就深度报告追问（关联报告）（2026-10-21）

**已交付，2026-10-21；方案 §6.5 问答与深度报告的衔接，MCP 部分属 §19.3。** 用户希望问答能接着一份已完成的深度报告往下问：回答读这份报告，并回到报告引用的原文找依据。合同由所属权威文档承载，本节只登记完成状态与回归入口。

- **两处入口与粘性标签（PR #待定）。** 报告详情的「就此追问」（开新会话、挂标签、高级模式下切到报告生成时的检索范围，交集为空或装不下时退回全选并说明）与问答输入框下方的「关联报告」选择器（只列本人在本笔记本里生成完成的报告）；标签在会话内粘性生效，每轮回执记下挂了哪份报告，重开会话按「在途任务的报告，否则最后一轮回执」恢复。发送前预检在标签旁就地说明本轮为什么不读。
- **冲突判据与读入方式。** 一个判词函数在本轮来源天花板下判报告的全部引用与个人记忆使用，提问执行、意图预检与发送前预检共用；冲突时本轮照常检索作答、报告只贡献题目，回执写明原因。可读时报告正文作不可引用的上下文（规则 15、首尾边界、执行摘要的标记删除、节正文只改写严格格式的标记），被引条目由检索层按 id 从当前原文复原、以原生证据项进入独立预算的分区（跨类按相关度入选，截断残片过短按未载入处理，挤掉的标记显示为未载入并在保存时剥除）。读入报告的 reasoning 轮次单次合成，chunk 轮次不走文档概览快路径；扩展引擎与全局问答不读报告。
- **存储与分享。** 回执随回答存 `answers.payload`；在途轮次的报告存新列 `ask_jobs.report_id`（SQLite v99 / PostgreSQL 0080）。会话公开分享不带报告正文与关联信息，分享前的个人记忆披露计入读入报告用到的个人记忆。新增两个 Settings（`ASK_REPORT_CONTEXT_MAX_CHARS`、`ASK_REPORT_EVIDENCE_BUDGET_CHARS`）与两个只读端点（发送前预检、「就此追问」范围）。
- **MCP。** `ask` 新增 `report_id`（单库专用、无粘性；与多库、`"*"`、`gconv-` 组合报 `invalid_argument`；先过笔记本授权再查报告；澄清句柄绑定报告），回执投影在 `scope.linked_report`（仅检索在顶层），不带个人记忆 id；`list_conversations` 行与 `get_conversation` 的 `conversation` 带会话最后一轮的 `linked_report: {report_id, title}`，每轮 `scope.linked_report` 与 `ask` 同形。
- 当前合同见[就深度报告追问（关联报告）](docs/product-and-api_zh.md#就深度报告追问关联报告)与[Memory 与 Agent MCP][memory]，配置见[部署][deploy]，事件见[运维][ops]，接入步骤见 [Agent MCP SOP](docs/agent-mcp-memory-sop_zh.md)，面向用户的说明见[用户手册](docs/user-manual_zh.md)；实施计划：[计划](docs/superpowers/plans/2026-10-21-ask-report-followup-plan_zh.md)。回归入口：[纯函数](backend/tests/test_report_followup.py)、[契约模型](backend/tests/test_report_followup_models.py)、[引用复原](backend/tests/test_report_reference_restore.py)、[服务编排](backend/tests/test_ask_report_followup_service.py)、[chunk 路径](backend/tests/test_ask_report_followup_chunk.py)、[reasoning 路径](backend/tests/test_ask_report_followup_reasoning.py)、[HTTP 端点](backend/tests/test_ask_report_followup_api.py)、[会话存储](backend/tests/test_conversation_linked_report_store.py)、[MCP ask](backend/tests/test_mcp_ask_linked_report.py)、[MCP 会话页](backend/tests/test_mcp_conversation_pages.py)、[前端选择器](frontend/tests/component/linked-report-picker.component.test.tsx)、[就此追问](frontend/tests/component/report-followup.component.test.tsx)、[回执](frontend/tests/component/answer-linked-report.component.test.tsx)。

[product]: docs/product-and-api_zh.md
[flow]: docs/product-and-api_zh.md#产品流程
[retrieval]: docs/product-and-api_zh.md#检索模式问答
[reports]: docs/product-and-api_zh.md#深度报告可信度与综合
[memory]: docs/product-and-api_zh.md#memory-与-agent-mcp
[knowhow]: docs/product-and-api_zh.md#knowhow-表
[global]: docs/product-and-api_zh.md#全局问答
[experience]: docs/product-and-api_zh.md#检索策略经验
[arch]: architecture.md
[deploy]: docs/deployment-and-configuration_zh.md
[ops]: docs/operations_zh.md
[dev]: docs/development_zh.md
[scripts]: scripts/README.md
[enum-design]: docs/superpowers/specs/2026-07-28-reasoning-enumeration-tools-design.md
[h1]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L33-L40
[h2]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L41-L52
[h3]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L53-L60
[h4]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L61-L69
[h5]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L70-L97
[h6]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L98-L107
[h7]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L108-L120
[h8]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L121-L124
[h9]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L125-L142
[h10]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L143-L151
[h11]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L152-L168
[h12]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L169-L192
[h13]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L193-L201
[h14]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L202-L207
[h15]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L208-L212
[h16]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L213-L216
[h17]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L217-L226
[h18]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L227-L242
[h19]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L243-L259
[h21]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L260-L270
[h22]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L271-L282
[h23]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L283-L293
[h24]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L294-L302
[h25]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L303-L313
[h26]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L314-L327
[h27]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L328-L380
[h28]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L381-L388
[h29]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L389-L397
[h30]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L398-L405
[h31]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L406-L480
[h32]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L481-L507
[h33]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L508-L544
[h34]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L545-L566
[h35]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L567-L589
[h36]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L590-L618
[h37]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L619-L644
[h38]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L645-L673
[h39]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L674-L721
[h40]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L722-L774
[h41]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L775-L805
[h42]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L806-L823
[h43]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L824-L844
[h44]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L845-L866
[h45]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L867-L894
[h46]: https://github.com/huyangc/silicon-notebook/blob/403b796f3c1620f93a4c35e92f035e68432290d4/fangan_done.md#L895-L938
