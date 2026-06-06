# silicon-notebook 架构与算法逻辑

更新日期：2026-06-05

本文件梳理当前实现的**核心算法逻辑**与**功能清单**（API / 数据表 / 知识对象 / 前端 / 配置）。代码以 `backend/app` 与 `frontend/app/page.tsx` 为准。

> 逐行核对源码的**算法 + 全量函数/接口/表清单**另见 [算法与功能清单.md](算法与功能清单.md)（含配置旋钮速查表）。

---

## 1. 系统总览

FastAPI + SQLite 后端，Next.js/TypeScript 前端。核心是一条对**任意用户上传内容**生效的闭环：

```
创建 notebook（直接进入，不弹窗）
 → 上传 source（PDF/MD/DOCX/PPTX/CSV/XLSX，multipart）
 → [process_source] 解析 parse → 元素向量化(后台 daemon) ‖ KG 抽取(前台)
 → extracted(绿) — 仅看 KG 抽取完成，不等向量化
 → 知识对象进 knowledge_objects + knowledge_relations
 → 混合检索（关键词 bi-gram + 语义矩阵 matmul）
 → KG-native 问答 ask()（逐句 [k_i] 引用 + 多轮会话）
 → 统一 KG / 跨文档概念聚类（concept_clusters）
 → 用户反馈 👍/👎
```

LLM / embedding / MinerU 任一未配置时，对应环节走 **deterministic 回退**，整条链路离线可跑。

### 组件地图

| 关注点 | 文件 |
|---|---|
| 路由 | `backend/app/api/routes.py` |
| 业务核心（摄取/问答/治理） | `backend/app/services/sqlite_repository.py` |
| 结构化解析 | `backend/app/services/structural_markdown.py` |
| 多格式解析适配器 | `backend/app/services/parsers.py` |
| MinerU 适配 | `backend/app/services/mineru_client.py` |
| KG 窗口化 | `backend/app/services/kg/windowing.py` |
| KG 抽取（LLM） | `backend/app/services/kg/extract.py` |
| KG 规范化/合并 | `backend/app/services/kg/canonicalize.py` |
| KG 摄取管线 | `backend/app/services/kg_ingest.py` |
| 检索打分 | `backend/app/services/retrieval.py` |
| 向量矩阵 | `backend/app/services/vector_index.py` |
| 向量缓存 | `backend/app/services/vector_cache.py` |
| 嵌入（dashscope） | `backend/app/services/embedding_dashscope.py` |
| 抽取 profile | `backend/app/services/kg/extraction_profiles.py` |
| LLM 客户端 | `backend/app/core/llm.py` |
| Schema | `backend/app/models/schemas.py` |
| 配置 | `backend/app/core/config.py` |
| 前端（单文件） | `frontend/app/page.tsx` |

---

## 2. 核心算法逻辑

### 2.1 文档解析（`structural_markdown.py` + `parsers.py` + `mineru_client.py`）

**唯一结构化 Markdown 解析器**(`structural_markdown.py`)：`markdown-it-py`（commonmark 预设 + 启用 table，不启用 linkify）→ `Block{type, text, raw, level, lang, char_start/end, line_start/end, section_path面包屑, anchor_id}`。支持类型：`heading / paragraph / list_item / code_block / table / image`。

被两个适配器复用：
- `parsers.parse_markdown` → `SourceElement`，供存储与嵌入。
- `kg/parsing.parse_elements` → `SourceElementQ`，供 KG 窗口化。

特殊规则：**代码块整块保留**但**不进 KG 抽取**（不出现在 `_PROSE_TYPES` 中）；`<a id>` 锚点被解析为 `anchor_id` 但在产出时丢弃。

其他格式：PDF 走 MinerU（`MINERU_MODE` off/http/cli）→ pypdf 兜底；DOCX / PPTX / CSV / XLSX 各有独立解析器。

MinerU 不可达/报错/产出空 → 静默回退 pypdf，上传永不阻塞。PDF 解析出 0 元素给"疑似扫描件"提示。

### 2.2 KG 窗口化（`kg/windowing.make_windows`）

`_PROSE_TYPES = (paragraph, list_item, formula, table, figure_caption)`（**不含 heading / code_block**）。

按文档顺序**贪心打包** prose 块到目标窗口字符、相邻 overlap `kg_window_overlap_chars`（450）；吸收碎小节；超长单元素按 step = target - overlap 内切。窗口大小**自适应**：`plan_window_size` 取 `clamp(内容字符 / KG_EXTRACT_WORKERS, KG_WINDOW_MIN_CHARS=4000, KG_WINDOW_MAX_CHARS=8000)` 并等长切分（`KG_WINDOW_TARGET_CHARS>0` 时固定为该值）。窗口数超 `kg_window_warn_threshold`（1200）记 WARNING，不截断。

### 2.3 KG 抽取（`kg_ingest.extract_graph`）

经**全局窗口池**（`kg/scheduler.py` 的 `submit_window`，容量 `KG_EXTRACT_WORKERS` 全局封顶、跨所有文档共享、FIFO）并发逐窗 LLM 抽取；文档级并发由**作业池** `KG_JOB_CONCURRENCY` 控制（上传分发经 `submit_job(process_source)`，替代顺序 BackgroundTask）：

- `NODE_TYPES = {Concept, Claim, Formula, Procedure}`
- `EDGE_TYPES = {defines, part_of, composed_of, contrasts_with, kind_of, …}`
- 抽取 profile：`academic_paper / textbook`（`kg/extraction_profiles.py`）

单窗失败隔离（`failed_windows` 计数），不影响其他窗口。`canonicalize` 做跨窗节点合并。`build_records` 把节点转知识对象，按以下顺序绑定 evidence：① 精确子串包含；② CJK token 重叠 ≥ 0.6 模糊回退。绑不上的节点整体丢弃。

### 2.4 嵌入（`embed_provider = dashscope`）

两类向量：
- **元素向量** `_embed_source` → `element_embeddings`（文本前 `embed_truncate_chars=2000` 字符）。
- **知识对象向量** `_embed_objects_batch` → `knowledge_embeddings`（payload 文本）。

**两者都用 `ThreadPoolExecutor` 并发**（线程前缀 `emb-el` / `emb-kg`，并发度 `embed_concurrency=50`，batch `embed_batch_size=10`）；**每批独立连接逐批落库**（WAL + busy_timeout），批失败隔离不中断整体。

### 2.5 摄取管线（`process_source`）

状态机：`queued → parsing → parsed → extracting → extracted`（失败 `failed`）。

解析后：**元素向量化在后台 daemon 线程**、与前台 KG 抽取并发；`extracted`（前端绿）**仅看 KG 抽取完成**，不等向量化；末尾 `embed_thread.join()` 收尾后台线程。

**导入后不再自动生成/覆盖笔记本名字或描述**。

### 2.6 存储（SQLite）

`_connect` 开 **WAL + busy_timeout**（`db_busy_timeout_ms=30000`）。共 20 张表（关键：`knowledge_objects / knowledge_relations / knowledge_embeddings / element_embeddings / conversations / concept_clusters / extraction_runs / sources / source_elements`，详见 [算法与功能清单.md §E](算法与功能清单.md)）。

### 2.7 混合检索（`retrieval.py` + `vector_index.py` + `vector_cache.py`）

关键词（CJK 连续串切字符 bi-gram + 中英停用词，`W_KEYWORD=0.4`）+ 语义（`W_SEMANTIC=0.6`，`RELEVANCE_FLOOR=0.12`）。

`ask()` 为每 notebook 构建 **L2 归一化 float32 矩阵**（`vector_index.build_matrix` 流式读、内存有界，约百 MB 级；旧版 Python list 达 1.3 G），用 `vector_cache`（版本键 = 表名/行数/max created_at）缓存；`query_sims` 单次 matmul。

`_TYPE_WEIGHT = {claim: 1.0, formula: 1.0, procedure: 0.7, concept: 0.5}`（仅用于跨类型排序/分组，不污染同类相关度）。

top-N `retrieval_top_n`（12）+ 沿 `knowledge_relations` **1-hop 扩展**邻居。

### 2.8 问答 `ask()`

KG-native 接地问答。

`AskResponse{answer（含 [k_i] 标记）, grounded, anchors[{key, object_id, name, definition, snippet, …}], related_knowledge, citations, llm_mode∈grounded/ungrounded/deterministic, conversation_id}`。

多轮：`conversations` 表 + `answers.conversation_id`。

LLM 合成（`_answer_kg`，给每命中 `k{i}` id + 定义/首现 snippet / procedure steps；concept 按 unified 簇去重）或确定性兜底（离线关键词 + 模板）。

### 2.9 统一 KG 与概念聚类

跨文档概念聚类/合并（`concept_clusters`），暴露于 `/unified-kg`、`/concepts/{id}/detail`、`/objects/{id}/context`；支持 pending-merges confirm / reject。

### 2.10 LLM 客户端（`llm.py`）

OpenAI 兼容；`chat_json`（`response_format=json_object` + `strip_json_fences` 去围栏）、`embed`。每次调用写 `.local/logs/llm.jsonl`（状态/延迟/token/错误）。

---

## 3. 功能清单

### 3.1 API（`/api` 前缀，`routes.py`）

**系统**：`GET /health`、`GET /me`、`GET /doc-types`、`GET /notebook-templates`

**Notebook**：`GET/POST /notebooks`、`GET/PATCH/DELETE /notebooks/{id}`、`GET /notebooks/{id}/analytics`

**Source**：`GET /notebooks/{id}/sources`、`POST /notebooks/{id}/sources`（上传 multipart）、`POST .../sources/import`、`GET/DELETE /sources/{id}`、`POST /sources/{id}/parse`、`GET /sources/{id}/elements`

**知识类型 / 知识对象**：`GET /notebooks/{id}/knowledge-types`、`GET /notebooks/{id}/knowledge`（`?type=`）、`PATCH /knowledge/{id}`

**Object Schema**：`GET/POST /object-schemas`、`PATCH/DELETE /object-schemas/{object_type}`、`POST /notebooks/{id}/schema-proposals`

**知识图谱**：`GET /notebooks/{id}/graph`

**检索 / 问答**：`GET /notebooks/{id}/search`、`POST /notebooks/{id}/ask`

**会话**：`GET /notebooks/{id}/conversations`、`GET/PATCH/DELETE /conversations/{id}`

**反馈**：`POST /answers/{answer_id}/feedback`

**统一 KG**：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`、`POST .../unified-kg/merges/{candidate_id}/confirm|reject`

**仍在但作用于当前对象**：duplicates、merge、articles(CRUD + research)、derived-rules(GET/approve/reject)

### 3.2 SQLite 表（19）

`users` · `user_profiles` · `notebooks` · `sources` · `source_elements` · `articles` · `extraction_runs` · `element_embeddings` · `knowledge_embeddings` · `knowledge_objects` · `knowledge_relations` · `answers` · `conversations` · `feedback` · `article_claims` · `derived_rule_candidates` · `object_schemas` · `concept_clusters` · `concept_merge_candidates`

### 3.3 知识对象类型与状态

- **对象类型**（4 种，统一存 `knowledge_objects`）：`concept / claim / formula / procedure`
- **知识状态**：`approved / reviewed / deprecated / conflict / project_specific`；仅 `USABLE_STATUSES`（approved/reviewed/project_specific/conflict）进入检索/回答，`deprecated` 排除。

### 3.4 `source.parse_status` 状态机

`queued → parsing → parsed → extracting → extracted`（失败 `failed`）。前端对非终态 source 每 ~1.5 s 轮询 `GET /sources/{id}`。

### 3.5 前端功能（`frontend/app/page.tsx`）

- **集合页**：tab 过滤 / grid·compact·list 视图 / 排序 / debounce 搜索 / 新建（直接创建未命名笔记本并进入，无弹窗）·编辑·删除。
- **工作区三栏**：左 Source Stack（上传 + 实时状态 + detail/删除）、中 tab（**问答 ask** ｜ **知识库 rules**，通用类型无关）、右 Studio（文章·派生规则·知识图谱）。
- **状态点**：绿色仅给 `extracted`，其余处理中为橙色。
- **知识图谱**：Concept / Claim / Formula / Procedure 同屏展示，节点/边/类型形状直接画在主视图。
- 回答区：逐句 `[k_i]` 引用 + anchors 展开 + 👍/👎 反馈。

### 3.6 配置开关（`.env` / `core/config.py`）

- **模型服务**：`openai_compat_base_url/api_key/model/timeout=60/max_retries=2`（所有模型经 URL 端点接入，不启动本地服务）。
- **嵌入**：`embed_provider`（""/dashscope）/`embed_model`/`embed_base_url`/`embed_api_key`/`embed_dim=1024`/`embed_truncate_chars=2000`/`embed_batch_size=10`/`embed_persist_chunk=200`/`embed_concurrency=50`。
- **KG 抽取**：`kg_extract_workers=16`（全局窗口并发上限）/`kg_job_concurrency=8`（文档级并发）/`kg_ask_reserve=64`（Ask 连接预留）/`kg_window_target_chars=0`（0=自适应）/`kg_window_min_chars=4000`/`kg_window_max_chars=8000`/`kg_window_overlap_chars=450`/`kg_window_warn_threshold=1200`。
- **DB**：`db_busy_timeout_ms=30000`。
- **检索**：`retrieval_top_n=12`。
- **MinerU**：`mineru_mode(off|http|cli)` · `mineru_api_url` · `mineru_backend` · `mineru_vlm_server_url` · `mineru_parse_method` · `mineru_lang` · `mineru_model_source` · `mineru_timeout_seconds` · `mineru_formula_enable` · `mineru_table_enable`。
- **存储/CORS**：`database_url` · `storage_dir` · `cors_origins`。

### 3.7 验证

`scripts/check.sh`：`py_compile` + 离线 hermetic `smoke_backend.py`（钉死 `mineru_mode=off`、清空 LLM/embedding，不读真实密钥）+ 前端 `tsc --noEmit`。

---

## 4. 关键设计取舍

- **解析单一结构化实现**：`structural_markdown.py` 是唯一 Markdown 解析器；代码块/表格保真输出，代码块不入 KG 实体。
- **KG 抽取并发 + 高效窗口化**：成本随文档线性（非爆炸）；单窗失败隔离不中断整体；窗口数超阈值告警不截断。
- **检索 float32 矩阵 + 缓存**：低内存（旧版 Python list 1.3 G → 现在 ~百 MB）；SQLite + numpy 不引向量库（超大规模再上 sqlite-vec）。
- **抽取优先、向量化后台并发**：绿色 = KG 就绪，用户不用等向量化完成。
- **Evidence-first 逐句引用**：抽取与回答都绑定 element 级证据，[k_i] 可追溯，杜绝"无出处结论"。
- **离线可跑**：无 LLM/embedder 时降级为关键词 + 确定性答案；测试不依赖外部服务与密钥。
- **GPU 解耦**：后端绝不 import torch/MinerU；重活在子进程/远端，本机/CI 始终轻量离线。
