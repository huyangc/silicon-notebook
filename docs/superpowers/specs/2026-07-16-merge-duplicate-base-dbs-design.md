# 合并两个共享 base 库的离线数据库合并工具（`scripts/merge_dbs.py`）

日期：2026-07-16
分支：`claude/merge-duplicate-dbs-d5e135`

## 背景与结论

用户有两套各自服务的部署，各有一个 `silicon_notebook.db`。两个库**共享同一个 base 库**（`notebooks.tier='base'`，且 notebook id 相同），其余 personal notebook 互不重叠。目标：把两个库合并成一个，**对于共享的 base，只保留内容更全的那份**，两边所有 personal notebook 全部并入。

关键事实（已在本机 v17 库上核实，两个待合并库分别为 `user_version=15` 与 `=16`）：

1. **只有 base 共享**。除 base 外 personal notebook id 不重叠 → 移植不会撞 id，无需 id 重映射（合并逻辑因此大幅简化为"选一个库当容器 + 把另一个库的非 base notebook 整体搬入"）。**这是一个必须由 preflight 强校验、而非假设的前提。**

2. **两库 schema 版本低于当前 `SCHEMA_VERSION=17`**，不能直接 ATTACH 合并。必须先各自迁到 17。`v15→17` 只跑 `_migration_16`/`_migration_17`，`v16→17` 只跑 `_migration_17`——两级都是**纯增量 `CREATE TABLE IF NOT EXISTS`**（knowhow 五表、`notebook_assets`、`source_paper_meta`、`source_authors`），无数据变换、不回填、不调 LLM。迁完这些新表在旧库里是空的，不影响合并。

3. **绝大多数表按 `notebook_id` 归属**，可直接按 notebook 筛行拷贝。少数子表通过父表间接归属。极少数是全局表（`users` 等）。FTS 影子表由虚表自身维护。

4. **磁盘侧**：`storage/notebooks/<id>/` 装原始上传文件（`sources.file_path` 指向），不可再生、必须跟 notebook 一起并；`kg_index`/`kg_viz` 是 ANN/可视化缓存、可再生，合并后在部署侧重建即可。

**最终范围** = 一个离线、非破坏性的 Python 脚本 `scripts/merge_dbs.py` + README 中英文用法。backend 不改代码，复用其 `SqliteMigrator`。

## 目标与非目标

**目标**
- 把两个共享 base 的库合并成一个 v17 库，可直接跑在当前 app 上。
- base 保留用户指定的那份（"更全的那个"）；两边 personal notebook 全部保留。
- 非破坏性：两个源库文件不被修改，产出独立的 `merged.db` + 合并后的 storage 目录。
- 一切前提（schema 版本、base 唯一且同 id、非 base id 不重叠、users 冲突）都**显式校验**，违反即中止并报告，绝不静默继续（遵循 [[cli-no-silent-degradation]]）。

**非目标**
- **不**做 base 内部的 KG 去重/融合。base 整份保留一侧、丢弃另一侧。
- **不**搬 `kg_index`/`kg_viz`（可再生，部署后重建）。
- **不**处理非 base id 撞车的自动 id 重映射（真撞了先中止报告，另议兜底；按前提 1 不应发生）。
- **不**改 backend 代码、**不**起服务、**不**自动部署。

## 数据模型：表的三类归属

按 SCHEMA_VERSION=17 归属分三类：A 类 40 张（直接带 `notebook_id`）+ B 类 8 张（子表）+ C 类 5 张（全局）= 53 张业务表；另有 3 张 FTS 虚表（其中 `chunks_fts`/`kg_objects_fts` 带 `notebook_id`）单独处理，及其 `*_fts_*` 影子表由虚表自维护、绝不直接碰。合并逻辑对三类分别处理。

### A. 按 `notebook_id` 直接归属（按 `notebook_id IN (副库非 base id)` 筛行拷贝）
```
sources, source_authors, source_paper_meta, chunks, chunk_embeddings,
element_embeddings, knowledge_objects, knowledge_embeddings, knowledge_relations,
knowledge_object_sources, object_schemas, concept_clusters, concept_comentions,
concept_merge_candidates, canonical_relations, communities, community_members,
mention_edges, relation_embeddings, unified_kg_state, kg_rebuild_checkpoint,
kg_cluster_scratch, kg_conflict_candidates, merge_review_jobs, promotion_candidates,
derived_rule_candidates, extraction_runs, extraction_candidates, articles,
article_claims, conversations, answers, feedback, ask_jobs, reports,
memory_items, knowhow_tables, notebook_assets, notebook_members,
agent_token_notebooks
```
> 说明：`kg_rebuild_checkpoint`/`unified_kg_state`/`kg_cluster_scratch` 是 KG 构建的中间/水位状态，引用的是可再生的 `kg_index` 产物。合并后这些产物在新部署上不存在 → **导入后应清空这些状态行，强制干净重建**（见"收尾"）。

### B. 子表：通过父表间接归属（按父行 id 集合筛）
| 子表 | 父表 | 连接键 |
|---|---|---|
| `source_elements` | `sources` | `source_id` |
| `knowhow_columns` | `knowhow_tables` | `table_id` |
| `knowhow_rows` | `knowhow_tables` | `table_id` |
| `knowhow_cells` | `knowhow_rows` | `row_id` |
| `memory_provenance` | `memory_items` | `memory_id` |
| `memory_revisions` | `memory_items` | `memory_id` |
| `memory_embeddings` | `memory_items` | `memory_id` |
| `ask_trace_steps` | `ask_jobs` | `job_id` |

### C. 全局表：按主键取并集（主库优先，冲突报告）
| 表 | 主键 | 冲突策略 |
|---|---|---|
| `users` | `id` | 同 id 视为同一人 → 需 `--assume-same-users` 确认；同 id 不同 email/role 打印差异 |
| `user_profiles` | `user_id` | 主库优先 |
| `agent_profiles` | `id` | 主库优先 |
| `agent_access_tokens` | `id` | 主库优先（`agent_token_notebooks` 归 A 类按 notebook 筛） |
| `concept_whitelist` | `term` | 并集，主库优先 |

> `users` 是唯一有语义风险的全局表：副库非 base notebook 的 `created_by` 必须在合并后 `users` 中存在。若两库 user id 重叠但代表不同人，会导致归属错乱 → preflight 报告重叠、要求显式确认。

### FTS 表（不当普通表拷）
- `chunks_fts`、`kg_objects_fts`：**独立内容 FTS5**（列含 `notebook_id UNINDEXED`，无触发器，app 手动写入）。→ 按 `notebook_id IN (副库非 base id)` 用**显式列清单** `INSERT INTO merged.x(col...) SELECT col... FROM sec.x WHERE ...` 拷贝（当 A 类处理，但必须列清单、不能 `SELECT *`）。
- `memory_items_fts`：**外部内容 FTS5**（`content='memory_items'` + 三触发器）。→ **不直接拷**；导入 `memory_items` 后跑 `INSERT INTO merged.memory_items_fts(memory_items_fts) VALUES('rebuild')` 从主表重建。
- 所有 `*_fts_{config,content,data,docsize,idx}` 影子表由虚表自身维护，**绝不直接操作**。

## 架构与流程

脚本单文件、纯离线、可在任何能同时读到两个库文件的机器上跑。分五段：

### 0. 入参与副本
```
python scripts/merge_dbs.py \
  --db-a /path/A.db --storage-a /path/A/storage \
  --db-b /path/B.db --storage-b /path/B/storage \
  --keep-base a|b \
  --out /path/merged.db --out-storage /path/merged_storage \
  [--assume-same-users] [--dry-run]
```
- `--keep-base` 指定哪个库的 base 保留 = 该库为 **primary/容器**；另一个为 **secondary**。
- 先把两个输入库各**拷一份**到临时工作区（绝不改动源文件）。所有迁移/合并都在副本上做。

### 1. 各自迁到 17（复用 app migrator）
对两个副本分别：
```python
settings = <构造 Settings，sqlite_path=<副本绝对路径>>
db = SqliteDatabase(settings, root_dir)
applied = SqliteMigrator(db, settings).migrate()   # 只 migrate()，不 initialize()
```
- **只调 `migrate()`**，不调 `initialize()`/`seed()`（避免 seed 塞默认用户/base 造成冲突）。
- 迁移器构造见 [`migrations.py`](../../../backend/app/repositories/sqlite/migrations.py) 的 `SqliteMigrator.__init__(database, settings)` 与 `migrate()`（`current+1..SCHEMA_VERSION` 逐级、每级 bump `user_version`）；`SqliteDatabase` 见 [`database.py`](../../../backend/app/repositories/sqlite/database.py)（`resolve_path` 对绝对路径原样返回）。
- Settings 构造走 app 的配置加载（注意 [[pydantic-env-alias-gotcha]]：pydantic-settings v2，环境变量映射用 `validation_alias`）。实现阶段确认最省事的构造方式（可能直接 `get_settings()` 后覆写 `sqlite_path`）。

### 2. Preflight 校验（任一不过 → 打印原因、非零退出、不产出）
1. 两副本迁移后 `PRAGMA user_version` 均 == 17。
2. 两库各恰好一个 `tier='base'`，且两个 base id **相同**。
3. 两库 notebook id 交集**恰好只有那个 base id**；若有其它交集（非 base id 撞车）→ 中止并列出撞车 id。
4. `users` id 交集：若非空且未给 `--assume-same-users` → 中止；给了则对同 id 不同 `email`/`role`/`display_name` 的行打印差异警告后继续（主库优先）。
5. 打印两库 base 的统计对照（`sources`/`chunks`/`knowledge_objects` 行数），供用户核对 `--keep-base` 选对了"更全的那份"。

### 3. 合并（ATTACH + 批量 INSERT，单事务）
- 输出 = **primary 副本整份复制**成 `--out`（base + primary 自己的 personal 全部保留，含其 FTS）。
- `ATTACH secondary副本 AS sec`，`secondary_nb = {secondary 全部 notebook id} - {base id}`。
- A 类表：`INSERT INTO main.t(<列清单>) SELECT <列清单> FROM sec.t WHERE notebook_id IN (secondary_nb)`（含 `chunks_fts`/`kg_objects_fts`，列清单排除 rowid）。
- B 类表：按父行集合筛，如
  `INSERT INTO main.source_elements(...) SELECT ... FROM sec.source_elements WHERE source_id IN (SELECT id FROM sec.sources WHERE notebook_id IN (secondary_nb))`。
- C 类表：`INSERT OR IGNORE INTO main.t SELECT * FROM sec.t`（主库优先），users 走带确认的并集。
- **rowid 处理**：所有 INSERT 用**显式列清单**（不含 rowid），让 SQLite 重分配 rowid，避免与 primary 已有 rowid 冲突；TEXT 主键（各类 id）因 notebook 不重叠天然不撞。
- 全程 `PRAGMA foreign_keys=OFF` 于批量导入期间、结束后 `PRAGMA foreign_key_check` 验证无悬挂引用（有则报告、回滚）。

### 4. FTS 收尾
- `chunks_fts`/`kg_objects_fts`：已在第 3 段按行拷入，无需重建。
- `memory_items_fts`：`INSERT INTO main.memory_items_fts(memory_items_fts) VALUES('rebuild')`。
- 可选完整性自检：对三个 FTS 跑 `('integrity-check')`。

### 5. KG 状态清理 + storage 合并 + 产出
- 清空/重置导入 notebook 的 `kg_rebuild_checkpoint`、`unified_kg_state`、`kg_cluster_scratch`（强制部署侧干净重建 ANN/图）。
- storage：`--out-storage` = primary 的 `storage/notebooks/` 整份 → 再把 secondary 的**每个非 base notebook 目录** `storage/notebooks/<id>/` 拷入（目标已存在同名目录=异常，报告）。`kg_index`/`kg_viz` 不拷。
- `VACUUM` 输出库（可选，缩体积）。
- 打印总结：迁移级数、导入 notebook 数与逐个行数、users 并集结果、FK 校验结果、输出路径。

### 部署后（写进 README，非脚本职责）
把 `merged.db` + `merged_storage` 放到要保留的那台部署，首次启动后在 app 内触发一次"重建索引/刷新图谱"重生成 `kg_index`/`kg_viz`/ANN。

## 错误处理与安全

- **非破坏性**：源库只读拷贝，任何失败都不影响两个源文件。
- **fail-loud**：所有前提校验失败即非零退出 + 明确原因，绝不静默降级。
- `--dry-run`：只跑迁移（在副本上）+ preflight + 打印将要导入的 notebook 清单与行数预估，不产出 `--out`。
- 单事务导入，`foreign_key_check` 不过即整体回滚。
- 二次运行防呆：`--out` 已存在 → 报错要求显式 `--force` 或换路径。

## 测试策略

- **构造 fixture**：用 app 的 repository/migrator 建两个小型 v17 库（或从 v15/v16 baseline 迁移），各含同一 base + 若干不重叠 personal，塞入 sources/chunks/kg_objects/memory_items/knowhow/source_elements 覆盖 A/B/C 三类与两种 FTS。
- **迁移路径**：单测 v15 副本与 v16 副本迁移后 `user_version==17` 且新表存在。
- **正例**：合并后行数守恒（primary 全量 + secondary 非 base 全量）；base 只保留 primary 那份；FK check 通过；三个 FTS 可查（对导入 notebook 的 chunk/kg/memory 关键词命中）。
- **反例（preflight 必须中止）**：schema 版本不一致、base id 不同、非 base id 撞车、users 撞车未确认。
- **storage**：非 base 目录被拷、base 目录取 primary、`kg_index` 未被拷。
- 参照 [[surface-manifest-line-shift-gotcha]]/[[schema-migration-convention]]：本工作**不新增表、不改 schema**，仅新增独立脚本与测试，不触碰 repository surface manifest。

## 落地方式

- worktree 隔离（已在 `merge-duplicate-dbs-d5e135`），实现走子代理逐任务（[[execution-mode-pref]]）。
- README.md / README_zh.md 补 `merge_dbs.py` 用法（[[document-cli-in-readme]]，通用口径、不写机器特定路径 [[committed-docs-stay-generic]]）。
- 收尾 rebase 到 master 提 PR（[[pr-merge-is-rebase]]、[[dev-flow-finish-with-pr]]）。
