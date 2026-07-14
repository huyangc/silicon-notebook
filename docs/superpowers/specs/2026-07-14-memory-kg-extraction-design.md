# Memory 确认后抽取进 notebook KG 设计

**日期**：2026-07-14
**状态**：已批准
**范围**：用户确认 Memory 后，若当前 notebook 满足 KG 抽取条件，则像新上传一篇文档一样，把 Memory 内容经真实抽取管线写入**当前 notebook 自己的 KG**（对象+关系边+证据绑定+增量融合）。

## 1. 背景与动机

Memory 系统（2026-07-13 落地）当前进 KG 的唯一通道是显式晋升：零 LLM 确定性派生（tags→concept、全文→claim、LaTeX→formula、列表→procedure）→ Track F 策展队列 → admin 批准进 base 库。该通道有两个结构性限制：

1. 只产**节点不产关系边**，新对象在图中初始孤立，只能靠归一化聚类/同义桥被动挂靠；
2. 只服务 base 共享场景，Memory 内容无法进入**用户自己 notebook 的个人 KG**，PPR/graph/reasoning 检索不到它的结构化形式。

本设计把「确认 Memory」对齐「上传文档」：门控相同、管线相同、生命周期语义相同。晋升通道保持不变。

## 2. 方案决策

**采用方案 A：memory-as-source（合成 source 行，复用完整摄取管线）。**

确认 Memory 时创建一条 `source_type='memory'` 的 sources 行（无本地文件），用现有 markdown 结构化解析器把 `content_md` 切成 source_elements，然后走上传抽取段的既有链路：分窗 LLM 抽取 → 证据绑 element → 孤立点 relink → `store_kg` → `incremental_fuse_source` → `maybe_auto_index` → 标脏。

**否决方案 B（直接抽取、证据绑 memory_id）**：需要给所有证据消费面（引用渲染、KG 证据卡、follow_chain 的 relation-evidence 约束）新增一种 evidence 类型，破坏「证据可回查 SourceElement」的既有不变量，侵入面远大于收益。

方案 A 的免费收益：Memory 编辑=reparse 语义（清旧派生 KG 再抽）、弃用=delete_source 级联清理、计数缓存按 `kg_mutation_seq` 自动失效、scale 库 auto-fold 自动生效、KG 证据卡出处可点。

## 3. 触发与门控

**挂钩点（MemoryService 内，共三处）**：

| 事件 | 行为 |
|---|---|
| `create_from_answer`（生而 confirmed） | 门通过且请求 `extract_kg=true`（默认）→ 建/复用派生源并调度抽取 |
| `confirm()`（candidate→confirmed） | 同上 |
| `update()`（对已 confirmed 的编辑） | **仅当派生源已存在**时重抽（替换 elements + 清旧派生 KG + 重抽）；确认时未勾选抽取的 Memory，编辑永不自动入图 |

`reject` / 对 candidate 的编辑不触发任何抽取。

**门 = `should_extract_kg(notebook_id) ∧ notebook.tier != 'base'`**：

- `should_extract_kg` 原样复用上传流程的判定（`backend/app/services/source_ingestion.py` 的「全局 `KG_AUTO_EXTRACT` 开 ∨ 该 notebook 已有 KG」），两条摄取路径共享同一个 opt-in 语义，不引入第二套门。
- base 库显式排除：进 base KG 必须继续走晋升人审，本功能不绕开策展纪律。
- **不新增环境变量**（遵守环境变量瘦身取向）；逐条否决权由前端 per-confirm 开关提供（§7）。

**执行方式**：抽取经 `kg_scheduler.submit_job` 异步执行（与上传一致，`copy_context` 传播当前用户），确认请求本身只做行创建+调度，不变慢。job 开跑时重读 Memory 当前内容与状态：已非 `confirmed` 则直接跳过（弃用竞态自然收敛）；以当前内容抽取（last-writer-wins）。

## 4. 数据模型

- `sources` 新增可空列 `memory_id TEXT`，加部分唯一索引（`WHERE memory_id IS NOT NULL`）——每条 Memory 至多一条派生源，重复确认/重抽幂等复用同一行。
- 走既有迁移惯例：**新增 `_migration_N` + bump `SCHEMA_VERSION`**（N=实现时现值+1），绝不塞进已封版迁移。
- 派生源字段：`source_type='memory'`、`title=Memory 标题`、`file_path=''`（无文件）、`file_name=''`、`file_size=0`、`file_hash=sha256(title+content_md)`（作内容指纹，供无变化跳过用）；创建后直接置 `parse_status='parsed'`（内容已是结构化 markdown，无解析阶段），抽取期间沿用 `extracting→extracted/failed` 状态机，pipeline 事件照常打点。
- Memory 行本身零新列；派生对象数等展示信息如将来需要，从 sources/knowledge 侧即时查询。

## 5. 数据流

```text
用户确认 Memory（门通过 + extract_kg=true）
  → upsert 派生 source 行（source_type='memory', memory_id 唯一）
  → structural_markdown 解析 content_md → source_elements（公式/表格/列表天然识别）
  → element embedding（后台，与上传一致）
  → kg_scheduler.submit_job：分窗抽取 → build_records（证据绑 element）
      → relink 孤立点 → store_kg → incremental_fuse_source（Tier1/Tier2 进 concept_clusters）
      → maybe_auto_index → mark_unified_kg_dirty
  → 下次「刷新图谱」对象进聚类；PPR/graph/reasoning/chunk-overlay 即刻可检索
```

组件接线：`MemoryService` 不直接依赖摄取域——由 `repository_runtime` 注入一个 `memory_kg_hook` 回调（模式同现有 `promotion_service`/`embedding_scheduler` 注入），实现放在 `source_ingestion` 域新增的 `ingest_memory_source(notebook_id, memory_id, title, content_md)` / `remove_memory_source(memory_id)` / `reingest_memory_source(...)` 上。facade 若需暴露新方法，走冻结契约 allowlist 惯例并保持一跳委托。

## 6. 生命周期联动

- **编辑（confirmed）**：派生源存在且 `sha256(title+content_md)` 与源行 `file_hash` 不同 → 替换 elements + `_clear_source_extraction_state`（清 extraction runs、source-derived knowledge、旧 embedding）→ 重抽（reparse 语义）。仅改 tags 的编辑指纹不变，零代价跳过。
- **弃用**：派生源存在 → 走 delete_source 同款级联（清派生 KG + 删源行；无文件可删）→ 标脏。弃用后的 Memory 不再给 KG 供数。
- **失败语义**：抽取失败置派生源 `parse_status='failed'` + `error_message`，事件/model_error 照常记录；Memory 本体不受影响（文本与向量检索照常）。v1 不在 Memory 面板新增失败 UI（可查事件日志），后续按需补。
- **离线/no-llm 边界**：未配置 LLM 时与上传一致——记 `error_message='no-llm'`，不伪造启发式对象（CLI/管线不静默降级原则）。

## 7. 前端触点（与后端同 PR 交付）

- **确认弹窗 / 答案保存弹窗**新增复选框「同时抽取到知识图谱」，默认勾选；仅当门通过时显示。请求体加 `extract_kg: bool`（默认 true）。
- 门的可见性：`POST /answers/{id}/memory-preview` 响应与 **notebook 级** Memory 列表端点（`GET /notebooks/{id}/memories`）响应新增 `kg_extract_eligible: bool`（后端算门，前端不自行拼判定）；用户级总列表（`GET /memories`，跨 notebook）不加。openapi golden 按 `_write_json(sort_keys=True)` 惯例 regen。
- **来源列表隐藏合成源**：`list_sources` / `list_sources_page` 与 NotebookSummary 的来源计数、看板 parse_status 分布，统一加 `source_type != 'memory'` 过滤（同一 WHERE 条件三处一致）。KG 证据卡、对象详情「出处」照常显示派生源标题。copy/scale-index/pending_kg 等内部路径**不过滤**（需要真实全集；「N 源待索引」计入 memory 派生源是如实的）。

## 8. 可见性与隐私

- 抽出的对象/关系是 notebook KG 的正式内容：**共享成员、拷贝接收方可见**——这是「进 notebook KG」的固有含义。进 KG 的只有用户确认过的 `title/content_md` 派生物；Memory 的私有 provenance（task_context、evidence_refs、对话上下文）**不进** source/element/KG 任何字段。
- **notebook 深拷贝**：Memory 行本身 owner 私有、不随拷贝。派生源行随 sources 正常拷贝（此后就是普通内容源），但拷贝副本中 `memory_id` 置空——不悬挂指向接收方不可见的 Memory；`copy_notebook` 的 id 重映射/完整性自检对该列按「清空不重映」处理。
- 与晋升的关系：不动。附带收益是派生对象可走**现有对象级晋升**（质量高于 memory 级确定性派生）；memory 级晋升继续服务「notebook 无 KG」场景。

## 9. 效率注记（一等约束）

每次确认的增量代价：1 次分窗抽取 LLM 调用（Memory 通常几百字=单窗）+ 少量对象/element embedding + 有界增量融合。全部异步离请求路径；无新增轮询；无新缓存——计数与图缓存靠既有 `kg_mutation_seq`/版本键自动失效。批量确认走既有 `KG_JOB_CONCURRENCY` 队列自然限流。

## 10. 非目标

- 不改 memory 级晋升/Track F 流程；不给 base 库开自动抽取。
- 不加新环境变量；不做 Memory 卡片「已入图」徽标（后续按需）。
- 不做候选（candidate）阶段抽取；不做深度报告→Memory（另案）。
- 不迁移存量：已 confirmed 的历史 Memory 不回填抽取（编辑时若门通过且派生源不存在也不建——规则见 §3，保持「确认时刻的选择」语义）。

## 11. 测试与验证

- 门控三态：notebook 有 KG / 无 KG 且 `KG_AUTO_EXTRACT` 关 / base 库（永不抽）。
- `extract_kg=false` 显式否决：不建派生源；其后编辑也不抽。
- 确认→派生源→抽取→融合→标脏全链路（离线用显式 KG 写入桩，同上传测试模式）。
- 编辑重抽：旧派生对象被清、新对象入库、幂等复用同一 source 行。
- 弃用清理：派生 KG 与源行消失、标脏；reject/candidate 编辑零副作用。
- 来源列表/计数/看板过滤一致性；内部路径不过滤。
- 拷贝：含派生源的 notebook 深拷贝后 `memory_id` 为空、KG 完整、完整性自检通过。
- no-llm 边界：`error_message='no-llm'`，不产对象。
- 冻结契约面：schema golden（`UPDATE_SCHEMA_GOLDEN=1` regen）、openapi golden、facade surface/manifest allowlist、`test_architecture_documentation.py` 若触及文档措辞需同步。
- 完整门禁：`bash scripts/check.sh` + 前端 build。

## 12. 实现注意（仓库惯例）

- 迁移：`_migration_N` + bump `SCHEMA_VERSION`（绝不改已封版迁移）；已部署库靠新迁移补列。
- pydantic 设置如有新字段用 `validation_alias`（本设计无新 env）。
- facade 新成员走 `STARTUP_READINESS_ALLOWED_NEW_MEMBERS`/surface-manifest allowlist，实现落组件、facade 一跳委托。
- 前后端同 PR；worktree 隔离开发；分支线性 rebase 合并。
