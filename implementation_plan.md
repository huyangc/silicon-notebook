# Silicon Notebook 实现计划

## 1. 项目目标

项目名：`silicon-notebook`

产品目标：

构建一个面向半导体研发团队的自建 knowhow notebook 平台。用户可以创建 notebook，上传历史规则文档、经验文档、案例文档、review checklist、技术文章、白皮书、app note 等资料；系统将资料转化为可查询、可引用、可审核、可推演、可持续演化的工程知识系统。

MVP 的核心闭环：

```text
创建 notebook
-> 上传 sources
-> 文档解析与摘要
-> 抽取规则 / 方法 / 风险 / checklist / 术语
-> Curator 审核候选知识
-> End User 场景化提问
-> 返回带引用的规则、方法、风险、案例和 checklist
-> 用户反馈
```

## 2. 实现原则

1. Evidence-first：所有回答、规则、方法、案例、checklist 都必须能回到元素级 source evidence。
2. Human-in-the-loop：AI 只生成候选知识，正式规则需要 curator 审核。
3. Scenario-driven：查询和推荐围绕工程场景展开，而不是普通文档问答。
4. Structured knowledge：优先沉淀 Rule / Method / Risk / Case / Checklist / Term 等结构化对象。
5. MVP 直接面向真实团队可试用 beta：本机 demo 只是第一种部署形态，功能设计需要能承接真实资料、真实审核和真实反馈。
6. Provider-agnostic：LLM、embedding、storage、parser 尽量通过适配层隔离，避免早期绑定过死。

## 3. 已确认约束

1. MVP 目标：真实团队可试用 beta，而不是纯内部演示 demo。
2. 第一版部署：本机 demo，不使用 Docker。
3. 技术栈：前端使用 Next.js / React / TypeScript；后端使用 Python。数据库：本机 beta 采用 SQLite（向量以 JSON 存于 `element_embeddings`，检索时用 Python 余弦计算），无需 pgvector；后续规模化部署再迁移至 PostgreSQL + pgvector。
4. LLM 接入：使用 OpenAI-compatible API，通过系统配置提供 base URL、API key、model name 和 embedding model name。
5. 首批文件类型：PDF、Markdown、DOCX、PPTX。
6. MVP 暂不做图片解析，也不做图片 OCR。
7. 引用粒度：需要到元素级别，例如 PDF 页面文本块、Markdown heading/paragraph/list/table、DOCX paragraph/table、PPTX slide shape/text box/table。
8. 用户系统：MVP 先做单用户模式，但保留 User / Session / UserProfile 等模型，为后续用户记忆做准备。
9. Article Studio 进入 MVP。
10. Case extraction 和 Case Search 进入 MVP。
11. 首批资料为中英混合。
12. 当前没有真实半导体 demo 资料，MVP 允许先构造 synthetic demo dataset。
13. MVP 时间目标：越快越好，优先快速跑通本机 beta 闭环。
14. 用户记忆先不做自动写入，由用户手动决定是否加入记忆。
15. 产品和项目统一命名为 `silicon-notebook`。

## 4. MVP 范围

### 4.1 MVP 必做功能

1. Notebook 创建与管理
   - 创建 notebook
   - 设置 name、purpose、target users、primary domain、source types、access scope
   - 查看 notebook 首页统计

2. Source 上传与管理
   - 上传 PDF、Markdown、DOCX、PPTX
   - 文件元数据管理
   - 解析状态展示
   - source summary 生成
   - MVP 不解析图片内容，不对图片做 OCR

3. 文档解析与索引
   - 文档文本抽取
   - chunk 切分
   - 元素级 evidence 定位
   - embedding 索引
   - keyword / BM25 索引

4. 候选知识抽取
   - Rule candidates
   - Method candidates
   - Risk items
   - Case candidates
   - Checklist items
   - Glossary terms
   - 每个候选项绑定元素级 source evidence

5. Curator 审核工作流
   - 查看候选规则
   - approve / reject / edit
   - 设置 severity、status、owner
   - 查看原文证据

6. Ask Knowhow
   - 自然语言提问
   - hybrid retrieval
   - 结合 approved / reviewed 规则生成回答
   - 回答包含结论、适用场景、推荐方法、潜在风险、相关规则、建议 checklist、缺失信息、引用来源

7. Scenario Query
   - 用户结构化输入 domain、block type、stage、package、concern 等字段
   - 返回适用规则、方法、风险、checklist 和缺失信息

8. Checklist Generator
   - 基于用户场景生成 review checklist
   - 每个 checklist item 带 severity、相关规则和引用

9. Case Search
   - 抽取历史案例或 issue
   - 按 symptom、context、root cause、resolution 检索相似案例
   - 将案例和相关规则、方法、evidence 关联

10. Article Studio
   - 上传或接入技术文章 source
   - 生成 Article Research Brief
   - 抽取 article claims、limitations、conditions
   - 分析文章与已有规则的 supports / extends / refines / challenges 关系
   - 生成 scenario-based implication、validation plan 和 derived rule candidates

11. 用户反馈
   - useful / not useful
   - comment
   - 关联到 answer 和 notebook

### 4.2 MVP 暂不做或只预留接口

1. Confluence / SharePoint / Jira / Slack / Teams / Email connector
2. 图片解析 / OCR / 版图图片识别
3. Review session / sign-off / reviewer comments
4. SSO、企业级 RBAC、完整多租户隔离
5. Rule version diff
6. Knowledge gap dashboard
7. 复杂知识图谱可视化
8. 多用户协作和团队权限管理
9. 长期用户记忆的完整产品化体验

## 5. 推荐技术架构

以下是第一版本机 beta 的推荐架构。

```text
Frontend
  Next.js / React / TypeScript

Backend API
  Python FastAPI

Async Worker
  Celery / RQ / Dramatiq

Database
  PostgreSQL
  pgvector
  PostgreSQL full-text search

Object Storage
  Local filesystem for MVP
  S3 / MinIO for later deployment

LLM Layer
  OpenAI-compatible client
  Configurable base URL / API key / model name / embedding model name
  Structured output schemas
  Prompt versioning

Document Parsing
  PDF parser
  DOCX parser
  PPTX parser
  Markdown parser

Deployment
  Local development scripts for MVP
  Docker / Docker Compose later
  Kubernetes / VPC deployment later
```

### 5.1 架构分层

```text
Application UI
  -> Notebook workspace
  -> Builder view
  -> Ask view
  -> Scenario query
  -> Checklist generator
  -> Case search
  -> Article Studio
  -> Evidence panel

API Layer
  -> Single-user auth/session foundation
  -> Notebook API
  -> Source API
  -> Extraction API
  -> Curation API
  -> Ask API
  -> Case API
  -> Article API
  -> Feedback API

Knowledge Services
  -> Document parser
  -> Chunker
  -> Citation mapper
  -> Embedding indexer
  -> Rule extractor
  -> Method extractor
  -> Risk extractor
  -> Case extractor
  -> Checklist extractor
  -> Glossary extractor
  -> Article claim extractor
  -> Article implication engine
  -> Hybrid retriever
  -> Answer generator

Storage Layer
  -> PostgreSQL structured data
  -> pgvector embeddings
  -> object storage original files
  -> extraction logs
```

## 6. 核心数据模型

MVP 建议先实现以下表或模型。

```text
User
UserProfile
UserMemoryStub
Notebook
NotebookMember
Source
SourceChunk
SourceElement
Evidence
ExtractionRun
RuleCandidate
Rule
MethodCandidate
Method
RiskItem
CaseCandidate
Case
ChecklistItem
GlossaryTerm
Article
ArticleClaim
ArticleImplication
DerivedRuleCandidate
ValidationPlan
Scenario
AskSession
Answer
AnswerCitation
Feedback
```

### 6.1 User / UserProfile

MVP 先做单用户模式，但数据模型保留后续扩展空间。

关键字段：

```text
User:
  id
  email
  display_name
  role
  status
  created_at
  updated_at

UserProfile:
  id
  user_id
  domain_focus
  preferred_answer_style
  default_notebook_id
  created_at
  updated_at

UserMemoryStub:
  id
  user_id
  memory_type
  key
  value
  source
  status
  created_at
  updated_at
```

`UserMemoryStub` 在 MVP 中只保留数据结构和内部接口，不主动做长期记忆体验。

### 6.2 Notebook

关键字段：

```text
id
name
purpose
target_users
expected_questions
source_types
primary_domain
taxonomy
access_scope
status
created_by
created_at
updated_at
```

### 6.3 Source

关键字段：

```text
id
notebook_id
title
type
owner
version
source_date
upload_time
status
file_path
file_hash
parse_status
summary
access_scope
```

### 6.4 SourceElement

用于元素级引用。

关键字段：

```text
id
source_id
chunk_id
element_type
element_id
page_number
slide_number
section_title
paragraph_index
table_index
shape_id
bbox
text
metadata
```

不同文件类型的元素映射：

```text
PDF      -> page text block / paragraph / table text
Markdown -> heading / paragraph / list item / table
DOCX     -> paragraph / heading / table cell
PPTX     -> slide text box / shape text / table cell / speaker note
```

图片、截图和图中 OCR 内容在 MVP 中不进入元素解析。

### 6.5 Evidence

关键字段：

```text
id
source_id
chunk_id
element_id
source_title
location_type
location_label
page_number
slide_number
section_title
paragraph_index
table_index
shape_id
extracted_fact
quoted_span
confidence
```

### 6.6 Rule / RuleCandidate

关键字段：

```text
id
notebook_id
title
statement
applies_to
condition
recommendation
risk_if_ignored
exception
severity
rule_type
status
owner
last_reviewed
source_evidence_ids
created_from
```

### 6.7 Case / CaseCandidate

关键字段：

```text
id
notebook_id
symptom
context
root_cause
resolution
lesson_learned
related_rule_ids
related_method_ids
source_evidence_ids
status
owner
created_from
```

### 6.8 Article / ArticleClaim / ArticleImplication

关键字段：

```text
Article:
  id
  notebook_id
  source_id
  title
  authors
  publication_date
  summary
  status

ArticleClaim:
  id
  article_id
  statement
  claim_type
  measurement_condition
  limitation
  confidence
  source_evidence_ids

ArticleImplication:
  id
  article_id
  claim_id
  relation_type
  related_rule_id
  related_method_id
  related_checklist_item_id
  implication
  uncertainty
  source_evidence_ids

DerivedRuleCandidate:
  id
  notebook_id
  article_id
  title
  proposed_rule
  applies_to
  rationale
  supporting_article_claim_ids
  supporting_existing_rule_ids
  limitations
  required_reviewer
  status
```

### 6.9 Scenario

关键字段：

```text
id
notebook_id
domain
block_type
design_stage
package_type
signal_type
concern
constraint
process_or_node
application
missing_information
raw_user_input
```

## 7. 核心流程实现

### 7.1 Notebook 创建流程

```text
用户填写 notebook 信息
-> API 创建 Notebook
-> 初始化默认 taxonomy
-> 初始化 dashboard counters
-> 进入 Builder View
```

验收：

1. 用户可以创建 notebook。
2. notebook 可以保存用途、范围、目标用户和领域。
3. notebook 首页可以展示 sources、rules、methods、risks、checklist 的数量。

### 7.2 Source 上传和解析流程

```text
用户上传 source
-> 保存原始文件
-> 创建 Source 记录
-> 后台任务解析文本
-> 生成 SourceElement 元素定位
-> chunk 切分
-> 生成 source summary
-> 建立 keyword / vector 索引
-> 更新 Source parse_status
```

验收：

1. 支持至少 PDF、Markdown、DOCX、PPTX。
2. 上传后能看到处理状态。
3. 处理完成后能看到 summary。
4. 每个 chunk 能追踪到 source、元素和位置。
5. MVP 不解析图片内容；图片只作为未解析元素保留元数据。

### 7.3 候选知识抽取流程

```text
Source 解析完成
-> 触发 extraction run
-> 按 schema 抽取 Rule / Method / Risk / Case / Checklist / Term
-> 校验结构化 JSON
-> 绑定 evidence
-> 写入 candidate tables
-> 在 Builder View 展示待审核项
```

验收：

1. 系统能从文档中抽取候选规则。
2. 每条候选规则至少有 title、statement、applies_to、recommendation、severity、元素级 source evidence。
3. 抽取失败时保留错误日志，用户界面显示失败状态。
4. Curator 可以查看候选项对应原文证据。

### 7.4 Curator 审核流程

```text
Curator 打开 Review Queue
-> 查看候选知识
-> 对照 evidence panel
-> edit / approve / reject
-> approve 后生成正式 Rule / Method / Case / Checklist
-> dashboard counter 更新
```

验收：

1. 支持 approve / reject / edit。
2. approved rule 可以进入 Ask Knowhow 的高优先级检索范围。
3. rejected candidate 不参与回答。

### 7.5 Ask Knowhow 流程

```text
用户输入问题
-> 解析隐含 scenario
-> 生成检索 query
-> metadata filter
-> keyword search
-> vector search
-> structured rule matching
-> rerank
-> citation validation
-> 生成结构化回答
-> 保存 answer 和 citations
```

回答结构：

```text
结论
适用场景
推荐方法
相关规则
潜在问题
历史案例
建议 checklist
缺失信息
引用
```

验收：

1. 回答必须包含引用。
2. 引用可以追溯到 Source / Evidence。
3. approved 规则优先于 draft candidate。
4. 当证据不足时，回答必须显式说明缺失信息。

### 7.6 Scenario Query 流程

```text
用户填写结构化 scenario
-> structured rule matching
-> case / method / checklist retrieval
-> 生成场景化建议
-> 标记 missing_information
```

验收：

1. 用户可以按 domain、block、stage、package、concern 查询。
2. 返回结果区分正式规则、经验建议、推演或不确定信息。
3. 能生成下一步验证动作或 review 建议。

### 7.7 Case Search 流程

```text
用户输入症状、问题描述或工程上下文
-> 解析 symptom / context / suspected cause
-> case semantic search
-> metadata filter
-> related rule / method expansion
-> 返回相似案例、原因、解决方法、形成规则和引用
```

验收：

1. 用户可以查询历史类似案例。
2. 返回内容包含 symptom、context、root cause、resolution、lesson learned。
3. 相似案例必须绑定元素级 source evidence。

### 7.8 Checklist Generator 流程

```text
用户输入 review 场景
-> 匹配相关规则和风险
-> 生成 checklist item
-> 每项绑定 rule / case / evidence
-> 支持导出 Markdown
```

验收：

1. checklist item 是可执行检查问题。
2. 每项包含 severity 和 required evidence。
3. 每项有引用来源或说明证据不足。

### 7.9 Article Studio 流程

```text
用户上传技术文章
-> 解析文章 source 和元素级 evidence
-> 生成 Article Research Brief
-> 抽取 ArticleClaim / limitation / condition
-> 匹配已有 Rule / Method / Case / Checklist
-> 标注 supports / extends / refines / challenges / creates risk
-> 生成 implication、validation plan 和 derived rule candidates
-> Curator 审核候选规则或 checklist 更新建议
```

验收：

1. 系统可以生成文章研究摘要。
2. 每个 claim 都有元素级引用。
3. 系统能说明文章对已有规则的关系。
4. 生成的 derived rule candidate 默认是 draft，必须人工审核。

## 8. 前端页面计划

### 8.1 全局应用结构

```text
/notebooks
/notebooks/:id
/notebooks/:id/sources
/notebooks/:id/review
/notebooks/:id/rules
/notebooks/:id/cases
/notebooks/:id/ask
/notebooks/:id/scenario
/notebooks/:id/checklist
/notebooks/:id/articles
/notebooks/:id/settings
```

### 8.2 Builder View

页面模块：

1. Sources
2. Extracted Rules
3. Extracted Methods
4. Extracted Risks
5. Extracted Cases
6. Checklist Items
7. Article Claims
8. Derived Rule Candidates
9. Glossary
10. Review Queue
11. Notebook Settings

### 8.3 User View

页面模块：

1. Ask
2. Scenario Query
3. Rule Browser
4. Case Search
5. Checklist Generator
6. Article Studio
7. Saved Answers
8. Evidence Panel

### 8.4 推荐布局

```text
左栏：notebook navigation / sources / rules / cases
中间：ask / scenario / case search / checklist / article studio 主工作区
右栏：evidence / related rules / related cases / uncertainty / actions
```

## 9. 后端 API 草案

```text
GET    /api/me
PATCH  /api/me/profile

POST   /api/notebooks
GET    /api/notebooks
GET    /api/notebooks/{notebook_id}
PATCH  /api/notebooks/{notebook_id}

POST   /api/notebooks/{notebook_id}/sources
GET    /api/notebooks/{notebook_id}/sources
GET    /api/sources/{source_id}
POST   /api/sources/{source_id}/parse
POST   /api/sources/{source_id}/extract
GET    /api/sources/{source_id}/elements

GET    /api/notebooks/{notebook_id}/candidates/rules
GET    /api/notebooks/{notebook_id}/candidates/cases
GET    /api/notebooks/{notebook_id}/candidates/derived-rules
PATCH  /api/rule-candidates/{candidate_id}
POST   /api/rule-candidates/{candidate_id}/approve
POST   /api/rule-candidates/{candidate_id}/reject

GET    /api/notebooks/{notebook_id}/rules
GET    /api/rules/{rule_id}
PATCH  /api/rules/{rule_id}

GET    /api/notebooks/{notebook_id}/cases
GET    /api/cases/{case_id}
POST   /api/notebooks/{notebook_id}/case-search

POST   /api/notebooks/{notebook_id}/articles
GET    /api/notebooks/{notebook_id}/articles
GET    /api/articles/{article_id}
POST   /api/articles/{article_id}/research

POST   /api/notebooks/{notebook_id}/ask
POST   /api/notebooks/{notebook_id}/scenario-query
POST   /api/notebooks/{notebook_id}/checklist

POST   /api/answers/{answer_id}/feedback
```

## 10. LLM 与抽取策略

### 10.1 LLM 配置

系统配置提供 OpenAI-compatible 参数：

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_EMBEDDING_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS
```

后端通过统一 client 调用 chat completion 和 embedding，业务层不直接依赖具体供应商。

### 10.2 Prompt 类型

1. Source summary prompt
2. Rule extraction prompt
3. Method extraction prompt
4. Risk extraction prompt
5. Case extraction prompt
6. Checklist extraction prompt
7. Glossary extraction prompt
8. Article claim extraction prompt
9. Article implication prompt
10. Derived rule candidate prompt
11. Scenario parsing prompt
12. Ask answer generation prompt
13. Checklist generation prompt
14. Citation validation prompt

### 10.3 结构化输出要求

所有抽取任务使用 JSON schema 校验。失败时需要重试或进入 failed 状态。

```text
LLM output
-> JSON parse
-> schema validation
-> element-level evidence validation
-> database write
```

### 10.4 Evidence 绑定策略

MVP 允许两种 evidence 绑定方式：

1. 抽取时让模型返回 source element id、source chunk id 和 quoted span。
2. 抽取后用 quoted span 回查 SourceElement / SourceChunk，校验是否存在对应文本。

如果无法校验 evidence，该候选项状态应为 `needs_review`，不能直接进入 approved。

## 11. 检索与回答策略

MVP hybrid retrieval：

```text
1. Scenario parsing
2. Metadata filter by notebook/source/status
3. PostgreSQL full-text search
4. pgvector semantic search
5. Structured rule matching
6. Case similarity search
7. Article claim / implication search
8. Weighted merge
9. Rerank
10. Answer generation
11. Citation validation
```

推荐排序权重：

```text
approved rule match > reviewed rule match > related case match > checklist match > method match > article claim match > source element semantic match > draft candidate
```

回答中必须区分：

1. 正式规则：Approved
2. 经验建议：Reviewed
3. 候选信息：Draft / Needs Review
4. 不确定推断：Hypothesis / Insufficient Evidence

## 12. 里程碑计划

### Phase 0：项目初始化

目标：建立可运行的工程骨架。

任务：

1. 初始化 frontend / backend / database 目录结构。
2. 配置本机开发脚本。
3. 建立 PostgreSQL 和迁移工具。
4. 建立基本 API health check。
5. 建立前端应用 shell。
6. 建立 OpenAI-compatible LLM 配置读取。
7. 建立 lint / format / test 命令。

交付：

1. 本地一条命令启动应用。
2. 前端能打开 notebook 列表空页面。
3. 后端 health check 可用。
4. 系统能读取 LLM base URL、API key、model name 配置。

### Phase 1：Notebook 与 Source 基础能力

目标：用户可以创建 notebook 并上传 source。

任务：

1. Notebook 数据模型和 API。
2. Source 数据模型和 API。
3. 文件上传。
4. Source 列表和详情页。
5. 解析任务状态。

交付：

1. 创建 notebook。
2. 上传文档。
3. 查看 source 状态。

### Phase 2：文档解析、chunk 与索引

目标：source 可以被解析、元素定位、切分、索引。

任务：

1. PDF / Markdown / DOCX / PPTX parser。
2. SourceElement extractor。
3. Chunking service。
4. Evidence location mapper。
5. Embedding generation。
6. Full-text index。
7. Source summary。

交付：

1. 上传文档后自动解析。
2. 能查看 summary。
3. chunk、element 和 evidence 可追溯。
4. PDF / Markdown / DOCX / PPTX 的文本元素可以定位；图片内容不解析。

### Phase 3：候选知识抽取

目标：系统自动抽取 rules、methods、risks、cases、checklist、terms。

任务：

1. 定义 extraction schemas。
2. 实现 extraction workers。
3. 实现 extraction run log。
4. 实现 candidate 列表。
5. 实现 evidence panel。

交付：

1. 从测试文档中抽取候选规则。
2. 从测试文档中抽取候选案例。
3. 候选项可查看元素级证据。

### Phase 4：Curator Review

目标：候选知识可以被审核并沉淀为正式知识。

任务：

1. Review Queue 页面。
2. Candidate approve / reject / edit。
3. Rule card 页面。
4. Rule status 和 owner。
5. Approved rules 入库。

交付：

1. Curator 可以审核规则。
2. approved rules 可以在 Rule Browser 中查看。

### Phase 5：Ask Knowhow、Scenario Query 与 Case Search

目标：用户可以基于自然语言、结构化场景和问题症状查询 knowhow。

任务：

1. Scenario parser。
2. Hybrid retriever。
3. Answer generator。
4. Citation validator。
5. Ask UI。
6. Scenario Query UI。
7. Case Search API 和 UI。

交付：

1. 输入工程问题后返回结构化回答。
2. 回答包含引用。
3. 能识别缺失信息和适用场景。
4. 输入症状或问题描述后能返回相似历史案例。

### Phase 6：Checklist Generator 与 Article Studio

目标：补齐 beta 中的 review checklist 和文章研究闭环。

任务：

1. Checklist generator。
2. Article Research Brief。
3. Article claim extraction。
4. Article implication analysis。
5. Derived rule candidate queue。
6. Validation plan generator。
7. 导出 Markdown。

交付：

1. 可以基于场景生成 package review checklist。
2. 可以上传技术文章并分析其对已有规则的影响。
3. 可以生成 derived rule candidates，且默认需要人工审核。

### Phase 7：Beta 打磨与本机 Demo

目标：形成真实团队可试用的本机 beta。

任务：

1. Feedback API 和 UI。
2. Demo dataset seed。
3. 单用户 session 和 UserProfile。
4. 错误处理和任务状态打磨。
5. 端到端测试。
6. UI polish。
7. 本机启动文档。

交付：

1. 支持 Analog IC Packaging Knowhow Notebook 本机 demo。
2. 可演示从上传文档到生成 checklist、查询案例、研究文章的完整闭环。
3. 支持真实团队拿少量内部资料进行试用。

## 13. v0.2 后续计划

目标：把候选知识沉淀成可维护规则库。

重点功能：

1. Duplicate rule merge
2. Conflict detection
3. Custom taxonomy
4. Notebook publish workflow
5. Answer quality analytics
6. Rule owner 和 last reviewed workflow
7. Rule lifecycle dashboard
8. Candidate batch review

## 14. v0.3 后续计划

目标：深化 Article Studio 和受控推演。

重点功能：

1. Article comparison across notebooks
2. Claim-level confidence calibration
3. Multi-article synthesis
4. Advanced supports / extends / challenges analysis
5. Scenario-based implication map
6. Derived rule versioning
7. Validation plan tracking
8. Checklist update suggestion workflow

## 15. v0.4 后续计划

目标：进入真实工程 review 流程。

重点功能：

1. Review session
2. Reviewer comments
3. Action items
4. Export review report
5. Sign-off evidence
6. Project-level workspace

## 16. v1.0 后续计划

目标：企业级平台能力。

重点功能：

1. RBAC
2. Source-level permission
3. Audit log
4. SSO
5. Private / VPC deployment
6. Connectors
7. Multi-notebook search
8. Rule version diff
9. Knowledge gap dashboard
10. Usage analytics

## 17. 测试与评估

### 17.1 工程测试

1. Backend unit tests
2. API integration tests
3. Parser tests
4. Element-level citation tests
5. Extraction schema validation tests
6. Retrieval tests
7. Frontend component tests
8. End-to-end demo tests

### 17.2 AI 质量评估

建立小型 gold dataset：

```text
10 个 source 文档
30 条人工标注规则
20 条 checklist item
10 个历史案例
5 篇技术文章
20 个典型工程问题
```

评估指标：

1. Rule extraction precision
2. Rule extraction recall
3. Checklist usefulness
4. Citation accuracy
5. Correct rule recall
6. Missing information detection
7. Case search relevance
8. Article claim extraction accuracy
9. Answer usefulness feedback

## 18. MVP 验收标准

MVP 完成时，至少能跑通下面本机 beta demo：

```text
1. 创建 notebook：Analog Packaging Knowhow
2. 上传 5-10 份测试文档
3. 系统生成 source summary
4. 系统抽取 rule / method / risk / case / checklist / term candidates
5. Curator approve 若干规则
6. 用户提问：
   低噪声模拟前端使用 wirebond 封装时，pin assignment 需要注意什么？
7. 系统返回：
   结论、适用场景、规则、方法、风险、checklist、引用、缺失信息
8. 用户查询类似历史案例
9. 用户生成 package review checklist
10. 用户上传一篇技术文章并生成 Article Research Brief
11. 系统分析文章对已有规则的影响并生成 derived rule candidate
12. 每个回答、case、claim 和 checklist item 都能追溯到元素级 source evidence
13. 用户提交 useful / not useful feedback
```

## 19. 已确认事项

### 19.1 已确认事项

1. MVP 目标是真实团队可试用 beta。
2. 第一版先做本机 demo，不使用 Docker。
3. 后端使用 Python，推荐 FastAPI。
4. LLM 使用 OpenAI-compatible API，通过配置提供 base URL、API key 和 model name。
5. 首批文件类型为 PDF、Markdown、DOCX、PPTX。
6. MVP 不考虑图片解析。
7. 引用粒度需要到元素级别。
8. MVP 先做单用户，但保留用户系统和未来用户记忆的数据基础。
9. Article Studio 进入 MVP。
10. Case extraction 和 Case Search 进入 MVP。
11. 首批资料主要是中英混合。
12. 当前没有可用于本机 demo 的真实半导体资料，可以先构造 synthetic demo dataset。
13. MVP 时间目标是越快越好，优先快速实现可运行的本机 beta。
14. 用户记忆由用户手动决定是否加入，MVP 先不做自动记忆。
15. 产品名和项目名统一为 `silicon-notebook`。
