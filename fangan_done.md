# silicon-notebook 方案已完成情况

更新日期：2026-09-22（账本精简与现状校正）

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

**SSO 代码已交付，生产切换未验收（2026-09-20）。** 外部 `auth.provider`、w3-auth 示例及本地凭据迁移/退役链路已实现；不把插件交付等同于真实 IDaaS 联调或生产切换完成。边界见[外部认证](docs/product-and-api_zh.md#外部认证与本地凭据退役)、[认证部署](docs/deployment-and-configuration_zh.md#外部认证部署)和[迁移运维](docs/operations_zh.md#认证迁移与退役)。回归入口：[认证宿主](backend/tests/test_auth_provider_host.py)。

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

**已交付，2026-09-20；方案 §5.2、§11 / 全局问答扩展。** 多轮检索回执聚合真实成功/降级状态；答案交付前复核冻结来源、可见性与适用的检索指纹。失败时整份答案按公开合同作废并提示重试，不冒充已重新检索的原答案。

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
