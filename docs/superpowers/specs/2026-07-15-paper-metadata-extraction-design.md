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
    keywords    TEXT,                          -- JSON array 字符串
    raw_json    TEXT,                          -- LLM 完整返回，向前兼容
    model       TEXT,                          -- 抽取用模型（溯源）
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

新方法放 `backend/app/repositories/sqlite/source_store.py`（sources 域），facade 一跳委托 + `ownership_manifest.SURFACE_MEMBERS` 登记：

- `upsert_paper_meta(source_id, notebook_id, meta: dict) -> None` — 单写事务：upsert 元数据行 + delete/insert 作者行。
- `get_paper_meta(source_id) -> Optional[dict]` — 元数据 + 按 `position` 排序的作者。
- `paper_meta_for_sources(source_ids) -> Dict[str, dict]` — 列表页批量水合；**IN 必须分批**（SQLite 变量上限教训）。
- `sources_missing_paper_meta(notebook_id) -> List[str]` — 回填用：doc_type=academic_paper、有解析文本、无 meta 行、非 memory 派生。

守卫：`test_repository_surface_manifest` 静态扫描按 file:line 精确比对——新增成员按测试输出对齐 consumers 清单；`RUNTIME_COMPONENT_OWNERS` 无需改（SourceStore 已注册）。

## 5. 抽取流程

### 5.1 Prompt 与调用

- `prompts.py` 新增 `paper_meta_prompt(head_text)` + schema hint：

```json
{"is_paper": true, "title": "...", "authors": [{"name": "...", "affiliations": ["..."]}],
 "venue": "...", "year": 2024, "doi": "...", "keywords": ["..."]}
```

  要求：只依据给定文本、不编造；非学术论文（网页、手册等）返回 `is_paper=false` 其余留空；作者按署名顺序；机构按上标/排版关联，不确定给空数组；DOI/arXiv 号出现才填。
- 调用：`kg_llm()`（缺省回退主 LLM 的现有语义）`chat_json` + `cap_kwargs(client, "openai_compat_max_tokens")`，低 temperature。输入 = 文档头部文本（在手 elements 取前若干元素 join，否则 `read_source_text` 截前 `paper_meta_head_chars` 字符）。

### 5.2 挂载点（`ensure_paper_metadata(source, *, elements=None, force=False)`）

统一 helper，幂等（有行且非 force 即跳过）：

1. **`process_source`**：解析成功、`summarize` 旁（终态转换之前），`force=True` —— 初次上传无行等价新抽；用户显式 re-parse 则刷新。
2. **`run_extraction` 开头 catch-up**，`force=False` —— 覆盖 batch `kg` phase 与后补 KG 的既有源。

Gate（全部满足才调 LLM）：`settings.paper_meta_enabled`、`doc_type == academic_paper`、非 memory 派生源、LLM 已配置、有解析文本。

失败语义：best-effort——`logger.exception` + `event_log.emit`（pipeline 侧惯例），**不写行**（下次可重试）、不碰 `extraction_runs`/`extraction_warning`、不阻断流水线。摄取侧不用 `note_model_error`（那是 Ask 链路的 sink）。

### 5.3 成本账（效率一等约束）

每个新 paper 源 +1 次小调用（输入 ~4000 字符 ≈1-2k tokens，输出 ~300 tokens）。相对每源已有 1 次 summary + 每窗数千 tokens 的多窗口 KG 抽取，增量 <2%。曾考虑并入 `summarize` 调用省一次——但 catch-up 路径仍需独立调用（双实现更复杂）、且两者 gate 不同（summary 全类型 / meta 仅论文），收益不抵复杂度，采用独立调用 + 严格 gate + 幂等。

## 6. API 变更

- `schemas.py`：新增 `PaperAuthor{name, affiliation}`、`PaperMeta{is_paper, title, venue, year, doi, keywords, authors}`；`SourceSummary` 增 `authors: List[str] = []`（姓名，按署名序）、`pub_year: Optional[int]`、`venue: Optional[str]`；`SourceDetail` 增 `paper_meta: Optional[PaperMeta]`。
- 水合：`sources_from_rows` 批量路径加一次分批 IN 查询；详情路径 `get_paper_meta`。
- 无新端点。api_contract.json golden 按既定流程 regen（先例 b7985e8 / knowhow PR）。

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
4. 列表行不动（侧栏行高保持紧凑）。

注意：API 路径不带 `/api` 前缀（双 /api 404 坑）；新增中文文案沿用现有弯引号风格，不做批量引号替换。

轮询兼容：poll merge 仅在 `parse_status` 变化时刷新对象——两个挂载点都在终态转换**之前**落库，元数据随终态转换自然到达前端；禁止终态之后异步补写 meta。

## 9. 批量回填 CLI

`scripts/batch_ingest.py`（→ `backend/app/services/batch_ingest.py`）新增 phase `metadata`：

- 目标 notebook 内 `sources_missing_paper_meta` 的源逐个 `ensure_paper_metadata`（复用现有有界并发），进度日志 N/M。
- LLM 未配置 → **报错退出**（CLI 不静默降级）。
- README.md + README_zh.md 同 PR 写用法（通用口径，不含机器路径）。

## 10. 配置（pydantic-settings v2，字段名即环境变量名，无需 alias）

- `paper_meta_enabled: bool = True`
- `paper_meta_head_chars: int = 4000`

不新增 token 上限旋钮（复用全局 `openai_compat_max_tokens` cap）。

## 11. 测试计划

- 迁移：全新库建表；**user_version=16 已部署库补建**（版本闸短路教训）；`schema_contract.txt` golden。
- store：upsert/get/批量水合/missing 列表/级联删除/IN 分批。
- 服务：FakeLLM 成功落库；`is_paper=false` 落标记行；幂等 skip；LLM 未配 skip 不抛；memory 派生源被 gate。
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
| 回填成本失控 | 不自动回填；显式 CLI phase，用户掌控 |
