# 论文元数据抽取与按作者搜索 — 设计

日期：2026-07-15
分支：`claude/paper-metadata-extraction-38ea3b`
状态：定稿（待实现）

## 1. 背景与目标

论文（PDF）摄取时，管线只保留了文件名（`sources.title` = 文件名），论文自身的元数据——作者、机构、真实标题、发表信息——完全没有抽取和存储。需求：

1. 摄取论文时抽取元数据（作者、机构、标题、venue、年份、DOI、关键词）并持久化。
2. 支持按作者搜索文章（近期需求），为将来更丰富的作者维度查询留好数据基础。
3. 前端可见：来源详情展示论文元数据；来源搜索框可按作者/论文标题命中。

## 2. 非目标

- **不改 `sources.title` 语义**：仍为文件名/展示名；论文真标题另存于 `source_paper_meta.paper_title`，并纳入搜索。
- **不做作者消歧/去重**（同名合并、ORCID、作者专页/facet UI）：按源存平面行，将来需要时在此数据上再建。
- **不把作者/机构建成 KG 节点**：按作者搜索是结构化查询，走关系表；进图会污染概念聚类、牵连 `CLUSTER_ALGO_VERSION` 语义。
- **不抽 abstract**：LLM 逐字复制浪费 token，已有 `summary` 承担概览职能。
- **不抽邮箱等个人联系方式**。
- **不自动回填存量源**：回填是显式 CLI 动作（成本由用户掌控）。

## 3. 数据模型（`_migration_17`，SCHEMA_VERSION 16→17）

```sql
CREATE TABLE IF NOT EXISTS source_paper_meta (
    source_id   TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    is_paper    INTEGER NOT NULL DEFAULT 0,   -- 0=已尝试但判定非论文（防重复调用）
    paper_title TEXT,
    venue       TEXT,
    pub_year    INTEGER,
    doi         TEXT,
    keywords    TEXT NOT NULL DEFAULT '[]',    -- JSON array 字符串
    raw_json    TEXT NOT NULL DEFAULT '{}',    -- 审计信封 {"llm": 原始返回, "dropped": 接地校验丢弃明细}
    model       TEXT NOT NULL DEFAULT '',      -- 抽取用模型（溯源）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_paper_meta_nb ON source_paper_meta(notebook_id);

CREATE TABLE IF NOT EXISTS source_authors (
    id          TEXT PRIMARY KEY,              -- _new_id("sauth")，满 128bit
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,              -- 署名顺序，0 起
    name        TEXT NOT NULL,
    affiliation TEXT NOT NULL DEFAULT '',      -- 多机构以 "; " 连接
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_authors_source ON source_authors(source_id);
CREATE INDEX IF NOT EXISTS idx_source_authors_nb ON source_authors(notebook_id);
```

约定遵循：新表只加 `_migration_17`（不回改 `_migration_1` 基线）；已部署库（user_version=16）走版本闸补建；`schema_contract.txt` golden 追加两表列与索引。

行存在即「已尝试」——`is_paper=0` 的标记行阻止对同一非论文源反复调 LLM。

## 4. 仓储层（owner = SourceStore，无新 runtime 组件）

新方法放 `backend/app/repositories/sqlite/source_store.py`（sources 域）：

- `upsert_paper_meta(source_id, notebook_id, meta: dict) -> None` — 单写事务：upsert 元数据行 + delete/insert 作者行。**store 内部**：调用方（`ensure_paper_metadata`）经注入的 `self.sources` 直达，不过 facade，不登记 `ownership_manifest.SURFACE_MEMBERS`。
- `get_paper_meta(source_id) -> Optional[dict]` — 元数据 + 按 `position` 排序的作者。facade 一跳委托 + `ownership_manifest.SURFACE_MEMBERS` 登记。
- `paper_meta_for_sources(source_ids) -> Dict[str, dict]` — 列表页批量水合；**IN 必须分批**（SQLite 变量上限教训）。**store 内部**：`get_paper_meta`/`sources_from_rows` 的私有水合帮手，同 `upsert_paper_meta` 不过 facade、不登记。
- `sources_missing_paper_meta(notebook_id) -> List[str]` — 回填用：doc_type=academic_paper、有解析文本、无 meta 行、非 memory 派生。facade 一跳委托 + `ownership_manifest.SURFACE_MEMBERS` 登记。

即：四个 store 方法里只有 `get_paper_meta`、`sources_missing_paper_meta` 经 facade 暴露；`upsert_paper_meta`、`paper_meta_for_sources` 留在 store 内部。

守卫：`test_repository_surface_manifest` 静态扫描按 file:line 精确比对（仅覆盖经 facade 暴露的两个成员）——新增消费点按测试输出对齐 consumers 清单；`RUNTIME_COMPONENT_OWNERS` 无需改（SourceStore 已注册）。

## 5. 抽取流程

### 5.1 Prompt 与调用

- `app/services/paper_meta.py` 新增 `paper_meta_prompt(head_text)` + `PAPER_META_SCHEMA_HINT`（与 5.3 节的零 LLM 接地校验 `verify_paper_meta` 同文件，而非 `prompts.py`——纯函数、无 DB/网络依赖，`source_ingestion.py` 直接 import）：

```json
{"is_paper": true, "title": "...", "authors": [{"name": "...", "affiliations": ["..."]}],
 "venue": "...", "year": 2024, "doi": "...", "keywords": ["..."]}
```

  要求：**只依据给定文本抽取，即使模型「认识」这篇论文也不得用记忆补全**；非学术论文（网页、手册等）返回 `is_paper=false` 其余留空；作者按署名顺序；机构按上标/排版关联，不确定给空数组；DOI/arXiv 号出现才填。
- 调用：`kg_llm()`（缺省回退主 LLM 的现有语义）`chat_json` + `cap_kwargs(client, "openai_compat_max_tokens")`，`temperature=0.0`。输入 = 文档头部文本（在手 elements 取前若干元素 join，否则 `read_source_text` 截前 `paper_meta_head_chars` 字符；`read_source_text` 对历史源回退用 DB 内 elements 重建，原始文件缺失亦可用）。

### 5.2 挂载点（`ensure_paper_metadata(source, *, elements=None, force=False)`）

统一 helper，幂等（有行且非 force 即跳过）：

1. **`process_source`**：解析成功、`summarize` 旁（终态转换之前），`force=True` —— 初次上传无行等价新抽；用户显式 re-parse 则刷新。
2. **`run_extraction` 开头 catch-up**，`force=False` —— 覆盖 batch `kg` phase 与后补 KG 的既有源。

Gate（全部满足才调 LLM）：`settings.paper_meta_enabled`、`doc_type == academic_paper`、非 memory 派生源、LLM 已配置、有解析文本。

失败语义：best-effort——`logger.exception` + `event_log.emit`（pipeline 侧惯例），**不写行**（下次可重试）、不碰 `extraction_runs`/`extraction_warning`、不阻断流水线。摄取侧不用 `note_model_error`（那是 Ask 链路的 sink）。

### 5.3 真实性：接地校验层（零 LLM，写库前强制）

风险：LLM 见过大量论文，可能对「认识的」论文用参数记忆补全作者/机构/venue——即使头部文本残缺。防线两道：

1. **Prompt 侧**（弱防线）：明示只准依据给定文本、不得用记忆补全；`temperature=0.0`。
2. **接地校验**（强防线，确定性代码）：`_grounded(value, head_text)` = 双方归一化（lowercase、去变音符、去空白/标点）后子串包含判定。逐字段策略：

| 字段 | 校验 | 未通过处置 |
| --- | --- | --- |
| 作者 name | 归一化包含于头部文本（容忍「姓, 名」次序翻转与有界 token 旋转，非全排列） | **丢弃该作者行**（假作者比漏作者更伤搜索可信度） |
| 作者 affiliation | 归一化包含（容忍缩写不匹配；非列表形状按单元素/空容错，不崩溃） | 置空字符串（保留作者，不保留不可验证的机构） |
| paper_title | 归一化包含 | 置 NULL |
| venue | 归一化包含（预印本常无 venue，宁缺毋滥） | 置 NULL |
| pub_year | 4 位数字独立出现于文本（DOI/arXiv 号内的数字段先被抹除不算数）且 1900≤y≤2100；JSON 浮点年份容错取整 | 置 NULL |
| doi | 匹配 `^10\.\d{4,9}/\S+$` 且与文本中提取出的完整 DOI token **精确相等**（大小写不敏感；容忍首尾 ASCII/Unicode 标点包裹）——子串包含不算，堵前缀截断漏洞 | 置 NULL |
| keyword | 归一化包含 | 丢弃该关键词 |

畸形 LLM 输出（authors/keywords 非列表、条目非 dict/str、顶层非对象 JSON 等）一律**优雅降级不崩溃**：字段级降级为空（原始输出仍在 raw_json.llm 里可审计）；顶层非对象 JSON 视作抽取失败（不落行、可重试），不落 `is_paper=0` 标记行以免错误压制重试。

- 审计：`raw_json` 存信封 `{"llm": <原始返回>, "dropped": <丢弃明细>}`；有丢弃时 `event_log.emit(kind="paper_meta", ...)` 记一条——「校验挡掉了什么」可追溯。
- **刻意不加第二次 LLM refine pass**：成本×2 违反效率约束；接地校验已确定性堵死「记忆补全」主通道。

### 5.4 成本账（效率一等约束）

每个新 paper 源 +1 次小调用（输入 ~4000 字符 ≈1-2k tokens，输出 ~300 tokens）。相对每源已有 1 次 summary + 每窗数千 tokens 的多窗口 KG 抽取，增量 <2%。曾考虑并入 `summarize` 调用省一次——但 catch-up 路径仍需独立调用（双实现更复杂）、且两者 gate 不同（summary 全类型 / meta 仅论文），收益不抵复杂度，采用独立调用 + 严格 gate + 幂等。

## 6. API 变更

- `schemas.py`：新增 `PaperAuthor{name, affiliation}`、`PaperMeta{is_paper, title, venue, year, doi, keywords, authors}`；`SourceSummary` 增 `authors: List[str] = []`（姓名，按署名序）、`pub_year: Optional[int]`、`venue: Optional[str]`；`SourceDetail` 增 `paper_meta: Optional[PaperMeta]`。
- 水合：`sources_from_rows` 批量路径加一次分批 IN 查询；详情路径 `get_paper_meta`。
- 新端点一个：`POST /notebooks/{notebook_id}/paper-meta/backfill` → `{"queued": int}`——见第 9 节应用内补抽；owner/admin 门控沿用现有 notebook 写权限守卫。
- api_contract.json golden 按既定流程 regen（先例 b7985e8 / knowhow PR）。

## 7. 搜索（按作者/论文标题）

`list_sources_page` 的 `q` LIKE 过滤从「title/file_name」扩展为：

```sql
... OR EXISTS(SELECT 1 FROM source_authors a
              WHERE a.source_id = sources.id AND LOWER(a.name) LIKE ?)
    OR EXISTS(SELECT 1 FROM source_paper_meta m
              WHERE m.source_id = sources.id AND LOWER(m.paper_title) LIKE ?)
```

notebook 内源数量级（数千～万级）下相关子查询开销毫秒级；不上 FTS（trigram 需 ≥3 字符，作者名短，YAGNI）。前端 `q` 管道已端到端存在，无新 UI 面。

## 8. 前端（同 PR 交付）

`frontend/app/page.tsx`：

1. TS 类型补新字段。
2. **来源详情 modal**（`source-detail-meta` 区）新增「论文信息」块，仅 `paper_meta?.is_paper` 时显示：论文标题、作者列（机构以次行小字或 hover title）、venue · 年份、DOI（链接 `https://doi.org/...`）、关键词 chips。对齐精致、省略号截断（UI 质量基线）。
3. **搜索框 placeholder**：「搜索来源（标题/作者/文件名）」。
4. **补抽入口**（owner 可见）：来源面板 add-source 按钮同区加次级按钮「补全论文信息」（in-flight 防重：请求期间禁用并显示「补全中…」），调 backfill 端点，toast 显示「已提交 N 篇论文的信息补全」/「论文信息已是最新，无需补全」；文案友好不暴露技术细节。
5. 列表行不动（侧栏行高保持紧凑）。

注意：API 路径不带 `/api` 前缀（双 /api 404 坑）；新增中文文案沿用现有弯引号风格，不做批量引号替换。

轮询兼容：poll merge 仅在 `parse_status` 变化时刷新对象——两个挂载点都在终态转换**之前**落库，元数据随终态转换自然到达前端；禁止终态之后异步补写 meta。

## 9. 历史文章补抽（三通道，均幂等可续跑）

存量库是主要资产，补抽是一等路径。三通道共用同一 `ensure_paper_metadata`，幂等键 = meta 行存在（含 `is_paper=0` 标记行），中断重跑天然从断点继续。历史源**原始 PDF 缺失也可补抽**：`read_source_text` 回退用 DB 内 `source_elements` 重建头部文本。

1. **CLI（批量主通道，管理员）**：`scripts/batch_ingest.py --phase metadata [--force]`
   - 目标 notebook 内 `sources_missing_paper_meta` 逐源补抽（复用现有有界并发），进度日志 N/M + 收尾统计（成功/非论文/失败）。
   - `--force` 对已有行重抽（prompt/校验升级后刷新用）。
   - LLM 未配置 → **报错退出**（CLI 不静默降级）；失败源不落行，下次重跑自动重试。
2. **应用内（notebook owner，无 shell 也能补）**：来源面板「补抽论文元数据」入口（按入口收拢惯例放现有菜单/工具区）→ `POST /notebooks/{id}/paper-meta/backfill` → `background_jobs.submit` 后台逐源补抽，立即返回 `{"queued": N}`，前端 toast 提示已提交；完成情况经事件日志可查，用户刷新列表可见。
3. **自动 catch-up**：`run_extraction` 开头（5.2 节挂载点②）——历史源将来重抽 KG 时顺带补上。

README.md + README_zh.md 同 PR 写 CLI 用法（通用口径，不含机器路径）。

## 10. 配置（pydantic-settings v2，按仓库惯例显式 validation_alias）

- `paper_meta_enabled: bool = True`（env `PAPER_META_ENABLED`）
- `paper_meta_head_chars: int = 4000`（env `PAPER_META_HEAD_CHARS`）

不新增 token 上限旋钮（复用全局 `openai_compat_max_tokens` cap）。

## 11. 测试计划

- 迁移：全新库建表；**user_version=16 已部署库补建**（版本闸短路教训）；`schema_contract.txt` golden。
- store：upsert/get/批量水合/missing 列表/级联删除/IN 分批。
- 服务：FakeLLM 成功落库；`is_paper=false` 落标记行；幂等 skip；LLM 未配 skip 不抛；memory 派生源被 gate。
- **接地校验**：幻觉作者被丢弃（名字不在文本）；机构不可验证置空；venue/年份/DOI 不在文本置 NULL；DOI 格式非法拒收；归一化匹配容忍大小写/空白/变音符差异；dropped 明细入 raw_json 信封。
- 补抽：backfill 端点（owner 门控、queued 计数、幂等跳过已有行）；CLI phase（missing 选取、--force 重抽、LLM 未配报错退出）。
- 搜索：q 按作者名、论文标题命中。
- 契约波及：SCHEMA_VERSION bump ripple（test_sqlite_migrator_component / test_legacy_db_compat / test_repository_facade_contract 等）、surface manifest、api_contract regen。

## 12. 风险与守卫

| 风险 | 守卫 |
| --- | --- |
| 巨型 IN 超 SQLite 变量上限 | `paper_meta_for_sources` 分批（沿用现有分批模式） |
| 前端 meta 不刷新 | 落库时序在终态转换前（8 节）；不做终态后异步写 |
| surface manifest 行号敏感 | 按测试输出对齐 consumers；只增不删不移现有行为 |
| 已部署库漏建表 | 新表只走 `_migration_17`，测试覆盖 v16→v17 升级路径 |
| 非论文源反复调 LLM | `is_paper=0` 标记行 + 幂等 skip |
| 回填成本失控 | 不自动回填；CLI/应用内均显式触发，幂等可续跑，用户掌控 |
| LLM 记忆补全（张冠李戴作者/机构） | 接地校验层（5.3）：不在头部文本中的字段不落库 + raw_json 审计信封 + 事件日志 |
| 解析噪声致真作者被校验误杀 | 归一化匹配（大小写/空白/标点/变音符不敏感）；丢弃明细可审计，prompt/校验升级后 `--force` 重抽 |
