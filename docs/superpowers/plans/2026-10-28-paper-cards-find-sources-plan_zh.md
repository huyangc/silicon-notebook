# 论文卡片、文档级检索与逐篇抽取 实施计划

日期：2026-10-28
基线：`master` `e5d5bd619`（行号以此为准，实现时以符号名为准）
规格：`docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md`（下称「规格」）

用户已拍板（规格 §1）：A–D 四件一轮做完；卡片懒生成、不回填；复用 `paper_metadata` 绑定；指标数值必须原文出现；
MCP `read` 档也可触发生成；卡片只覆盖论文。**本计划范围一轮做完，不留延后项。**

下文 `$W` 指实施 worktree 根目录。

---

## 待确认

规格 §9 第 1–6 条（chunk 模式不接入、`extract_from_sources` 为 `read` 档、失败冷却 1h/24h、卡片不同步不导出、
表格由 `answer_layout` 决定、读者可为有读权的库写缓存）按默认执行，用户可在对应任务开工前改。
第 7 条已裁决（2026-10-28）：模型预标 + 只标候选集；**预标调用前必须在对话里征得用户同意**；阈值校准放到最后（T10）。

评审记录：2026-10-28 opus 评审（3 个 P0、11 个 P1、若干 P2）已全部落进规格与本计划，对应改动见规格 §4.4–§7.4、§8。

---

## 0. 每个实现子代理都要遵守的公共约束

- 开工前读：`$W/AGENTS.md`、`$W/CLAUDE.md`、`$W/docs/development_zh.md`（架构边界、SQL 绑定 id 清单、验证、文档维护）、
  规格全文、`$W/release-notes/README.md`。
- 本机经验（前缀 `/Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/`）：
  `postgres-is-primary-environment.md`、`retrieval-owns-permissions.md`、`agentic-tools-not-regex-routing.md`、
  `json-hint-strict-alignment-merged.md`、`golden-snapshots-cross-platform.md`、`migration-number-collision-on-rebase.md`、
  `function-ceiling-counts-to-last-statement.md`、`local-heavy-jobs-serialize.md`、`local-pg-test-db.md`、
  `pipeline-exit-code-masking.md`、`edit-tool-unicode-escape-nul.md`、`mcp-interface-redesign-merged.md`、
  `mcp-ask-evidence-output-merged.md`、`tests-use-deepseek-flash.md`、`no-deferred-feature-parts.md`。
- 只在 `$W` 里工作，命令用绝对路径；编辑用 Edit/Write。注释里不写 NUL 转义，写完 `grep -nP '\x00'` 扫一遍。
- `frontend/node_modules` 先 `ls -l`，不存在就软链主 checkout 并写进共享 `info/exclude`；**不得 `npm install`**。
- 重负载命令一律经 `bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh <标签> -- <命令>`；
  单文件 pytest（`-n` ≤2）、单个 PG 测试文件（`-n 1`）、vitest 单文件、`tsc --noEmit` 可直接跑。
- 门禁退出码直接读，不用管道尾，不把「跑 && 贴」串成一条。
- PG 测试用本机一次性库（`silicon_notebook_pcards_test`，装 `pg_trgm`、`btree_gin`，URL 带用户名，`-n 1`）。
- 架构守卫：`python3 $W/scripts/check_architecture_boundaries.py`。新增 port 方法会改 `ports_protocol_method_count`，
  在同一 diff 里改基线并在报告说明；`function_length_ceiling` 清单里的热函数（`reflect`、`_dispatch_reflect_action`、
  `_answer_reasoning` 等）加语句要放在尾注释之前，基线零松弛同 diff 改。新消费点后跑
  `python3 $W/scripts/generate_repository_contract_fixtures.py --rebaseline-surface`，diff 只能是新增行。
- 浮点分数不进逐字快照；阈值比较用档位结果断言。
- 每个任务交付：改动清单、跑过的命令与退出码、阻塞与存疑。

---

## 1. PR 划分

规模太大，单 PR 会被 GitHub 拒绝 rebase 合入（`github-rebase-merge-refuses-huge-prs`）。分三个依次叠加的 PR，
**全部在本轮合入**：

| PR | 任务 | 合入后的可见效果 |
|---|---|---|
| PR1 存储与服务 | T1–T4 | 无用户可见变化（服务有单测与 PG 测试；门控默认关） |
| PR2 Agent 与合成 | T5–T7 | 问答 reasoning 模式可用三个动作与论文表；门控默认开 |
| PR3 MCP 与文档 | T8–T9 | MCP 24 个工具；文档、更新说明 |

PR2 基于 PR1 合入后的 master，PR3 基于 PR2；每个 PR 开之前 rebase 到最新 master 并重跑门禁。

---

## 2. 任务

### T1 存储：`source_paper_cards`（sonnet，`impl-task`）

1. 迁移号：开工时 `git fetch` 后看 `origin/master` 最新号，当前应为 SQLite **v103** / PG **0084**；号被占就顺延，
   两侧同号。（实际落号 SQLite **v104** / PG **0085**：v103/0084 已被主线热路径批 7 的
   `idx_source_paper_meta_source_nb` 占用，下文按实际号写。）
2. PG `0085_source_paper_cards.sql`：规格 §4.4 的表、FK（`fk_<table>_<col>__<parent>`，`ON DELETE CASCADE`）、
   `idx_source_paper_cards_nb`。
3. SQLite `_migration_104`（模板 `_migration_17`、`_migration_101`）；`SCHEMA_VERSION`；版本注释；
   `tests/sqlite_migration_testkit.py` 加 `rollback_v104`（由 `rollback_v103` 先行调用）；`scripts/verify_repository_snapshot.py` 加 v104 DDL 与 (103, 104) 一跳。
4. 登记（逐项，规格 §4.4 口径）：
   - `postgres/schema_manifest.py` 版本对 + `POSTGRES_BUSINESS_TABLES` + jsonb 列；
   - `postgres/_store_utils._TIMESTAMP_COLUMNS_BY_TABLE`；
   - `migration/shadow/manifest.py` `RUNNING_SCHEMA_PAIR` + `_TABLES`（`LOCAL_EPHEMERAL`）；
   - `migration/sync/manifest.py` `LOCAL`；`docs/incremental-sync-design.md` §3.3 的 LOCAL 表清单；
   - `services/notebook_delete_tables.py`：A 类（随 notebooks 级联），进第 3 阶段计划，
     `EXPECTED_PHASE_3_TABLE_COUNT` 69→70，及 `test_notebook_delete_rows_and_files.py` / `test_notebook_delete_review_fixes.py`；
   - `scripts/merge_dbs.py` 分类；
   - `tests/postgres/test_postgres_schema.py` 的迁移阶段清单与列/FK 断言；
   - 复制/归档闭包不加；`notebook_copy_writer`、`sharing_store._COPY_SNAPSHOT_QUERIES`、`copy_memory_cases.py`
     等守卫里有「不复制」名单的显式列入。
5. store 方法（两端，port 声明在 `ports.py`）：
   - `paper_cards_for_sources(conn, ids, *, notebook_id) -> dict[source_id, row]`（一条 SQL，按 `notebook_id` 收窄）；
   - `upsert_paper_card(source_id, notebook_id, *, basis, card_version, status, card, raw_json, model)`：
     锁序与 `upsert_paper_meta` 相同（notebook → source `FOR KEY SHARE`，校验归属）；**条件写**——同 basis 同版本已是
     `ready` 时 `failed` 不覆盖；`attempts` 在 SQL 里自增、成功清零；来源已删时抛 `KeyError` 供上层映射 `not_found`；
   - `source_element_generation_utc(source_id)`：与现有 `source_element_generation` 同一聚合，统一输出 UTC ISO；
   - `clear_paper_meta` 同一事务里追加 `DELETE FROM source_paper_cards WHERE source_id=…`。
6. 测试：SQLite 单测 + PG 集成测试（读写、归属校验、级联删除、`clear_paper_meta` 连带删除、条件写竞争、
   PG 会话时区改变后 basis 不变）；`tests/test_dual_backend_parity.py`、`test_sync_manifest.py`、
   `test_shadow_sqlite_schema_validation.py`、`test_merge_dbs_taxonomy.py`、`test_notebook_delete_*` 全绿。

### T2 卡片纯函数与生成服务（opus 实现子代理：采样与校验有判断）

依赖 T1。

1. `app/services/paper_cards.py`（纯函数，无 DB/网络）：`PAPER_CARD_VERSION`、`PAPER_CARD_SCHEMA_HINT`、
   `paper_card_prompt(input_block)`、`verify_paper_card(parsed, input_elements) -> (card, dropped)`、
   数值归一化与 token 匹配（规格 §4.2）。
2. 采样：在 `document_source_overview.py` 增加按规格 §4.3 的「开头 + 表格 + 均匀」采样函数，复用分页读取与生成见证；
   返回元素 id → 文本映射和输入块。
3. 元数据抽取的可取消变体：在 `SourceIngestionService` 里把 `ensure_paper_metadata` 的主体抽成接收 `cancel_event`
   的内部函数，取消与 `ModelQueueTimeout`/`ModelQueueFull` 原样上抛；原入口行为不变（仍吞异常），只是改为调用它。
4. `PaperCardService.get_cards(...)`（规格 §4.5）：按所属库分组、入口校验来源集合、缓存命中判定（UTC basis）、
   元数据前置并计入生成上限、镜像笔记本 `meta_pending`、全部返回值显式映射、`deferred`、线程池并发、取消、
   `retrieval_fanout_slot`、队列超时/满降级、失败冷却、进程内单飞（等待在池外、轮询取消）、`paper_card` 事件。
   - basis 一律用 T1 的卡片 basis 方法（单篇/批量），**绝不**用 `CatalogStore.source_element_generation`（PG 上按会话时区渲染）；
     写卡用的 basis 必须是守护那次采样的同一个见证值（把卡片 basis 读取函数作为 `generation_reader` 传入采样，取它的值），
     不能在采样之外再单独读一次；
   - `upsert_paper_card` 返回 False 表示写入未落地（被 ready 保护或 basis 已过期），服务据此重读该行，不得假定写入成功。
5. **装配**：在 `repository_runtime.py`（`SourceIngestionService` 与检索服务的装配处）构造 `PaperCardService`，
   暴露给检索服务与 MCP；若新增 facade 成员，跑 `--rebaseline-surface` 并说明。
6. 配置（`app/core/config.py`）：`PAPER_CARD_INPUT_CHARS=16000`、`PAPER_CARD_MAX_WORKERS=4`、`PAPER_CARD_RETRY_SECONDS=3600`、
   `PAPER_CARD_ENABLED=true`（部署级开关）。
7. 测试：
   - 校验单测：合法/非法 anchor、数值写法变体（全角、千分位、尾零、百分号、`185.23` 不命中 `85.2`）、以送给模型的截断文本为准、
     全空判失败、截断、24 个 anchor 的保留顺序；
   - schema hint 快照纳入 `test_model_schema_hint_snapshots`；
   - 服务单测（假模型）：命中、过期（basis 变、版本变、改判非论文）、元数据前置计入上限、每种元数据返回值的映射、
     镜像笔记本、天花板外来源被拒、生成上限、`deferred`、冷却、单飞、取消、`ModelQueueTimeout` 不记失败、
     `failed` 不覆盖并发写入的 `ready`；
   - PG 集成：一篇真实元素数据的端到端生成与读回。

### T3 `find_sources` 服务（opus 实现子代理：SQL 与分档）

可与 T2 并行（只读 T1 的卡片状态，T1 合入前先用 port 桩）。

1. store：`find_sources_metadata_rows(conn, notebook_id, *, terms, year_from, year_to, venue, author, kind, allowed_source_ids)`
   两端实现；PG 为规范定义，结构沿用 0048 的 UNION 写法并重复部分索引谓词，`lower(col) LIKE`，`%`/`_` 转义，
   可见性谓词与 `list_sources_page` 一致；返回每篇命中字段集合与是否词边界命中；`kind="papers"` 含无元数据行但
   `doc_type` 合格的来源。
2. **测量**：本机 PG 生产规模合成数据（≥49k 来源、单库 10k），**有/无 0048 热路径索引两种情况**都跑
   `EXPLAIN (ANALYZE, BUFFERS)`，记录各腿耗时（含 venue/summary/keywords）；>50ms 才在 `HOTPATH_INDEX_SPECS` 加索引
   （同时改 `build_hotpath_indexes.py` 与对应守卫）。测量结果写进 PR 描述。
3. 端口：给 `RetrievalPort` 加 `retrieve_chunks_in_sources(nb, query, recall, allowed_source_ids)`，内部调用
   `_retrieve_chunks(..., allowed_source_ids=)`，经 `filter_retrieval_items`，并返回所走车道；联邦按库分别调用。
4. `SourceFinder.find(...)`（`app/services/source_finder.py`）：
   - 来源集合：问答侧复用 `_participants` + `scope_source_plan` + `scope_ceiling_digest`，天花板下推时按
     `verify_unbound_read` 事后校验；**MCP 侧显式传 `all_visible_source_ids(nb) ∪ hidden_source_ids(nb, 主人)`**；
   - 两条腿、按来源分组、按 `relevance` 与车道分档、词边界规则、确定性排序、游标（偏移 + 参数指纹）、
     `lexical_complete`、`semantic_recall_capped`、`card_status`（批量读 T1）。
5. 配置：`FIND_SOURCES_CHUNK_RECALL=400`、`FIND_SOURCES_STRONG_RELEVANCE`、`FIND_SOURCES_RELATED_RELEVANCE`
   （初值待 T4 校准后写死）。
6. 测试：两端 SQL 单测（每个字段命中、过滤组合、转义、`kind`、元数据未抽的论文、词边界）、分档与排序单测（用档位断言，
   不快照浮点）、FTS 降级车道的分档、游标在参数变化时作废、天花板之外的来源永远不出现、**无运行作用域时别人的 Memory
   投影片段不出现**（MCP 口径）、大库走 ANN/FTS 降级时仍可分组。

### T4 `extract_from_sources` 服务 + 标注集（opus 实现子代理）

依赖 T2 的数值接地函数（同 PR，T2 完成后开始）。

1. `PaperExtractionService.extract(items, fields, *, cancel_event, limit)`（规格 §6）：入口校验来源集合；
   `section_outline` 取 chunk id；**每个字段单独**调 `section_chunk_similarities`，无向量时回落单篇 FTS；单次模型调用；
   anchor 与数值校验；值的 anchor 映射到元素，交给卡片证据段（T6）登记；并发/取消/降级同 T2。
2. CI 夹具：`backend/tests/fixtures/paper_topics/` 40 篇合成论文 + 标注 + 确定性假嵌入器（规格 §8 第 1 层），
   供 T3/T4/T6 的测试共用。
3. 校准脚本 `backend/app/eval/find_sources_calibrate.py`（规格 §8 第 2 层）：`candidates` / `prelabel` / `sweep` 三个子命令；
   `prelabel` 不带 `--confirm` 时只打印调用次数与模型、不发请求。**本任务只写脚本和它的单测（用假模型），不运行**；
   运行在 T10。`FIND_SOURCES_*_RELEVANCE` 先写保守的临时值。
4. 测试：假模型单测（逐字段片段选择、无向量回落、非法 anchor、数值不接地丢弃、非论文可用、天花板外来源被拒、取消）。

**PR1 门禁**：T1–T4 完成后，主 agent 跑 §3 全量门禁，开 PR1，走 codex 评审闭环合入。

### T5 Reflect 动作接入（opus 实现子代理：触及热函数与 prompt）

依赖 PR1 合入。

1. `domain/reflect_action.py`：三个动作 id 进 `CORE_REFLECT_ACTION_IDS`，嵌套键进 `REFLECT_SCHEMA_TOP_LEVEL_FIELDS`
   （不用 `STRICT_SCHEMA_FALLBACK_KEYS` 里的名字）。
2. `reasoning_retrieval.py`：
   - 动作常量；`core_reflect_actions` 加三个门；门判定函数仿照 `document_read_active`
     （开关、本次运行剩余次数、天花板非空、Knowhow 补全时关闭）；
   - `ReflectDecision` 新字段与解析分支（枚举回落、缺参 fail_closed、自由文本进 2000 字有界字段门）；
     `answer_layout` 作为顶层字段（进 `REFLECT_SCHEMA_TOP_LEVEL_FIELDS`，与卡片/抽取门一起门控），任何一轮可写、取最后一次；
   - `find_sources` 翻页计入次数；`get_paper_cards` / `extract_from_sources` 接收的句柄或 source_id 在执行器入口按运行天花板校验；
   - `_dispatch_reflect_action` 三个分支与执行器 `_action_find_sources` / `_action_get_paper_cards` /
     `_action_extract_from_sources`；运行状态新增句柄表、卡片结果、抽取结果、计数；
   - 账本反馈（规格 §7.1）；`ReasoningResult` 新字段并在 `_finish_run` 填充。
3. `prompts.py`：`reflect_schema_hint` 三个门参数 + 分支 + `answer_layout`；`reflect_prompt` 用法段（只在门开时出现）。
4. 预算：`reports/policy.py` 计数；`ask_retrieval_policy.py` 五行加 5 个字段（规格 §8 表）+ 注释表；
   `config.py` 开关 `REASONING_FIND_SOURCES_ENABLED`、`REASONING_PAPER_CARDS_ENABLED`、`REASONING_EXTRACT_ENABLED`（默认开）。
5. 词表：`retrieval_experience_projection.RETRIEVAL_ACTIONS` 末尾追加；`retrieval_experience_block` 映射；
   `reasoning_trace_stats.STEP_TYPES`；`tests/test_trace_result_ids.py` 清单；前端 `reasoning-trace.ts`、
   `profile-model.ts` 中文标签与单测。
6. 守卫与快照：`test_model_schema_hint_snapshots`（全开门 hash）、`test_reflect_action_contract._KNOWN_REFLECT_SCHEMA_GATES`、
   `test_prompts`（关门字节不变、开门三处投影都有）、`test_peer_mode_reflect_actions`、`test_experience_offered_actions`、
   `test_retrieval_experience_job` 的尾部位置断言。
7. 测试：每个动作的解析/门控/计数/降级单测；一次假模型的完整运行（find → cards → extract → answer）。

### T6 合成：卡片证据段与论文表（opus 实现子代理）

依赖 T5。

1. 证据段：`PAPER_CARD_KEY_BASE=9000` 三处常量 + `ask_evidence.kind_for_key`；`AskEvidenceKind` Literal 与 api_contract 夹具
   （跑 `generate_repository_contract_fixtures.py`）；`_assemble_reasoning_context` 第四段分区；`_REPORT_KEY_BANDS`；
   `read_document` 可用性判定改 9000，`REASONING_MAX_DOCUMENT_READS × DOCUMENT_OVERVIEW_MAX_ELEMENTS > 2000` 时启动告警。
2. 按元素 id 跨库批量读取的端口（按所属库分组），供登记卡片 anchor 元素用。
3. `app/services/paper_card_answer.py`（仿 `document_read_answer.py`）：
   - **登记**全部 anchor 元素进 `id_map` / 引用卡，不占结构化侧提示词预算；
   - **提示词**只放每篇紧凑摘要，在结构化侧预算内按「knowhow → 枚举 → 卡片摘要 → 文档读取」拼装，全有或全无。
4. 论文表：`render_paper_table(...)`（规格 §7.3），只渲染已登记的 key；在 `_answer_reasoning` 的模型正文之后、
   `_parse_answer_anchors` 之前拼接；sectioned 路径：卡片证据只进第 0 节、表格只出现一次、第 1 节起不给摘要；
   合成 prompt 增加「表格由系统生成」说明（只在 `paper_table` 时出现）。
5. 测试：表格渲染单测（转义、截断、行数上限、未展开清单、覆盖说明三种情况）；**表里每个 `[kN]` 都能解析回元素**
   （40 张卡的 exhaustive 情形也成立）；抽取值的引用可解析；落库回答、流式 final 事件与引用卡一致；sectioned 路径只出现一次表；
   报告复原证据能识别 9000 段。

### T7 真模型验收（主 agent）

依赖 T6。本机 deepseek-flash（单服务副本配置），用 T4 的合成论文笔记本跑规格 §8 的 6 题，standard 与 exhaustive 各一遍：
记录耗时、生成篇数、`deferred` 数、表内每个数字是否能在引用原文找到（人工核对），结果贴 PR2 描述。不达标回 T5/T6 修。

**PR2 门禁**：§3 全量门禁 + 前端 `tsc`/vitest，开 PR2，codex 闭环合入。

### T8 MCP 工具（sonnet，`impl-task`）

技术上只依赖 PR1；为减少与 PR2 的冲突，排在 PR2 合入之后开 PR3（测试骨架可在 PR2 评审期间先写）。

1. `mcp_tools/sources.py`：`list_sources` 加 `authors/pub_year/venue` 及字段限长；新增 `find_sources`、`get_paper_cards`、
   `extract_from_sources`（规格 §7.4），`tier="read"`；来源集合显式传 `all_visible ∪ 主人 hidden`；
   会调模型的两个包在 `_run_with_progress` + stop 事件 + `model_work_scope(INTERACTIVE, actor_id=凭证主人)`；
   **新增按主人计数**的 2 并发槽，超出 `busy`；每次 ≤3 篇；顶层去重 `refs` 表 + 字段内序号；片段先映射到首个元素再出 ref；
   `_NOT_FOUND_RESOURCE` 登记。
2. `mcp_server.py` instructions 增加工作流段。
3. 测试：`test_mcp_bundle_architecture`（列表与档位过滤）、`test_memory_mcp`、`test_agent_mcp_onboarding`、
   新工具的权限（白名单外笔记本、主人失去读权、**他人 Memory 投影不出现**）、3 篇卡片的真实字节数在 12000 内、
   预算截断、取消、`busy`、`deferred`。

### T9 文档与更新说明（sonnet，`impl-task`）

依赖 T8。

- `docs/product-and-api.md` / `_zh.md`：工具表与数量 21 → 24（「二十四个工具」）、reasoning 动作清单、论文表行为；
- `docs/agent-mcp-memory-sop.md` / `_zh.md` 档位表；
- `docs/development.md:518` / `docs/development_zh.md:390` 里「21 个工具…」那句（MCP smoke 钉住）；
- 更新说明写明三个新动作 id 与 `answer_layout` 成为保留名（仓库外插件同名动作会注册失败），以及换嵌入模型后要重跑校准脚本；
- `docs/deployment-and-configuration*.md`：新增环境变量（规格 §4.5、§5、§7.1）与「`paper_metadata` 绑定现在也承担卡片与抽取」的容量提示；
- `release-notes/`：按 README 分级写一条重点更新；
- `fangan_todo.md` 登记本特性条目并在合入后移到完成。
- `tests/test_architecture_documentation.py` 全绿。

**PR3 门禁**：§3 全量门禁，开 PR3，codex 闭环合入。

### T10 阈值校准（主 agent，最后做）

依赖 PR3 合入。

1. 请用户指定真实论文笔记本与 6 个主题；跑 `candidates`（只用嵌入检索）。
2. 跑 `prelabel` 不带 `--confirm`，把调用次数与模型报给用户，**等用户在对话里明确同意后**再带 `--confirm` 运行。
3. 用户核对标注文件；跑 `sweep`，把阈值写回 `app/core/config.py`，同时更新部署文档里的校准说明。
4. 小 PR（PR4）：配置值 + 曲线与嵌入模型写进 PR 描述；§3 门禁 + codex 闭环合入。

---

## 3. 全量门禁（每个 PR）

```bash
# G1 标准门（经全局锁）
bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh g1 -- bash $W/scripts/check.sh
# G3 PostgreSQL（经全局锁；本机一次性库）
bash /Users/huzhifeng/.claude/projects/-Users-huzhifeng-workspace-silicon-notebook/memory/debt-fix-2026-10-12/heavy-gate.sh g3 -- <PG 泳道命令，见 docs/development_zh.md>
# 架构守卫（秒级）
python3 $W/scripts/check_architecture_boundaries.py
```

rebase 合入前逐提交检出跑相关测试与守卫（`rebase-merge-lands-every-commit`）；`git log --merges` 必须为空。

---

## 4. 子代理编排

- 每个任务一个新的实现子代理，完成后各跑一次 `spec-review` 与 `code-quality-review`，问题修完再推进。
- 可并行：T2 ∥ T3（不相交文件：`paper_cards.py`/`document_source_overview.py` vs `source_finder.py`/store 查询/`retrieval_service.py`）；
  T8 的测试骨架可在 PR2 评审期间先写。其余串行。并发 ≤4，经全局锁跑重负载。
- 子代理写兄弟 worktree 前先 `EnterWorktree`（`subagent-sibling-worktree-needs-enterworktree`）；派代理与切 worktree 不在同一条消息。

---

## 5. 风险清单

| 风险 | 应对 |
|---|---|
| 迁移号与并行分支撞号 | T1 开工与每次 rebase 都核对 `origin/master` 最新号，PG 同号文件并存不算 git 冲突，靠守卫发现 |
| 热函数行数天花板 | 新逻辑放执行器与新模块，热函数只加分派一行；基线同 diff 改 |
| reflect schema 变化导致经验/快照大面积红 | T5 一次性改完全部快照与尾部位置断言，报告列出每个 hash 变化 |
| 阈值校准依赖标注集质量 | 标注集与评测脚本随 PR1 合入；阈值只用档位断言，换模型后重跑脚本 |
| 首问变慢 | 规格 §8 的生成上限与 `deferred`；T7 记录实测耗时，超出预期时调低 standard 档生成数 |
| `paper_metadata` 服务被交互卡片挤占 | 队列超时降级为 `deferred`；部署文档提示按需提高该服务 `max_concurrency` |
