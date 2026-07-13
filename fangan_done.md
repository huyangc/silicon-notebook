# silicon-notebook 方案已完成情况

更新日期：2026-07-13

对照依据：`silicon_notebook_fangan.md`（产品方案）。

## 总体状态

实现已经从早期“demo 表演”阶段进入**真实本机 beta 闭环**。核心链路对任意用户创建的 notebook 都用其真实上传内容工作，不再依赖单一硬编码 demo notebook：

```text
创建 notebook
-> 上传 PDF / Markdown / DOCX / PPTX source（异步处理）
-> 保存原始文件
-> 解析为 source elements（元素级 + location_label）
-> 生成 source summary
-> 配置 LLM 时抽取 Concept / Claim / Formula / Procedure KG 对象（带 evidence 绑定与关系边）
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
- 本机默认持久化为 SQLite：`sqlite:///.local/silicon_notebook.db`（标准库 `sqlite3`）。
- 原始上传文件保存到 `.local/storage`。
- repository 边界：`SQLiteRepository` 是组合式 `RepositoryRuntime` 之上的兼容 facade——主业务库 SQL 只在 `backend/app/repositories/sqlite/` 领域 store，编排在 application services，消费者依赖 `backend/app/repositories/ports.py` 小型 Protocol；PostgreSQL adapter 留待将来在同一 ports 后实现。旧库兼容由冻结 v9 fixture、schema golden 与 `scripts/verify_repository_snapshot.py` backup-only 真库验证共同守护。
- 向量检索本机实现：embedding 本地持久化，查询时使用有界 float32 矩阵或 scale index；PostgreSQL + pgvector 留作后续放量方向。
- LLM 通过 OpenAI-compatible 配置接入：`OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL` / `OPENAI_COMPAT_TIMEOUT_SECONDS`；embedding 独立使用 `EMBED_*` 配置。
- `Settings.llm_configured` 与 `Settings.embedding_configured` 分别控制问答/抽取 LLM 与 embedding 能力是否启用。

## 3. 用户系统与分享

- 当前为多账号 owner 隔离：自助注册/登录使用不透明 Bearer session，admin 管理全局 base tier。
- 分享链接已实现：小 notebook 复制到接收者账号；大 notebook 以只读成员方式加入。当前没有实时协同编辑或修改密码流程。
- 用户记忆模式字段保留为 `manual`（不做自动记忆）。

## 4. Notebook 创建与管理

- Notebook CRUD API 完整：list / create / get / patch / delete。
- `notebooks` 表持久化，后端重启不丢失。
- 新建默认名 `Untitled notebook`，创建后直接进入来源界面。
- Notebook summary 包含 name / purpose / primary_domain / status / counts / created_label。
- **counts 为真实统计**：来源、当前知识对象类型、conversation 与 report 等统计来自当前数据库，不再依赖已退役内容表或写死 demo 数字。
- 前端集合页：tab 过滤、新建、卡片菜单、编辑、删除、grid/compact/list 预览、最近/名称/来源排序、列表表格视图。

## 5. Notebook 工作区界面

- 两列工作区：左 Source Stack / 右侧主区域；主区域为 Ask / Knowledge / Memory / Deep Report 四个 tab，固定 Studio 右栏已移除。
- 左上角 notebook 名称可编辑保存；左栏显示来源数量、仅显示用户导入文件；网络来源检索保留为 disabled affordance。
- Notebook 顶栏保持紧凑：标题下不再渲染 description，description 在没有对话时进入问答欢迎态；顶部分析工具栏具备横向 overflow 保护，桌面宽度下动作标签不会被截断。
- source card 可打开 source detail，查看元素级文本，支持手动重解析。
- **来源状态轮询**：上传后对非终态 source 每 ~1.5s 轮询 `GET /sources/{id}`（~3min 上限），实时展示 queued→parsing→parsed→extracting→extracted/failed；到达 extracted 自动刷新候选数与 counts。
- **主栏当前 tab**：Ask / Knowledge / Memory / Deep Report；Scenario / Case / Checklist 已退役。
  - 问答：自由提问走 `/ask`（已移除写死 scenario）；支持多个 conversation/session，会话历史通过顶部紧凑上下文栏 + 可展开会话管理面板切换/新建/重命名/删除，避免把主问答区长期切成更窄的左右两栏；欢迎区标题与 prompt chips 会根据 notebook 已导入来源的标题/摘要生成，并触发真实 ask。输入框支持 `Enter` 发送、`Shift+Enter` 换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制并恢复草稿问题。
  - 深度报告：两阶段后台 job，先审阅大纲再生成各节；支持实时进度、取消、删除、Markdown 与批量 zip 导出。
  - **知识库（多类型浏览）**：前端从 `/knowledge-types` 动态获取对象类型，再用 `/knowledge?type=...` 浏览任意类型（Concept / Claim / Formula / Procedure 以及 legacy/custom 类型）；卡片含状态徽标 + 状态下拉（reviewed/approved/deprecated/conflict/project_specific）+ owner 内联编辑 → `PATCH /knowledge/{id}`；按状态过滤；「查重」「冲突」面板（重复组带合并按钮、冲突对展示）。
  - 回答含 citation 与 👍/👎 反馈；引用在前端以 `[1]`、`[2]` 顺序编号展示，点击引用会在答案面板内展开详情（避免浮窗越界）；模型直接输出的数字复合引用（如 `[1, 2, 3]`）在每个编号都能映射到已知引用时也会拆成可点击引用；答案正文支持 Markdown/code/formula/table 渲染，并提供复制按钮；chat 菜单可清空对话。
- **候选知识治理**：候选知识列表、evidence 与 approve / reject 后端能力保留；左侧 Source Stack 不再显示独立「审核队列」按钮，避免出现无效入口。
- **source detail 结构化渲染**：`formula` 元素用 KaTeX 排版（失败回退原始 LaTeX）、`table` 元素用 sanitized `table_html` 渲染、其余文本 + element_type 徽标。
- 「分析」菜单当前只含晋升队列（admin）、tier 切换（admin）与边审查队列；看板、Schema、全屏知识图谱为其他顶栏动作。当前没有文章、思维导图、信息图或派生规则入口。

## 6. Source 上传与管理（异步闭环）

- multipart 上传：`POST /api/notebooks/{notebook_id}/sources`，支持 PDF / Markdown / DOCX / PPTX。
- **上传不阻塞**：上传请求先建记录并返回（parse_status=`queued`），解析 + embedding + 抽取经共享 KG job scheduler 异步执行。
- parse_status 状态机：`queued -> parsing -> parsed -> extracting -> extracted`（失败置 `failed`）。
- repository 通过注入 `scheduler` 回调保持同步可测：HTTP 层提交 `kg_scheduler.submit_job`，冒烟/脚本不传则同步跑完整管线。
- 记录 metadata：file_name / file_size / file_hash / source_type / parse_status / summary。
- 相关 API：list sources、get source detail、手动重跑 `POST /api/sources/{source_id}/parse`（委托 `process_source`）、delete source 与受约束 URL import。
- 重新解析保留 source 行与原始文件，替换 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用相同 cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。

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
- exact-score 的 `base` 次序只适用于知识对象命中（`federated_retrieve()`）；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。base-wins 矛盾规则是独立的答案合成策略。

## 10. 自动抽取 Pipeline

- 当前主线为 KG-native 抽取：`backend/app/services/kg_ingest.py` 调用 `kg.extract_window`，把 source 分窗后抽取 Concept / Claim / Formula / Procedure 节点与关系边，再由 `build_records()` 绑定到 `SourceElement` evidence。
- `backend/app/services/extraction_profiles.py` 当前只维护 `academic_paper` / `textbook` 两类 profile，二者对象集均为 `concept / claim / formula / procedure`；`doc_type` 按单个 source 存储。
- 配置 `OPENAI_COMPAT_*` 时，`_run_extraction()` 走 LLM KG 抽取并把对象直接写入 `knowledge_objects`（status=approved）与 `knowledge_relations`；`extraction_runs.run_type='kg'`。
- 未配置 LLM 时，`_run_extraction()` 仍写入 completed run，但 `error_message='no-llm'`，不会生成启发式候选或假 KG。离线本机 beta 仍能解析、搜索、摘要和回答；需要知识召回时必须配置 LLM 或由测试/治理显式写入知识对象。
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

- **`ask()` 删除 demo 分叉**，全部数据驱动：
  1. 在 `claim/formula/procedure/concept` 四类 KG 对象上做混合检索，按类型权重做跨类型排序。
  2. 对 top hits 做 1-hop KG 关系扩展，且 hit 与 neighbour 都必须是 USABLE 状态。
  3. 配置 LLM 时生成带 `[k]` 标记的自然语言答案；未配置时返回 deterministic conclusion 与 `related_knowledge`/citations。
  4. citation 必须能回查到有效 `element_id`。
  5. 保存 answer 并返回 `answer_id` 与 `conversation_id`，用于反馈和多 session 会话。
- **历史记录（已退役）**：曾实现 `scenario_query()` / `case_search()` / `checklist()`；当前主线不再暴露这些 endpoint 或 tab。
- **推理模式 agentic search 实时进度（§6.5 / §11）**：新增 `POST /api/notebooks/{id}/ask/stream` NDJSON 流；后端先发 `start`，再把 `ReasoningRetriever` 的 plan / retrieve / reflect / expand / fallback / answer trace step 逐行推给前端，最后发送完整 `AskResponse`。前端推理按钮开启时走 stream，在 pending answer 中实时显示一行 agent 轨迹摘要，按最新 progress 事件刷新，点击后展开完整步骤；最终回答中保留默认折叠的 `reasoning_trace` 供回看；普通 `/ask` 仍作为兼容非流式路径。Ask 生命周期已全栈接通：transport 断连、页面导航或刷新只停止当前客户端继续接收，detached Ask job 继续执行并持久化最终回答；只有用户显式点击中断、前端调用 `POST /notebooks/{id}/ask/jobs/{job_id}/cancel` 时，后端才设置 cancellation event，`ask_chunk` / `ask_reasoning` / `ask_graph` 与 LLM 流式读取路径在关键阶段停止，且不会保存被显式取消的最终回答。已通过 `scripts/check.sh` 与 `cd frontend && npm run build`。
- **`follow_chain` 类型化查询期推理（§5.9 query-time 子集 / §6.5 / §11）**：`reasoning` agent 新增两跳链路动作，通过已有 source/target 索引做有界局部抽样，仅组合 `derived_from/kind_of/prerequisite_of/precedes/part_of` 同类型关系。该能力不新增 migration/索引/历史回填；高度节点被截断且无法确认不存在直接边时 fail-closed。三节点类型与 USABLE 状态、每跳 relation quote、edge review、候选起点、方向、环路/直接边重复、最低链可信度和 `validity_scope` 也全部 fail-closed；结果只进入本轮 Ask/深度报告上下文和实时 `推导` trace，不写回 KG。两条原始 hop 各自成为 relation evidence anchor；新抽取关系尽力绑定 `SourceElement`，旧 quote-only 关系降级为 source-level 展示且不影响原检索路径；临时 `A→C` 必须标作「推断」且不伪造引用。已通过 `scripts/check.sh` 与 `cd frontend && npm run build`。

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
- **前端可见性（`frontend/app/page.tsx`）**：`api()` 包装器 console.debug 方法/路径/耗时/request_id，并把后端 `detail` 透传进错误信息；轮询时显示"处理中（已 Ns）：文件: 阶段"，超时提示查看 `events.jsonl`，source 进入 `failed` 时展示 `error_message`。
- **配置**：`config.py` + `.env.example` 新增 `EVENT_LOG_ENABLED` / `EVENT_LOG_DIR` / `SLOW_REQUEST_MS`（沿用既有 `LLM_LOG_*`）。
- **验证**：`scripts/smoke_backend.py` 新增 `check_event_logging`（JSONL 可解析、禁用不写、写失败不抛）与 `check_pipeline_event_logging`（管线阶段事件产出 + `error_message` bug 回归）；`scripts/check.sh` 纳入 `event_logging.py` 编译。
- **慢因诊断脚本**：`scripts/diag_slow.py` 保持只读/脱敏，新增 strict reasoning / PPR 路径审计，基于 DB 聚合与 scale-index manifest 输出 indexed-core 覆盖率、chunk/relation ANN 状态、delta 策略与跨 base 可能触发 active 全量向量加载的风险，用于部署机上定位大库 reasoning 卡顿。

## 19. 历史新增（dev 分支，方案 §6/§7/§16，部分已被 KG-native 主线替代）

- **规则解释旧方案（§6.10）**：早期实现过 rule card 的 explain 方向；当前主线不再暴露 `/rules/{rule_id}/explain` 旧路由，改由通用 knowledge 详情与全屏 KG 详情展示 `出处`、相关节点和关系。
- **历史记录（已退役）：Derived Rule Candidate 审核队列（§7.5）**：过去曾有 `/derived-rules` 审核 API 与「派生规则候选」弹窗；当前 runtime 已移除。
- **创建富字段 + 模板（§6.1/§6.2）**：`NotebookCreate/Update/Summary` 增 `target_users/expected_questions/source_types/taxonomy/access_scope`（notebooks 表迁移）；6 套模板 `GET /notebook-templates`，创建按模板预填；前端集合页「从模板…」选择器 + 编辑弹窗富字段。
- **CSV / Excel 解析（§6.3）**：`parse_csv`(stdlib) + `parse_xlsx`(openpyxl) → `table_row` 元素；上传校验/accept 扩 `.csv/.xlsx/.xlsm`。
- **质量/分析看板（§16）**：`GET /notebooks/{id}/analytics`（有用率、低分提问=知识缺口、候选状态分布、知识覆盖、来源状态）；前端「看板」弹窗。
- **测试硬化**：`smoke_backend.py` 三处 `Settings` 清空 `OPENAI_COMPAT_*` + `mineru_mode=off`，`scripts/check.sh` 不再调用真实 LLM/embedding（即便 `.env` 有 key），全程离线 1–2s。
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
- **新规矩（AGENTS.md「Full-Stack Parity」）**：本系统中**任何面向用户的后端能力必须同变更内附带对应前端界面，不允许只实现一半**；"done" 的判定含后端端点、前端入口、`check.sh` 绿、`npm run build` 通过。
- **验证**：`smoke_backend.py` 增 knowledge_types/list_knowledge 断言；当前 TestClient smoke 确认动态 knowledge API 可用；`check.sh` 全绿。

## 23. Schema 管理 + 归纳 + 关系图 + ask 织入 + 抽取自我修正

- **可编辑 schema 注册表**：新增 `object_schemas` 表（迁移时从代码默认 seed，`INSERT OR IGNORE` 保留人工编辑）。抽取改为读 **DB 生效 schema**（`effective_schemas()` 叠加在代码默认上），prompt/schema-hint/字段排序全部按生效注册表。端点 `GET/POST/PATCH/DELETE /object-schemas`（内置可停用不可删、自定义可删）。前端「Schema」弹窗：列出/编辑字段·标签·说明、启用/停用、新增自定义类型。
- **Schema 归纳（建议态，§开放发现）**：`POST /notebooks/{id}/schema-proposals` 用 LLM 从笔记本内容提议新类型（offline 为 no-op），存为 `status='proposed' source='induced'`，绝不自动启用；前端在 Schema 弹窗审核（批准→active / 拒绝→删除）。
- **关系边消费（§7.4 基础）**：当前主线使用 `knowledge_relations` 与 `/unified-kg`，不再依赖各对象 payload 中的 `related_rules/cases/methods/concepts` 自由文本去临时推边。
- **Object 级知识图谱可视化（§7.4）**：前端「知识图谱」改为读取 `/unified-kg?level=object`，Concept / Claim / Formula / Procedure 同屏展示；主 canvas 直接绘制节点名称、类型形状/颜色、边关系标签，并按容器尺寸响应式布局；密集全量视图用类型分区与标签降噪，左侧提供可选一种或多种类型的过滤；侧栏提供按类型分组的节点总览，选中节点会聚焦 canvas 并展示 payload、相邻关系和「出处」；「出处」使用证据卡片分离来源元数据与原文正文，避免长文件名/英文段落/公式在窄侧栏中挤成细列。Concept 节点继续拉取详情，相关 Claim / Formula / Procedure 以「相关节点」展示在出处下方，并按类型分组且复用 canvas 的类型颜色/形状。
- **新类型织入 ask**：`AskResponse.related_knowledge`（通用块）召回 KG top 命中 + USABLE 一跳邻居；前端 AnswerView 不再把所有相关知识平铺在答案下方，而是用顺序引用承接证据，并在引用区提供知识图谱入口供用户继续浏览相关节点。
- **证据绑定升级**：KG `build_records()` 会把 LLM 节点 evidence 绑定到 source elements；离线 smoke 覆盖 exact/fuzzy binding、window grounding 与 ungrounded node drop。
- **顺带修复**：`routes.py` 缺失的 `NotebookTemplate` import（API 模块导入即 NameError）。
- **验证**：当前 `smoke_backend.py` 覆盖 `check_object_schemas / check_kg_record_binding / check_kg_extract_window_grounding / check_kg_store_ask_and_conversations` + API route smoke；`check.sh` 全绿。

## 24. 类型决策从「建库」移到「上传/单文件」+ 描述自动生成 + API 层冒烟

- **动机**：库类型不应在建库时选；应按**文档内容类型**选 schema，且粒度到**单个文件**（一个库可混论文/方案/复盘）。
- **建库极简化**：去掉模板/库类型与富字段预填，建库只留**名称 + 描述**。描述留空 → `purpose_auto=1`，在用户添加**首批来源**后由来源内容自动生成（LLM 配置时 1–2 句摘要，否则「N 个来源 + 类型涵盖…」启发式）；用户手改描述后置 `purpose_auto=0`，不再覆盖。
- **per-file 文档类型**：`sources.doc_type`（迁移）；上传接口增 `doc_types` 表单数组（与 files 按序对齐）；当前 profile 解析为 **source.doc_type 优先 → 空/auto 内容判别 → 默认 academic_paper**。`GET /doc-types` 暴露自动检测 + academic_paper/textbook。
- **前端**：建库改「名称+描述」弹窗；上传改**暂存式**——选文件→列清单→每文件文档类型下拉(默认自动检测)+「全部设为…」→确认上传；移除「从模板…」入口。
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
- **嵌入并发化**：元素向量（`_embed_source`/emb-el）与知识对象向量（`_embed_objects_batch`/emb-kg）都改线程池并发（`embed_concurrency=50`）+ 逐 batch 独立连接落库 + 失败隔离。
- **抽取优先管线**：`process_source` 前台跑 KG 抽取，元素向量化在后台 daemon 线程并发；`extracted`（前端绿）只看抽取完成。`_connect` 开 WAL + `busy_timeout` 支撑并发写。
- **检索内存/性能**：`ask()` 把向量流式读成每-notebook L2 归一化 **float32 numpy 矩阵**（`vector_index`）+ `vector_cache` 版本键缓存，`query_sims` 单次 matmul。峰值内存大幅下降（实测大 KG 1.3G→约 500M），重复查询亚秒，消除大 KG 下的 OOM/卡死。`_TYPE_WEIGHT`=claim/formula/procedure/concept=1.0/1.0/0.7/0.5。
- **产品行为**：导入后不再自动生成/覆盖笔记本名字/描述；前端「＋新建」直接创建未命名笔记本并进入（去弹窗）；状态点绿色只给 `extracted`、中间态橙。
- **配置旋钮**：新增 `kg_window_target_chars/overlap`、`kg_extract_workers`、`kg_window_warn_threshold`、`embed_concurrency/batch_size/truncate_chars/persist_chunk`、`db_busy_timeout_ms`、`retrieval_top_n`。
- **死代码清理（历史记录修正）**：当时先移除了 `/case-search`、`/checklist`、`/sources/{id}/extract` 等 legacy；其后 articles / derived-rules 也已从当前 runtime 移除。当前保留的是通用 knowledge 治理、duplicates/merge/conflicts、reports 与图谱治理路径。
- **验证**：`bash scripts/check.sh` 通过（py_compile + KG-native smoke + 前端 `tsc --noEmit`）。

## 27. Agent Memory 与 MCP（方案 §19；Agent Memory 设计 §4～§13）

- **独立 Memory 层**：schema v11 增加 `memory_items`、revision、provenance、embedding/FTS、
  Agent profile/token/allowlist 表。每条 Memory 同时绑定 `created_by` 与一个 notebook；总 Memory
  页面只聚合当前用户，notebook 卡片以批量 summary query 显示当前用户数量，工作区标签为
  `Ask | Knowledge | Memory | Deep Report`。共享 notebook 不共享成员 Memory。
- **手动回答沉淀**：Ask 回答提供“保存到 Memory”，先调用 preview、允许编辑，再由用户确认写入
  confirmed Memory 和可信 answer/citation provenance。同一用户重复保存同一 answer 幂等返回已有
  Memory；预览后 answer 删除则保存返回冲突。未配置或调用失败的 LLM 使用问题标题 + 清理显示引用
  后的回答作为确定性 fallback。
- **生命周期与权限**：Agent 只能创建 candidate；用户可编辑、确认、拒绝、弃用。Candidate 在同一
  用户、同一 notebook 下由具备 `memory:read_candidates` 的所有 Agent profile 共享；不同用户、
  不同 notebook、rejected/deprecated 均排除。用户丢失 notebook 访问权时 Memory 暂不可读/检索；
  notebook 删除按 FK 生命周期级联成员绑定的私有 Memory，前端删除确认已有不泄露成员明细的警告。
- **两个检索平面**：candidate+confirmed 只进入 scoped Agent Memory 平面；正式 notebook Ask、
  notebook 搜索、Deep Report 与 `search_notebook_context` 只接收 confirmed。Memory 命中使用独立
  anchor/provenance，不伪造 source/element id。排序先判相关性，只有等分/冲突再应用
  `candidate < personal source < confirmed Memory < base KG/base source` 权威规则。
- **Agent 接入 UI 与 token**：总 Memory 页可创建/停用稳定 Agent profile，签发明文只显示一次的
  opaque token，配置默认 notebook、notebook allowlist、过期时间与最小 scope，并列出、撤销 token。
  Scope 为 `knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
  `ask:execute`；撤销、过期、profile 停用或 notebook 权限变化会在后续调用重新校验并立即生效。
- **官方 MCP Streamable HTTP**：`/mcp` 提供精确七工具 `list_notebooks`、`select_notebook`、
  `search_agent_memory`、`search_notebook_context`、`get_memory`、`ask_notebook`、
  `propose_memory`。每个新 session 必须先显式选择 allowlisted notebook；数据工具继续校验 notebook，
  候选只能提交不能由 Agent 确认/拒绝/弃用/晋升。loopback 可用 HTTP，非 loopback/public URL 强制
  HTTPS；返回私有文本按不可信 evidence 处理并做长度/结果数上限。
- **Memory → KG**：仅 confirmed 且尚未提案的 Memory 可由创建者提议；既有 admin promotion
  queue 展示脱敏后的结构化提取候选与服务端验证过的 evidence，不提供原始 Memory
  revision/provenance 浏览。编辑或弃用 proposed Memory 会在同一事务中拒绝活跃队列项、保留固定
  快照审计并清除当前 promotion 状态。批准前重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，批准
  复用 dedupe/merge，创建或合并一个或多个 Base KG 对象；API 与晋升审计保存完整
  `base_object_ids`。私有 Memory 的 owner/status 不改变，完整私有任务上下文不进入 Base KG 对象。
- **确定性评价与验证**：固定 gold 计算 Recall@5/MRR/nDCG，candidate→正式平面、跨用户、跨
  notebook 三个泄漏计数均为 0；A/B harness 覆盖 no-Memory、KB-only、KB+confirmed-Memory。
  `scripts/check.sh` 已包含官方 `mcp` client 离线 smoke，验证七工具、session 选择隔离、candidate
  正式平面隔离和同用户同 notebook 跨 Agent 召回。本次门禁结果：后端 `2934 passed, 1 skipped`、
  前端 `189 passed`、TypeScript 与 Next.js production build 均成功。

## 20. 当前边界（后续阶段，未计入已完成）

- **历史 Article 方案**：已退役，不属于当前后续承诺；当前长内容产出路径是 Deep Report。
- **v0.4 Review Mode**：review session、场景 checklist sign-off、reviewer 评论、action items、导出 review 报告。
- **v1.0 企业**：RBAC / source 级权限 / 审计 / SSO / 私有部署 / Confluence·SharePoint·Jira·Git·Slack connectors / 多 notebook 搜索 / rule version diff。
- 检索：BM25 / FTS5 / pgvector 放量、结构化硬过滤、Knowledge graph（已评估为低 ROI / 基础设施级，暂缓）。
- 扫描件 OCR、DOCX/PPTX 公式（OMML）解析；MinerU 已覆盖 PDF 的公式/表格/版面（本机 MLX 或 GPU 主机）。
- **架构渐进整改后续阶段**：阶段 3（FastAPI routers 与前端 API client）与阶段 5（前端 workspace 状态拆分）仍为计划项。阶段 2、4、6 的 Repository 部分已由 Repository composition refactor 交付（见第 19 节账本 2026-07-11 条目）；其中旧阶段 4 的 Pydantic 模型分文件与旧阶段 6 的 FastAPI lifespan / 统一应用生命周期延后为独立工作。

> 已完成里程碑：v0.1 闭环、Tier 1（场景/案例/Checklist/知识库前端 + 上传轮询 + knowledge 向量召回）、PDF MinerU(MLX) + KaTeX/表格渲染、**Tier 2 知识治理（状态生命周期 + 多类型浏览 + 合并 + 冲突检测）**、**检索/抽取算法升级（CJK 分词 + hybrid 融合 + 结构化场景匹配 + payload 级向量 + 全文分窗口抽取 + 鲁棒证据绑定）**、**全链路可观测日志系统（LLM/HTTP/管线三通道 JSONL + 控制台）**。

- 已完成（2026-06-06）：大笔记本 KG 性能与合并治理——Ask 去同步 backfill/全量扫描 + notebook 级索引 + 阶段计时；node_context/concept_detail 收窄查询；unified-KG 改显式 rebuild + dirty status（摄取不再同步重建、打开图谱不自动重建）；跨文档概念合并改有界 top-k 向量候选 + 别名归一化；可选 LLM 概念合并预审。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-06-06）：推理模式 agentic search 实时进度——`/ask/stream` 输出 NDJSON progress/final 事件，Ask 前端在运行中展示按事件刷新的折叠 agent 轨迹摘要，点击可展开完整步骤，并在答案中保留默认折叠的最终 trace。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-07-10）：reasoning `follow_chain`——该能力不新增 migration 或改写历史数据，查询期复用既有端点索引，对有证据、审核可用、条件兼容的同类型两跳关系做有界类型化组合；关系前提可引用、推论不入库，Ask/深度报告与流式 `推导` 轨迹均已接通。已通过 `scripts/check.sh` 与前端 build。
- 已完成（2026-06-25）：用户账号系统——
  - **后端**：`auth_sessions` 表存储不透明 Bearer session token；`app/services/auth_utils.py` 封装 PBKDF2-SHA256 密码哈希与 token 生成；`app/api/auth_routes.py` 实现 `POST /auth/register`、`POST /auth/login`、`POST /auth/logout`、`GET /auth/me`；`app/api/deps.py` 提供 `get_current_user` 依赖用于路由级鉴权；`notebooks.created_by` 列实现按 owner 隔离（用户只能看/操作自己的 notebook）；内置 `user-local` 账号原地升级为 `admin`（id 不变，登录用户名 `admin`，密码由 `SILICON_NOTEBOOK_ADMIN_PASSWORD` 控制，本地默认 `admin`，每次后端启动重置；production/对外监听必须改为强密码）；admin 拥有既有 notebook 并是唯一可标记基准库的用户；基准库从普通用户列表隐藏但仍参与问答上下文检索。新增环境变量：`SILICON_NOTEBOOK_ADMIN_PASSWORD`（admin 密码）和 `SILICON_NOTEBOOK_AUTH_OPTIONAL`（默认 false=强制登录；true=无 token 请求回退 admin，仅本地/测试）。
  - **前端**：首次加载展示登录/注册界面；注册用户名规则为单个字母 + `00` + 6 位数字（如 `a00123456`，存为小写）；Bearer token 写入 localStorage 并由 api() 自动注入请求头；顶栏展示当前登录用户名与退出按钮；"设为基准库"操作仅 admin 可见。
  - **测试**：新增 `tests/test_auth.py`（注册/登录/会话/退出）、`tests/test_user_isolation.py`（notebook owner 隔离）以及集成场景覆盖；全部 ~990 测试通过，`scripts/check.sh` 与 `npm run build` 绿。
  - 本轮有意不包含：修改密码、共享、协作。
