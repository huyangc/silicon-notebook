# 用户界面词汇约定

[Back to English product reference](./product-and-api.md) · [返回中文产品参考](./product-and-api_zh.md)

本文件是中文用户界面用词的单一真源。它只约束展示给用户的文案，包括 JSX 文本、
`label`、`title`、`placeholder`、`aria-label`、toast、错误和表头；代码、类型、协议、
注释与架构文档继续使用原有内部名称。

## 界面词汇表

| 内部 / 黑话（界面文案里不得出现） | 界面词 |
|---|---|
| 基准库 / 基准语料 / 底层库 (base) / 权威参考层 | 公共知识库 |
| 个人层 | 个人知识库 |
| notebook / Notebook（散文中） | 笔记本 |
| 建图 / 构建·建立知识图谱（作动作） | 整理（知识图谱） |
| 入图 / 未入图 | 已分析 / 待分析 / 已分析·无知识 |
| 抽取 / 补抽 / 重抽 | 分析 / 分析新增 / 全部重新分析 |
| 向量检索索引 / CSR 图 / ANN / 暴力检索 | 索引（整句重写，如「建立快速查找结构」；小库说「直接搜索已够快」） |
| TopN / Top-N | 最相关的若干条（档位说明一律只讲效果，不讲取数方式） |
| chunk / chunks | 段 |
| 节点 / 知识节点（散文，非图谱技术上下文） | 知识对象 / 知识条目（不可统一降格为「概念」） |
| 边 / 关系边（散文） | 关联 |
| 投影 / 投影产物 / 重建投影（knowhow） | 同步 / 重新同步 |
| LLM 预审 / 预审 | 自动判重 |
| 去重 | 合并重复 |
| 晋升（用户侧）：动作 / 状态 / 队列 | 贡献到公共知识库 / 已收录 / 内容审核 |
| promotion（来源类型，公共知识库里由贡献生成的来源） | 收录（详情里的类型标签）／已收录（列表徽标） |
| 孤立节点 / 补连边 | 没建立关联的内容 / 补上关联 |
| 边审 / 边审查队列 | 关系审核 / 关系审核队列 |
| Memory（残留英文散文） | 记忆 |
| schema（散文） | 内容类型 / 抽取字段（图谱对象类型/字段管理功能的界面名可用「图谱 Schema」） |
| deprecated（toast 直出） | 已弃用 |
| community / 社区 | 主题板块 |
| canonical 簇 / cluster | 合并后的知识对象 |
| squash 同名 / spelling variant（待确认合并里两个名字只差空格、连字符、下划线、点号或括号的一对，`kind=spelling_variant`） | 同名不同写法 |
| outlier source | 关联稀疏的来源 |
| centrality | 核心度 |
| finding（图谱分析问题清单里的一条） | 问题 |
| dismiss / exemption | 标为正常 / 撤销 |
| off-topic source（F1） | 疑似内容错配 |
| duplicate import（F2） | 疑似重复导入 |
| parse damage（F3） | 疑似解析残缺 |
| under-merge（F4） | 疑似没合并的同名概念 |
| giant component / 主连通块 | 主体内容 |
| island component | 孤岛 |
| handled（已处置、等下一次更新分析核验） | 已处理，更新后核验 |
| reparse placeholder（已提交、这一次还没跑完的重新解析；前端 `reparsingSourceIds`） | 已提交重新解析，等待完成 |
| dev-side defect | 需要开发处理 |
| Jaccard / 连通分量 | 不上屏（重合度说「概念重合度」，结构说「主体内容」「孤岛」） |
| 画像 / 底座画像（agent_notebook_profile 共享层） / 巡固（consolidation job） | AI 对这个库的理解 |
| 画像 / 覆盖层画像（agent_notebook_profile 个人层） | 我的检索心得 |
| 观察队列（observation, agent_observations） | Agent 记录 |
| 调用记账（call ledger，`agent_observations.kind='call'`） | 调用记录 |
| 能力档（capability scope，`ask:execute` 这类协议串同样不上屏） | 按动作说人话（提问 / 查资料 / 添加资料…） |
| 回答风格偏好（search profile, user_profiles.search_profile_json） | 我的回答偏好 |
| consult_memory（模型主动拉取检索经验的 reflect 动作／trace 步） | 回想 |
| spreadsheet analysis / workbook analysis（用户界面） | Excel 专业分析 |
| analysis issue / parse failure（管理员界面） | 解析问题 |

## 解释与例外

- 来源类型 `promotion`（PR-E8）是公共知识库为每份贡献原件建立的来源：来源详情的类型标签显示
  「收录」（`frontend/app/source-management.ts::sourceTypeTag`）；来源列表的徽标在仍有知识条目引用它时显示
  「已收录」，已无条目引用时显示「不在图谱中」（`source-kg-badge.ts`）；详情不出现「重新解析」，删除确认写明
  只有失去全部证据的条目会一起删除（`sourceDeleteMessage`）。它的标题「收录自：<原标题>」／「收录自个人记忆：<记忆标题>」
  是界面文案，**受词表约束**（用户 2026-10-19 裁决把 9-29 的「晋升自」改为「收录自」）：`check_ui_vocabulary.py` 的第七条通道扫
  `promotion_source.py` 里写入用的前缀常量，写回「晋升自」会红。2026-10-19 之前写入的存量标题在库里仍是「晋升自：…」，
  不改库内数据，读取时经 `presented_source_title` 映射成「收录自」（`LEGACY_*` 前缀常量是存储拼写，不上屏，守卫不扫）；
  引用卡上显示的是原件标题。
- 「仅检索」是只做检索、不生成回答的提问（MCP `ask` 的 `output="evidence"`，内部值 `evidence`）
  在活动记录与提问分析里的标签；详情写「仅检索，未生成回答」。内部值 `evidence` / `output`
  不上屏，也不写成「证据模式」。
- 全局问答的 👍/👎 并进笔记本知识分析看板与管理端提问分析时，来源一律写「全局问答」：数字后注「其中全局问答 N 条」（为 0 不显示），看板「低分提问（知识缺口）」列表末尾写「另有 N 条来自全局问答」（全局提问原文不上屏）；看板的提问数含归入本库的全局问答（与 👍/👎 同一归因口径），标签写「提问」，后注「（其中全局问答 N 条）」（为 0 不显示）；不再写「笔记本内提问」。评分两档沿用回答下方按钮的名字「有用」「需改进」，不写「点赞」「差评」或内部值 `useful` / `not_useful`。
- 「知识对象」或「知识条目」是图谱对象的统称；「概念」只是内置 `concept` 类型的
  界面名，不能覆盖 claim、formula、procedure 或 Knowhow 自定义对象类型。
- 「图谱 Schema」是 `schema` 行唯一允许的英文复合界面短语。裸 `schema` / `Schema`
  仍不允许；`scripts/check_ui_vocabulary.py::SANCTIONED_UI` 只放行这一复合短语。
- 保留「知识图谱」「索引」和 Knowledge 页签名「知识库」。图谱技术上下文可用裸
  「节点」「边」；散文优先用「知识对象」「关联」。由于中文子串歧义，自动守卫只检查
  「孤立节点」「补连边」「关系边」「边审」等无歧义复合形态。
- 图谱质量分析中的 `canonical` / `clusters` 使用「合并后的知识对象」，
  `communities.centrality` 使用「核心度」；`basis` 映射为：`usable_live` →
  「整理当时的实时口径」、`community_snapshot` → 「上次主题板块划分」、
  `unified_rebuild_snapshot` → 「上次整理时的规模」。
- 图谱分析的问题清单（`frontend/app/kg-analysis-model.ts`）：规则编号 F1–F5 不上屏，界面只说
  「疑似内容错配」「疑似重复导入」「疑似解析残缺」「疑似没合并的同名概念」「没建立关联的内容」（F5 沿用
  既有词）；严重度说「高 / 中 / 低」；读侧状态 `handled` 说「已处理，更新后核验」、`dismissed` 说「已标为
  正常」、F4 的 `decided_not_same` 说「已判定不是同一个」、F5 的 `relink_spent` 说「补上关联已经跑过」（放在
  诊断数据里，不算「已通过」）；四个区叫「待处理」「需要开发处理」「诊断数据」
  「已通过」。「主体内容」指彼此有关联连成的最大那一片知识对象，「孤岛」指与它没有任何关联的小块。
  「补上关联」「待确认合并」「重新解析」「删除来源」「主题板块」「合并后的知识对象」沿用既有词。
  重新解析从提交到这一次跑完，引用那篇来源的「重新解析」入口（问题清单的每一行、来源详情）置灰并就地写
  「已提交重新解析，等待完成」，不说「排队」「作业」。
## 答案正文内的标记

问答与深度报告正文里句首/段首出现的「（推断）」「(推断)」「Likely,」与「【通识】」是模型输出
内容的一部分，不是本文件登记的界面文案（JSX 文本、`label`、`title`、`placeholder`、
`aria-label`、toast、错误、表头）；它们各自的界面呈现名是「推断」与「通识」。前端渲染层只把
这几种句首/段首前缀包成不可点的行内标签样式，不改写、不新增文本内容，复制结果不变。
`scripts/check_ui_vocabulary.py` 的黑名单扫描面是渲染进前端源码的固定文案，以及后端写、
前端原样展示的文案（`user_error()` 消息、推理轨迹摘要、登记的展示字段、模型显示名），不覆盖模型生成的
答案正文，因此这两个标记不登记进「界面词汇表」，也不需要 `NOT_LINTABLE` 豁免理由。

## 自动守卫边界

`scripts/check_ui_vocabulary.py` 由 `scripts/check.sh` 执行。守卫作用域跟着信任边界走，
不跟着目录树走：它既扫描 `frontend/app` 与 `frontend/features` 的渲染文本，也扫描后端
`user_error(status, "…")` 的消息，因为这类 4xx `detail` 会被明确标为可展示并由
前端原样呈现；推理轨迹摘要同理。裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内；它是内部诊断 / MCP
合同，不会直接上屏。

后端写、前端原样展示的另一类文案是**登记的展示字段**：索引管线选项的
`IndexingPipelineOption(label=…, description=…)`，以及知识图谱分析作业的失败文案
（`….kg_build_jobs.finish/set_stage(…, error_message=…)`，经 `KgBuildJobStatus.user_message`
弹出）。它们按调用形状显式登记（`DISPLAY_FIELD_CALLS` / `DISPLAY_FIELD_METHODS`），不按 kwarg
名泛扫——别处的 `error_message=` 多是不上屏的诊断原文。新增一种会原样上屏的后端字段时，把它
的形状登记进去。作业接收者认一跳别名（`jobs = r.kg_build_jobs; jobs.finish(…)`）；启动兜底用
SQL 字面量 `UPDATE kg_build_jobs SET … error_message='…'` 写入的文案（SQLite 迁移与 PG 维护
路径）同样在扫描面内。`error_message` 在两端存储里是仅限关键字参数，不存在位置传参；`**kwargs`
展开、多于一跳的别名与跨模块传入的接收者静态解析不了，刻意跳过。

问答「就此追问」一轮不读关联报告时，回执（`LinkedReportReceipt(reason=…)`）与标签旁的发送前
预检（`ReportLinkCheck(reason=…)`）原样展示同一句原因，这两个调用形状也登记为展示字段。这句话
由前缀 `REASON_PREFIX` 加一个子句组成，子句只写在 `backend/app/domain/report_followup.py` 的具名
常量里；调用点经跨模块导入和下标取用，上面的解析器按设计不跟这两种写法，所以守卫直接扫描这几个
出处常量（字符串与字典的每个值各算一条），并按名字集合核对：改名或删掉其中一个都会让守卫失败。

**模型显示名**也原样上屏：`backend/app/services/model_registry.py` 里 `_WORKLOAD_LABELS` 的值
作为 `workload_label` 出现在模型失败横幅、管理员用量页和解析问题档案里；仓库出厂的模型服务
显示名（`scripts/migrate_legacy_model_env.py` 的 `ROLE_LABELS`、`model-services.example.toml`
的 `display_name` 行）会写进部署的配置，再出现在模型服务状态卡片上。守卫只读这几处的字符串
字面量，各有非空下限。运维自己在部署配置里写的服务名是部署数据，不在扫描面内；解析问题档案里
已经保存下来的旧显示名也不会被改写。

后端 `user_error()` 消息、推理轨迹摘要（`TRACE_SUMMARY_MODULES` 登记模块里的
`TraceStep(summary=…)`）与展示字段三条通道不要求调用点写字面量：先赋给变量（按作用域查所在
函数及外层函数的赋值，再查模块常量）、
条件表达式（两个分支各算一条）、`+` 拼接、`%` / `.format`、`X.get(k, 默认)` 与本模块 helper
的 `return` 都会被解析后检查；`str(exc)`、异常属性、跨模块导入的常量等静态解析不了的仍跳过。
这三条通道在匹配前剥掉部署实际读取的环境变量名（从 `backend/app/core/config.py` 推出，如
`SILICON_NOTEBOOK_AUTH_OPTIONAL`）；裸 `notebook`、裸 `KG`，以及 `KG_NOT_READY` 这类不是部署
配置键的全大写内部码照报。

守卫是词黑名单，不是完整语义检查；它会剥离注释、标识符和插值，新增界面文案仍须人工
对照本表。`frontend/tests/guards/raw-enum-fallback.test.mjs` 另行禁止把未知枚举原值直接
展示给用户；`frontend/tests/guards/english-control-copy-guard.test.mjs` 禁止 JSX `title` /
`aria-label` 的字面结果值是纯英文（含英文字母且不含中文；符号、空串、数字不算），它看不见
先算好再传的变量、对象取值、展开属性、自定义 prop 转手与 `setAttribute`，这些仍靠人工；
`backend/tests/test_ui_vocabulary_guard.py` 保证表中每个词条都被规则覆盖或有显式豁免理由。
