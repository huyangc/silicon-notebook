# RepositoryFacade 公开方法退役账本（B5 结构项，第一阶段）

**状态**：`retire-now` 4 个已于早前 PR（B5 第二阶段）退役。随后修复了普查脚本的一处盲区——`RepositoryFacade.__init__` 把若干自己的公开方法用迟绑定 lambda 布线进 `RepositoryRuntime`（`self.notebook_copy_stats(...)` 这类），此前脚本整个跳过 facade 文件自身、看不到这批引用；修复后这类引用计入独立的「facade内部」列并折入生产计数，6 个方法（`notebook_copy_stats`、`decided_seed_pairs`、`relations_for_notebook`、`set_conflict_status`、`concept_whitelist_terms`、`maybe_auto_index`）从 `test-only` 改判 `keep`。之后社区报告死功能退役删掉了 `get_community_reports`、`summarize_communities` 两个 `test-only` 方法。

**S2 批 A（2026-10 技术债修复）**：18 个 `test-only` 方法退役（`memory_revisions` 同时从 `MemoryRepository` 协议删除，`ports_protocol_method_count` 975 → 974），测试改为直调 `repo._runtime.<组件>.<方法>`（检索类改为 `repo.retrieval.graph.scale_ppr` / `repo.retrieval.candidates.federated_retrieve(_relations)`）：`kg_cluster_size_histogram`、`kg_largest_clusters`、`kg_community_overview`、`kg_relation_provenance_counts`、`knowhow_history_head_seq`、`list_knowhow_changes`、`list_knowhow_milestones`、`source_asset_ids`、`memory_revisions`、`backfill_kg_fts`、`backfill_chunk_fts`、`build_viz_index`、`get_paper_meta`、`paper_meta_backfilling`、`paper_meta_backfill_progress`、`federated_retrieve_relations`、`scale_ppr`、`federated_retrieve`。同批把 `facade_public_surface` 改为零松弛，并新增运行时按名字取用的引用检查（见「退役时必须同步的守卫/baseline」第 1 条）。按 S2 余下批次（D → B → C → E → F）继续。

**S2 批 D（2026-10 技术债修复）**：Ask 与查询类 8 个 `test-only` 方法退役——`ask_chunk`、`ask_reasoning`、`ask_job_status`、`list_user_activity`、`list_user_notebooks`、`list_user_usage`、`notebook_exists_for_owner`、`pending_actions_projection_rows`；测试改为直调 `repo._runtime.ask_component.ask_chunk_current` / `ask_reasoning_current`、`repo._runtime.ask_state.ask_job_status`、`repo._runtime.queries.<同名>`（175 处，参数逐字不变）。生产里的同名调用都不经 facade：`ASK_MODES` 的 handler 在 `AskService.ask` 上用 `getattr(self, spec.handler)` 解析，管理端查询走 `admin_query_repository()`（即 `_runtime.queries`），`PendingActionsService` 持有的是 `QueryStore`。协议同步：`AskExecutionPort` 删 `ask_chunk`/`ask_reasoning`，`AskStateRepository` 删 `ask_job_status`，`NotebookRepository` 不再继承 `AdminQueryRepository`（那组方法由 `admin_query_repository()` 交出的 `QueryStore` 实现，facade 从来不是它的实现者）；`ask_state_repository()` 改为把 facade 交成只含 `answer_memory_source` 的 `AnswerMemorySourceRepository`（两条 Memory 预览路由只调这一个方法），不再冒充整个 `AskStateStorePort`。`ports_protocol_method_count` 974 → 972，`facade_protocol_binding_gaps` 44 → 23。同批先单独提交了一次 `--rebaseline-surface` 纯重基线，物化此前各特性 PR 累积的 `facade_surface.json` 漂移。

**S2 批 C（2026-10 技术债修复）**：KG 生命周期类 7 个 `test-only` 方法退役——`write_clusters`、`append_clusters`、`incremental_fuse_source`、`rebuild_canonical_relations`、`rebuild_mention_bridge`、`rebuild_communities`、`list_communities`（`summarize_communities` / `get_community_reports` 已随 C3 社区摘要退役删除）；测试改为直调 `repo._runtime.knowledge_lifecycle.<同名>`（188 处，含 4 处 `getattr(repo, "<名字>")(...)` 写法，参数逐字不变），`test_knowledge_lifecycle_delegation.py` 删掉对应 7 行。`incremental_fuse_source` **不是**真正只被测试调用：`RepositoryFacade.__init__` 交给 scale builder 的弱引用 lambda 在每次 fold 时调用它，普查看不到这条路径；同一改动先把 lambda 改指 `_repository_from_weakref(...)._runtime.knowledge_lifecycle.incremental_fuse_source`，再从 facade 删除，并加了真实调用 `repo._runtime.scale_builder.incremental_fuse_source(...)` 的用例和 fold 无 `incremental_fuse_failed` 事件的断言。`dynamic_facade_reference_violations` 同步扩展：经 `._runtime.<组件>.<成员>` 的接缝，成员必须仍在组件类（含基类）上。

**S2 批 E（2026-10 技术债修复）**：身份类 6 个 `test-only` 方法退役——`create_user`、`authenticate_user`、`create_session`、`delete_session`、`global_document_limit_default`、`user_document_limit_override`；测试改为直调 `<facade>._runtime.identity.<同名>`（153 处，接收者 `repo` / `repository` / `world.repo` / `postgres_repository` / `instance` / `probe`，参数逐字不变）。生产里的同名调用都走 `identity_repository()`（即 `_runtime.identity`）或身份存储内部。协议同步：`IdentityRepository` 拆出基类 `IdentityReadRepository`（`current_user`、`resolve_session`、`audit_labels_for_user_ids` 三个 facade 自己也有的读），`NotebookRepository` 改为只继承它——账户写入、会话与每用户设置由 `identity_repository()` 交出的身份存储实现，facade 从来不是它们的实现者。方法只移动不新增，`ports_protocol_method_count` 不变；`facade_protocol_binding_gaps` 中 `NotebookRepository` 19 → 6（合计 23 → 10）。

**S2 批 F（2026-10 技术债修复）**：共享与成员类 15 个 `test-only` 方法退役——`share_notebook`、`unshare_notebook`、`find_notebook_by_share_token`、`copy_notebook`、`is_member`、`user_can_admin_notebook`、`user_can_read_source`、`add_member`、`remove_member`、`kick_all_members`、`list_members`、`join_shared`、`leave_notebook`、`conversation_owner`、`user_can_read_answer`；测试改为直调 `<facade>._runtime.sharing.<同名>`（293 处，参数逐字不变）。生产里的同名调用都走 `notebook_access_repository()` / `notebook_sharing_repository()`（即 `_runtime.sharing`）、`group_repository()` 或服务内部。adapter `add_relations` 按裁决挪进测试工具 `backend/tests/kg_relation_testkit.py::add_relations(repo, …)`，保留 review-queue 排名 memo 的失效（9 处）。协议同步：`NotebookAccessRepository` 拆出 `NotebookAccessReadRepository`（facade 实有的 5 个访问读），`NotebookSharingRepository` 拆出 `NotebookShareReadRepository`（facade 实有的 4 个共享读），`NotebookRepository` 与 `McpMemoryRepository` 改为只继承读基类；`KnowledgeLifecycleRepository.finish_indexing_pipeline_job`、`ReportRepository.complete_report_generation` 两条只服务 facade 却从未被 facade 实现的声明删除。`ports_protocol_method_count` 972 → 970，`facade_protocol_binding_gaps` 10 → 1（只剩 `NotebookRepository.rerank`，来自 `FacadePropertyContract` 继承的 `ModelClientProvider`）。

**S2 批 B（2026-10 技术债修复，S2 收官）**：KG 治理类 10 个 `test-only` 方法退役——`review_queue`、`write_merge_candidate`、`set_merge_decision`、`write_conflict_candidate`、`get_conflict_candidate`、`apply_conflict_resolution`、`resolve_notebook_conflicts`、`decided_pairs`、`approve_promotion`、`reject_promotion`；测试改为直调 `<facade>._runtime.knowledge_governance.<同名>`（198 处，参数逐字不变），`test_knowledge_governance_delegation.py` 删掉对应 10 行和只测 `review_queue` 委托的一条用例，`test_knowledge_lifecycle_delegation.py` 删掉只测 `resolve_notebook_conflicts` 委托的一条用例。K1 合入后重新普查：生产里的同名调用都在治理/生命周期服务内部（`self.*`、`self.governance_store.*`、`self.governance.resolve_notebook_conflicts`），审阅入口走 facade 保留的 `approve_promotion_as_reviewer` / `reject_promotion_as_reviewer`。目标方法比 facade 多出的参数（`approve_promotion` / `reject_promotion` 的 `reviewer_id=''`、K1 给 `write_conflict_candidate` 加的 kw-only `reset_epoch=None`）都带默认值，facade 从未传过，改写等价。协议同步：`KnowledgeGovernanceRepository` 只服务 facade，删掉其中 `resolve_notebook_conflicts`、`approve_promotion`、`reject_promotion` 三条声明，`ports_protocol_method_count` 970 → 967；`facade_protocol_binding_gaps` 不变（只剩 `NotebookRepository.rerank`）。`rebuild_notebook_kg` / `relink_notebook_kg` 按裁决保留：facade 这层的索引流水线写闸是被测对象。当前快照：`test-only` 只剩这 2 个，`ambiguous` 12 个，即计划的终态。

**S2-A（2026-10，协议绑定缺口清零）**：最后一条缺口 `NotebookRepository.rerank` 来自 `FacadePropertyContract` 继承的 `ModelClientProvider`。facade 只实现 `chat` / `configured` / `parallelism`，从未实现 `rerank`，也不该补：`rerank` 的生产调用方（`AskService` 的 chunk 精排、`retrieval_candidates`、深度报告依赖里的 `ModelClientProvider`、`ReasoningRetriever` 的原文段精排）拿的都是运行时模型提供方 `_runtime.models`；knowhow 智能补全走的 `ReasoningRetriever.from_repository` 把 facade 当 `model_clients`，`_chunk_rerank_client` 按属性探测 `rerank`、探不到就走 MMR——给 facade 补委托会让这条路径开始精排，是行为变化。插件拿不到 `NotebookRepository`（插件架构设计「上下文不得暴露」），仓库外也就没有经它调 `rerank` 的消费者。所以只改声明：`FacadePropertyContract` 的基类由 `ModelClientProvider` 改为 `ReasoningModelProvider`；`ModelClientProvider` 及其 `rerank` 原样保留给报告引擎与 `AskModelClientProvider`。facade 公开面、运行时行为不变；`ports_protocol_method_count` 不变（方法未删）；`facade_protocol_binding_gaps` 1 → 0。

**S2-E（2026-10，按接收者普查 + facade 私有包装退役）**：组件普查 `scripts/audit_component_callers.py` 改为按接收者判定并把 facade 层纳入（`facade_surface.json` 钉住的名字豁免，仍归本账本）。facade 私有兼容包装里生产零调用的成员全部退役：`RepositoryFacade` 158 个（委托、属性描述符与它们的 setter，如 `_keyword_chunk_candidates`、`_vector_cache`、`_scale_idx_cache`、`_test_insert_object`），`SQLiteRepository` 12 个（`_write_lock`、`_migrate`、`_seed` 及 `*_legacy` 等）。测试按包装原样委托的路径改走生产组件（773 处，`repo._x(...)` → `repo._runtime.<组件>.<方法>(...)` / `repo.retrieval.candidates.<方法>(...)`，参数逐字不变；签名不同的几处逐条改：`chunk_context` 补 `notebook_id=""`、`count_knowledge` 补 `USABLE_STATUSES`、行映射改调模块函数 `promotion_row_to_dict`、`_viz_index_dir` 写成 `str(...scale_artifact_store.viz_dir(...))`），每个目标做了变异证明。公开方法同批退役 7 个：`decided_seed_pairs`、`share_state`、`visible_source_count`、`visible_source_ids`、`set_knowhow_hidden_source`、`bump_knowhow_mutation_seq`，以及 `SQLiteRepository.eval_insert_source_for_test`。协议同步：`share_state` 从 `NotebookShareReadRepository` 挪到 `NotebookSharingRepository`（路由经共享服务读它，facade 不再需要实现），组件层随之退役的端口声明共 15 条，`ports_protocol_method_count` 981 → 966；`facade_protocol_binding_gaps` 仍为空。`begin_ask_job` / `append_ask_trace` / `finish_ask_job` / `shared_by_me` / `shared_preview` 保留：契约夹具生成器 `generate_repository_contract_fixtures.py` 驱动真实仓库调用它们（此前该脚本整个不在普查范围内）。`rebuild_notebook_kg` / `relink_notebook_kg` 按 S2 批 B 裁决保留并登记为观察点。待拍板：`answer_owner` / `source_owner` 两条链生产零调用，但代码注释写明「为冻结兼容面保留」「别因为没人用顺手删它」，而 `facade_surface.json` 已不含这两个名字——登记为 pending-decision，未动。按名字判定的残留：`load_notebook_scale_facts`、`report_source_rows`、`report_source_identity_rows`、`validate_reasoning_submission` 的生产调用点接收者是 `Any` 席位（`self.queries`、`self.source_store`、`self.ask`），普查无法判定；从构造点看生产走的是 store / `AskService` 本身，facade 版本疑似零调用，但它们是 facade 绑定协议的声明，本批不动。表格与汇总仍是 S2 批 B 后的快照；当前 `audit_facade_callers.py` 复跑：keep 244、ambiguous 7（上面 2 条待拍板 + 4 条残留 + `resolve_agent_token_status`，后者经 MCP 的 `repository_provider()` 拿到 facade，确属在用）、test-only 2（`rebuild_notebook_kg` / `relink_notebook_kg`）。

**范围（本文件产出时，B5 第一阶段）**：只读普查，产出账本；该阶段不改动、不退役任何方法——实际退役动作记在上面的「状态」一行，属于随后的 B5 第二阶段。

**目标文件**：`backend/app/services/repository_facade.py::RepositoryFacade`，当前 257 个公开成员（`@property` 与 setter 同名合并计 1 行；已退役方法不再出现在快照里，见上方「状态」）。

## 重新生成

```bash
cd /Users/huzhifeng/workspace/silicon-notebook  # 或对应 worktree 根
PYTHONPATH=backend python3 scripts/audit_facade_callers.py \
  --json /tmp/facade_audit.json \
  --markdown /tmp/facade_table.md
```

`--json` 输出完整结构化报告（含每个方法的采样调用点路径:行号，最多 5 条/桶）；`--markdown` 输出本文件「完整表格」一节的 GFM 表格本体（按方法名字母序）。本文件的表格是某次运行的快照，脚本本身是唯一权威、可重跑的真源——脚本的方法论、已知局限和设计取舍全部记录在其模块 docstring 里（`scripts/audit_facade_callers.py` 顶部），本文件只做摘要，不复述。

## 汇总

| 分类 | 数量 | 含义 | 下一步 |
|---|---|---|---|
| `retire-now` | 0 | 生产/脚本/测试/facade内部四桶均零调用，且撞名未解析计数也为零（`classify()` 判据：所有已解析计数与撞名计数皆为零；早前一批 4 个已随本 PR 退役，见上方「状态」） | 可直接进入退役 PR，逐条附证据见下 |
| `test-only` | 2 | 生产/脚本/facade内部三桶零调用，仅测试文件调用 | 退役需连同其测试一起处理（删除或改为直打 runtime 组件），见下 |
| `ambiguous` | 12 | 四桶解析调用为零，但存在同名的 `<expr>.<method>(` 调用其接收者无法确认/排除——人工复核 | 不得直接退役；需人工确认接收者身份 |
| `keep` | 243 | 至少一处生产、脚本或 facade内部桶解析调用 | 保留 |
| **合计** | **257** | | |

`facade内部`列（`facade_internal_calls`，JSON 同名字段）不是第五个独立分类桶——它在 `classify()` 里折入上面「生产调用数」参与判据，只是在完整表格里单独成列以便追溯「这条 keep 判定是不是靠 facade 自己的迟绑定布线撑起来的」。方法论见下一节第 6 点，脚本 docstring 权威。

## 核心架构性发现：`deps.py` 窄口径旁路模式

这是解释绝大多数 `retire-now`/`test-only`/`ambiguous` 行的**唯一最大成因**，值得先说清楚，否则账本的分类会显得像误报。

`backend/app/api/deps.py` 里，仅 6 个访问器返回 facade 自身（`repository`、`source_repository`、`ask_stream_repository`、`memory_service`、`ask_state_repository`、`mcp_memory_repository`）；其余全部（`identity_repository`、`admin_query_repository`、`notebook_catalog_repository`、`notebook_access_repository`、`notebook_sharing_repository`、`group_repository`、`notebook_store_port`）都是 `return repository()._runtime.<component>`——直接拿 `RepositoryRuntime` 的子组件，**完全绕开 facade**。

例如：
- `notebook_sharing_repository().share_notebook(...)`（`backend/app/api/notebook_routes.py:216`）调用的是 `SharingStore.share_notebook`，不是 `RepositoryFacade.share_notebook`（后者的委托目标恰好也是 `_runtime.sharing.share_notebook`，但从未被这条路径触达）。
- `group_repository().remove_member(...)`（`backend/app/api/group_routes.py:520`）同理绕开 facade 直达 `_runtime.groups`。

这解释了为什么 `share_notebook`/`unshare_notebook`/`remove_member`/`is_member`/`list_members`/`leave_notebook`/`kick_all_members`/`join_shared`/`answer_owner`/`source_owner`/`source_notebook_id`/`conversation_owner`/`user_can_admin_notebook`/`user_can_read_answer`/`user_can_read_source`/`copy_notebook`/`find_notebook_by_share_token` 等一整批 sharing/groups 相关 facade 方法只有测试调用甚至零调用——生产代码从来就没有通过 facade 走这条路，而是通过窄口径访问器直达底层组件。facade 上的这些方法本身可能是历史遗留的兼容层（当年可能确有调用方，后来迁移到窄口径访问器后未清理），也可能从一开始就只服务测试。**这不是脚本的误判，是可复现的生产事实**——见上面两条引用行。

同一模式也解释了 `create_user`/`authenticate_user`/`create_session`/`delete_session`/`resolve_session`（部分）——真实登录路径 `login_with_password` 与 `identity_repository()` 直接持有 `IdentityStore`，而不是经 facade。


## 方法论摘要

详见脚本 docstring（权威、含所有取舍理由）。要点：

1. **委托分类（delegate/adapter）**复用 `backend/tests/architecture/facade_contract.py` 的 `_function_contract_owner` 等 AST helper，与 `facade_delegate_evidence`（唯一被 `scripts/generate_repository_contract_fixtures.py` 消费、用于生成 `facade_surface.json` 的 owner 列）判定口径一致。**但发现一个需要澄清的事实**：该模块里的 `facade_body_violations`/`manifest_delegate_mismatches` 两个函数目前**没有被任何测试或脚本调用**——不是活的硬门，是休眠的校验逻辑。真正在跑的表面守卫是 `scripts/check_architecture_boundaries.py` 的 `facade_violations`（`facade_surface_additions` + `facade_surface_stale`，S2 批 A 起双向零松弛），它只比对公开**名字集合**（对 `scripts/architecture_boundary_baseline.json::facade_public_surface` 的 `allowed_names`），不关心方法体是不是干净的单跳委托。`adapter`（非单跳）行以脚本当次输出为准（例如 `source_parse_busy`、`claim_report_generation`）；`federated_retrieve` 已在 S2 批 A 退役。
2. **调用者判定按"接收者白名单"而非纯按名字**：直接 `grep -rw` 会把 `notebook_catalog_repository().get_notebook(...)`（调用的是 `NotebookCatalogService`，不是 facade）之类的窄口径旁路错记成 facade 调用。脚本对 Name/Attribute/Call 三种接收者形态分别判定，并追加了两处在迭代中发现确有必要的扩展：
   - 文件内单跳变量传播（`service = memory_service(); ...; service.foo(...)`）；
   - FastAPI `Depends(memory_service)` 参数默认值传播，以及 `run_in_threadpool(service.foo, ...)` 这类"方法作为裸可调用对象传参"的形态（本仓库 `deps.py` 鉴权核心与 `memory_routes.py` 大量使用后者）。
   这两处扩展都是在第一遍运行后，人工核对可疑结果（`resolve_agent_token`/`list_memories`/`list_agent_tokens` 等误判为零调用）时反推出来的真实缺口，修复后从 `ambiguous`/无信号 转为正确的 `keep`。
3. **"撞名未解析"（ambiguous 判据）**：三桶解析调用为零时，额外统计同名 `<expr>.<method>(` 调用但接收者不在白名单内的次数；非零则归 `ambiguous` 而非 `retire-now`——这是保守设计，宁可让人工多看几眼，也不假阳性判"可删除"。
4. **`@property`/setter** 单独处理：调用形态是裸属性读写而非带括号调用，脚本另计一套按桶分类的 `property_attr_hint_*`，仅对 property/setter 行折算进有效计数（普通方法不折算，因为裸属性访问对普通方法是反常模式，已由上面的"方法作为裸可调用对象传参"覆盖了常见的正当用法）。
5. **`facade_internal`（曾经的盲区，现已修复）**：脚本第一版把 `repository_facade.py` 自身整个排除在扫描根之外——理由是"被审计的对象不该把自己算成自己的调用方"，但这条排除过宽：`RepositoryFacade.__init__` 会把若干自己的公开方法用迟绑定 lambda 布线进 `RepositoryRuntime`（例如 `notebook_copy_stats=lambda notebook_id: self.notebook_copy_stats(notebook_id)`，`repository_facade.py:630`），这类引用是 `RepositoryRuntime` 在真正调用时才触发的、货真价实的生产调用点，不是死代码，却因为整文件排除而从未被看到。修复：facade 文件本身现在**会**被扫描，但只扫一件事——`self.<name>` 引用（call 形态或裸属性形态均可）落在 `<name>` **自己的 `def` 之外**（同名方法递归调用自己不算"外部调用者"）。命中计入独立的 `facade_internal` 桶（完整表格的"facade内部"列），并在 `classify()` 里无条件折入生产计数。修复前，`notebook_copy_stats`、`decided_seed_pairs`、`relations_for_notebook`、`set_conflict_status`、`concept_whitelist_terms`、`maybe_auto_index` 这 6 个方法因此被误判 `test-only`——它们的唯一"生产"调用点全部只存在于 facade 自己的 `__init__` 里。

## 与既有 caller 契约的关系

`backend/tests/architecture/repository_callers.py` **不是**同类的"facade 方法级调用者"清单，而是回答一个不同的问题：**谁被允许绕开 repository/facade 抽象直接碰 SQL / 私有属性 / sqlite3.connect / 导入具体 SQLiteRepository**（`facade_import_sites`/`private_repository_sites`/`product_sql_sites`/`sqlite_connect_sites`，均需在硬编码的 `*_REASON_BY_PATH` 字典里登记逐路径理由，被 `test_repository_dependency_contract.py` 消费并断言）。它按**文件路径**登记例外，不按**方法名**统计调用方——两者互补而不重叠：
- 我的普查回答"这个 facade 方法有没有人在用、在哪用"；
- `repository_callers.py` 回答"哪些文件被允许绕过 facade 这层抽象本身"。
两者共享同一批底层 AST 概念（`REPO_NAME_RE`——本脚本直接复用了这个正则；`_repo_bindings`/`_is_repo_expression` 的变量传播思路——本脚本独立实现了一个更窄的版本，专门针对"facade 访问器"而非泛化的"repo 形状对象"，因为后者的类型注解匹配口径过宽，会把 `notebook_catalog_repository()` 这类窄口径访问器也误判为 facade 接收者）。
`backend/tests/architecture/facade_contract.py`（本脚本直接 import 复用其 AST helper）、`repository_contract.py`（`live_surface()`，被 `test_repository_monkeypatch_owners.py` 用于校验 `LATE_BOUND_COMPATIBILITY_SEAMS` 的 owner 归属，是另一个消费同一份 facade 表面但目的不同的守卫）——三份契约文件加上本脚本，共同覆盖了"表面有哪些名字"（`facade_surface_additions`）、"每个名字归哪个底层组件所有"（`facade_delegate_evidence`/`live_surface`）、"谁被允许绕开抽象"（`repository_callers.py`）、"每个名字实际被谁调用"（本脚本，此前完全空白的一块）四个维度。**结论：本普查填补的是一个此前没有任何工具覆盖的空白维度，不与任何既有契约重复或冲突。**

## 我对"按名字普查"误判风险的评估

- **假阴性（真有调用但判成零/ambiguous）风险中等，已通过两轮真实案例修正大幅降低但未消除**。已确认并修复的两类系统性缺口（文件内变量传播、方法作为裸可调用对象传参）是在核对具体可疑行时发现的，不是理论推导——说明"看起来奇怪的零调用"值得逐条人工核实，账本不能盲信。仍未覆盖、已知会漏记的形态：字符串驱动的完全动态 `getattr(x, some_var)`（`some_var` 非字面量）、反射/序列化驱动的分派、跨函数（而非跨文件全量传播）的更复杂变量流。这类形态一旦存在，会让某个方法被误判 `retire-now`——**这是退役前必须对 4 个 `retire-now` 候选逐个额外跑一次窄范围 `grep`（不限属性调用语法）人工复核的理由，账本不能替代最终这一步人工确认**。
- **假阳性（判成 keep 但其实是误报）风险低**：`keep` 只在接收者能被白名单确认（包括两处新增的传播规则）时才成立，白名单本身收窄到 `FACADE_ACCESSOR_CALL_NAMES` 这个从 `deps.py` 源码逐一核实过的精确集合，不是泛化的 `*_repository` 后缀匹配——已验证过至少 3 个反例（`notebook_catalog_repository`、`notebook_access_repository`、`group_repository` 都返回子组件而非 facade，若用后缀匹配会把它们的调用误记成 facade 调用）。
- **`ambiguous` 桶设计本身就是应对上述不确定性的安全阀**：15 个方法的"撞名未解析"计数all 1-12 次，多数样本核实后确认是同名但接收者明确是其他类（如 `MemoryService` 内部的 `self.store.xxx`），但脚本无法 100% 排除少数是白名单遗漏的可能，因此没有归入 `retire-now`。**这是刻意的保守，不是精度不足的借口**。
- **308 个方法中真正的通用名冲突风险（如 `close`、`ask`、`chat`）**已单独核查：`close` 因被 `PostgresRepository` 覆盖而单独标注 wrapper 镜像列；`ask`/`chat` 均有充分的白名单内解析调用，未落入任何有风险的桶。
- **曾经的盲区：整文件排除掉了 facade 自己对自己的合法引用**。脚本第一版把 `repository_facade.py` 整个排除出扫描根，理由是"被审计对象不该给自己计分"——但这条规则把真实生产调用一起排除了：`RepositoryFacade.__init__` 用迟绑定 lambda 把若干自己的公开方法布线进 `RepositoryRuntime`（`self.notebook_copy_stats(...)` 这类，`RepositoryRuntime` 在运行期才真正调用它），这是不折不扣的生产调用点。修复后（见「方法论摘要」第 5 点）facade 文件本身也会被扫描，只是判据换成"该 `self.<name>` 引用是否落在 `<name>` 自己的 `def` 之外"，命中折入独立的 `facade_internal` 桶。6 个方法因此从 `test-only` 改判 `keep`——这不是一次误判修正，而是审计范围本身此前有一个可复现的空洞：一个方法即使被 facade 自己反复使用，只要没有第二个外部调用方，此前就会被错误地判定"无人使用"。这条修复引入的新风险很小且方向单一（只会新增计数，绝不会移除既有计数）：唯一残留的盲区是 facade 文件内部的完全动态分派（`getattr(self, some_var)`，`some_var` 非字面量）——与文件其余部分的已知局限相同，未发现有此类写法。


## 退役时必须同步的守卫/baseline

1. **`scripts/architecture_boundary_baseline.json::facade_public_surface.RepositoryFacade.allowed_names`**——被 `scripts/check_architecture_boundaries.py` 的 `facade_violations` 消费。S2 批 A 起**双向零松弛**：多出一个公开名字（新增）或残留一个已不存在的名字（陈旧）都会报错，所以退役方法必须在同一改动里从 `allowed_names` 删掉对应名字。同一脚本的 `dynamic_facade_reference_violations` 另管两类普查看不到的引用：`repository_facade.py` 里 `_repository_from_weakref(...).<名字>` 接缝（名字须仍在 `RepositoryFacade` 上；经 `._runtime.<组件>.<成员>` 的接缝，成员须仍在组件类上，S2 批 C 起；`_repository_from_weakref(...)` 的结果只许按这两种形态读、其后最多一次调用，赋给变量或海象、交给 `getattr`、下标、更深的属性链一律报错），以及 `backend/app`、`scripts` 中对 facade 形状接收者的字面量 `getattr`/`hasattr` 探测（名字须仍在某个 repository 类上，例外登记在 `DYNAMIC_FACADE_REFERENCE_EXEMPTIONS`，同样零松弛）。这两类调用方在名字消失后会静默降级而不是报错。普查脚本只统计字面量 `getattr`，而且只在名字还在 facade 上时才算；`hasattr` 探测和 weakref 接缝它从来看不到（例如 `incremental_fuse_source` 在 S2 批 C 之前于普查里显示为 test-only，但生产经 weakref 接缝调用它），所以这两类只能靠这道检查。检查同样看不到别名、`operator.attrgetter`、运行时拼出的名字和白名单外的接收者名，退役前仍要对名字做一次不限语法的 `grep`。
1a. **被当作协议交出去的 facade**：`backend/app` 里 `cast(P, repository())`，以及标注 `-> P` 并直接 `return repository()` 的访问器（今天是 `deps.py` 的 `memory_service`、`mcp_memory_repository`、`ask_state_repository`（S2 批 D 起交成只含 `answer_memory_source` 的 `AnswerMemorySourceRepository`）、`source_repository`、`ask_stream_repository` 和 `repository` 本身），承诺 facade 实现了 `P` 的每个方法。仓库不跑类型检查，退役一个仍被 `P` 声明的方法，调用方写出的 `service.<名字>(...)` 会在运行时 AttributeError。`check_architecture_boundaries.py` 的 `facade_protocol_binding_violations` 对这些协议（含其协议基类）逐个比对 facade：退役时要么同时从协议里删掉该方法（并零松弛下调 `ports_protocol_method_count`），要么不退役。守卫上线前就存在的缺口按协议登记在基线 `facade_protocol_binding_gaps`（S2 批 A 登记时 `AskStateStorePort` 20 个、`McpMemoryRepository` 4 个、`NotebookRepository` 20 个；S2 批 D 后只剩 `McpMemoryRepository` 4 个、`NotebookRepository` 19 个；S2 批 E 后 `NotebookRepository` 6 个；S2 批 F 后只剩 `NotebookRepository` 的 `rerank` 1 个；S2-A 后清零，名单为空），同样零松弛：新增缺口报错，缺口补上后必须从名单删除。
2. **`function_length_ceiling`**（同一 baseline 文件）里登记了 `RepositoryFacade.__init__`（468 行）与 `RepositoryRuntime.__init__`（443 行）的行数上限——只在**削减** `__init__` 本身逻辑时才相关（例如若退役导致某个 host 参数/组件不再需要在 `__init__` 里接线），退役单个委托方法本身不触碰 `__init__`，通常无需改动，但若退役连带清理了 runtime 组件的构造代码，须核实是否需要相应下调（只许降、降了必须同步 baseline，见 AGENTS.md「Architecture Baseline」)。
3. **`backend/tests/test_repository_api_contract.py`**——对比 `facade_surface.json`/`api_contract.json` 等固化的响应快照（`FIXTURE` 常量指向 `backend/tests/fixtures/repository_contract/*.json`），退役面向 API 的方法（尤其是有路由直接调用的）需要重跑 `scripts/generate_repository_contract_fixtures.py`（默认模式，非 `--rebaseline*`）刷新快照；纯内部/无路由消费的方法退役通常不影响这份契约。
4. **`backend/tests/test_repository_dependency_contract.py`**——消费 `repository_callers.py` 的 `private_repository_sites()`/`production_source_index()`，并维护一份手工的 `LIFECYCLE_STORE_CALLS` 允许清单（哪些内部服务允许绕过 facade 直调 store 方法）。退役 facade 方法本身**不会**触发这份契约变红（它检查的是私有属性访问边界，不是 facade 公开方法存在与否），但若退役后某个内部服务需要改为直调 runtime 组件（例如把测试从 `repo.foo()` 改成 `repo._runtime.<component>.foo()`），可能需要在 `LIFECYCLE_STORE_CALLS` 里登记新的允许项。
5. **`backend/tests/test_repository_protocol_coverage.py`**——覆盖 `AskCandidatePort`/`AskGraphPort`/`AskStreamPort`/`RetrievalPort` 等 consumer-owned Protocol 的方法签名一致性，与 `BUNDLE_STORE_SEATS` 无关的 facade 方法退役通常不触碰它；仅当退役的方法恰好是这几个 Protocol 声明的一部分时才需要同步检查。
6. **`backend/tests/architecture/facade_contract.py::RUNTIME_COMPONENT_OWNERS`/`OWNER_CONTRACT_EXCEPTIONS`/`MODULE_SURFACE_OWNER_EXCEPTIONS`**——若退役后某个 `_runtime.<component>` 分量因此不再被任何公开方法引用，不必清理这份映射表（它描述的是"组件名→owner 类名"的静态字典，不因某个方法消失而失效），但若整个组件本身也一并退役（超出本阶段范围），需要一并清理。
7. **`docs/product-and-api*.md` / `AGENTS.md` / `README*.md`**（`CLAUDE.md` 已不在常规同步集合里，判据见 `AGENTS.md`「Documentation Sync」）——若退役的方法在这些文档里被点名提及（当前未发现任何 `retire-now`/`ambiguous` 候选被点名，已用 `grep` 核实），无需同步；若后续退役 `test-only` 桶里的方法时发现文档提及，需按"文档同步"红线一并更新。

## 退役 PR 建议顺序

**第一批（`retire-now`，4 个，最高置信度，建议随首个退役 PR 一起处理）—— 已完成（B5 第二阶段）：**

| 方法 | 委托目标 | 证据摘要 |
|---|---|---|
| `add_knowhow_rows` | `_runtime.knowhow_store.add_knowhow_rows` | Protocol 声明 + 双后端 store 实现均存在，但生产/脚本/测试三桶及撞名调用均为零；生产批量写路径已改用 `append_knowhow_rows`（`backend/app/services/knowhow/api.py` 974 行附近注释确认了这次历史替换，虽然注释字面指向的是另一条不可达分支，但佐证了同一次重构）。 |
| `set_knowhow_row_projection` | `_runtime.knowhow_store.set_knowhow_row_projection` | 同上，Protocol+双后端实现存在但零调用；生产路径已改用 CAS 变体 `set_knowhow_row_projection_if_table_seq`（`backend/app/services/knowhow/projection.py` 多处），本方法是被替换前的旧接口。 |
| `retrieval_experience_jobs`（property） | `_runtime.retrieval_experience_jobs` | `RepositoryRuntime` 内部（`repository_runtime.py`）对同名属性的引用是 `self.retrieval_experience_jobs`（`self`=`RepositoryRuntime` 实例），不经 facade；facade 这层 property 转发无任何外部读者，含裸属性读取在内。 |
| `search_profile_jobs`（property） | `_runtime.search_profile_jobs` | 与上一行同构。 |

**注意**：退役前仍需对这 4 个各手动跑一次不限属性调用语法的宽松 `grep -rn '\bmethod_name\b'`（见上文风险评估），排除脚本未覆盖的动态分派形态（本次已对全部 4 个做过这一步人工复核，结果与账本一致：均无遗漏引用，`retrieval_experience_jobs`/`search_profile_jobs` 命中的全部 10/9 处要么是 `RepositoryRuntime` 自身引用要么是测试文件里的**字符串字面量**断言源码片段，非真实调用）。

**第二批（`test-only`，69 个，次高优先级）**：建议按主题分组分批处理，而不是一次性全删（每组通常对应同一个 `deps.py` 窄口径旁路子系统，删除时应同时评估对应测试是否该删除、改写为直打 `_runtime.<component>`、或保留作为 runtime 组件自身的单测）。**注**：`notebook_copy_stats`、`decided_seed_pairs`、`relations_for_notebook`、`set_conflict_status`、`concept_whitelist_terms`、`maybe_auto_index` 这 6 个已随 facade 内部自布线判据（见「方法论摘要」第 5 点）改判 `keep`，不再出现在下面各组：
- **sharing/groups**（14 个）：`add_member`、`answer_owner`(ambiguous,见下)、`conversation_owner`、`copy_notebook`、`find_notebook_by_share_token`、`is_member`、`join_shared`、`kick_all_members`、`leave_notebook`(ambiguous)、`list_members`、`remove_member`、`share_notebook`、`unshare_notebook`、`user_can_admin_notebook`、`user_can_read_answer`、`user_can_read_source` —— 对应 `notebook_sharing_repository()`/`notebook_access_repository()`/`group_repository()` 旁路。
- **identity/auth**（6 个）：`authenticate_user`、`create_session`、`create_user`、`delete_session`、`global_document_limit_default`、`user_document_limit_override` —— 对应 `identity_repository()` 旁路。
- **queries/usage**（4 个）：`list_user_activity`、`list_user_notebooks`、`list_user_usage`、`notebook_exists_for_owner` —— 对应 `admin_query_repository()` 旁路。
- **KG lifecycle 批量操作**（约 19 个）：`append_clusters`、`apply_conflict_resolution`、`decided_pairs`、`get_community_reports`、`get_conflict_candidate`、`incremental_fuse_source`、`list_communities`、`rebuild_canonical_relations`、`rebuild_communities`、`rebuild_mention_bridge`、`rebuild_notebook_kg`、`reject_promotion`、`relink_notebook_kg`、`set_merge_decision`、`summarize_communities`、`write_clusters`、`write_conflict_candidate`、`write_merge_candidate`、`approve_promotion` —— 这批多数目前**只**在测试中直调 facade，需要单独确认是否有内部 CLI/job 路径通过其他窄接收者调用（本账本的"撞名未解析"计数对这批普遍非零，建议退役前抽样核实几个而非全信任 `test-only` 分类）。
- **knowhow 历史/杂项**（约 6 个）：`knowhow_history_head_seq`、`list_knowhow_changes`、`list_knowhow_milestones`、`memory_revisions`、`source_asset_ids`、`bump_knowhow_mutation_seq`(ambiguous)、`set_knowhow_hidden_source`(ambiguous)。
- **scale index/viz**（约 3 个）：`build_viz_index`、`fold_scale_index_delta`、`scale_ppr`。
- **KG 统计只读**（4 个）：`kg_cluster_size_histogram`、`kg_community_overview`、`kg_largest_clusters`、`kg_relation_provenance_counts`。
- **其余零散**（约 16 个，见完整表格逐条核对）：`ask_chunk`、`ask_graph`、`ask_job_status`、`ask_reasoning`、`backfill_chunk_fts`、`backfill_kg_fts`、`federated_retrieve`、`federated_retrieve_relations`、`get_paper_meta`、`list_notebook_templates`(ambiguous)、`paper_meta_backfill_progress`、`paper_meta_backfilling`、`retrieval_experiences`（property，与第一批两个 `retire-now` property 同源但本身有 22 处测试调用，未落入 retire-now）。

**第三批（`ambiguous`，15 个，需人工确认接收者身份后才能归类，不建议在本阶段之后立即退役）**：`answer_owner`、`bump_knowhow_mutation_seq`、`leave_notebook`、`list_notebook_templates`、`load_notebook_scale_facts`、`pending_actions_projection_rows`、`report_source_identity_rows`、`report_source_rows`、`set_knowhow_hidden_source`、`share_state`、`source_notebook_id`、`source_owner`、`validate_reasoning_submission`、`visible_source_count`、`visible_source_ids`。这批的"撞名未解析"调用点集中在两类：(a) 各 Service 类内部对自己私有存储引用的同名方法（如 `NotebookSharingService` 内 `self._store.source_owner(...)`，与 facade 无关，核实后可下沉为 `test-only` 甚至 `retire-now`）；(b) 脚本白名单尚未覆盖的接收者命名模式。**建议下一阶段先对这 15 个逐一跑精确 `grep` 定位接收者，而不是批量假设。**

## 完整表格（257 行，按方法名字母序；数据来自 S2 批 B 退役后的一次运行，参见文首重新生成命令）
| 方法 | 行号 | 委托目标 | 分类(kind) | 生产调用数 | facade内部 | 脚本调用数 | 测试调用数 | 撞名未解析 | wrapper镜像 | 档 |
|---|---|---|---|---|---|---|---|---|---|---|
| `add_knowhow_column` | 4184 | `_runtime.knowhow_store.add_knowhow_column` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `add_knowhow_row` | 4063 | `_runtime.knowhow_store.add_knowhow_row` | delegate | 1 | 0 | 0 | 33 | 82 | - | **keep** |
| `add_url_sources` | 1757 | `_runtime.source_ingestion.add_url_sources_compat` | adapter | 2 | 0 | 0 | 20 | 1 | - | **keep** |
| `agent_memory_hits` | 1349 | `_runtime.memory_retriever.agent_memory_hits` | delegate | 1 | 0 | 0 | 0 | 8 | - | **keep** |
| `agent_observations` (property) | 1108 | `_runtime.agent_observations` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `agent_profile` (property) | 1034 | `_runtime.agent_profile` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `agent_profile_jobs` (property) | 1076 | `_runtime.agent_profile_jobs` | delegate | 0 | 0 | 0 | 0 | 1 | - | **keep** |
| `agent_token_secret` | 1328 | `_runtime.memory_service.agent_token_secret` | delegate | 1 | 0 | 0 | 0 | 11 | - | **keep** |
| `all_visible_source_ids` | 1563 | `_runtime.source_store.all_visible_source_ids` | delegate | 2 | 0 | 0 | 1 | 34 | - | **keep** |
| `answer_memory_links` | 1435 | `_runtime.memory_service.answer_memory_links` | delegate | 1 | 0 | 0 | 0 | 4 | - | **keep** |
| `answer_memory_source` | 1361 | `_runtime.ask_state.answer_memory_source` | delegate | 2 | 0 | 0 | 0 | 3 | - | **keep** |
| `answer_owner` | 1493 | `_runtime.sharing.answer_owner` | delegate | 0 | 0 | 0 | 0 | 3 | - | **ambiguous** |
| `append_ask_trace` | 3701 | `_runtime.ask_component.append_trace_fail_open` | delegate | 0 | 0 | 1 | 9 | 0 | - | **keep** |
| `append_knowhow_rows` | 4071 | `_runtime.knowhow_store.append_knowhow_rows` | delegate | 1 | 0 | 0 | 2 | 5 | - | **keep** |
| `approve_promotion_as_reviewer` | 2827 | `_runtime.knowledge_governance.approve_promotion` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `ask` | 3415 | `_runtime.ask_component.ask_current` | delegate | 2 | 0 | 6 | 62 | 86 | - | **keep** |
| `ask_answer_detail` | 3727 | `_runtime.ask_state.ask_answer_detail` | delegate | 1 | 0 | 0 | 1 | 6 | - | **keep** |
| `ask_job_detail` | 3723 | `_runtime.ask_state.ask_job_detail` | delegate | 4 | 0 | 2 | 10 | 10 | - | **keep** |
| `ask_job_origin` | 3717 | `_runtime.ask_state.ask_job_origin` | delegate | 2 | 0 | 0 | 2 | 9 | - | **keep** |
| `ask_with_job` | 3436 | `_runtime.ask_component.ask_current_with_job` | delegate | 2 | 0 | 0 | 1 | 0 | - | **keep** |
| `audit_labels_for_user_ids` | 827 | `_runtime.identity.audit_labels_for_user_ids` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `backfill_paper_metadata` | 4157 | `_runtime.source_ingestion.backfill_paper_metadata` | delegate | 1 | 0 | 0 | 4 | 9 | - | **keep** |
| `begin_ask_job` | 3665 | `_runtime.ask_component.begin_job_current` | delegate | 0 | 0 | 1 | 16 | 0 | - | **keep** |
| `build_notebook_kg` | 1811 | `_runtime.knowledge_lifecycle.build_notebook_kg` | adapter | 2 | 0 | 0 | 22 | 2 | - | **keep** |
| `build_scale_index` | 3079 | `_runtime.scale_artifacts.build` | adapter | 4 | 0 | 2 | 156 | 3 | - | **keep** |
| `bulk_delete_conversations` | 3766 | `_runtime.ask_component.bulk_delete_conversations_current` | delegate | 1 | 0 | 0 | 3 | 5 | - | **keep** |
| `bulk_delete_memories` | 1415 | `_runtime.memory_service.bulk_delete` | delegate | 1 | 0 | 0 | 3 | 5 | - | **keep** |
| `bump_knowhow_mutation_seq` | 4127 | `_runtime.knowhow_store.bump_knowhow_mutation_seq` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `cancel_ask_job` | 3684 | `_runtime.ask_component.cancel_job` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `cancel_scale_index` | 3166 | `_runtime.scale_artifacts.cancel` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `chat` | 842 | `_runtime.models.chat` | delegate | 1 | 0 | 2 | 0 | 131 | - | **keep** |
| `claim_report_generation` | 3866 | `_runtime.report_store.claim_report_generation` | adapter | 1 | 0 | 0 | 16 | 9 | - | **keep** |
| `claim_report_intent` | 3859 | `_runtime.report_store.claim_report_intent` | delegate | 1 | 0 | 1 | 7 | 1 | - | **keep** |
| `close` | 998 | `_runtime.close` | delegate | 6 | 0 | 9 | 247 | 503 | postgres:override | **keep** |
| `cluster_map` | 2421 | `retrieval.candidates.cluster_map` | delegate | 1 | 1 | 0 | 81 | 8 | - | **keep** |
| `collection_catalog` (property) | 3272 | `_runtime.collection_catalog` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `collection_enumeration` (property) | 3282 | `_runtime.collection_enumeration` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `command_catalog` (property) | 1066 | `_runtime.command_catalog` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `concept_detail` | 2737 | `_runtime.knowledge_query.concept_detail` | delegate | 1 | 0 | 0 | 62 | 0 | - | **keep** |
| `concept_whitelist_add` | 2543 | `_runtime.knowledge_governance.concept_whitelist_add` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_list` | 2540 | `_runtime.knowledge_governance.concept_whitelist_list` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_remove` | 2546 | `_runtime.knowledge_governance.concept_whitelist_remove` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_terms` | 2537 | `_runtime.knowledge_governance.concept_whitelist_terms` | delegate | 0 | 1 | 0 | 4 | 4 | - | **keep** |
| `configured` | 845 | `_runtime.models.configured` | delegate | 13 | 0 | 2 | 6 | 82 | - | **keep** |
| `confirm_conflict` | 2484 | `_runtime.knowledge_governance.confirm_conflict` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `confirm_memory` | 1398 | `_runtime.memory_service.confirm` | delegate | 1 | 0 | 0 | 18 | 4 | - | **keep** |
| `confirm_merge` | 2445 | `_runtime.knowledge_governance.confirm_merge` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `conflict_resolution_admitted` | 2291 | `_runtime.knowledge_lifecycle.conflict_resolution_admitted` | delegate | 1 | 0 | 0 | 4 | 2 | - | **keep** |
| `conversation_creator` | 3784 | `_runtime.ask_state.conversation_creator` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `conversation_share_state` | 3809 | `_runtime.ask_state.conversation_share_state` | delegate | 2 | 0 | 0 | 3 | 27 | - | **keep** |
| `create_agent_profile` | 1255 | `_runtime.memory_service.create_agent_profile` | delegate | 1 | 0 | 2 | 10 | 42 | - | **keep** |
| `create_knowhow_milestone` | 4283 | `_runtime.knowhow_history_store.create_milestone` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `create_knowhow_table` | 4032 | `_runtime.knowhow_store.create_knowhow_table` | delegate | 1 | 0 | 0 | 40 | 29 | - | **keep** |
| `create_knowhow_table_with_rows` | 4041 | `_runtime.knowhow_store.create_knowhow_table_with_rows` | delegate | 1 | 0 | 0 | 0 | 5 | - | **keep** |
| `create_memory_candidate` | 1364 | `_runtime.memory_service.create_candidate` | delegate | 1 | 0 | 0 | 33 | 5 | - | **keep** |
| `create_memory_from_answer` | 1382 | `_runtime.memory_service.create_from_answer` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `create_notebook` | 1218 | `_runtime.catalog.create_notebook` | delegate | 2 | 0 | 9 | 1948 | 91 | - | **keep** |
| `create_notebook_object_schema` | 2186 | `_runtime.schema_registry.create_notebook_object_schema` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `create_object_schema` | 2212 | `_runtime.schema_registry.create_object_schema` | delegate | 1 | 0 | 1 | 5 | 0 | - | **keep** |
| `create_report` | 3837 | `_runtime.report_application.create_report` | delegate | 1 | 0 | 2 | 70 | 29 | - | **keep** |
| `current_user` | 818 | `_runtime.identity.current_user` | delegate | 19 | 1 | 1 | 90 | 20 | - | **keep** |
| `decided_seed_pairs` | 2528 | `_runtime.knowledge_governance.decided_seed_pairs` | delegate | 0 | 1 | 0 | 3 | 2 | - | **keep** |
| `delete_agent_profile` | 1277 | `_runtime.memory_service.delete_agent_profile` | delegate | 1 | 0 | 0 | 0 | 9 | - | **keep** |
| `delete_conversation` | 3763 | `_runtime.ask_state.delete_conversation` | delegate | 1 | 0 | 1 | 1 | 7 | - | **keep** |
| `delete_knowhow_cell_code` | 4236 | `_runtime.knowhow_store.delete_knowhow_cell_code` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `delete_knowhow_column` | 4206 | `_runtime.knowhow_store.delete_knowhow_column` | delegate | 1 | 0 | 0 | 5 | 1 | - | **keep** |
| `delete_knowhow_milestone` | 4290 | `_runtime.knowhow_history_store.delete_milestone` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `delete_knowhow_row` | 4213 | `_runtime.knowhow_store.delete_knowhow_row` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `delete_knowhow_table` | 4119 | `_runtime.knowhow_store.delete_knowhow_table` | delegate | 1 | 0 | 0 | 1 | 9 | - | **keep** |
| `delete_memory` | 1412 | `_runtime.memory_service.delete` | delegate | 1 | 0 | 0 | 8 | 4 | - | **keep** |
| `delete_notebook` | 1249 | `_runtime.catalog.delete_notebook` | delegate | 1 | 0 | 1 | 9 | 4 | - | **keep** |
| `delete_notebook_kg` | 1496 | `_runtime.knowledge_lifecycle.delete_notebook_kg` | delegate | 0 | 0 | 1 | 60 | 23 | - | **keep** |
| `delete_notebook_object_schema` | 2205 | `_runtime.schema_registry.delete_notebook_object_schema` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `delete_object_schema` | 2220 | `_runtime.schema_registry.delete_object_schema` | delegate | 1 | 0 | 2 | 3 | 0 | - | **keep** |
| `delete_report` | 3899 | `_runtime.report_store.delete_report` | delegate | 1 | 0 | 0 | 2 | 3 | - | **keep** |
| `delete_source` | 1973 | `_runtime.source_ingestion.delete_source_compat` | delegate | 2 | 0 | 1 | 21 | 1 | - | **keep** |
| `delete_source_asset_rows` | 4146 | `_runtime.knowhow_store.delete_source_asset_rows` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `deprecate_memory` | 1409 | `_runtime.memory_service.deprecate` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `effective_document_limit` | 833 | `_runtime.identity.effective_document_limit` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `effective_schemas` | 2171 | `_runtime.schema_registry.effective_schemas` | delegate | 0 | 0 | 3 | 13 | 3 | - | **keep** |
| `evidence_elements` | 1960 | `_runtime.source_store.evidence_elements` | delegate | 1 | 0 | 0 | 0 | 9 | - | **keep** |
| `execute_indexing_pipeline_rebuild` | 1618 | `<complex>` | adapter | 0 | 1 | 0 | 0 | 0 | - | **keep** |
| `execute_notebook_kg_job` | 4333 | `_runtime.knowledge_lifecycle.execute_notebook_kg_job` | delegate | 3 | 0 | 0 | 36 | 1 | - | **keep** |
| `export_reports` | 3902 | `_runtime.report_store.export_reports` | delegate | 1 | 0 | 0 | 2 | 1 | - | **keep** |
| `extract_source` | 1976 | `_runtime.source_ingestion.run_extraction` | adapter | 3 | 0 | 0 | 3 | 2 | - | **keep** |
| `fail_conflict_resolution_submission` | 2315 | `_runtime.knowledge_lifecycle.fail_conflict_resolution_submission` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `fail_kg_delete_submission` | 2731 | `_runtime.knowledge_lifecycle.fail_kg_delete_submission` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `fail_notebook_kg_job_submission` | 4328 | `_runtime.knowledge_lifecycle.fail_notebook_kg_job_submission` | delegate | 3 | 0 | 0 | 0 | 0 | - | **keep** |
| `fail_notebook_relink_submission` | 2283 | `_runtime.knowledge_lifecycle.fail_notebook_relink_submission` | delegate | 1 | 0 | 0 | 3 | 1 | - | **keep** |
| `fail_unified_kg_rebuild_submission` | 2707 | `_runtime.knowledge_lifecycle.fail_unified_kg_rebuild_submission` | delegate | 1 | 0 | 0 | 6 | 1 | - | **keep** |
| `find_duplicates` | 2897 | `_runtime.knowledge_governance.find_duplicates` | delegate | 1 | 0 | 1 | 7 | 0 | - | **keep** |
| `finish_ask_job` | 3676 | `_runtime.ask_component.finish_job` | delegate | 0 | 0 | 1 | 9 | 0 | - | **keep** |
| `fold_scale_index_delta` | 3090 | `_runtime.scale_artifacts.fold` | adapter | 1 | 0 | 0 | 24 | 0 | - | **keep** |
| `get_conversation` | 3750 | `_runtime.ask_state.get_conversation` | delegate | 4 | 0 | 5 | 27 | 19 | - | **keep** |
| `get_knowhow_cell_code` | 4233 | `_runtime.knowhow_store.get_knowhow_cell_code` | delegate | 2 | 0 | 0 | 0 | 2 | - | **keep** |
| `get_knowhow_change` | 4258 | `_runtime.knowhow_history_store.get_change` | delegate | 2 | 0 | 0 | 2 | 0 | - | **keep** |
| `get_knowhow_row_location` | 4248 | `_runtime.knowhow_store.get_knowhow_row_location` | delegate | 6 | 0 | 0 | 0 | 0 | - | **keep** |
| `get_knowhow_table` | 4060 | `_runtime.knowhow_store.get_knowhow_table` | delegate | 15 | 0 | 1 | 159 | 60 | - | **keep** |
| `get_memory` | 1418 | `_runtime.memory_service.get` | delegate | 3 | 0 | 0 | 12 | 0 | - | **keep** |
| `get_notebook` | 1221 | `_runtime.catalog.get_notebook` | delegate | 28 | 1 | 11 | 121 | 63 | - | **keep** |
| `get_notebook_asset` | 4143 | `_runtime.knowhow_store.get_notebook_asset` | delegate | 5 | 0 | 0 | 42 | 12 | - | **keep** |
| `get_report` | 3883 | `_runtime.report_store.get_report` | delegate | 2 | 0 | 5 | 90 | 27 | - | **keep** |
| `get_source` | 1727 | `_runtime.source_store.get_source` | delegate | 5 | 0 | 4 | 110 | 49 | - | **keep** |
| `guarded_ask_detail` | 3732 | `_runtime.ask_state.guarded_ask_detail` | delegate | 1 | 0 | 0 | 8 | 8 | - | **keep** |
| `guarded_report_detail` | 3886 | `_runtime.report_store.guarded_report_detail` | delegate | 1 | 0 | 0 | 13 | 18 | - | **keep** |
| `hidden_source_ids` | 1710 | `_runtime.source_store.hidden_source_ids` | delegate | 1 | 0 | 0 | 6 | 27 | - | **keep** |
| `import_sources` | 1753 | `_runtime.source_ingestion.import_sources_compat` | adapter | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `index_status` | 3115 | `_runtime.scale_artifacts.index_status` | delegate | 1 | 0 | 0 | 5 | 0 | - | **keep** |
| `indexing_pipeline_options` | 1567 | `_runtime.indexing_pipeline.projection` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `insert_notebook_asset` | 4130 | `_runtime.knowhow_store.insert_notebook_asset` | delegate | 2 | 0 | 0 | 2 | 5 | - | **keep** |
| `issue_agent_token` | 1280 | `_runtime.memory_service.issue_agent_token` | delegate | 1 | 0 | 3 | 18 | 14 | - | **keep** |
| `kg_analysis` (property) | 1052 | `_runtime.kg_analysis` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `kg_delete_status` | 2721 | `_runtime.knowledge_lifecycle.kg_delete_status` | delegate | 1 | 0 | 0 | 13 | 1 | - | **keep** |
| `kg_neighbors` | 2632 | `_runtime.knowledge_lifecycle.kg_neighbors` | delegate | 1 | 0 | 0 | 49 | 17 | - | **keep** |
| `kg_search` | 1535 | `_runtime.knowledge_query.search` | delegate | 1 | 0 | 0 | 19 | 1 | - | **keep** |
| `knowhow_cell_history` | 4268 | `_runtime.knowhow_history_store.cell_history` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowhow_changes_between` | 4261 | `_runtime.knowhow_history_store.changes_between` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowhow_history_page` | 4251 | `_runtime.knowhow_history_store.history_page` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowledge_graph` | 2230 | `_runtime.knowledge_query.graph` | delegate | 1 | 0 | 3 | 8 | 0 | - | **keep** |
| `knowledge_types` | 2132 | `_runtime.knowledge_query.knowledge_types` | delegate | 1 | 0 | 4 | 2 | 0 | - | **keep** |
| `list_agent_profiles` | 1262 | `_runtime.memory_service.list_agent_profiles` | delegate | 3 | 0 | 0 | 0 | 6 | - | **keep** |
| `list_agent_tokens` | 1298 | `_runtime.memory_service.list_agent_tokens` | delegate | 1 | 0 | 0 | 0 | 18 | - | **keep** |
| `list_conversations` | 3755 | `_runtime.ask_component.list_conversations_current` | delegate | 1 | 0 | 3 | 12 | 16 | - | **keep** |
| `list_knowhow_cell_code` | 4244 | `_runtime.knowhow_store.list_knowhow_cell_code` | delegate | 4 | 0 | 0 | 0 | 0 | - | **keep** |
| `list_knowhow_tables` | 4057 | `_runtime.knowhow_store.list_knowhow_tables` | delegate | 1 | 0 | 1 | 37 | 7 | - | **keep** |
| `list_knowledge` | 2146 | `_runtime.knowledge_query.list_knowledge` | delegate | 1 | 0 | 14 | 18 | 0 | - | **keep** |
| `list_memories` | 1421 | `_runtime.memory_service.list_memories` | delegate | 2 | 0 | 0 | 2 | 16 | - | **keep** |
| `list_notebook_bases` | 1233 | `_runtime.notebook_store.list_mount_edges_for_notebook` | delegate | 3 | 0 | 0 | 12 | 0 | - | **keep** |
| `list_notebook_object_schemas` | 2179 | `_runtime.schema_registry.list_notebook_object_schemas` | delegate | 1 | 0 | 0 | 2 | 3 | - | **keep** |
| `list_notebooks` | 1215 | `_runtime.catalog.list_notebooks` | delegate | 0 | 0 | 1 | 16 | 1 | - | **keep** |
| `list_object_schemas` | 2176 | `_runtime.schema_registry.list_object_schemas` | delegate | 1 | 0 | 3 | 1 | 0 | - | **keep** |
| `list_promotion_queue` | 2820 | `_runtime.knowledge_governance.list_promotion_queue` | delegate | 1 | 0 | 0 | 15 | 0 | - | **keep** |
| `list_reports` | 3894 | `_runtime.report_store.list_reports` | delegate | 1 | 0 | 1 | 5 | 0 | - | **keep** |
| `list_sources` | 1548 | `_runtime.source_ingestion.list_sources` | delegate | 0 | 0 | 3 | 47 | 8 | - | **keep** |
| `list_sources_page` | 1551 | `_runtime.source_ingestion.list_sources_page` | delegate | 3 | 0 | 0 | 14 | 8 | - | **keep** |
| `load_notebook_scale_facts` | 839 | `_runtime.queries.load_notebook_scale_facts` | delegate | 0 | 0 | 0 | 0 | 2 | - | **ambiguous** |
| `mark_notebook_base` | 1227 | `_runtime.catalog.mark_notebook_base` | delegate | 0 | 0 | 1 | 173 | 2 | - | **keep** |
| `maybe_auto_index` | 3174 | `_runtime.scale_artifacts.maybe_auto_index` | delegate | 0 | 1 | 0 | 13 | 8 | - | **keep** |
| `memory_kg_eligible` | 1390 | `_runtime.source_ingestion.memory_kg_eligible` | delegate | 3 | 0 | 0 | 2 | 6 | - | **keep** |
| `merge_knowledge` | 2906 | `_runtime.knowledge_governance.merge_knowledge` | delegate | 1 | 0 | 1 | 11 | 0 | - | **keep** |
| `merge_review_job_status` | 2517 | `_runtime.knowledge_governance.merge_review_job_status` | delegate | 2 | 0 | 0 | 4 | 9 | - | **keep** |
| `mountable_notebooks` | 1236 | `_runtime.notebook_store.mountable_for_notebook` | delegate | 2 | 0 | 0 | 7 | 2 | - | **keep** |
| `mounted_by_count` | 1239 | `_runtime.notebook_store.mounted_by_count_for_notebook` | delegate | 1 | 0 | 0 | 6 | 2 | - | **keep** |
| `node_context` | 2770 | `_runtime.knowledge_query.node_context` | delegate | 1 | 0 | 1 | 63 | 43 | - | **keep** |
| `notebook_analytics` | 3951 | `_runtime.catalog.notebook_analytics` | delegate | 0 | 0 | 1 | 13 | 4 | - | **keep** |
| `notebook_copy_stats` | 1458 | `_runtime.scale_artifacts.notebook_copy_stats` | delegate | 0 | 2 | 0 | 15 | 26 | - | **keep** |
| `notebook_owner` | 836 | `_runtime.identity.notebook_owner` | delegate | 1 | 0 | 0 | 2 | 2 | - | **keep** |
| `notebook_relink_status` | 2272 | `_runtime.knowledge_lifecycle.notebook_relink_status` | delegate | 1 | 0 | 0 | 13 | 2 | - | **keep** |
| `parallelism` | 848 | `_runtime.models.parallelism` | delegate | 1 | 0 | 0 | 0 | 18 | - | **keep** |
| `parse_source` | 1885 | `_runtime.source_ingestion.parse_source_compat` | adapter | 2 | 0 | 2 | 1 | 1 | - | **keep** |
| `participant_notebook_ids` | 3557 | `_runtime.notebook_store.participant_notebook_ids` | delegate | 4 | 0 | 0 | 18 | 20 | - | **keep** |
| `pending_actions` | 3931 | `_runtime.pending_actions_service.list_for_user` | delegate | 3 | 0 | 0 | 27 | 0 | - | **keep** |
| `pending_conflicts` | 2470 | `_runtime.knowledge_governance.pending_conflicts` | delegate | 1 | 0 | 0 | 15 | 1 | - | **keep** |
| `pending_merges` | 2428 | `_runtime.knowledge_governance.pending_merges` | delegate | 1 | 0 | 0 | 16 | 2 | - | **keep** |
| `prepare_notebook_kg_job` | 4312 | `_runtime.knowledge_lifecycle.prepare_notebook_kg_job` | adapter | 3 | 0 | 0 | 53 | 9 | - | **keep** |
| `preview_reasoning_intent` | 3451 | `_runtime.ask_component.preview_reasoning_intent` | delegate | 3 | 0 | 0 | 0 | 1 | - | **keep** |
| `process_source` | 1851 | `_runtime.source_ingestion.process_source_compat` | adapter | 9 | 0 | 2 | 56 | 6 | - | **keep** |
| `propose_memory_promotion` | 1442 | `_runtime.memory_service.propose_promotion` | delegate | 1 | 0 | 0 | 29 | 2 | - | **keep** |
| `propose_promotion` | 2803 | `_runtime.knowledge_governance.propose_promotion` | delegate | 1 | 0 | 0 | 52 | 2 | - | **keep** |
| `propose_schemas` | 2223 | `_runtime.schema_registry.propose_schemas` | delegate | 1 | 0 | 1 | 10 | 0 | - | **keep** |
| `prune_knowhow_history` | 4295 | `_runtime.knowhow_history_store.prune` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `public_conversation_by_token` | 3824 | `_runtime.ask_state.public_conversation_by_token` | delegate | 2 | 0 | 0 | 0 | 35 | - | **keep** |
| `public_report_by_token` | 3921 | `_runtime.report_store.public_report_by_token` | delegate | 1 | 0 | 0 | 0 | 15 | - | **keep** |
| `rebuild_notebook_kg` | 1838 | `_runtime.knowledge_lifecycle.rebuild_notebook_kg` | adapter | 0 | 0 | 0 | 5 | 1 | - | **test-only** |
| `rebuild_unified_kg` | 2679 | `_runtime.knowledge_lifecycle.rebuild_unified_kg` | adapter | 4 | 0 | 2 | 277 | 14 | - | **keep** |
| `refresh_agent_principal` | 1339 | `_runtime.memory_service.refresh_agent_principal` | delegate | 3 | 0 | 0 | 0 | 5 | - | **keep** |
| `reject_conflict` | 2494 | `_runtime.knowledge_governance.reject_conflict` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `reject_memory` | 1406 | `_runtime.memory_service.reject` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `reject_merge` | 2453 | `_runtime.knowledge_governance.reject_merge` | delegate | 1 | 0 | 0 | 5 | 0 | - | **keep** |
| `reject_promotion_as_reviewer` | 2852 | `_runtime.knowledge_governance.reject_promotion` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `relations_for_notebook` | 2324 | `_runtime.knowledge_query.relations_for_notebook` | delegate | 0 | 1 | 0 | 20 | 14 | - | **keep** |
| `relink_notebook_kg` | 2261 | `_runtime.knowledge_lifecycle.relink_notebook_kg` | adapter | 0 | 0 | 0 | 20 | 3 | - | **test-only** |
| `rename_conversation` | 3760 | `_runtime.ask_state.rename_conversation` | delegate | 1 | 0 | 1 | 1 | 1 | - | **keep** |
| `rename_knowhow_column` | 4192 | `_runtime.knowhow_store.rename_knowhow_column` | delegate | 1 | 0 | 0 | 0 | 2 | - | **keep** |
| `replace_notebook_bases` | 1242 | `_runtime.notebook_store.replace_mounts` | delegate | 1 | 0 | 0 | 144 | 4 | - | **keep** |
| `report_execution` (property) | 3925 | `_runtime.report_execution` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `report_share_token` | 3918 | `_runtime.report_store.report_share_token` | delegate | 1 | 0 | 0 | 6 | 1 | - | **keep** |
| `report_source_identity_rows` | 1740 | `_runtime.source_store.report_source_identity_rows` | delegate | 0 | 0 | 0 | 0 | 4 | - | **ambiguous** |
| `report_source_rows` | 1730 | `_runtime.source_store.report_source_rows` | delegate | 0 | 0 | 0 | 0 | 12 | - | **ambiguous** |
| `require_agent_access` | 1342 | `_runtime.memory_service.require_agent_access` | delegate | 6 | 0 | 0 | 0 | 18 | - | **keep** |
| `require_indexing_pipeline_write` | 1706 | `_runtime.indexing_pipeline.require_write_admission` | delegate | 5 | 22 | 0 | 2 | 0 | - | **keep** |
| `resolve_agent_token` | 1331 | `_runtime.memory_service.resolve_agent_token` | delegate | 1 | 0 | 0 | 1 | 23 | - | **keep** |
| `resolve_agent_token_status` | 1334 | `_runtime.memory_service.resolve_agent_token_status` | delegate | 0 | 0 | 0 | 0 | 21 | - | **ambiguous** |
| `resolve_reasoning_followup` | 3472 | `_runtime.ask_component.resolve_reasoning_followup` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `resolve_session` | 824 | `_runtime.identity.resolve_session` | delegate | 2 | 0 | 0 | 8 | 57 | - | **keep** |
| `retrieval` (property) | 3267 | `_runtime.retrieval_component` | delegate | 0 | 55 | 0 | 0 | 6 | - | **keep** |
| `retrieval_experiences` (property) | 1088 | `_runtime.retrieval_experiences` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `revert_knowhow_table` | 4275 | `_runtime.knowhow_history_store.revert_to` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `review_pending_merges` | 2502 | `_runtime.knowledge_governance.review_pending_merges` | delegate | 1 | 0 | 0 | 10 | 1 | - | **keep** |
| `review_queue_page` | 2379 | `_runtime.knowledge_governance.review_queue_page` | delegate | 1 | 0 | 0 | 22 | 0 | - | **keep** |
| `revoke_agent_token` | 1303 | `_runtime.memory_service.revoke_agent_token` | delegate | 1 | 0 | 0 | 5 | 16 | - | **keep** |
| `run_conflict_resolution_job` | 2308 | `_runtime.knowledge_lifecycle.run_conflict_resolution_job` | adapter | 1 | 0 | 0 | 4 | 2 | - | **keep** |
| `run_kg_delete_job` | 2725 | `_runtime.knowledge_lifecycle.run_kg_delete_job` | delegate | 1 | 0 | 0 | 6 | 1 | - | **keep** |
| `run_merge_review_job` | 2520 | `_runtime.knowledge_governance.run_merge_review_job` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `run_notebook_relink_job` | 2276 | `_runtime.knowledge_lifecycle.run_notebook_relink_job` | adapter | 1 | 0 | 0 | 6 | 2 | - | **keep** |
| `run_unified_kg_rebuild_job` | 2700 | `_runtime.knowledge_lifecycle.run_unified_kg_rebuild_job` | adapter | 2 | 0 | 0 | 5 | 1 | - | **keep** |
| `scale_index_status` | 3111 | `_runtime.scale_artifacts.status` | delegate | 4 | 0 | 0 | 35 | 0 | - | **keep** |
| `search_notebook` | 2918 | `_runtime.catalog.search_notebook` | delegate | 1 | 0 | 3 | 15 | 7 | - | **keep** |
| `set_conflict_status` | 2474 | `_runtime.knowledge_governance.set_conflict_status` | delegate | 0 | 1 | 0 | 7 | 1 | - | **keep** |
| `set_edge_review` | 2390 | `_runtime.knowledge_governance.set_edge_review` | delegate | 1 | 0 | 0 | 32 | 2 | - | **keep** |
| `set_indexing_pipeline` | 1571 | `<complex>` | adapter | 1 | 0 | 0 | 5 | 0 | - | **keep** |
| `set_knowhow_anchor_column` | 4176 | `_runtime.knowhow_store.set_knowhow_anchor_column` | delegate | 1 | 0 | 0 | 3 | 3 | - | **keep** |
| `set_knowhow_column_kind` | 4199 | `_runtime.knowhow_store.set_knowhow_column_kind` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `set_knowhow_hidden_source` | 4122 | `_runtime.knowhow_store.set_knowhow_hidden_source` | delegate | 0 | 0 | 0 | 0 | 2 | - | **ambiguous** |
| `set_notebook_personal` | 1230 | `_runtime.catalog.set_notebook_personal` | delegate | 0 | 0 | 2 | 12 | 1 | - | **keep** |
| `share_conversation` | 3795 | `_runtime.ask_state.share_conversation` | delegate | 1 | 0 | 0 | 0 | 65 | - | **keep** |
| `share_report` | 3908 | `_runtime.report_store.share_report` | delegate | 1 | 0 | 0 | 6 | 14 | - | **keep** |
| `share_state` | 1455 | `_runtime.sharing.share_state` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `shared_by_me` | 1464 | `_runtime.sharing.shared_by_me` | delegate | 0 | 0 | 1 | 1 | 2 | - | **keep** |
| `shared_preview` | 1461 | `_runtime.sharing.shared_preview` | delegate | 0 | 0 | 1 | 4 | 1 | - | **keep** |
| `source_elements` | 1915 | `_runtime.source_store.source_elements` | delegate | 2 | 1 | 10 | 23 | 15 | - | **keep** |
| `source_elements_page` | 1918 | `_runtime.source_store.source_elements_page` | delegate | 2 | 0 | 0 | 1 | 25 | - | **keep** |
| `source_id_by_hash` | 1931 | `_runtime.source_store.source_id_by_hash` | delegate | 1 | 0 | 0 | 0 | 14 | - | **keep** |
| `source_metadata` | 1945 | `_runtime.source_store.source_metadata` | delegate | 3 | 0 | 0 | 0 | 9 | - | **keep** |
| `source_notebook_id` | 1482 | `_runtime.sharing.source_notebook_id` | delegate | 1 | 0 | 0 | 0 | 14 | - | **keep** |
| `source_owner` | 1479 | `_runtime.sharing.source_owner` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `source_parse_busy` | 1856 | `<complex>` | adapter | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `sources_missing_paper_meta` | 4150 | `_runtime.source_store.sources_missing_paper_meta` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `start_ask_stream` | 4013 | `_runtime.ask_execution.start` | adapter | 2 | 0 | 0 | 1 | 0 | - | **keep** |
| `start_conflict_resolution` | 2297 | `_runtime.knowledge_lifecycle.start_conflict_resolution` | adapter | 1 | 0 | 0 | 14 | 2 | - | **keep** |
| `start_kg_delete` | 2717 | `_runtime.knowledge_lifecycle.start_kg_delete` | delegate | 1 | 0 | 0 | 11 | 1 | - | **keep** |
| `start_notebook_relink` | 2267 | `_runtime.knowledge_lifecycle.start_notebook_relink` | adapter | 1 | 0 | 0 | 23 | 5 | - | **keep** |
| `start_unified_kg_rebuild` | 2691 | `_runtime.knowledge_lifecycle.start_unified_kg_rebuild` | adapter | 2 | 0 | 0 | 22 | 1 | - | **keep** |
| `storage_dir` (property+setter) | 3995 | `_runtime.storage_dir` | delegate | 0 | 2 | 0 | 0 | 0 | - | **keep** |
| `store_kg` | 2252 | `_runtime.knowledge_lifecycle.store_kg` | delegate | 0 | 0 | 6 | 441 | 23 | - | **keep** |
| `submit_feedback` | 3948 | `_runtime.ask_state.submit_feedback` | delegate | 1 | 0 | 2 | 0 | 8 | - | **keep** |
| `transfer_memories` | 4299 | `_runtime.memory_service.transfer` | delegate | 1 | 0 | 0 | 28 | 0 | - | **keep** |
| `trigger_scale_index_rebuild` | 3147 | `_runtime.scale_artifacts.trigger` | adapter | 2 | 0 | 0 | 6 | 0 | - | **keep** |
| `unified_graph` | 2607 | `_runtime.knowledge_lifecycle.unified_graph` | delegate | 1 | 0 | 0 | 72 | 2 | - | **keep** |
| `unified_kg_rebuild_status` | 2696 | `_runtime.knowledge_lifecycle.unified_kg_rebuild_status` | delegate | 1 | 0 | 0 | 11 | 1 | - | **keep** |
| `unified_kg_status` | 2588 | `_runtime.knowledge_lifecycle.unified_kg_status` | delegate | 2 | 0 | 1 | 11 | 2 | - | **keep** |
| `unshare_conversation` | 3818 | `_runtime.ask_state.unshare_conversation` | delegate | 1 | 0 | 0 | 0 | 7 | - | **keep** |
| `unshare_report` | 3915 | `_runtime.report_store.unshare_report` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `update_agent_profile` | 1270 | `_runtime.memory_service.update_agent_profile` | delegate | 1 | 0 | 0 | 1 | 12 | - | **keep** |
| `update_agent_token_access` | 1308 | `_runtime.memory_service.update_agent_token_access` | delegate | 1 | 0 | 0 | 1 | 26 | - | **keep** |
| `update_knowhow_cell` | 4078 | `_runtime.knowhow_store.update_knowhow_cell` | delegate | 1 | 0 | 0 | 14 | 34 | - | **keep** |
| `update_knowhow_cells` | 4087 | `_runtime.knowhow_store.update_knowhow_cells` | delegate | 1 | 0 | 0 | 1 | 3 | - | **keep** |
| `update_knowhow_cells_bulk_guarded` | 4096 | `_runtime.knowhow_store.update_knowhow_cells_bulk_guarded` | delegate | 0 | 0 | 1 | 0 | 3 | - | **keep** |
| `update_knowhow_cells_guarded_atomic` | 4104 | `_runtime.knowhow_store.update_knowhow_cells_guarded_atomic` | delegate | 2 | 0 | 0 | 0 | 4 | - | **keep** |
| `update_knowhow_table_meta` | 4167 | `_runtime.knowhow_store.update_knowhow_table_meta` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `update_knowledge` | 2860 | `_runtime.knowledge_governance.update_knowledge` | delegate | 1 | 0 | 5 | 11 | 2 | - | **keep** |
| `update_memory` | 1393 | `_runtime.memory_service.update` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `update_notebook` | 1224 | `_runtime.catalog.update_notebook` | delegate | 0 | 0 | 1 | 3 | 1 | - | **keep** |
| `update_notebook_object_schema` | 2193 | `_runtime.schema_registry.update_notebook_object_schema` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `update_object_schema` | 2215 | `_runtime.schema_registry.update_object_schema` | delegate | 1 | 0 | 2 | 5 | 0 | - | **keep** |
| `update_report` | 3849 | `_runtime.report_store.update_report` | delegate | 3 | 0 | 1 | 67 | 64 | - | **keep** |
| `upload_sources` | 1772 | `_runtime.source_ingestion.upload_sources_compat` | adapter | 5 | 0 | 7 | 47 | 5 | - | **keep** |
| `upsert_knowhow_cell_code` | 4223 | `_runtime.knowhow_store.upsert_knowhow_cell_code` | delegate | 1 | 0 | 0 | 5 | 5 | - | **keep** |
| `user_can_access_notebook` | 1473 | `_runtime.sharing.user_can_access_notebook` | delegate | 1 | 0 | 0 | 15 | 9 | - | **keep** |
| `user_can_read_notebook` | 1476 | `_runtime.sharing.user_can_read_notebook` | delegate | 5 | 0 | 0 | 39 | 21 | - | **keep** |
| `validate_cell_target` | 4220 | `_runtime.knowhow_store.validate_cell_target` | delegate | 6 | 0 | 0 | 0 | 0 | - | **keep** |
| `validate_reasoning_submission` | 3464 | `_runtime.ask_component.validate_reasoning_submission` | delegate | 0 | 0 | 0 | 0 | 5 | - | **ambiguous** |
| `visible_document_count` | 830 | `_runtime.source_store.visible_document_count` | delegate | 1 | 0 | 0 | 9 | 3 | - | **keep** |
| `visible_source_count` | 1716 | `_runtime.source_store.visible_document_count` | delegate | 0 | 0 | 0 | 0 | 3 | - | **ambiguous** |
| `visible_source_ids` | 1559 | `_runtime.index_projections.visible_source_ids` | delegate | 0 | 0 | 0 | 0 | 3 | - | **ambiguous** |
| `visible_source_scope_snapshot` | 1719 | `_runtime.source_store.visible_source_scope_snapshot` | delegate | 1 | 0 | 0 | 0 | 6 | - | **keep** |
| `warm_open_path_caches` | 3954 | `_runtime.catalog.warm_open_path_caches` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
