# silicon-notebook 方案已完成情况

更新日期：2026-05-29

对照依据：`silicon_notebook_fangan.md`（产品方案）与 `implementation_plan.md`（实现计划）。

## 总体状态

实现已经从早期“demo 表演”阶段进入**真实本机 beta 闭环**。核心链路对任意用户创建的 notebook 都用其真实上传内容工作，不再依赖单一硬编码 demo notebook：

```text
创建 notebook
-> 上传 PDF / Markdown / DOCX / PPTX source（异步处理）
-> 保存原始文件
-> 解析为 source elements（元素级 + location_label）
-> 生成 source summary
-> 自动抽取 rule / method / risk / case / checklist / glossary 候选（带 evidence 绑定）
-> Curator 审核（approve / reject / edit）-> 落入正式知识表
-> 混合检索（关键词 + 向量余弦，按知识类型加权）
-> 场景化 Ask / Scenario / Case / Checklist 回答（带 citation 校验）
-> Article Studio 从文章自身内容抽取 claims + 关联规则 + 派生规则候选
-> 用户反馈 useful / not useful
```

LLM 未配置时，全链路退化为 deterministic fallback（启发式抽取、模板组装回答），保证离线也能跑通整个闭环冒烟。

之前 fangan_done.md 中标记为“demo-backed / 未实现”的抽取、审核、检索、真实问答、文章研究、反馈等，**现已实现并接通真实代码路径**。

## 1. 产品与项目基础

- 产品名和项目名统一为 `silicon-notebook`。
- 已初始化 Git 仓库，远端：`git@gitee.com:justkitt/silicon-notebook.git`。
- 第一版不使用 Docker。
- 后端统一使用本机 Miniconda Python：`/opt/homebrew/Caskroom/miniconda/base/bin/python`。
- 维护文档：`AGENTS.md`、`README.md`、`README_zh.md`、`.env.example`。

## 2. 技术架构基础

- 后端使用 Python FastAPI。
- 前端为唯一主线：Next.js / React / TypeScript，目录 `frontend/`。**原静态 `web/` fallback 目录已删除**，`scripts/dev.sh` 与 `scripts/check.sh` 已移除相关引用。
- 本机默认持久化为 SQLite：`sqlite:///.local/silicon_notebook.db`（标准库 `sqlite3`）。
- 原始上传文件保存到 `.local/storage`。
- repository 边界：当前实现 `SQLiteRepository`，对外通过 `NotebookRepository` Protocol，预留未来 PostgreSQL + pgvector 替换空间。
- 向量检索本机实现：`element_embeddings` 表存 JSON 向量，检索时在 Python 内算余弦；PostgreSQL + pgvector 留作后续放量方向。
- LLM 通过 OpenAI-compatible 配置接入：`OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL` / `OPENAI_COMPAT_EMBEDDING_MODEL` / `OPENAI_COMPAT_TIMEOUT_SECONDS`。
- `Settings.llm_configured` 与 `Settings.embedding_configured` 分别控制问答/抽取 LLM 与 embedding 能力是否启用。

## 3. 用户系统基础

- 单用户模式；`UserProfile` schema 与 `users` / `user_profiles` 表已实现。
- 默认本机 curator 用户；用户记忆模式字段保留为 `manual`（不做自动记忆）。

## 4. Notebook 创建与管理

- Notebook CRUD API 完整：list / create / get / patch / delete。
- `notebooks` 表持久化，后端重启不丢失。
- 新建默认名 `Untitled notebook`，创建后直接进入来源界面。
- Notebook summary 包含 name / purpose / primary_domain / status / counts / created_label。
- **counts 为真实统计**：rules / cases / checklist_items 来自正式知识表 `knowledge_objects` 的 approved 计数，article 数来自 `article_claims` 相关统计，不再写死。
- 前端集合页：tab 过滤、新建、卡片菜单、编辑、删除、grid/compact/list 预览、最近/名称/来源排序、列表表格视图。

## 5. Notebook 工作区界面

- 三栏工作区：左 Source Stack / 中 Knowhow 工具 / 右 Studio。
- 左上角 notebook 名称可编辑保存；左栏显示来源数量、仅显示用户导入文件；网络来源检索保留为 disabled affordance。
- source card 可打开 source detail，查看元素级文本，支持手动重解析。
- **来源状态轮询**：上传后对非终态 source 每 ~1.5s 轮询 `GET /sources/{id}`（~3min 上限），实时展示 queued→parsing→parsed→extracting→extracted/failed；到达 extracted 自动刷新候选数与 counts。
- **中栏 knowhow 工具 tab**：问答 / 场景查询 / 案例检索 / Checklist / 知识库。
  - 问答：自由提问走 `/ask`（已移除写死 scenario）；prompt chips 触发真实 ask。
  - 场景查询：9 字段结构化表单走 `/scenario-query`，复用统一 AnswerView。
  - 案例检索 / Checklist：分别走 `/case-search`、`/checklist`，渲染 CaseCard / ChecklistItem。
  - **知识库（多类型浏览）**：规则 / 方法 / 风险 / 术语子切换，分别走 `/rules|/methods|/risks|/glossary`；卡片含状态徽标 + 状态下拉（reviewed/approved/deprecated/conflict/project_specific）+ owner 内联编辑 → `PATCH /knowledge/{id}`；按状态过滤；「查重」「冲突」面板（重复组带合并按钮、冲突对展示）。
  - 回答含 citation 与 👍/👎 反馈；chat 菜单可清空对话。
- **Review Queue 面板**：候选知识列表 + evidence 展示 + approve / reject 操作。
- **文章创建入口**：真实文章创建 modal，移除写死的 `ARTICLE_ID`、`DEMO_NOTEBOOK_ID` 常量。
- **source detail 结构化渲染**：`formula` 元素用 KaTeX 排版（失败回退原始 LaTeX）、`table` 元素用 sanitized `table_html` 渲染、其余文本 + element_type 徽标。
- Studio 输出区保留思维导图 / 新建文章 / 信息图入口。

## 6. Source 上传与管理（异步闭环）

- multipart 上传：`POST /api/notebooks/{notebook_id}/sources`，支持 PDF / Markdown / DOCX / PPTX。
- **上传不阻塞**：上传请求先建记录并返回（parse_status=`queued`），解析 + embedding + 抽取通过 FastAPI `BackgroundTasks` 异步执行。
- parse_status 状态机：`queued -> parsing -> parsed -> extracting -> extracted`（失败置 `failed`）。
- repository 通过注入 `scheduler` 回调保持同步可测：HTTP 层传入 BackgroundTasks，冒烟/脚本不传则同步跑完整管线。
- 记录 metadata：file_name / file_size / file_hash / source_type / parse_status / summary。
- 相关 API：list sources、get source detail、手动重跑 `POST /api/sources/{source_id}/parse`（委托 `process_source`）、`POST /api/sources/{source_id}/extract`、metadata-only import。

## 7. 文档解析与元素级 Evidence

- `SourceElement`（id / source_id / element_type / location_label / text / metadata）+ `source_elements` 表。
- parser：Markdown（heading/paragraph/list_item）、DOCX（paragraph/table_row）、PPTX、PDF、plain text fallback。
- **PPTX 升级为元素级**：按 shape / text box 逐个产出 `slide_text` 元素，并解析 `ppt/notesSlides/*.xml` speaker notes 为 `speaker_notes` 元素。
- **PDF 解析经 MinerU 适配器（`mineru_client.py`）与 GPU 解耦**：`MINERU_MODE=http` 调远端 `mineru-api`，`cli` 在隔离 Python 子进程中调用 MinerU `do_parse/read_fn`，`off`（默认）用 pypdf 回退。FastAPI 后端进程不引入 torch/MinerU；MinerU 不可达/出错时降级 pypdf，并在 pipeline log / source `error_message` 留下回退诊断。MinerU 输出映射为结构化元素：公式→`formula`（保留 LaTeX）、表格→`table`（HTML 存 metadata）、标题保留层级。
- **off 回退质量已提升**：`parse_pdf_pypdf` 改用 pypdf **layout 抽取模式**（更好的阅读顺序、行/列间距），并按空行切分为 `heading` / `page_text` 元素（不再单块压平）；零新依赖、许可证友好。无公式/表格保真（那需 MinerU）。
- **本机已启用 MinerU(MLX)**：本机为 Apple Silicon，已装 `mineru[core]` + `mlx-vlm`（VLM 模型 MinerU2.5-Pro 已下载），`.env` 设 `MINERU_MODE=cli`、`MINERU_BACKEND=vlm-auto-engine`、`MINERU_PARSE_METHOD=auto`、`MINERU_LANG=en`、`MINERU_TIMEOUT_SECONDS=1800`，`vlm-auto-engine` 自动走 MLX 引擎（Engram 第一页实测 24.57s，完整论文可能超过 600s）。公式/表格/版面离线可得。
- **空 PDF 止血**：PDF 解析出 0 元素时写明确提示（疑似扫描/图片型 PDF，需 MinerU/OCR），避免"假成功空结果"。
- 每个元素带 `location_label`，作为 evidence citation 锚点。
- `.env.example` 默认仍保持 `MINERU_MODE=off`，其它环境默认离线 pypdf。

## 8. Source Summary

- 每个 source 解析后生成 summary，LLM 已配置走 LLM，否则 deterministic fallback；持久化并在前端展示。

## 9. 检索：关键词 + 向量混合

- Notebook 内搜索 API：`GET /api/notebooks/{notebook_id}/search?q=`，覆盖 notebook/source/element/article 文本。
- **新增 `backend/app/services/retrieval.py`**：
  - `element_embeddings(element_id, source_id, vector)` 存 JSON 向量；解析后对每个 element 调 `llm_client.embed()` 写入（未配置 embedding 则跳过）。
  - **CJK 感知分词**：`_tokens` 对中文连续串产出字符 bi-gram（单字→uni-gram），拉丁/数字保持词级，修复"整串中文变一个 token 导致中文关键词检索失效"的硬伤；该 tokenizer 被抽取的 evidence 绑定与场景 boost 复用。
  - **真正的 hybrid 融合**：`relevance = w_kw·keyword + w_sem·semantic` 加权和（默认 0.4/0.6，常量集中），按生效信号重归一化；未配 embedding 时退化为纯关键词且不被截顶。相关度门控 `RELEVANCE_FLOOR` 砍噪声。
  - **类型权重去污染**：rule 1.0 > case 0.9 > checklist 0.8 > method 0.7 > risk 0.6 > glossary 0.4 仅作 `weight` 字段（跨类型分组/tie-break），不再乘进同类型相关度排序。
  - **payload 级向量（WS4）**：新增 `knowledge_embeddings` 表，对知识对象 payload 本身建向量（approve / `PATCH /knowledge` / merge 时写入，存量 lazy 回填）；语义分 = `max(payload 向量, 证据向量)`，修正"只用证据原文向量"的存储/意义错位。
  - **结构化场景匹配（WS3）**：`score_knowledge` 接收 scenario dict，与规则 `applies_to/condition/title` 做 token 重叠，`final = relevance·(1+α·boost)` 软加权（不硬过滤）；`scenario_query` 透传结构化字段。
- 集合页搜索调用后端搜索，并加 250ms debounce，避免每键触发请求。
- 当前为 SQLite 文本匹配 + Python 余弦；尚未引入 BM25 / FTS5 / pgvector。

## 10. 自动抽取 Pipeline

- `backend/app/services/extraction.py`：从 source 元素抽取 rule / method / risk / case / checklist / glossary 候选。
- **LLM 模式（配置后）**：按 `WINDOW_MAX_CHARS` 分窗覆盖**整篇文档**（不再只取前 9000 字符），逐窗 `chat_json` 抽取后跨窗去重；要求返回 `quoted_span`，**evidence 绑定**先精确子串、再 **CJK 感知 `token_overlap≥0.6` 模糊回退**（避免改写/标点差异导致 needs_review 无引用）；`chat_json` 用 `response_format=json_object`（不支持则回退）+ 去 markdown fence。
- **deterministic 启发式（离线默认）已重写**：句子级切分，一段可出多条候选；glossary **仅从定义句式**（`术语：定义` / `X is defined as` / `指/是指/定义为`）生成，**不再每个 heading 产空定义噪声**；`heading/table/formula/image_caption/speaker_notes` 不走指令/案例启发式；rule 拆出 recommendation 与 risk_if_ignored（否则/otherwise/to avoid）、轻量 applies_to（领域/封装/信号等范围词）；带 confidence。
- **候选卫生**：`run_extraction` 对两条路径统一 `_dedupe_records` 去重并按 confidence 排序。
- 写入 `extraction_runs` / `extraction_candidates`（payload + evidence）。触发：`process_source` parse 成功后自动执行；手动 `POST /api/sources/{source_id}/extract`。
- 注：本机若未配置 `OPENAI_COMPAT_*` 则走启发式；要获得真正 LLM 抽取质量，可把 `OPENAI_COMPAT_*` 指向本机 OpenAI 兼容服务（ollama / `mlx_lm.server` / llama.cpp）离线运行。

## 11. Curator 审核、正式知识表与知识治理（方案 v0.2）

- 正式知识统一存于 `knowledge_objects`（object_type = rule/method/risk/case/checklist/glossary，status、owner、`last_reviewed`、payload、evidence 内联 JSON）。
- 审核 API：
  - `GET /api/notebooks/{notebook_id}/candidates`（全部）
  - `GET /api/notebooks/{notebook_id}/candidates/{type}`（rules/methods/risks/cases/checklist/glossary）
  - `PATCH /api/candidates/{candidate_id}`（编辑 payload/status）
  - `POST /api/candidates/{candidate_id}/approve`（候选 payload 落入正式表，status=approved，并从队列移除）
  - `POST /api/candidates/{candidate_id}/reject`（status=rejected，删除对应正式记录）
- **知识治理（Tier 2）**：
  - **状态生命周期**：`reviewed / approved / deprecated / conflict / project_specific`。仅 USABLE 集合（approved/reviewed/project_specific/conflict）进入答案/检索，`deprecated` 排除。
  - **浏览**：`GET /api/notebooks/{id}/rules|methods|risks|glossary`（返回各类 Card，含 status；rules 返回全部状态供浏览筛选）。
  - **审核后编辑**：`PATCH /api/knowledge/{id}` 改 status/owner/payload，并盖章 `last_reviewed`。
  - **重复合并**：`GET /api/notebooks/{id}/duplicates?type=` 同类型相似度（关键词 + 证据向量 cosine ≥0.6）成组；`POST /api/knowledge/{id}/merge`（折叠 evidence、源置 deprecated）。
  - **冲突检测**：`GET /api/notebooks/{id}/conflicts`（规则同范围、取向相反的对）；`ask()` 命中 conflict 状态规则时在 missing_information 加冲突提示（§12 治理）。
- notebook counts 改为对正式表做真实统计（rules/cases/checklist_items/methods/risks/glossary，统计 USABLE 状态）。

## 12. 真实 Ask / Scenario / Case / Checklist

- **`ask()` 删除 demo 分叉**，全部数据驱动：
  1. 取 approved 知识对象 + 相关 elements，混合检索打分。
  2. LLM 模式生成结构化 `AskResponse`（conclusion / applicable_scenario / recommended_methods / related_rules / potential_risks / related_cases / checklist / missing_information / citations / llm_mode）；无 LLM 时用模板组装检索结果。
  3. **Citation 校验**：回答引用必须能回查到有效 element_id，否则丢弃并在 missing_information 标注。
  4. 证据不足时显式返回缺失信息。
  5. 保存 answer 并返回 `answer_id`（供反馈关联）。
- `scenario_query()`：`ScenarioQueryRequest` 扩展 signal_type / constraint / process_or_node / application 等字段，构造 scenario 后走 ask。
- `case_search()`：对 cases 知识对象 + element 相似检索，删除写死案例。
- `checklist()`：从匹配的 rules / risks 生成 checklist item，删除写死 3 条。

## 13. 真实 Article Studio

- 文章 API：list / create / `POST /api/articles/{article_id}/research`。
- **`research_article()` 删除硬编码 bondwire brief**：从文章自身 title/abstract（及 element）抽取 claims（LLM 或句子级 fallback），用 `keyword_score` 与已有 rules 关联，标注 supports/extends/refines/challenges，生成 implication、validation plan 与 derived rule candidate（默认 draft）。
- 持久化 `article_claims` / `derived_rule_candidates` 表。
- 回归保证：上传与 bondwire 无关的文章，research_article 输出 claims 来自该文章内容，不再出现写死文本（smoke 已断言）。

## 14. 用户反馈

- `answers` 表保存每次回答，`feedback` 表关联反馈。
- `POST /api/answers/{answer_id}/feedback`（rating useful / not_useful + comment）。
- 前端 AnswerView 增加 👍 / 👎 与评论提交。

## 15. 数据模型

- schema：Evidence / RuleCard（含 owner/last_reviewed）/ CaseCard / Citation / ChecklistItem / AskResponse / ScenarioQueryRequest（已扩展）/ ArticleSummary / ArticleResearchBrief / Candidate / CandidateUpdate / ExtractionCandidate / MethodCard / RiskItemCard / GlossaryTermCard / ArticleClaimCard / FeedbackRequest / FeedbackResponse / **KnowledgeUpdate / KnowledgeRef / DuplicateGroup / ConflictPair / MergeRequest**。
- 表：users / user_profiles / notebooks / sources / source_elements / articles / extraction_runs / extraction_candidates / element_embeddings / knowledge_objects（含 status/owner/last_reviewed）/ answers / feedback / article_claims / derived_rule_candidates。

## 16. Demo Dataset（走真实管线）

- 保留 synthetic mixed 中英 semiconductor demo（默认 seed notebook `Analog Packaging Knowhow` 等），但**作为正常 seed 数据走同一条真实抽取/检索/问答路径**，不再有 `notebook_id == DEMO` 的硬编码分叉。

## 17. 本机运行与验证

- `scripts/dev.sh` 同时启动 FastAPI 后端与 Next.js 前端（要求 `frontend/node_modules`，否则提示先 `npm install`）。
- 服务地址：前端 `http://localhost:3000`（占用时切 3001），后端 `http://127.0.0.1:8000`，CORS 默认放行 3000/3001。
- `scripts/check.sh`：
  - 后端 Python syntax（含 extraction.py / prompts.py / retrieval.py / mineru_client.py）
  - `scripts/smoke_backend.py`（SQLite 持久化、上传解析、summary、搜索、**抽取→approve→ask→feedback→article 全闭环**、**异步 scheduler 路径**、PPTX 元素级 + speaker notes、research 误导性回归断言、**MinerU content_list→元素映射离线单测**、**检索打分（关键词/向量/None 三态）**、**知识状态机（deprecated 不召回 / reviewed 召回 / 非法状态报错）+ Method/Risk/Glossary 浏览 + 重复合并**、重启后持久化）
  - 前端 `tsc --noEmit`
- 全部检查通过；`npm run build` 通过。

## 18. 可观测性 / 日志系统（全链路）

为解决"网页操作时卡住、不知道发生了什么"的痛点，建立统一结构化日志：JSONL 文件（`.local/logs/`，已 gitignore）+ Python `logging` 控制台简要行，对离线/未配置无副作用，写日志失败绝不影响主流程。

- **通用底座 `backend/app/core/event_logging.py`**：`EventLogger(settings, channel)` 负责 JSONL 追加 + 控制台行 + 永不抛异常；自动补 `ts/channel`，按 `LLM_LOG_MAX_CHARS` 截断；`new_id(prefix)` 生成关联 id。
- **LLM 交互日志（`llm.jsonl`）**：`LLMInteractionLogger` 基于 `EventLogger`，埋点在唯一钖点 `OpenAICompatibleClient`（`chat_json`/`embed`），覆盖抽取/问答/文章研究/summary 全部路径。chat 记录 prompt/响应/token/latency（截断）；embedding 记摘要（model/耗时/维度/成功失败，不存向量）；**失败记 `status=error` 后 re-raise**，让原本被启发式回退静默吞掉的错误可见。
- **HTTP 请求日志（`requests.jsonl`）**：`backend/app/main.py` 新增 middleware，记录每个请求 `method/path/status_code/latency_ms/client/request_id`；超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 供前后端关联。
- **异步管线阶段日志（`events.jsonl`）**：`process_source` 对 parse/embed/extract/pipeline 各阶段两端计时打点（`kind=pipeline`，含 elements/parser_mode 等），`_set_source_status` 每次状态机跃迁 emit `kind=status`，失败记异常堆栈（`logger.exception`），可精确定位卡在哪一步、各步耗时与失败原因。
- **修复真实 bug**：`_set_source_status` 原 `params.insert(2, summary)` 误写到 `error_message` 列，导致失败时真实错误从未落库；现已修正，前端 source detail 可显示具体错误（smoke 加回归断言守护）。
- **前端可见性（`frontend/app/page.tsx`）**：`api()` 包装器 console.debug 方法/路径/耗时/request_id，并把后端 `detail` 透传进错误信息；轮询时显示"处理中（已 Ns）：文件: 阶段"，超时提示查看 `events.jsonl`，source 进入 `failed` 时展示 `error_message`。
- **配置**：`config.py` + `.env.example` 新增 `EVENT_LOG_ENABLED` / `EVENT_LOG_DIR` / `SLOW_REQUEST_MS`（沿用既有 `LLM_LOG_*`）。
- **验证**：`scripts/smoke_backend.py` 新增 `check_event_logging`（JSONL 可解析、禁用不写、写失败不抛）与 `check_pipeline_event_logging`（管线阶段事件产出 + `error_message` bug 回归）；`scripts/check.sh` 纳入 `event_logging.py` 编译。

## 19. 本轮新增（dev 分支，方案 §6/§7/§16）

- **Explain Rule（§6.10）**：`GET /notebooks/{id}/rules/{rule_id}/explain` 把规则反向追溯到来源 evidence，并召回相关 case/risk/checklist；前端规则卡片「解释」按钮 + 弹窗。
- **Derived Rule Candidate 审核队列（§7.5）**：`GET /notebooks/{id}/derived-rules` + `POST /derived-rules/{id}/approve|reject`；approve 携 evidence 落入正式 `knowledge_objects`(rule)；前端 Studio「派生规则候选」弹窗审核。
- **创建富字段 + 模板（§6.1/§6.2）**：`NotebookCreate/Update/Summary` 增 `target_users/expected_questions/source_types/taxonomy/access_scope`（notebooks 表迁移）；6 套模板 `GET /notebook-templates`，创建按模板预填；前端集合页「从模板…」选择器 + 编辑弹窗富字段。
- **CSV / Excel 解析（§6.3）**：`parse_csv`(stdlib) + `parse_xlsx`(openpyxl) → `table_row` 元素；上传校验/accept 扩 `.csv/.xlsx/.xlsm`。
- **质量/分析看板（§16）**：`GET /notebooks/{id}/analytics`（有用率、低分提问=知识缺口、候选状态分布、知识覆盖、来源状态）；前端「看板」弹窗。
- **测试硬化**：`smoke_backend.py` 三处 `Settings` 清空 `OPENAI_COMPAT_*` + `mineru_mode=off`，`scripts/check.sh` 不再调用真实 LLM/embedding（即便 `.env` 有 key），全程离线 1–2s。

## 21. 文档类型抽取 profile 注册表（方案 §5 对象模型 + §6.2 模板）

- **问题**：原抽取对所有文档硬套固定 6 类（rule/method/risk/case/checklist/glossary），只适合方案/总结；论文/课本硬套会产噪声、漏抽。
- **profile 注册表**（`backend/app/services/extraction_profiles.py`）：
  - `OBJECT_SCHEMAS`——共享 typed 知识模型，按 §5 补齐缺失字段，尤其**关系字段**：rule 增 `condition/exception/rule_type/related_cases/related_methods`；method 增 `tradeoff/required_condition/related_rules/related_cases`；case 增 `related_rules/related_methods`；checklist 增 `applies_to/related_rules/related_cases`；risk 增 `mitigation/related_rules`；glossary 增 `aliases`。新增论文/课本类型：`claim`(claim_type/measurement_condition…)、`finding`、`concept`、`principle`、`example`。
  - `PROFILES`——文档类型 → 启用对象集 + prompt 框定：`design_spec / method / postmortem / review / academic_paper / textbook / general`。
  - `TEMPLATE_PROFILE`——notebook 模板(§6.2) → 默认 profile。
  - `detect_doc_type` / `resolve_profile`——离线 bilingual 线索打分做 per-source 文档类型判别（明显胜出才覆盖模板默认，阈值：≥2 命中且领先 ≥2）。
- **接入**：`run_extraction(..., profile)` 按 profile 动态生成 schema hint(`build_extraction_schema_hint`) + prompt(`extraction_prompt`)；启发式路径按 profile 过滤只产出激活类型；`notebooks` 表迁移加 `template` 列并在创建时落库；`_run_extraction` 解析 profile（模板默认 + 内容判别），并把 `profile=<id>` 记入 extraction_runs。
- **验证**：`scripts/smoke_backend.py::check_extraction_profiles`——论文 doc 在 rule notebook 中仍判为 academic_paper 且不产 rule 候选；模板默认在判别不确定时生效；schema hint/prompt 反映各 profile 对象集与新字段；general 仍覆盖六类。`scripts/check.sh` 全绿、离线。

## 22. 新类型通用浏览闭环 + 全栈对等规则

- **背景**：§21 让论文/课本类型（claim/finding/concept/principle/example）以及一直没有浏览入口的 case/checklist 能被抽取并 approve 入库，但**前端只有 rule/method/risk/glossary 四个 tab** → 「可审批但不可见」。本轮补齐前后端闭环。
- **后端**：
  - `GET /notebooks/{id}/knowledge-types` → 该 notebook 现有对象类型 + 非 deprecated 计数 + 中文 label（`KnowledgeTypeCount`）。
  - `GET /notebooks/{id}/knowledge?type=<type>` → 任意对象类型的通用记录（`KnowledgeRecord`：headline + 按 `OBJECT_SCHEMAS` 排序的 `fields[]` + status/owner/last_reviewed/evidence），与既有 PATCH `/knowledge/{id}` 治理通用。
  - `search_notebook` 纳入 knowledge_objects（全类型）→ 新类型可被 notebook 检索命中。
- **前端**（`frontend/app/page.tsx`）：知识库 tab 改为**动态**——四个定型 tab 始终在，其余类型从 `/knowledge-types` 动态出现（带计数徽标）；非定型类型用通用渲染（headline + 字段表，字段名走中文 label 映射），复用状态/owner 编辑与查重。
- **同时修复**：`routes.py` 缺失的 `NotebookTemplate` import（此前 API 模块导入即 NameError，但 check.sh 只导 services 未触发）。
- **新规矩（AGENTS.md「Full-Stack Parity」）**：本系统中**任何面向用户的后端能力必须同变更内附带对应前端界面，不允许只实现一半**；"done" 的判定含后端端点、前端入口、`check.sh` 绿、`npm run build` 通过。
- **验证**：`smoke_backend.py` 增 knowledge_types/list_knowledge 断言；TestClient 实跑确认 demo notebook 现可浏览 rule/case/checklist/glossary；`check.sh` 全绿、`npm run build` 通过。

## 23. Schema 管理 + 归纳 + 关系图 + ask 织入 + 抽取自我修正

- **可编辑 schema 注册表**：新增 `object_schemas` 表（迁移时从代码默认 seed，`INSERT OR IGNORE` 保留人工编辑）。抽取改为读 **DB 生效 schema**（`effective_schemas()` 叠加在代码默认上），prompt/schema-hint/字段排序全部按生效注册表。端点 `GET/POST/PATCH/DELETE /object-schemas`（内置可停用不可删、自定义可删）。前端「Schema」弹窗：列出/编辑字段·标签·说明、启用/停用、新增自定义类型。
- **Schema 归纳（建议态，§开放发现）**：`POST /notebooks/{id}/schema-proposals` 用 LLM 从笔记本内容提议新类型（offline 为 no-op），存为 `status='proposed' source='induced'`，绝不自动启用；前端在 Schema 弹窗审核（批准→active / 拒绝→删除）。
- **关系边消费（§7.4 基础）**：`GET /notebooks/{id}/graph` 把各对象 `related_rules/cases/methods/concepts` 自由文本按 headline 模糊匹配解析成边（nodes+edges）；Explain Rule 增 `related_knowledge`（规则连出的对象）；前端「关系图」弹窗 + Explain 弹窗内关系块。
- **新类型织入 ask**：`AskResponse.related_knowledge`（通用块）召回非核心类型（claim/finding/concept/principle/example/glossary/自定义）的 top 命中；前端 AnswerView 渲染。
- **抽取自我修正 + 证据绑定升级**：LLM 路径加一轮自检（drop 幻觉/含糊、回填更忠实 verbatim span，`REFINE_SCHEMA_HINT`/`refine_prompt`，offline no-op）；`bind_evidence` 命中元素后取最佳**逐句 verbatim** 作为引文。
- **顺带修复**：`routes.py` 缺失的 `NotebookTemplate` import（API 模块导入即 NameError）。
- **验证**：`smoke_backend.py` 增 `check_object_schemas / check_self_refinement / check_knowledge_graph` + ask `related_knowledge` 断言；TestClient 实跑全部新端点 200；`check.sh` 全绿、`npm run build` 通过。

## 20. 当前边界（后续阶段，未计入已完成）

- **Article 深度可视化**：typed 关系下游动作（suggests_checklist/creates_risk）、Implication Map（§7.4）、Inference 分层（§7.3）+ Hypothesis（§5.9）、研究简报字段补齐（§7.1）。
- **v0.4 Review Mode**：review session、场景 checklist sign-off、reviewer 评论、action items、导出 review 报告。
- **v1.0 企业**：RBAC / source 级权限 / 审计 / SSO / 私有部署 / Confluence·SharePoint·Jira·Git·Slack connectors / 多 notebook 搜索 / rule version diff。
- 检索：BM25 / FTS5 / pgvector 放量、结构化硬过滤、Knowledge graph（已评估为低 ROI / 基础设施级，暂缓）。
- 扫描件 OCR、DOCX/PPTX 公式（OMML）解析；MinerU 已覆盖 PDF 的公式/表格/版面（本机 MLX 或 GPU 主机）。

> 已完成里程碑：v0.1 闭环、Tier 1（场景/案例/Checklist/知识库前端 + 上传轮询 + knowledge 向量召回）、PDF MinerU(MLX) + KaTeX/表格渲染、**Tier 2 知识治理（状态生命周期 + 多类型浏览 + 合并 + 冲突检测）**、**检索/抽取算法升级（CJK 分词 + hybrid 融合 + 结构化场景匹配 + payload 级向量 + 全文分窗口抽取 + 鲁棒证据绑定）**、**全链路可观测日志系统（LLM/HTTP/管线三通道 JSONL + 控制台）**。
