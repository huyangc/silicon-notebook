# silicon-notebook 架构与算法逻辑

更新日期：2026-05-29

本文件梳理当前实现的**核心算法逻辑**与**功能清单**（API / 数据表 / 知识对象 / 前端 / 配置）。代码以 `backend/app` 与 `frontend/app/page.tsx` 为准。

---

## 1. 系统总览

FastAPI + SQLite 后端，Next.js/TypeScript 前端。核心是一条对**任意用户上传内容**生效的闭环：

```
创建 notebook
 → 上传 source（PDF/MD/DOCX/PPTX，multipart）
 → [异步 BackgroundTask] 解析 parse → 嵌入 embed → 抽取 extract
 → 候选进 Review Queue，curator approve/reject/edit → 正式 knowledge_objects
 → 知识治理（状态机 / 合并 / 冲突）
 → 混合检索（关键词 + 向量 + 场景 boost）
 → 结构化问答 Ask / Scenario / Case / Checklist（citation 校验）
 → Article Studio 研究简报（claims + 关系 + 派生规则候选）
 → 用户反馈 👍/👎
```

LLM / embedding / MinerU 任一未配置时，对应环节走 **deterministic 回退**，整条链路离线可跑。

### 组件地图
| 关注点 | 文件 |
|---|---|
| 路由 | `backend/app/api/routes.py` |
| 仓库（业务核心） | `backend/app/services/sqlite_repository.py` |
| 解析 | `backend/app/services/parsers.py` |
| MinerU 适配 | `backend/app/services/mineru_client.py` |
| 抽取 | `backend/app/services/extraction.py` |
| 检索打分 | `backend/app/services/retrieval.py` |
| LLM 客户端 | `backend/app/core/llm.py` |
| Prompt/schema | `backend/app/services/prompts.py` |
| Schema | `backend/app/models/schemas.py` |
| 配置 | `backend/app/core/config.py` |
| 前端（单文件） | `frontend/app/page.tsx` |

---

## 2. 核心算法逻辑

### 2.1 文档解析（parsers.py + mineru_client.py）
- 按扩展名分发：Markdown（heading/paragraph/list_item）、DOCX（paragraph/table_row）、PPTX（按 shape 的 `slide_text` + `speaker_notes`）、PDF、plain text 回退。
- **PDF 与 GPU 解耦的 MinerU 适配器**（`MINERU_MODE`）：
  - `http` → POST 远端 `mineru-api` `/file_parse`；`cli` → subprocess 本机 `mineru`（Apple Silicon 自动走 MLX）；`off`（默认）→ pypdf 纯文本。
  - MinerU 的 `content_list` 经 `mineru_content_list_to_elements` 映射为结构化 `SourceElement`：公式→`formula`（保留 LaTeX，去 `$$`）、表格→`table`（HTML 存 `metadata.table_html`，正文展平）、标题保留 `text_level`、`page_idx` 转 1-based。
  - MinerU 不可达/报错或产出空 → **静默回退 pypdf**，上传永不阻塞。PDF 解析出 0 元素时给"疑似扫描件"提示。
- 每个元素带 `location_label`，作为 evidence/citation 锚点。

### 2.2 嵌入（sqlite_repository `_embed_source` / `_embed_query`）
- `embedding_configured` 时，解析后对每个 element 调 `llm_client.embed(text[:2000])`，向量以 JSON 存入 `element_embeddings`（payload 级向量表 `knowledge_embeddings` 已就绪）。未配置则跳过，检索退化为纯关键词。

### 2.3 混合检索打分（retrieval.py）—— 最关键的算法
- **CJK 感知分词** `_tokens`：中文连续串切**字符 bi-gram**（单字→uni-gram），拉丁/数字保留词级 token。被检索、抽取证据绑定、场景 boost 共用。
- **keyword_score(query,text)** = query token 命中占比（0..1）；**cosine** = JSON 向量余弦。
- **融合 `_fuse`**：`(W_KEYWORD*kw + W_SEMANTIC*sem)/denom`，`denom` 按是否有向量重归一化 → 纯关键词对象不被 `W_KEYWORD` 上限压制。权重 `W_KEYWORD=0.4 / W_SEMANTIC=0.6`。
  - knowledge 语义分 = query 向量与该对象**证据元素向量**（及 payload 向量）的最佳 cosine。
- **结构化场景 boost `structured_boost`**：场景 9 字段 token 与规则 `applies_to/condition/title/use_when` 的重合比例；`final = relevance * (1 + SCENARIO_BOOST_ALPHA*boost)`，`SCENARIO_BOOST_ALPHA=0.5`（软加权，不硬过滤）。
- **噪声地板** `RELEVANCE_FLOOR=0.12`：低于则丢弃。
- **类型权重** `_TYPE_WEIGHT`（rule 1.0 > case 0.9 > checklist 0.8 > method 0.7 > risk 0.6 > glossary 0.4）：仅用于跨类型排序/分组，不污染同类相关度。
- `query_vector` 为空 → 自动退化为纯关键词，行为可复现（离线冒烟据此断言）。

### 2.4 知识抽取（extraction.py）
两条路径，`run_extraction` 统一去重 + 按 confidence 排序：
- **LLM 路径（配置后）**：
  - **分窗** `_element_windows`：按 `WINDOW_MAX_CHARS=9000`(+`WINDOW_OVERLAP_CHARS=500`) 覆盖**整篇**文档（`MAX_WINDOWS=0` 即不限窗，可设上限控成本），逐窗 `chat_json` 抽取后跨窗 `_dedupe_records`。
  - **证据绑定 `bind_evidence`**：①精确子串包含 → ②CJK `token_overlap ≥ BIND_OVERLAP_THRESHOLD(0.6)` 模糊回退（容忍改写/标点差异）；绑定成功 `status=candidate`，否则 `needs_review`。
  - **JSON 稳健**（llm.py `chat_json`）：`response_format=json_object`（服务不支持则回退）+ `strip_json_fences` 去 ```` ``` ```` 围栏。
- **启发式回退（离线）**：
  - **句子级**切分（一段可多候选）；按优先级 `_classify_sentence`：rule > case > checklist > risk > method。
  - rule 拆出 recommendation 与 `risk_if_ignored`（否则/otherwise/to avoid）、轻量 `applies_to`（领域/封装/信号范围词）。
  - **glossary 仅从定义句式**（`术语：定义` / `is defined as` / `指/是指/定义为`）；**heading/table/formula/image_caption/speaker_notes 不走指令/案例启发式**（消除旧版"每个标题→空 glossary"噪声）。
- **去重 `_dedupe_records`**：按 (type, 归一化主文本) 去重；候选带 `confidence` 与 `extraction_mode`(llm/heuristic)。
- 触发：`process_source` 解析后自动；手动 `POST /sources/{id}/extract`。落 `extraction_runs` / `extraction_candidates`。

### 2.5 审核与知识治理（sqlite_repository.py）
- 候选 approve → 写入 `knowledge_objects`（`object_type` + `payload` + `evidence` + `status`）；reject → 标记并清理。
- **状态机**：`KNOWLEDGE_STATUSES = approved/reviewed/deprecated/conflict/project_specific`；**仅 `USABLE_STATUSES`(approved/reviewed/project_specific/conflict) 进入检索/回答**，`deprecated` 排除。`PATCH /knowledge/{id}` 改 status/owner/payload 并盖 `last_reviewed`。
- **重复检测/合并**：同类型两两相似度（payload `keyword_score` + 证据向量 cosine，阈值 0.6 成组）→ `GET /duplicates`；`POST /knowledge/{id}/merge` 并合 evidence、源置 `deprecated`。
- **冲突检测**：规则对 applies_to/title 高重合但 recommendation 取向相反 → `GET /conflicts`；`ask()` 命中 `conflict` 状态规则时在 `missing_information` 追加冲突提示（§12）。

### 2.6 问答（ask / scenario_query / case_search / checklist）
1. 解析问题 + scenario 标签 → query；取 `USABLE` 知识 + 相关 elements。
2. `score_knowledge`/`score_elements` 混合打分（截断 rules5/cases4/checklist6/methods4/risks4/elements8）。
3. **LLM 模式**：`answer_prompt` 生成结构化 `AskResponse`（conclusion/applicable_scenario/recommended_methods/related_rules/potential_risks/related_cases/checklist/missing_information/citations/llm_mode）；**无 LLM 用模板组装**检索结果。
4. **Citation 校验**：引用的 element_id 必须能回查到有效元素，否则丢弃并在 missing_information 标注。
5. 保存 `answers` 并返回 `answer_id`（供反馈关联）。
- `scenario_query` 把 9 字段拼成场景后走 ask；`case_search`/`checklist` 走对应类型检索（checklist 无命中时从 rules 兜底生成）。

### 2.7 Article Studio（research_article）
- 文章作为 source/标题+摘要文本；LLM 或句子级回退抽取 claims（带 evidence）。
- `_attach_claim_relationships`：每条 claim 用 `keyword_score` 找最佳规则，启发式判 `relation_type`（supports/extends/refines/challenges…）+ `related_rule_id` + `implication`。
- 产出 `core_contribution / claims / limitations / notebook_relationships / derived_rule_candidates(带 rationale+evidence) / validation_plan`，持久化 `article_claims` / `derived_rule_candidates`。
- 回归保证：不再泄露写死 bondwire 文本。

### 2.8 LLM 客户端与日志（llm.py）
- OpenAI 兼容；`chat_json`（json_object + fence 清理）、`embed`。
- 每次调用写 `.local/logs/llm.jsonl`（状态/延迟/token/错误），便于排查端点。

---

## 3. 功能清单

### 3.1 API（37 条，`/api` 前缀）
- **系统**：`GET /health`、`GET /me`
- **Notebook**：`GET/POST /notebooks`、`GET/PATCH/DELETE /notebooks/{id}`
- **Source**：`GET /notebooks/{id}/sources`、`POST /notebooks/{id}/sources`(上传)、`POST .../sources/import`、`GET/DELETE /sources/{id}`、`POST /sources/{id}/parse`、`GET /sources/{id}/elements`、`POST /sources/{id}/extract`
- **候选审核**：`GET /notebooks/{id}/candidates[/{type}]`、`PATCH /candidates/{id}`、`POST /candidates/{id}/approve|reject`
- **知识浏览/治理**：`GET /notebooks/{id}/rules|methods|risks|glossary`、`PATCH /knowledge/{id}`、`GET /notebooks/{id}/duplicates`、`POST /knowledge/{id}/merge`、`GET /notebooks/{id}/conflicts`
- **检索/问答**：`GET /notebooks/{id}/search`、`POST /notebooks/{id}/ask|scenario-query|case-search|checklist`
- **Article**：`GET/POST /notebooks/{id}/articles`、`DELETE /articles/{id}`、`POST /articles/{id}/research`
- **反馈**：`POST /answers/{answer_id}/feedback`

### 3.2 SQLite 表（15）
`users`、`user_profiles`、`notebooks`、`sources`、`source_elements`、`articles`、`extraction_runs`、`extraction_candidates`、`element_embeddings`、`knowledge_embeddings`、`knowledge_objects`、`answers`、`feedback`、`article_claims`、`derived_rule_candidates`

### 3.3 知识对象与状态
- 类型：rule / method / risk / case / checklist / glossary（统一存 `knowledge_objects`，payload 内联）。
- 状态：reviewed / approved / deprecated / conflict / project_specific（候选另有 candidate / needs_review / approved / rejected）。
- 卡片 schema：`RuleCard`(title/statement/applies_to/recommendation/risk_if_ignored/severity/status/owner/last_reviewed/evidence) · `CaseCard` · `MethodCard` · `RiskItemCard` · `GlossaryTermCard` · `ChecklistItem` · `ArticleClaimCard`。

### 3.4 source parse_status 状态机
`queued → parsing → parsed → extracting → extracted`（失败 `failed`）。前端对非终态 source 每 ~1.5s 轮询 `GET /sources/{id}`。

### 3.5 前端功能（`frontend/app/page.tsx`）
- 集合页：tab 过滤 / grid·compact·list 视图 / 排序 / debounce 搜索 / 新建·编辑·删除。
- 工作区三栏：左 Source Stack（上传 + 实时状态 + detail/删除 + Review Queue）、中 Knowhow 工具 tab（问答 / 场景查询 / 案例检索 / Checklist / **知识库**）、右 Studio（思维导图 / 新建文章 / 信息图）。
- 知识库：规则/方法/风险/术语子切换、状态徽标、状态下拉 + owner 编辑（PATCH）、查重/冲突面板 + 合并。
- source detail：KaTeX 渲染公式、HTML 渲染表格、element_type 徽标。
- 回答区：结构化卡片 + citation + 👍/👎 反馈。

### 3.6 配置开关（`.env` / config.py）
- LLM：`OPENAI_COMPAT_BASE_URL/API_KEY/MODEL/EMBEDDING_MODEL/TIMEOUT_SECONDS`（`llm_configured` / `embedding_configured`）。
- MinerU：`MINERU_MODE(off|http|cli)`、`MINERU_API_URL`、`MINERU_BACKEND`、`MINERU_VLM_SERVER_URL`（仅 `vlm-*-client` 后端用，指向独立 VLM 推理服务器）、`MINERU_MODEL_SOURCE`、`MINERU_TIMEOUT_SECONDS`、`MINERU_FORMULA_ENABLE`、`MINERU_TABLE_ENABLE`。
- 存储/CORS：`DATABASE_URL`、`SILICON_NOTEBOOK_STORAGE_DIR`、`SILICON_NOTEBOOK_CORS_ORIGINS`。

### 3.7 验证
- `scripts/check.sh`：py_compile + 离线 hermetic `smoke_backend.py`（钉死 `mineru_mode=off`、清空 LLM/embedding，不读真实密钥）+ 前端 `tsc --noEmit`。
- smoke 覆盖：上传/解析、MinerU 映射、检索打分（关键词/向量/None 三态 + 场景 boost）、抽取（启发式多候选/无 glossary 噪声/去重/绑定）、状态机（deprecated 不召回）、合并/冲突、article、反馈、JSON fence 清理、异步 scheduler 路径、重启持久化。

---

## 4. 关键设计取舍
- **GPU 解耦**：后端绝不 import torch/MinerU；重活在子进程/远端，本机/CI 始终轻量离线。
- **离线可跑**：每个 LLM/embedding/MinerU 环节都有 deterministic 回退；测试不依赖外部服务与密钥。
- **Evidence-first**：抽取与回答都绑定 element 级证据，citation 回查校验，杜绝"无出处结论"。
- **SQLite + Python 余弦**：本机 beta 不引 pgvector；预留 PostgreSQL/pgvector 放量方向。
