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
| 孤立节点 / 补连边 | 没建立关联的内容 / 补上关联 |
| 边审 / 边审查队列 | 关系审核 / 关系审核队列 |
| Memory（残留英文散文） | 记忆 |
| schema（散文） | 内容类型 / 抽取字段（图谱对象类型/字段管理功能的界面名可用「图谱 Schema」） |
| deprecated（toast 直出） | 已弃用 |
| community / 社区 | 主题板块 |
| canonical 簇 / cluster | 合并后的知识对象 |
| outlier source | 关联稀疏的来源 |
| centrality | 核心度 |
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

## 答案正文内的标记

问答与深度报告正文里句首/段首出现的「（推断）」「(推断)」「Likely,」与「【通识】」是模型输出
内容的一部分，不是本文件登记的界面文案（JSX 文本、`label`、`title`、`placeholder`、
`aria-label`、toast、错误、表头）；它们各自的界面呈现名是「推断」与「通识」。前端渲染层只把
这几种句首/段首前缀包成不可点的行内标签样式，不改写、不新增文本内容，复制结果不变。
`scripts/check_ui_vocabulary.py` 的黑名单扫描面是渲染进前端源码的固定文案与后端
`user_error()` 消息字面量，不覆盖模型生成的答案正文，因此这两个标记不登记进「界面词汇表」，
也不需要 `NOT_LINTABLE` 豁免理由。

## 自动守卫边界

`scripts/check_ui_vocabulary.py` 由 `scripts/check.sh` 执行。守卫作用域跟着信任边界走，
不跟着目录树走：它既扫描 `frontend/app` 与 `frontend/features` 的渲染文本，也扫描后端
`user_error(status, "…")` 的消息字面量，因为这类 4xx `detail` 会被明确标为可展示并由
前端原样呈现。裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内；它是内部诊断 / MCP
合同，不会直接上屏。

守卫是词黑名单，不是完整语义检查；它会剥离注释、标识符和插值，新增界面文案仍须人工
对照本表。`frontend/tests/guards/raw-enum-fallback.test.mjs` 另行禁止把未知枚举原值直接
展示给用户；`backend/tests/test_ui_vocabulary_guard.py` 保证表中每个词条都被规则覆盖或有
显式豁免理由。
