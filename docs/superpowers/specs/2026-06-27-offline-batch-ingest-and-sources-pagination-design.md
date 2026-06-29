# 离线批量摄取 + 来源分页 — 设计文档

> 日期: 2026-06-27
> 状态: 待评审
> 驱动场景: 给定一个目录(上万个 `.md`,偶发 `.pdf`),离线复用现有管线抽 KG + embedding 灌进项目 SQLite db;上万来源会撑爆来源面板,需配套分页。

---

## 1. 背景与目标

用户有上万个离线 Markdown 文件(少量 PDF),希望:

1. 给定一个目录,递归读取 `.md/.markdown`(及偶发 `.pdf`),**复用项目现有摄取/抽取/嵌入管线**,构建到项目的 `.local` SQLite db,产出 source → source_elements → chunks(+向量)→ KG(knowledge_objects/relations + 向量)→ concept_clusters。
2. 因为单个 notebook 会有上万个 source,现有"来源"列表(后端一次性返回全部、前端无虚拟滚动)会卡死/OOM,需要**分页**。

这是**两个独立交付物**:**Part A 离线批量摄取脚本**(后端纯脚本)与 **Part B 来源分页**(全栈特性)。两者独立、各自一个 PR,先 A 后 B。

**关键约束(来自用户决策):**
- **分阶段管线**:Phase 1 先 chunk+embedding(无 LLM、快、便宜,chunk-native 问答立即可用);Phase 2 KG 抽取作为单独可恢复的第二趟,按需触发、可先小范围验证。
- **输入几乎全是 MD**:MD 走原生结构化解析;偶发 PDF 走免费 `pypdf` 兜底(不引入 MinerU 云端/本地)。

---

## 2. 范围

**In scope**
- 新脚本 `scripts/batch_ingest.py`(两阶段子命令)。
- 复用现有 `upload_sources` / `process_source` / `build_notebook_kg` / `rebuild_unified_kg` / 嵌入 backfill,不重写解析/分块/抽取。
- 批量场景所需的少量增强:调用前 `file_hash` 去重、进度清单(manifest)、有界并发、低并发嵌入 backfill。
- **README(中英)记录 `batch_ingest` CLI 用法**(产品口径、不含机器特定路径,详见 §4.9)。
- Part B:`GET /sources` 分页(`offset/limit` + `total_count`)、`search_notebook` 全量加载 OOM 修复、前端"加载更多 + 搜索框"。

**Out of scope(YAGNI)**
- MinerU 云端/本地 PDF 解析(输入几乎全 MD;PDF 走 pypdf 兜底足够)。
- 跨源 KG 抽取的"问题感知重抽"(见既有 C5)。
- 前端虚拟滚动库引入(先用"加载更多" + 搜索;若仍慢再议)。
- 把外部 Neo4j KG 导入(见 memory `neo4j-kg-import-rejected`,已否决)。
- 多机/分布式调度;本设计为单机离线脚本。

---

## 3. 现有管线事实(已核实,作为复用面)

| 能力 | 入口 | 文件:行 | 关键行为 |
|---|---|---|---|
| 注册+处理文件 | `upload_sources(nb, files, scheduler=None)` | sqlite_repository.py:1420 | `scheduler=None` → **同步**逐文件 `process_source`;拷贝字节到 `.local/storage/notebooks/{nb}/{src}_{name}`;算 `file_hash`(SHA256);**自身不查重** |
| 全管线 | `process_source(source_id)` | sqlite_repository.py:1553 | parse → source_elements → `_build_chunks_for_source` → 后台线程 embed(`_embed_source`+`_embed_chunks_for_source`)→ 若 `_should_extract_kg` 则 `_run_extraction`;末尾 join 嵌入线程 |
| 嵌入门控 | `embedder_configured` | config.py:305 | `= embed_provider == "dashscope"`;为空则所有嵌入步骤 no-op(快) |
| KG 门控 | `_should_extract_kg(nb)` | sqlite_repository.py:1497 | `= kg_auto_extract(默认False) OR _notebook_has_kg(nb)` |
| MD 解析 | `parse_markdown` → `structural_markdown.parse_blocks` | parsers.py:81 | heading/paragraph/code/table/image 块 → SourceElement,**原生支持,无需转换** |
| PDF 解析 | `parse_pdf`(MinerU 云/本地/`pypdf` 兜底) | parsers.py:319 | 无 MinerU 配置时自动 `parse_pdf_pypdf` |
| 分块 | `build_chunks(elements, target_chars=600)` | chunking.py:11 | heading 切边界 + prose 贪心合并;`_build_chunks_for_source` 幂等(先删旧) |
| chunk 向量 | `_embed_chunks_for_source(sid)` | sqlite_repository.py:2155 | 门控 `embedder_configured`;批并发 `embed_concurrency` |
| KG 抽取(单源) | `_run_extraction(sid)` | sqlite_repository.py:1876 | 幂等(先清旧 KG);`extract_graph`→`build_records`→`store_kg`→(若 fusion 开)`incremental_fuse_source` |
| KG 抽取(整库,缺啥补啥) | `build_notebook_kg(nb)` | sqlite_repository.py:1513 | **只抽尚无 KG 的 source、幂等跳过、单源失败隔离、返回 `{done,failed}`**;需 LLM 已配 |
| 全量融合 | `rebuild_unified_kg(nb)` | sqlite_repository.py:3664 | 聚类全 notebook concept → concept_clusters;返回簇数 |
| KG 向量 backfill | `_backfill_knowledge_embeddings(db,nb,objs)` | sqlite_repository.py:2263 | 幂等只补缺失;`scripts/backfill_kg_embeddings.py` 已封装低并发+抗429+多轮 |
| 来源列表 | `list_sources(nb)` | sqlite_repository.py:1322 | `SELECT * ... ORDER BY created_at ASC` **fetchall 全量**,无分页 |

**相关配置(config.py):** `embed_provider`(63)、`kg_extract_workers`=16(80,窗口级)、`kg_incremental_fusion_enabled`=True(91)、`kg_job_concurrency`=8(95)、`kg_auto_extract`=False(184)、`embed_concurrency`/`embed_batch_size`。

---

## 4. Part A — 离线批量摄取 `scripts/batch_ingest.py`

### 4.1 架构总览

脚本是现有函数的**编排层**,不重写任何解析/分块/抽取。两个阶段,各自可独立、可重复运行:

```
Phase 1 (ingest, 无 LLM):  目录 → [去重] → upload_sources(scheduler=None, KG/EMBED 关)
                            → 收尾低并发 backfill chunk 向量
                            → 产出 sources+elements+chunks(+chunk向量),chunk-native 问答可用
Phase 2 (kg, LLM 重):       build_notebook_kg(nb)(per-source 融合关)
                            → 一次 rebuild_unified_kg
                            → backfill 节点向量(关系向量跳过)
```

### 4.2 CLI

```
PYTHONPATH=backend python scripts/batch_ingest.py <phase> --input-dir DIR [opts]

phase            ingest | kg | all            # all = ingest 然后 kg
--input-dir DIR  递归扫描的根目录(必填,ingest 阶段)
--notebook-id    现有 notebook id;省略则按 --notebook-name 新建
--notebook-name  新建 notebook 名(默认取目录名)
--owner          notebook.created_by(默认配置的 admin / user-local)
--group-by-subdir  把 input-dir 的每个一级子目录映射成一个 notebook(默认全进 1 个)
--workers N      文件级并发(默认 = kg_job_concurrency)
--limit N        本次只处理 N 个(kg 阶段用于先验证子集)
--glob           额外扩展名(默认 .md,.markdown,.pdf)
--dry-run        只扫描+报告(文件数/去重命中/预估),不写库
--resume         读 manifest 跳过已完成(默认开)
```

退出后打印汇总:扫描数 / 去重跳过 / 成功 / 失败 / 各阶段计数 / manifest 路径。

### 4.3 Phase 1 — ingest(无 LLM)

**环境/Settings 覆盖(仅本进程):**
- `EMBED_PROVIDER=""` → 摄取期零嵌入(parse+chunk 飞快,无 429 风险)。
- `kg_auto_extract=False`(默认)+ 目标 notebook 无 KG → `process_source` 经 `_should_extract_kg` 自动跳过 KG。

**步骤:**
1. **解析/获取 notebook**:`--notebook-id` 或新建(`create_notebook`,`created_by=--owner`)。`--group-by-subdir` 时每子目录一个 notebook。
2. **扫描目录**:递归 `--glob`,排序固定遍历顺序(可恢复)。
3. **去重**:对每个文件算 SHA256,查 `SELECT id FROM sources WHERE notebook_id=? AND file_hash=?`,命中则跳过(并记 manifest)。
4. **有界并发摄取**:`ThreadPoolExecutor(workers)`,每文件 `upload_sources(nb, [UploadedSourceFile(...)], scheduler=None)`(同步 parse+chunk;KG/EMBED 关)。逐文件 try/except 隔离失败,结果写 manifest。
   - 注:`upload_sources` 内 `process_source` 末尾会 join 后台嵌入线程,但 EMBED 关时为 no-op,几乎零开销。
5. **收尾 backfill chunk 向量**:`EMBED_PROVIDER=dashscope`、`embed_concurrency` 调低(默认 4),遍历本批 source 调 `_embed_chunks_for_source(sid)`,多轮+轮间退避抗 429(复用 `backfill_kg_embeddings.py` 的模式,新增等价的 chunk 版,或在脚本内内联)。
   - **待实现期确认**:retrieval 是否依赖 `element_embeddings`(`_embed_source`)。chunk-native 主路径用 `chunk_embeddings`;若 element 向量非必需则默认跳过(省大量嵌入),需要则同法 backfill。

**Phase 1 完成态**:chunk-native(默认)问答即可检索到这批数据。

### 4.4 Phase 2 — kg(LLM 重,单独触发,可恢复)

**环境/Settings 覆盖:**
- `kg_incremental_fusion_enabled=False` → `_run_extraction` 跳过 per-source 融合(避免随图增大的 O(已有×新增) 累积),改为收尾一次全量融合。
  - **待实现期确认**:`_run_extraction` 确实读该标志后才跳过 `incremental_fuse_source`;若未读,补一行守卫(1 行)。
- `EMBED_PROVIDER=""` 抽取期仍置空(`store_kg` 末尾的对象/关系嵌入 no-op),向量留到收尾 backfill。

**步骤:**
1. **抽取**:默认 `build_notebook_kg(nb)`(只抽尚无 KG 的 source、幂等、失败隔离)。`--limit N` 时改为:选前 N 个"尚无 KG"的 source,逐个 `_run_extraction` + 状态置位(镜像 `build_notebook_kg` 的隔离逻辑),用于先验证质量再整批。
   - 跨源并发:**默认不在源级再并发**——单源 `_run_extraction` 已用 `kg_extract_workers=16` 窗口并发;源级再并行会成倍放大 LLM QPS 触发 429。源级串行 + 窗口并行是更稳的默认。
2. **一次全量融合**:`rebuild_unified_kg(nb)`。
3. **backfill 节点向量**:`EMBED_PROVIDER=dashscope`、低并发,复用 `backfill_kg_embeddings.py` 逻辑;**关系向量默认跳过**(关系检索默认关,51万嵌入不值)。

**Phase 2 完成态**:reasoning/graph 推理模式可用,跨文档概念簇建立。

### 4.5 去重 / 幂等 / 可恢复

- **去重**:`file_hash` 命中即跳过(同一文件重复出现/重跑)。
- **幂等**:`_run_extraction` 先清旧 KG;`_build_chunks_for_source` 先删旧 chunk;`build_notebook_kg` 跳过已抽的 source。重跑安全。
- **断点续跑(manifest)**:`.local/batch_ingest/<run-id>.jsonl`,每文件一行 `{path, file_hash, source_id, phase, status, error, ts}`。`--resume`(默认)读 manifest + db 状态跳过已完成。中断后重跑自动续。
  - 时间戳:脚本在写 manifest 时用 `time.time()`(脚本环境无 workflow 的时钟限制)。

### 4.6 并发与限流

| 维度 | 控制 | 默认 |
|---|---|---|
| 文件级(Phase 1 parse+chunk) | `--workers` / `kg_job_concurrency` | 8 |
| 窗口级(Phase 2 单源抽取) | `kg_extract_workers` | 16 |
| 源级(Phase 2) | 串行(见 4.4) | 1 |
| 嵌入 backfill | `embed_concurrency` 调低 + 多轮退避 | 4 |

### 4.7 失败处理与可观测

- 逐文件/逐源 try/except 隔离;一坏文件不中断整批。
- 复用现有 `events` 事件日志(`process_source` 各阶段已 emit `pipeline` 事件)。
- 脚本自身 stdout 进度(`[i/N] ...`)+ manifest 落盘 + 末尾汇总。
- 扫描到空解析(如扫描版 PDF 无文本层)按现有 `empty_hint` 标注,计入失败汇总。

### 4.8 owner / notebook / 磁盘

- notebook `created_by=--owner`(默认 admin / user-local;遵守 owner 隔离,见 memory `user-system-state`)。
- `upload_sources` 会把字节拷进 `.local/storage/...`;MD 文件小,上万个磁盘压力可忽略。
- 全部进 1 个 notebook(匹配"来源分页"场景);`--group-by-subdir` 可拆多 notebook。

### 4.9 文档(随 PR-1 交付)

在 `README.md` 与 `README_zh.md` 增加"离线批量摄取"小节,内容:

- 完整 CLI(见 §4.2):`ingest`/`kg`/`all` 子命令、各 `--opt` 说明、两阶段语义(先 chunk+embedding、再 KG)。
- 典型用法示例:先 `ingest` 验证 → `kg --limit N` 小范围验证质量 → 整批 `kg`;以及 `--resume` 续跑、`--dry-run` 预估。
- 前置条件:`.env` 配置(embed/KG_LLM)、`PYTHONPATH=backend`、产出落 `.local`。
- **按 memory `committed-docs-stay-generic` 保持通用**:用 `python scripts/batch_ingest.py ...` 的产品口径写,**不写**本机解释器绝对路径/端口/"这台 Mac"等机器特定细节。

验收:README(中英)均含该小节且示例命令可照抄运行。

### 5.1 后端 API

- 路由 `GET /notebooks/{id}/sources`([routes.py:265](backend/app/api/routes.py)) 增加 `offset: int = Query(0, ge=0)`、`limit: int = Query(50, ge=1, le=200)`。
- 返回从 `List[SourceSummary]` 改为包装对象 `PaginatedSources { items: List[SourceSummary], total_count: int, offset: int, limit: int }`(新 schema)。对齐项目既有 `edge_review_queue` 的 query-param 风格,并补 `total_count` 以便前端判断是否还有下一页。
- `list_sources(nb, offset, limit)`([sqlite_repository.py:1322](backend/app/services/sqlite_repository.py)):SQL 加 `LIMIT ? OFFSET ?`,保持 `ORDER BY created_at ASC`;另跑一条 `COUNT(*)` 取 total。更新 `repository.py` Protocol 签名。
- **兼容**:这是破坏性返回结构变更;同 PR 改前端唯一调用点,无其它消费者(已核实引用渲染不依赖全量 sources)。

### 5.2 `search_notebook` OOM 修复(正确性,非仅体验)

- 现状([sqlite_repository.py:4720](backend/app/services/sqlite_repository.py))把**全部 sources + 全部 source_elements(JOIN)+ articles + knowledge_objects** 一次性 fetchall 进内存再在 Python 侧过滤 → 上万 source 必 OOM。
- 改为 **SQL 侧 `LIKE` 过滤 + `LIMIT`**(各实体分别限量),不再全量进内存。保持现有搜索语义/返回结构,只改取数方式。

### 5.3 前端(`frontend/app/page.tsx`)

- 数据加载([page.tsx:1385](frontend/app/page.tsx)):`openNotebook` 改为拉首屏(`offset=0&limit=50`),存 `sources` + `sourcesTotalCount` + `sourceOffset`。
- 渲染([page.tsx:2615](frontend/app/page.tsx)):列表尾部加"加载更多({已加载}/{total})"按钮,点击追加下一页;空态不变。
- **搜索框**:来源面板加输入框,走后端搜索或按 title 过滤(上万条必须能快速定位)。
- 状态:沿用原生 `useState`(项目无 react-query/SWR);新增 `sourceOffset/sourcesTotalCount/sourceQuery` 几个 state。

### 5.4 引用不受影响

后端 `citation.label` 已含 `source_title`([sqlite_repository.py:6663](backend/app/services/sqlite_repository.py)),前端引用渲染用 `citation.label`,**不依赖前端持有全量 sources**;分页后引用标题照常显示。

---

## 6. 测试策略

**Part A**
- 单测:去重(同 hash 跳过)、manifest 续跑(中断后跳过已完成)、phase 1 不触发 KG(`_should_extract_kg` False 路径)、`--group-by-subdir` 映射。
- 小集成:临时目录放 3~5 个 `.md` + 1 个 `.pdf`,跑 `ingest` 断言 sources/elements/chunks 行数;`EMBED_PROVIDER=dashscope` 下断言 chunk 向量补齐(或 mock embedder)。
- `kg` 阶段:mock kg_llm 或用既有 KG 测试夹具,断言 `build_notebook_kg` 幂等跳过 + 收尾 `rebuild_unified_kg` 被调一次。

**Part B**
- 后端:`list_sources` 分页边界(offset/limit/total)、`search_notebook` 大数据不 OOM(造 N 条断言 SQL LIMIT 生效)。
- 前端:tsc 干净 + "加载更多"追加 + 搜索过滤(视觉验证,真机)。

`scripts/check.sh` 全绿;遵循既有 736+ 测试基线。

---

## 7. 实现顺序与 PR 拆分

1. **PR-1 Part A**:`scripts/batch_ingest.py` + 必要的小 helper(chunk 向量 backfill、可能的 fusion 守卫 1 行)+ 单测 + **README(中英)CLI 用法小节(§4.9)**。先 A 把数据灌进去。
2. **PR-2 Part B**:分页 API + `search_notebook` 修复 + 前端。撑住 UI。

均按 memory `dev-flow-finish-with-pr` / `pr-merge-is-rebase`:分支 rebase 到 master 保持线性 → push → `gh pr create --base master`。本工作已在专属 worktree。

---

## 8. 风险与开放问题

1. **LLM 成本/时长(Phase 2)**:上万文档 × 多窗口 = 数万次 LLM 调用、数小时~数天。缓解:分阶段 + `--limit` 先验证子集 + manifest 可中断续跑 + 源级串行避 429。
2. **`_run_extraction` 是否读 `kg_incremental_fusion_enabled`**:若未读,需补 1 行守卫确保批量时跳过 per-source 融合(4.4)。实现期首先确认。
3. **`element_embeddings` 是否必需**:决定 Phase 1 是否要额外 backfill 元素向量(4.3)。实现期确认 retrieval 用法。
4. **分页破坏性返回结构**:`GET /sources` 从数组改包装对象;须同 PR 改前端唯一调用点(已核实无其它消费者)。
5. **`build_notebook_kg` 无 `--limit`**:子集验证走"选 N 个 + 逐个 `_run_extraction`"(4.4),不改既有函数签名。

---

## 9. 决策记录(本设计已定)

- 分阶段(chunk 先、KG 后);几乎全 MD,PDF 走 pypdf 兜底。
- 复用现有函数编排,不重写解析/分块/抽取。
- 摄取期 EMBED 置空 + 收尾低并发 backfill;批量关 per-source 融合,收尾一次 `rebuild_unified_kg`;关系向量跳过。
- 默认全进 1 个 notebook;owner=admin。
- 分页用 `offset/limit + total_count` 包装对象;顺带修 `search_notebook` OOM。
- A、B 各一个 PR,先 A 后 B。
