# 半导体 Knowhow Notebook 产品方案

## 1. 产品定位

产品定位为：

> **面向半导体研发团队的用户自建 knowhow notebook 平台。**

用户可以创建 notebook，上传或接入历史规则文档、经验文档、案例文档、review checklist、技术文章、白皮书、app note 等资料。系统把这些资料转化为可查询、可引用、可审核、可推演、可持续演化的工程知识系统。

核心价值是：

> **帮助半导体团队把散落的历史经验和新技术文章，沉淀成可被工程师在具体场景下使用的规则、方法、风险提醒、历史案例和检查清单。**

---

## 2. 目标使用场景

产品服务于半导体研发团队的日常知识复用和技术判断场景。

典型场景包括：

1. 工程师遇到某个设计、封装、layout、ESD、可靠性或 debug 场景，希望查询已有经验。
2. 新员工需要理解团队过去积累的规则、方法和踩坑案例。
3. 项目 review 前，需要根据当前场景生成检查清单。
4. 资深工程师希望把历史文档整理成可维护的规则库。
5. 团队阅读一篇新技术文章后，希望判断它对已有规则和方法的影响。
6. 团队希望根据文章和历史 knowhow 推演新的风险、方法、验证计划或候选规则。

---

## 3. 用户角色

## 3.1 Notebook Builder / Curator

负责创建和维护 notebook。

典型用户包括：

* 资深模拟设计工程师
* 封装专家
* layout 方法学负责人
* ESD / reliability 专家
* 技术负责人
* design review owner
* 项目知识管理人员

他们的主要任务是：

* 创建 notebook
* 定义 notebook 的用途和范围
* 上传或接入资料
* 审核系统抽取出的规则、方法、案例和 checklist
* 合并重复规则
* 处理冲突规则
* 发布 notebook 给团队使用
* 根据用户反馈持续更新 notebook
* 根据新文章沉淀新的候选规则

---

## 3.2 Notebook Consumer / End User

使用 notebook 解决具体工程问题。

典型用户包括：

* 新工程师
* 项目工程师
* 模拟设计工程师
* layout 工程师
* 封装工程师
* debug 工程师
* review 参与者
* 技术经理

他们的主要任务是：

* 根据具体场景提问
* 查询适用规则
* 查询推荐方法
* 查询潜在风险
* 查询历史案例
* 生成 review checklist
* 理解某条规则的来源和原因
* 基于文章进行技术推演
* 反馈答案质量

---

## 4. Notebook 的产品结构

每个 notebook 是一个用户创建的知识应用。

一个 notebook 包含：

```text
Notebook
  ├─ 用户定义的用途和范围
  ├─ 用户上传或接入的 sources
  ├─ 规则 Rule
  ├─ 方法 Method
  ├─ 风险 Risk
  ├─ 案例 Case
  ├─ 检查项 Checklist Item
  ├─ 术语 Term
  ├─ 文章研究 Article Research
  ├─ 推演结果 Inference / Hypothesis
  ├─ 候选规则 Derived Rule Candidate
  ├─ 人工审核状态
  ├─ 用户反馈
  └─ 权限、版本和发布状态
```

用户可以创建不同 notebook，例如：

```text
Analog Packaging Knowhow
Analog Layout Best Practices
High Voltage Design Rules
ESD Review Checklist
Sensor Interface Debug Cases
Power Module Reliability Lessons
SerDes Bring-up Knowhow
Foundry Communication Notes
Mixed-Signal Noise Isolation
```

---

## 5. 核心知识对象

系统需要把用户资料转化为结构化知识对象。

## 5.1 Source

原始资料。

```yaml
Source:
  title:
  type: guideline / report / checklist / training / article / whitepaper / app_note / other
  owner:
  version:
  date:
  upload_time:
  status:
  access_scope:
```

---

## 5.2 Evidence

所有结论、规则、方法、案例和推演都绑定证据。

```yaml
Evidence:
  source_id:
  source_title:
  location: page / section / slide / paragraph / table / figure
  extracted_fact:
  quoted_span:
  confidence:
```

---

## 5.3 Scenario

用户当前查询的工程场景。

```yaml
Scenario:
  domain:
  block_type:
  design_stage:
  package_type:
  signal_type:
  concern:
  constraint:
  process_or_node:
  application:
  missing_information:
```

示例：

```text
Domain: Analog IC
Block: Low-noise analog front-end
Stage: Package review
Package: Wirebond
Concern: Noise / parasitic / ESD
```

---

## 5.4 Rule

规则卡片。

```yaml
Rule:
  title:
  statement:
  applies_to:
  condition:
  recommendation:
  risk_if_ignored:
  exception:
  severity:
  rule_type: mandatory / recommended / advisory / project_specific
  status: draft / reviewed / approved / deprecated / conflict
  source_evidence:
  owner:
  last_reviewed:
```

---

## 5.5 Method

方法或 best practice。

```yaml
Method:
  name:
  use_when:
  benefit:
  limitation:
  tradeoff:
  required_condition:
  related_rules:
  related_cases:
  source_evidence:
```

---

## 5.6 Case / Issue

历史案例或踩坑记录。

```yaml
Case:
  symptom:
  context:
  root_cause:
  resolution:
  lesson_learned:
  related_rules:
  related_methods:
  source_evidence:
```

---

## 5.7 Checklist Item

可执行检查项。

```yaml
ChecklistItem:
  question:
  applies_to:
  required_evidence:
  related_rules:
  related_cases:
  severity:
  source_evidence:
```

---

## 5.8 Article Claim

文章中的技术主张。

```yaml
ArticleClaim:
  statement:
  claim_type: mechanism / result / recommendation / warning / comparison
  evidence:
  measurement_condition:
  source_location:
  confidence:
```

---

## 5.9 Inference / Hypothesis

基于文章、规则和场景生成的推演。

```yaml
Inference:
  statement:
  based_on_claims:
  based_on_rules:
  reasoning_type:
  confidence:
  uncertainty:
```

```yaml
Hypothesis:
  statement:
  why_plausible:
  required_validation:
  risk_if_wrong:
  related_scenarios:
  related_rules:
  source_basis:
```

---

## 5.10 Derived Rule Candidate

从文章或推演中生成的候选规则。

```yaml
DerivedRuleCandidate:
  title:
  proposed_rule:
  applies_to:
  rationale:
  supporting_article_claims:
  supporting_existing_rules:
  limitations:
  status: draft
  required_reviewer:
```

---

# 6. 核心功能模块

## 6.1 Notebook 创建

用户创建 notebook 时，填写：

```text
Notebook name
Purpose
Target users
Expected questions
Source types
Primary domain
Taxonomy
Access scope
```

示例：

```text
Notebook name:
Analog Packaging Knowhow

Purpose:
帮助模拟设计和封装团队查询历史规则、风险、案例和 review checklist。

Target users:
模拟设计工程师、封装工程师、新员工、项目 review team。
```

---

## 6.2 Notebook 模板

模板用于定义结构和处理方式。

建议提供：

| 模板                        | 用途                 | 核心结构                              |
| ------------------------- | ------------------ | --------------------------------- |
| Rule Notebook             | 内部规则、设计指南          | Rule / severity / exception       |
| Method Notebook           | 方法选择、best practice | Method / tradeoff / applicability |
| Case Notebook             | 历史问题、debug 经验      | Symptom / root cause / resolution |
| Review Notebook           | 设计审查、封装审查          | Checklist / evidence / sign-off   |
| Article Research Notebook | 技术文章研究             | Claim / limitation / implication  |
| General Knowhow Notebook  | 混合文档               | 通用知识对象                            |

---

## 6.3 Source 上传与接入

MVP 支持：

* PDF
* PPTX
* DOCX
* Markdown
* CSV / Excel
* 技术文章
* app note
* 白皮书
* guideline
* checklist
* review slides
* debug report
* postmortem report
* training material

后续支持：

* Confluence
* SharePoint
* Google Drive
* Jira
* Git
* Slack / Teams
* Email archive

---

## 6.4 自动抽取

系统处理资料后，自动生成：

```text
规则候选
方法候选
风险项
历史案例
checklist items
术语表
冲突点
文章 claim
文章 limitation
候选推演
候选新规则
```

系统可以在 notebook 首页展示：

```text
本 notebook 已检测到：

- 78 条候选规则
- 21 个候选方法
- 34 个历史案例
- 56 条 checklist items
- 42 个术语
- 7 处潜在冲突
- 5 条文章推演结论
- 3 条候选新规则
```

---

## 6.5 Ask Knowhow

最终用户通过自然语言提问。

示例问题：

> 低噪声模拟前端使用 wirebond 封装时，pin assignment 需要注意什么？

系统回答包含：

```text
结论
适用场景
适用规则
推荐方法
潜在问题
历史案例
建议 checklist
缺失信息
引用来源
```

Ask 必须区分“找最相关证据”和“完整读取有限集合”。逐步推理提供五个稳定档位，默认 `standard`；档位只改变相关性检索、反思与答案合成预算，不得把用户显式要求的“所有/完整/总数”降级成普通 Top-N。最终相关性预算按 `min(cap, max(floor, aspect × 实际执行查询数))` 计算，模型可在证据充分时提前停，但不能突破上限：

| 档位 id | 界面名 | 每查询取数 | 最终 floor/aspect/cap | 最大步骤/首轮子查询 | KG/原文上下文字符 |
|---|---|---:|---:|---:|---:|
| `overview` | 概览 | 4 | 8/2/12 | 4/2 | 4,000/12,000 |
| `standard` | 标准 | 8 | 20/3/36 | 8/5 | 6,000/30,000 |
| `deep` | 深入 | 8 | 24/4/48 | 16/6 | 8,000/50,000 |
| `thorough` | 详尽 | 12 | 32/5/64 | 32/8 | 12,000/80,000 |
| `exhaustive` | 穷尽 | 16 | 40/6/96 | 50/10 | 16,000/120,000 |

候选召回不随档位变化，而由部署参数独立控制：`CHUNK_RECALL` 默认 200，分别约束带索引 Chunk/KG 的 ANN 与词法窗；`RELATION_RECALL` 默认 200，分别约束 Relation ANN 与词法关系 ID 总窗（source/target 两方向仍在该总窗内预留份额）。这些值可由部署修改，不能在界面中冒充请求级硬上限。意图合同把结果范围标成 `ranked` / `complete` / `aggregate` / `hybrid`。Knowhow 的后三类请求使用稳定游标枚举，不靠放大 Top-N；100 行表应能返回可验证的 `100/100` 覆盖率。五档共用以下完整枚举上限，较低档位也不得缩小：每页 25 行、最多 50 页/1,250 物理行、8 表、每表 8 列、模型单元格摘录 1,000 字符、结构化载荷 256,000 字符、答案正文内联 100 行、结果卡初始显示 20 行。只有游标耗尽且表目录、行数、列元数据和所选表范围均稳定才可标记完整；触及任一行/页/表/列/载荷上限或并发改表，必须返回 `complete=false` 与 `explicit_partial`，绝不能写成“全部”。正文和界面初始显示上限只影响展示，已返回的结构化行仍可展开并跳回原 Knowhow 行。当前完整枚举范围只覆盖 Knowhow 表；KG 对象、来源元素、Memory 等其他集合在没有对应枚举器时仍是相关性结果，必须披露“未完整枚举”。

确认时必须按最终编辑措辞与权威澄清答案重算 scope。结构化执行器只接受可证明的整表物理行/方法清单、直接物理行/记录计数及其 hybrid；条件筛选、“多少种”等去重/种类计数和分组没有确定性计划时回退并披露不支持完整。轻量 catalog 最多返回 8 个表描述且不读取格/代码/健康详情，并在截窗前优先纳入显式点名表。响应分开单表、批次与分析 coverage：200/200 枚举配 100/200 分析明确为“枚举完整、分析部分”，8 表截断不把已耗尽单表降为 partial。KG/Memory/链共享 KG 字符硬预算，结构化预览/chunk/direct element 共享原文预算，最终证据不超过两者之和。

---

## 6.6 Scenario Builder

用户通过结构化方式描述场景。

例如：

```text
Domain: Analog design
Block: Low-noise AFE
Stage: Package review
Package: Wirebond
Concern: Noise / parasitic / ESD
```

系统生成：

* 适用规则
* 相关方法
* 风险清单
* 历史案例
* 检查清单
* 缺失信息
* 下一步验证动作

---

## 6.7 Rule Browser

用户可以筛选规则：

```text
Domain = Analog design
Package = Wirebond
Concern = Noise
Severity = High
Status = Approved
```

每条规则卡片展示：

* 规则内容
* 适用场景
* 例外条件
* 风险
* 相关方法
* 相关案例
* 来源
* 状态
* owner
* 更新时间

---

## 6.8 Case Search

用户可以查询历史类似案例。

示例问题：

> 实验室测得噪声比仿真高，怀疑和封装有关，以前有类似问题吗？

系统返回：

* 相似案例
* 当时的上下文
* 症状
* root cause
* 解决方法
* 形成的规则
* 引用来源

---

## 6.9 Checklist Generator

用户输入场景：

> 我要 review 一个低噪声模拟前端的 QFN 封装方案。

系统生成：

```text
Package Review Checklist

1. Sensitive analog pins
2. Ground return path
3. Package parasitic
4. Thermal gradient
5. ESD path
6. Stress-sensitive devices
7. Board / package / die consistency
```

每个 checklist item 包含：

* 检查问题
* 适用场景
* 相关规则
* 相关案例
* 所需 evidence
* severity
* source citation

---

## 6.10 Explain Rule

用户可以询问某条规则的原因。

示例问题：

> 为什么在这个场景下需要检查 bondwire coupling？

系统回答：

* 规则原文
* 规则来源
* 形成原因
* 历史案例
* 适用场景
* 例外条件
* 相关风险
* 相关检查项

---

# 7. Article Studio：文章研究与推演

Article Studio 用于研究新文章，并将文章内容和已有 notebook knowhow 关联起来。

## 7.1 Article Research Brief

用户上传文章后，系统生成：

```text
文章核心贡献
解决的问题
关键 claim
文章提出的方法
技术机制
实验条件
适用边界
局限性
潜在风险
和已有规则的关系
可沉淀的新规则候选
建议验证计划
```

---

## 7.2 文章与 notebook 的关联

系统判断文章内容与已有规则、方法、案例的关系：

```text
supports：支持已有规则
extends：扩展已有规则
refines：细化适用条件
challenges：挑战已有规则
creates risk：提示新风险
suggests checklist：产生新检查项
suggests validation：产生验证动作
```

---

## 7.3 推演分层

文章推演结果按层级表达：

```text
Level 0：文章直接证据
Level 1：文章内部解释
Level 2：结合 notebook 的场景化推论
Level 3：面向用户场景的技术假设
Level 4：研究方向或验证建议
```

这样用户可以清楚看到：

* 哪些来自文章原文
* 哪些来自系统归纳
* 哪些来自与已有规则的关联
* 哪些是场景假设
* 哪些需要后续验证

---

## 7.4 Implication Map

系统展示文章对 notebook 的影响。

示例：

```text
Article Claim A
  ├─ supports Rule PKG-017
  ├─ extends Method M-012
  ├─ suggests Checklist Item C-031
  └─ creates Hypothesis H-004

Article Claim B
  ├─ challenges Rule PKG-023
  └─ requires packaging owner review
```

---

## 7.5 Derived Rule Candidate Queue

文章推演可以生成候选规则。

示例：

```text
Candidate Rule:
对于低噪声模拟输入，package review 应检查 bondwire / return path / high-switching loop 之间的耦合关系。

Supporting evidence:
- Article X, Section 3
- Existing Rule PKG-017
- Historical Case CASE-2021-AFE-Noise

Status:
Draft

Required reviewer:
Packaging owner
```

Curator 审核后，候选规则可以进入正式规则库。

---

# 8. 回答格式规范

系统回答采用工程化结构。

## 8.1 场景问答格式

```text
结论：
基于当前 notebook 中的资料，该场景下建议重点关注 A / B / C。

适用场景：
这些建议适用于哪些 block、package、信号类型、设计阶段。

推荐方法：
1. 方法 A
   - 适用原因
   - 前提条件
   - 潜在副作用
   - 来源

2. 方法 B
   ...

相关规则：
- Rule 1：状态、适用条件、来源
- Rule 2：状态、适用条件、来源

潜在问题：
- 问题 1：触发条件、后果、历史案例
- 问题 2：触发条件、后果、历史案例

历史案例：
- Case A：症状、原因、解决方法、来源

建议 checklist：
- 检查项 1
- 检查项 2
- 检查项 3

缺失信息：
- 当前问题缺少哪些上下文
- 当前 notebook 覆盖到哪些范围

引用：
每条规则、案例、方法和 checklist 都可跳转到原始 source。
```

---

## 8.2 文章推演格式

```text
文章直接结论：
文章明确陈述或证明的内容。

文章机制解释：
文章内部逻辑支持的理解。

与 notebook 的关系：
支持、补充、细化或挑战哪些规则。

场景化推演：
在用户指定场景下可能产生的影响。

候选规则：
可以沉淀的新规则或规则更新建议。

建议验证：
建议做的实验、仿真、review 或资料补充。

引用：
所有 claim、推演和候选规则都绑定证据。
```

---

# 9. 产品界面

## 9.1 Builder View

面向 notebook 创建者和维护者。

页面包括：

```text
Sources
Extracted Rules
Extracted Methods
Extracted Cases
Checklist Items
Article Claims
Derived Rule Candidates
Conflicts
Taxonomy
Review Queue
Notebook Settings
Analytics
```

核心能力：

* 查看资料处理状态
* 审核候选规则
* 编辑 rule card
* 合并重复规则
* 处理冲突
* 审核文章推演
* 发布 notebook
* 查看使用反馈

---

## 9.2 User View

面向最终用户。

页面包括：

```text
Ask
Scenario Query
Rule Browser
Case Search
Checklist Generator
Article Studio
Saved Answers
Evidence Panel
```

核心能力：

* 提问
* 构造工程场景
* 查询规则
* 查询案例
* 生成 checklist
* 研究文章
* 查看证据
* 保存和导出结果

---

## 9.3 推荐三栏布局

```text
左栏：Sources / Rules / Cases / Articles
中间：Ask / Scenario / Article Research / Checklist
右栏：Evidence / Related Rules / Related Cases / Uncertainty / Actions
```

这个布局可以让用户同时看到问题、答案和证据链。

---

# 10. 技术架构

```text
Data Sources
  ├─ Uploaded docs: PDF / PPTX / DOCX / XLSX / Markdown
  ├─ Internal docs: wiki / review / debug / guideline
  ├─ Articles: paper / whitepaper / app note / internal report
  └─ Connectors: Confluence / SharePoint / Jira / Git / Slack

Ingestion Layer
  ├─ Document parser
  ├─ Slide parser
  ├─ Table parser
  ├─ Layout-aware PDF parser
  ├─ Rule extractor
  ├─ Method extractor
  ├─ Case extractor
  ├─ Checklist extractor
  ├─ Article claim extractor
  ├─ Scenario tagger
  └─ Citation mapper

Knowledge Layer
  ├─ Vector index
  ├─ BM25 / keyword index
  ├─ Structured rule database
  ├─ Scenario ontology
  ├─ Method library
  ├─ Case library
  ├─ Checklist library
  ├─ Article claim store
  ├─ Evidence store
  └─ Knowledge graph

Reasoning Layer
  ├─ Scenario understanding
  ├─ Hybrid retrieval
  ├─ Rule matching
  ├─ Case matching
  ├─ Applicability check
  ├─ Conflict detection
  ├─ Article implication engine
  ├─ Hypothesis generation
  ├─ Checklist generation
  ├─ Citation validation
  └─ Answer grounding

Application Layer
  ├─ Notebook creation
  ├─ Builder view
  ├─ User ask view
  ├─ Rule browser
  ├─ Case search
  ├─ Review mode
  ├─ Article Studio
  ├─ Evidence panel
  ├─ Curator workflow
  └─ Analytics

Security & Governance
  ├─ RBAC
  ├─ Source-level permission
  ├─ Tenant isolation
  ├─ Audit log
  ├─ Version control
  ├─ Approval workflow
  ├─ Data encryption
  └─ Private / VPC deployment
```

---

# 11. 检索和推理策略

系统采用 hybrid retrieval。

```text
1. Keyword / BM25
   精确匹配术语、规则编号、封装类型、模块名、项目名。

2. Vector Search
   找语义相近的经验、案例和解释。

3. Metadata Filter
   根据 notebook、source 类型、版本、状态、权限、适用场景过滤。

4. Structured Rule Matching
   根据 scenario 匹配 rule applies_to / condition。

5. Case Similarity Search
   根据 symptom、context、root cause 检索历史案例。

6. Knowledge Graph Traversal
   查找 rule、case、method、article claim 之间的关系。

7. Reranking
   对候选证据排序。

8. Citation Validation
   校验回答中的结论和证据对应关系。
```

---

# 12. 规则治理机制

规则状态建议设计为：

```text
Draft:
AI 从文档抽取出的候选规则。

Reviewed:
专家已查看并初步确认。

Approved:
团队正式认可并可用于回答。

Deprecated:
已废弃。

Conflict:
存在冲突，需要处理。

Project-specific:
仅适用于某个项目或特定上下文。

Article-derived:
由文章研究和推演生成，等待验证和审核。
```

系统在回答中展示规则状态，例如：

```text
正式规则：
Rule A，Approved，适用于当前场景。

经验建议：
Rule B，Reviewed，适合作为参考。

历史案例：
Case C，与当前场景相似，封装类型略有差异。

文章推演：
Hypothesis D，来自新文章推演，需要进一步验证。

冲突信息：
Rule E 与 Rule F 在适用条件上存在差异，建议 owner review。
```

---

# 13. Notebook 生命周期

```text
1. 用户创建 notebook
2. 用户上传历史文档
3. 系统解析 source
4. 系统抽取候选规则 / 方法 / 案例 / checklist
5. Curator 审核、修改、合并、废弃
6. Notebook 发布给最终用户
7. 用户进行场景查询和 checklist 生成
8. 用户反馈答案质量
9. 新文章进入 Article Studio
10. 系统推演文章对已有规则的影响
11. 系统生成候选规则或候选 checklist
12. 专家审核后更新 notebook
```

---

# 14. MVP 路线

## v0.1：Knowhow Notebook MVP

目标：

> 支持用户创建 notebook，上传历史 knowhow 文档，系统抽取规则和经验，并支持基于场景的问答和 checklist 生成。

P0 功能：

```text
1. 创建 notebook
2. 上传 sources
3. source summary
4. 自动抽取 rule candidates
5. 自动抽取 method candidates
6. 自动抽取 risk items
7. 自动抽取 checklist items
8. 自动抽取 glossary terms
9. 自然语言问答
10. 场景化查询
11. 引用来源
12. 生成 checklist
13. curator approve / reject / edit candidate rules
14. end user feedback
```

---

## v0.2：Rule Card & Curation

目标：

> 把候选知识沉淀成可维护规则库。

新增功能：

```text
1. Rule card
2. Rule status
3. Rule owner
4. Duplicate rule merge
5. Conflict detection
6. Case extraction
7. Similar case search
8. Custom taxonomy
9. Notebook publish workflow
10. Answer quality analytics
```

---

## v0.3：Article Studio & Derivation

目标：

> 支持新文章研究和基于 notebook 的受控推演。

新增功能：

```text
1. Article Research Brief
2. Article claim extraction
3. Method / limitation / condition extraction
4. Related rule matching
5. Supports / extends / challenges analysis
6. Scenario-based implication
7. Derived rule candidate queue
8. Validation plan generator
9. Checklist update suggestion
```

---

## v0.4：Review Mode

目标：

> 进入真实工程 review 流程。

新增功能：

```text
1. Review session
2. 场景化 checklist
3. Reviewer comments
4. Action items
5. Export review report
6. Sign-off evidence
7. Project-level workspace
```

---

## v1.0：Enterprise Platform

目标：

> 支持企业级部署、权限、安全和团队级知识治理。

新增功能：

```text
1. RBAC
2. Source-level permission
3. Audit log
4. SSO
5. Private / VPC deployment
6. Confluence / SharePoint / Jira connector
7. Multi-notebook search
8. Rule version diff
9. Knowledge gap dashboard
10. Usage analytics
```

---

# 15. MVP Demo 建议

第一个 demo 建议聚焦：

> **Analog IC Packaging Knowhow Notebook**

准备资料：

```text
10 份模拟设计 guideline
10 份封装设计 guideline
10 份 design review slides
10 份历史 debug / postmortem
5 份 checklist
3–5 篇相关技术文章
```

Demo 流程：

```text
1. 用户创建 notebook：Analog Packaging Knowhow
2. 上传历史资料
3. 系统自动抽取规则、方法、案例、checklist 和术语
4. Curator approve 一部分规则
5. 最终用户提问：
   “低噪声模拟前端使用 wirebond 封装时要注意什么？”
6. 系统输出：
   方法、风险、规则、历史案例、checklist、引用
7. 用户上传一篇新文章
8. 系统分析文章对已有规则的影响
9. 系统生成候选新规则和 checklist 更新建议
10. Curator 审核候选规则
```

这个 demo 展示完整闭环：

```text
历史文档
→ 结构化 knowhow
→ 场景化查询
→ 工程 checklist
→ 文章研究
→ 技术推演
→ 规则演化
```

---

# 16. 质量评估指标

## 16.1 检索质量

```text
正确文档召回率
正确规则召回率
相关案例召回率
关键风险覆盖率
引用版本准确率
```

---

## 16.2 回答质量

```text
适用场景表达准确率
规则状态表达准确率
强制规则和经验建议区分准确率
checklist 可执行性
引用准确率
缺失信息识别能力
冲突规则识别能力
```

---

## 16.3 抽取质量

```text
Rule precision
Rule recall
Method extraction accuracy
Risk extraction accuracy
Case extraction accuracy
Checklist extraction accuracy
Applicability extraction accuracy
Duplicate merge accuracy
Conflict detection accuracy
```

---

## 16.4 文章推演质量

```text
Article claim extraction accuracy
Limitation extraction accuracy
Condition matching accuracy
Rule impact detection accuracy
Hypothesis 标注清晰度
Derived rule evidence strength
Validation plan usefulness
```

---

## 16.5 组织价值指标

```text
新人查找规则时间减少
Senior engineer 重复答疑次数减少
Review checklist 生成时间减少
历史问题复发率下降
内部 knowhow 文档使用率提升
高价值规则沉淀数量
用户反馈有用率
notebook 活跃使用率
```

---

# 17. 产品护城河

核心护城河来自：

```text
1. 用户内部私有 knowhow 的持续沉淀
2. Rule card 和 Case card 体系
3. 半导体工程场景 ontology
4. 场景化规则匹配能力
5. 历史案例复用能力
6. Article Studio 带来的规则演化能力
7. 进入 design review / package review 流程后的组织粘性
8. 证据链、权限、审核和版本治理能力
```

---

# 18. 最终方案一句话

> **构建一个面向半导体研发团队的用户自建 knowhow notebook 平台：用户上传历史规则、经验文档、案例、checklist 和技术文章后，系统自动抽取规则、方法、风险、案例和文章 claim，并支持基于具体工程场景的问答、方法推荐、风险提醒、历史案例检索、review checklist 生成，以及基于新文章的技术推演和候选规则沉淀。**

---

# 19. Agent Memory 系统（2026-07-13 已实施）

本节是当前产品对早期“用户记忆”概念的正式设计，详细契约见
`docs/superpowers/specs/2026-07-13-agent-memory-system-design.md`。它新增独立的 Memory
层，不恢复已退役的 Studio、Article 或 KG candidate queue，也不把 Memory 伪装成 source、
chunk 或 knowledge object。

## 19.1 形成、隐私与页面

- 每条 Memory 必须绑定一个 `notebook_id` 和一个创建者，归创建者私有；总 Memory 页面只是
  当前用户跨 notebook 聚合，notebook 卡片数量和 `问答 (Ask) | 知识库 (Knowledge) | 记忆 (Memory) | 深度报告 (Deep Report)`
  中的 Memory 标签是 notebook 局部视图。
- Ask 回答底部提供“保存到 Memory”。系统先生成不落库的标题/Markdown/标签预览，用户可编辑，
  确认后直接形成 `confirmed`。LLM 未配置或预览失败时，确定性使用问题作标题、清理显示引用后的
  回答作正文；预览后原 answer 被删除则拒绝保存可信 Ask Memory。
- Agent 经 MCP 只能写入 `candidate`。生命周期为
  `candidate | confirmed | rejected | deprecated`，所有修改与审核保留 revision/provenance。
  用户确认后 candidate 才成为正式 notebook Memory；拒绝或弃用后不再召回。
- 即使 notebook 通过链接共享，成员的 Memory 仍互不可见。Notebook 删除会按生命周期级联删除
  所有成员绑定在该 notebook 的私有 Memory；删除提示只说明后果，不展示成员身份、内容或数量。

## 19.2 两个检索平面与权威

- Agent 候选平面允许同一用户、同一 notebook 下获授权的所有 Agent profile 读取 candidate 和
  confirmed；必须具备 `memory:read_candidates` 才能读取 candidate。不同用户、不同 notebook、
  rejected/deprecated 均严格排除。
- Notebook 正式平面只有 confirmed，可进入网页 Ask、notebook 搜索、Deep Report 和 MCP
  `search_notebook_context`；candidate 没有临时绕过开关。
- 混合检索先用关键词/向量相关性形成候选，再以权威处理等分或冲突，固定次序为
  `candidate < personal 原始证据 < confirmed Memory < base KG/base 原始证据`。
  Memory 引用保留独立 provenance，不伪造 source/element id。

## 19.3 Agent profile、token 与 MCP

- 用户在总 Memory 页创建稳定 Agent profile，并签发明文只显示一次的 opaque token。Token 配置
  过期时间、默认 notebook、notebook allowlist 和最小 scope；可即时撤销。可用 scope 只有
  `knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
  `ask:execute`、`knowhow:code`。
- 签发回执同时给出公开、机器可读的 `GET /api/agent-mcp/onboarding` 说明链接。用户把链接与
  token 分开交给 Agent，由 Agent 在尚未配置 MCP 时先读取本部署的精确地址、客户端配置步骤与
  当前工具清单；链接与说明正文都不得携带或回显 token。
- MCP 使用官方 SDK 的 Streamable HTTP `/mcp`。本机只允许 loopback HTTP；远程默认允许明文 HTTP（可信内网默认：放宽 Host/Origin 校验并打印启动告警），设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS 与 DNS-rebinding 保护。
  每个新 session 先显式调用 `select_notebook`，服务端在后续每次数据调用重新校验 token、scope、
  allowlist、所选 notebook 和用户当前访问权。
- 精确工具集共十一个工具（七个 Memory/context 工具与四个 knowhow 工具）：
  `list_notebooks`、`select_notebook`、`search_agent_memory`、`search_notebook_context`、
  `get_memory`、`ask_notebook`、`propose_memory`、`list_knowhow_tables`、
  `get_knowhow_discrimination`、`get_knowhow_row`、`put_knowhow_cell_code`。
  knowhow 读取需 `knowledge:read`，格子代码写入需 `knowhow:code`。
  （以上是本方案定稿时的工具面；后续已扩展至二十四个工具——引用点查、来源管理、构建与库理解工具组，
  当前权威清单见 `mcp_server.PUBLIC_TOOLS` 与 `docs/product-and-api_zh.md`。）
  Agent 不能确认、拒绝、弃用或晋升 Memory；返回内容始终作为不可信 evidence/data，不作为指令。

## 19.4 Memory → KG 治理

只有 confirmed Memory 可由创建者提议提升到 KG。提案进入现有管理员 promotion queue；队列
展示脱敏后的结构化提取候选与服务端验证过的 evidence，不提供原始 Memory revision/provenance
浏览。批准前会重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，再复用既有
dedupe/merge，创建或合并一个或多个 Base KG 对象，并由 API 与晋升审计记录完整
`base_object_ids`。批准不会把私有 Memory 改为 base，也不会暴露完整私有任务上下文；二者仅通过
审核结果关联。

## 19.5 评价与发布门槛

固定 gold 计算 Recall@5、MRR、nDCG，并以三项零容忍计数守卫 candidate→正式平面、跨用户、
跨 notebook 泄漏；另有 A/B harness 对比 no-Memory、KB-only、KB+confirmed-Memory。发布门槛
还包含官方 MCP client 离线 smoke：定稿时为十一个工具契约（七个 Memory/context 工具与四个 knowhow
工具；该 smoke 现按上述扩展后的工具面锁定）、session 选择隔离、candidate 不进入正式上下文，以及同用户同 notebook 的跨 Agent
candidate 召回。
