# RepositoryFacade 公开方法退役账本（B5 结构项，第一阶段）

**状态**：`retire-now` 4 个已于早前 PR（B5 第二阶段）退役。随后修复了普查脚本的一处盲区——`RepositoryFacade.__init__` 把若干自己的公开方法用迟绑定 lambda 布线进 `RepositoryRuntime`（`self.notebook_copy_stats(...)` 这类），此前脚本整个跳过 facade 文件自身、看不到这批引用；修复后这类引用计入独立的「facade内部」列并折入生产计数，6 个方法（`notebook_copy_stats`、`decided_seed_pairs`、`relations_for_notebook`、`set_conflict_status`、`concept_whitelist_terms`、`maybe_auto_index`）从 `test-only` 改判 `keep`。当前快照：`test-only` 69 个与 `ambiguous` 15 个留待下一阶段（需要逐个决定是删测试还是把测试改打 runtime）。

**范围（本文件产出时，B5 第一阶段）**：只读普查，产出账本；该阶段不改动、不退役任何方法——实际退役动作记在上面的「状态」一行，属于随后的 B5 第二阶段。

**目标文件**：`backend/app/services/repository_facade.py::RepositoryFacade`，308 个公开成员（含 11 个 `@property` / 1 个 setter，`storage_dir` 的 getter+setter 合并计 1 行；4 个已退役方法已不再出现在这份快照里，见上方「状态」）。

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
| `test-only` | 69 | 生产/脚本/facade内部三桶零调用，仅测试文件调用 | 退役需连同其测试一起处理（删除或改为直打 runtime 组件），见下 |
| `ambiguous` | 15 | 四桶解析调用为零，但存在同名的 `<expr>.<method>(` 调用其接收者无法确认/排除——人工复核 | 不得直接退役；需人工确认接收者身份 |
| `keep` | 224 | 至少一处生产、脚本或 facade内部桶解析调用 | 保留 |
| **合计** | **308** | | |

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

1. **委托分类（delegate/adapter）**复用 `backend/tests/architecture/facade_contract.py` 的 `_function_contract_owner` 等 AST helper，与 `facade_delegate_evidence`（唯一被 `scripts/generate_repository_contract_fixtures.py` 消费、用于生成 `facade_surface.json` 的 owner 列）判定口径一致。**但发现一个需要澄清的事实**：该模块里的 `facade_body_violations`/`manifest_delegate_mismatches` 两个函数目前**没有被任何测试或脚本调用**——不是活的硬门，是休眠的校验逻辑。真正在跑的表面守卫是 `test_phase0_architecture_guard.py` 里的 `facade_surface_additions`，它只比对公开**名字集合**（对 `scripts/architecture_boundary_baseline.json::facade_public_surface` 的 `allowed_names`），不关心方法体是不是干净的单跳委托。当前 308 行里 3 个是 `adapter`（非单跳）：`source_parse_busy`、`federated_retrieve`、`claim_report_generation`（均已被生产/测试调用，不在退役候选里，只是提请注意它们不是纯委托）。
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

1. **`scripts/architecture_boundary_baseline.json::facade_public_surface.RepositoryFacade.allowed_names`**——被 `test_phase0_architecture_guard.py` 的 `facade_surface_additions` 消费，当前只拦"新增"（`assert ... == []`，即活跃表面相对 baseline 只能持平或减少）。**退役任何方法后必须显式从这份 `allowed_names` 数组里删除对应名字**，否则该名字会一直留在"历史允许"名单里，守卫不会报错但账本会失真（baseline 允许的名字集合不再等于活跃表面）。这是唯一会因退役而"变红"或需要主动更新的硬门——反过来说，这份 baseline 目前**没有**"退役压力"（删方法不会导致测试失败），必须靠人工同步。
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

## 完整表格（308 行，按方法名字母序；页数据来自本次运行，参见文首重新生成命令）
| 方法 | 行号 | 委托目标 | 分类(kind) | 生产调用数 | facade内部 | 脚本调用数 | 测试调用数 | 撞名未解析 | wrapper镜像 | 档 |
|---|---|---|---|---|---|---|---|---|---|---|
| `add_knowhow_column` | 4061 | `_runtime.knowhow_store.add_knowhow_column` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `add_knowhow_row` | 3934 | `_runtime.knowhow_store.add_knowhow_row` | delegate | 1 | 0 | 0 | 22 | 81 | - | **keep** |
| `add_member` | 1489 | `_runtime.sharing.add_member` | delegate | 0 | 0 | 0 | 50 | 22 | - | **test-only** |
| `add_relations` | 2084 | `_runtime.knowledge.add_relations_current` | delegate | 0 | 0 | 0 | 7 | 2 | - | **test-only** |
| `add_url_sources` | 1656 | `_runtime.source_ingestion.add_url_sources_compat` | delegate | 2 | 0 | 0 | 12 | 1 | - | **keep** |
| `agent_memory_hits` | 1325 | `_runtime.memory_retriever.agent_memory_hits` | delegate | 1 | 0 | 0 | 0 | 8 | - | **keep** |
| `agent_observations` (property) | 1117 | `_runtime.agent_observations` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `agent_profile` (property) | 1045 | `_runtime.agent_profile` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `agent_profile_jobs` (property) | 1087 | `_runtime.agent_profile_jobs` | delegate | 0 | 0 | 0 | 0 | 1 | - | **keep** |
| `all_visible_source_ids` | 1606 | `_runtime.source_store.all_visible_source_ids` | delegate | 1 | 0 | 0 | 1 | 2 | - | **keep** |
| `answer_memory_links` | 1411 | `_runtime.memory_service.answer_memory_links` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `answer_memory_source` | 1337 | `_runtime.ask_state.answer_memory_source` | delegate | 1 | 0 | 0 | 0 | 2 | - | **keep** |
| `answer_owner` | 1516 | `_runtime.sharing.answer_owner` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `append_ask_trace` | 3615 | `_runtime.ask_component.append_trace_fail_open` | delegate | 0 | 0 | 1 | 9 | 0 | - | **keep** |
| `append_clusters` | 2240 | `_runtime.knowledge_lifecycle.append_clusters` | delegate | 0 | 0 | 0 | 7 | 2 | - | **test-only** |
| `append_knowhow_rows` | 3942 | `_runtime.knowhow_store.append_knowhow_rows` | delegate | 1 | 0 | 0 | 2 | 5 | - | **keep** |
| `apply_conflict_resolution` | 2369 | `_runtime.knowledge_governance.apply_conflict_resolution` | delegate | 0 | 0 | 0 | 22 | 2 | - | **test-only** |
| `approve_promotion` | 2729 | `_runtime.knowledge_governance.approve_promotion` | delegate | 0 | 0 | 0 | 37 | 1 | - | **test-only** |
| `approve_promotion_as_reviewer` | 2736 | `_runtime.knowledge_governance.approve_promotion` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `ask` | 3357 | `_runtime.ask_component.ask_current` | delegate | 3 | 0 | 6 | 46 | 54 | - | **keep** |
| `ask_answer_detail` | 3634 | `_runtime.ask_state.ask_answer_detail` | delegate | 1 | 0 | 0 | 0 | 4 | - | **keep** |
| `ask_chunk` | 3346 | `_runtime.ask_component.ask_chunk_current` | delegate | 0 | 0 | 0 | 46 | 4 | - | **test-only** |
| `ask_graph` | 3528 | `_runtime.ask_component.ask_graph_current` | delegate | 0 | 0 | 0 | 11 | 3 | - | **test-only** |
| `ask_job_detail` | 3631 | `_runtime.ask_state.ask_job_detail` | delegate | 3 | 0 | 2 | 2 | 4 | - | **keep** |
| `ask_job_status` | 3612 | `_runtime.ask_state.ask_job_status` | delegate | 0 | 0 | 0 | 6 | 14 | - | **test-only** |
| `ask_reasoning` | 3516 | `_runtime.ask_component.ask_reasoning_current` | delegate | 0 | 0 | 0 | 4 | 21 | - | **test-only** |
| `audit_labels_for_user_ids` | 763 | `_runtime.identity.audit_labels_for_user_ids` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `authenticate_user` | 751 | `_runtime.identity.authenticate_user` | delegate | 0 | 0 | 0 | 5 | 18 | - | **test-only** |
| `backfill_chunk_fts` | 1540 | `_runtime.knowledge_query.backfill_chunk_fts` | delegate | 0 | 0 | 0 | 11 | 0 | - | **test-only** |
| `backfill_kg_fts` | 1531 | `_runtime.knowledge_query.backfill_kg_fts` | delegate | 0 | 0 | 0 | 1 | 0 | - | **test-only** |
| `backfill_paper_metadata` | 4034 | `_runtime.source_ingestion.backfill_paper_metadata` | delegate | 1 | 0 | 0 | 4 | 9 | - | **keep** |
| `begin_ask_job` | 3576 | `_runtime.ask_component.begin_job_current` | delegate | 0 | 0 | 1 | 15 | 0 | - | **keep** |
| `build_notebook_kg` | 1705 | `_runtime.knowledge_lifecycle.build_notebook_kg` | delegate | 2 | 0 | 0 | 21 | 2 | - | **keep** |
| `build_scale_index` | 2986 | `_runtime.scale_artifacts.build` | delegate | 3 | 0 | 1 | 81 | 2 | - | **keep** |
| `build_viz_index` | 3090 | `_runtime.scale_artifacts.build_viz` | delegate | 0 | 0 | 0 | 5 | 0 | - | **test-only** |
| `bulk_delete_conversations` | 3665 | `_runtime.ask_component.bulk_delete_conversations_current` | delegate | 1 | 0 | 0 | 3 | 4 | - | **keep** |
| `bulk_delete_memories` | 1391 | `_runtime.memory_service.bulk_delete` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `bump_knowhow_mutation_seq` | 3998 | `_runtime.knowhow_store.bump_knowhow_mutation_seq` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `cancel_ask_job` | 3595 | `_runtime.ask_component.cancel_job` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `cancel_scale_index` | 3063 | `_runtime.scale_artifacts.cancel` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `chat` | 817 | `_runtime.models.chat` | delegate | 1 | 0 | 1 | 0 | 97 | - | **keep** |
| `claim_report_generation` | 3758 | `_runtime.report_store.claim_report_generation` | adapter | 1 | 0 | 0 | 8 | 9 | - | **keep** |
| `claim_report_intent` | 3751 | `_runtime.report_store.claim_report_intent` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `close` | 973 | `_runtime.close` | delegate | 3 | 0 | 0 | 20 | 217 | postgres:override | **keep** |
| `cluster_map` | 2267 | `retrieval.candidates.cluster_map` | delegate | 1 | 1 | 0 | 47 | 8 | - | **keep** |
| `collection_catalog` (property) | 3180 | `_runtime.collection_catalog` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `collection_enumeration` (property) | 3190 | `_runtime.collection_enumeration` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `command_catalog` (property) | 1077 | `_runtime.command_catalog` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `concept_detail` | 2658 | `_runtime.knowledge_query.concept_detail` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `concept_whitelist_add` | 2462 | `_runtime.knowledge_governance.concept_whitelist_add` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_list` | 2459 | `_runtime.knowledge_governance.concept_whitelist_list` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_remove` | 2465 | `_runtime.knowledge_governance.concept_whitelist_remove` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `concept_whitelist_terms` | 2456 | `_runtime.knowledge_governance.concept_whitelist_terms` | delegate | 0 | 1 | 0 | 4 | 4 | - | **keep** |
| `configured` | 820 | `_runtime.models.configured` | delegate | 13 | 0 | 2 | 6 | 80 | - | **keep** |
| `confirm_conflict` | 2394 | `_runtime.knowledge_governance.confirm_conflict` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `confirm_memory` | 1374 | `_runtime.memory_service.confirm` | delegate | 1 | 0 | 0 | 12 | 2 | - | **keep** |
| `confirm_merge` | 2303 | `_runtime.knowledge_governance.confirm_merge` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `conflict_resolution_admitted` | 2126 | `_runtime.knowledge_lifecycle.conflict_resolution_admitted` | delegate | 1 | 0 | 0 | 4 | 2 | - | **keep** |
| `conversation_creator` | 3683 | `_runtime.ask_state.conversation_creator` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `conversation_owner` | 1513 | `_runtime.sharing.conversation_owner` | delegate | 0 | 0 | 0 | 1 | 5 | - | **test-only** |
| `conversation_share_state` | 3708 | `_runtime.ask_state.conversation_share_state` | delegate | 1 | 0 | 0 | 1 | 13 | - | **keep** |
| `copy_notebook` | 1461 | `_runtime.sharing.copy_notebook` | delegate | 0 | 0 | 0 | 35 | 3 | - | **test-only** |
| `create_agent_profile` | 1263 | `_runtime.memory_service.create_agent_profile` | delegate | 1 | 0 | 2 | 6 | 11 | - | **keep** |
| `create_knowhow_milestone` | 4170 | `_runtime.knowhow_history_store.create_milestone` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `create_knowhow_table` | 3903 | `_runtime.knowhow_store.create_knowhow_table` | delegate | 1 | 0 | 0 | 28 | 28 | - | **keep** |
| `create_knowhow_table_with_rows` | 3912 | `_runtime.knowhow_store.create_knowhow_table_with_rows` | delegate | 1 | 0 | 0 | 0 | 5 | - | **keep** |
| `create_memory_candidate` | 1340 | `_runtime.memory_service.create_candidate` | delegate | 1 | 0 | 0 | 21 | 2 | - | **keep** |
| `create_memory_from_answer` | 1358 | `_runtime.memory_service.create_from_answer` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `create_notebook` | 1226 | `_runtime.catalog.create_notebook` | delegate | 2 | 0 | 8 | 1483 | 34 | - | **keep** |
| `create_notebook_object_schema` | 2018 | `_runtime.schema_registry.create_notebook_object_schema` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `create_object_schema` | 2044 | `_runtime.schema_registry.create_object_schema` | delegate | 1 | 0 | 1 | 5 | 0 | - | **keep** |
| `create_report` | 3736 | `_runtime.report_application.create_report` | delegate | 1 | 0 | 1 | 64 | 14 | - | **keep** |
| `create_session` | 754 | `_runtime.identity.create_session` | delegate | 0 | 0 | 0 | 4 | 13 | - | **test-only** |
| `create_user` | 748 | `_runtime.identity.create_user` | delegate | 0 | 0 | 0 | 70 | 121 | - | **test-only** |
| `current_user` | 742 | `_runtime.identity.current_user` | delegate | 16 | 1 | 1 | 38 | 15 | - | **keep** |
| `decided_pairs` | 2444 | `_runtime.knowledge_governance.decided_pairs` | delegate | 0 | 0 | 0 | 3 | 1 | - | **test-only** |
| `decided_seed_pairs` | 2447 | `_runtime.knowledge_governance.decided_seed_pairs` | delegate | 0 | 1 | 0 | 3 | 2 | - | **keep** |
| `delete_conversation` | 3662 | `_runtime.ask_state.delete_conversation` | delegate | 1 | 0 | 1 | 1 | 3 | - | **keep** |
| `delete_knowhow_cell_code` | 4113 | `_runtime.knowhow_store.delete_knowhow_cell_code` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `delete_knowhow_column` | 4083 | `_runtime.knowhow_store.delete_knowhow_column` | delegate | 1 | 0 | 0 | 5 | 1 | - | **keep** |
| `delete_knowhow_milestone` | 4177 | `_runtime.knowhow_history_store.delete_milestone` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `delete_knowhow_row` | 4090 | `_runtime.knowhow_store.delete_knowhow_row` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `delete_knowhow_table` | 3990 | `_runtime.knowhow_store.delete_knowhow_table` | delegate | 1 | 0 | 0 | 1 | 9 | - | **keep** |
| `delete_memory` | 1388 | `_runtime.memory_service.delete` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `delete_notebook` | 1257 | `_runtime.catalog.delete_notebook` | delegate | 1 | 0 | 0 | 7 | 3 | - | **keep** |
| `delete_notebook_kg` | 1522 | `_runtime.knowledge_lifecycle.delete_notebook_kg` | delegate | 0 | 0 | 1 | 5 | 4 | - | **keep** |
| `delete_notebook_object_schema` | 2037 | `_runtime.schema_registry.delete_notebook_object_schema` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `delete_object_schema` | 2052 | `_runtime.schema_registry.delete_object_schema` | delegate | 1 | 0 | 2 | 3 | 0 | - | **keep** |
| `delete_report` | 3783 | `_runtime.report_store.delete_report` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `delete_session` | 760 | `_runtime.identity.delete_session` | delegate | 0 | 0 | 0 | 1 | 2 | - | **test-only** |
| `delete_source` | 1858 | `_runtime.source_ingestion.delete_source_compat` | delegate | 2 | 0 | 1 | 5 | 2 | - | **keep** |
| `delete_source_asset_rows` | 4020 | `_runtime.knowhow_store.delete_source_asset_rows` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `deprecate_memory` | 1385 | `_runtime.memory_service.deprecate` | delegate | 1 | 0 | 0 | 3 | 0 | - | **keep** |
| `effective_document_limit` | 775 | `_runtime.identity.effective_document_limit` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `effective_schemas` | 2003 | `_runtime.schema_registry.effective_schemas` | delegate | 0 | 0 | 3 | 13 | 2 | - | **keep** |
| `evidence_elements` | 1845 | `_runtime.source_store.evidence_elements` | delegate | 1 | 0 | 0 | 0 | 5 | - | **keep** |
| `execute_notebook_kg_job` | 4230 | `_runtime.knowledge_lifecycle.execute_notebook_kg_job` | delegate | 3 | 0 | 0 | 20 | 0 | - | **keep** |
| `export_reports` | 3786 | `_runtime.report_store.export_reports` | delegate | 1 | 0 | 0 | 2 | 1 | - | **keep** |
| `extract_source` | 1861 | `_runtime.source_ingestion.run_extraction` | delegate | 3 | 0 | 0 | 1 | 2 | - | **keep** |
| `fail_conflict_resolution_submission` | 2148 | `_runtime.knowledge_lifecycle.fail_conflict_resolution_submission` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `fail_notebook_kg_job_submission` | 4225 | `_runtime.knowledge_lifecycle.fail_notebook_kg_job_submission` | delegate | 3 | 0 | 0 | 0 | 0 | - | **keep** |
| `fail_notebook_relink_submission` | 2118 | `_runtime.knowledge_lifecycle.fail_notebook_relink_submission` | delegate | 1 | 0 | 0 | 3 | 1 | - | **keep** |
| `fail_unified_kg_rebuild_submission` | 2618 | `_runtime.knowledge_lifecycle.fail_unified_kg_rebuild_submission` | delegate | 1 | 0 | 0 | 2 | 1 | - | **keep** |
| `federated_retrieve` | 3213 | `retrieval.candidates.federated_retrieve` | adapter | 0 | 0 | 0 | 9 | 20 | - | **test-only** |
| `federated_retrieve_relations` | 3232 | `retrieval.candidates.federated_retrieve_relations` | delegate | 0 | 0 | 0 | 2 | 7 | - | **test-only** |
| `find_duplicates` | 2811 | `_runtime.knowledge_governance.find_duplicates` | delegate | 1 | 0 | 1 | 5 | 0 | - | **keep** |
| `find_notebook_by_share_token` | 1443 | `_runtime.sharing.find_notebook_by_share_token` | delegate | 0 | 0 | 0 | 4 | 3 | - | **test-only** |
| `finish_ask_job` | 3587 | `_runtime.ask_component.finish_job` | delegate | 0 | 0 | 1 | 9 | 0 | - | **keep** |
| `fold_scale_index_delta` | 2996 | `_runtime.scale_artifacts.fold` | delegate | 0 | 0 | 0 | 2 | 0 | - | **test-only** |
| `get_community_reports` | 2655 | `_runtime.knowledge_lifecycle.get_community_reports` | delegate | 0 | 0 | 0 | 4 | 1 | - | **test-only** |
| `get_conflict_candidate` | 2363 | `_runtime.knowledge_governance.get_conflict_candidate` | delegate | 0 | 0 | 0 | 9 | 3 | - | **test-only** |
| `get_conversation` | 3649 | `_runtime.ask_state.get_conversation` | delegate | 2 | 0 | 5 | 24 | 18 | - | **keep** |
| `get_knowhow_cell_code` | 4110 | `_runtime.knowhow_store.get_knowhow_cell_code` | delegate | 2 | 0 | 0 | 0 | 2 | - | **keep** |
| `get_knowhow_change` | 4145 | `_runtime.knowhow_history_store.get_change` | delegate | 2 | 0 | 0 | 2 | 0 | - | **keep** |
| `get_knowhow_row_location` | 4125 | `_runtime.knowhow_store.get_knowhow_row_location` | delegate | 6 | 0 | 0 | 0 | 0 | - | **keep** |
| `get_knowhow_table` | 3931 | `_runtime.knowhow_store.get_knowhow_table` | delegate | 15 | 0 | 1 | 142 | 60 | - | **keep** |
| `get_memory` | 1394 | `_runtime.memory_service.get` | delegate | 3 | 0 | 0 | 9 | 0 | - | **keep** |
| `get_notebook` | 1229 | `_runtime.catalog.get_notebook` | delegate | 24 | 1 | 10 | 79 | 61 | - | **keep** |
| `get_notebook_asset` | 4014 | `_runtime.knowhow_store.get_notebook_asset` | delegate | 5 | 0 | 0 | 40 | 10 | - | **keep** |
| `get_paper_meta` | 4024 | `_runtime.source_store.get_paper_meta` | delegate | 0 | 0 | 0 | 18 | 1 | - | **test-only** |
| `get_report` | 3775 | `_runtime.report_store.get_report` | delegate | 2 | 0 | 2 | 61 | 18 | - | **keep** |
| `get_source` | 1627 | `_runtime.source_store.get_source` | delegate | 5 | 0 | 4 | 95 | 43 | - | **keep** |
| `global_document_limit_default` | 769 | `_runtime.identity.global_document_limit_default` | delegate | 0 | 0 | 0 | 3 | 5 | - | **test-only** |
| `hidden_source_ids` | 1610 | `_runtime.source_store.hidden_source_ids` | delegate | 1 | 0 | 0 | 3 | 9 | - | **keep** |
| `import_sources` | 1653 | `_runtime.source_ingestion.import_sources_compat` | delegate | 1 | 0 | 0 | 2 | 1 | - | **keep** |
| `incremental_fuse_source` | 2247 | `_runtime.knowledge_lifecycle.incremental_fuse_source` | delegate | 0 | 0 | 0 | 47 | 3 | - | **test-only** |
| `index_status` | 3020 | `_runtime.scale_artifacts.index_status` | delegate | 2 | 0 | 0 | 5 | 0 | - | **keep** |
| `insert_notebook_asset` | 4001 | `_runtime.knowhow_store.insert_notebook_asset` | delegate | 2 | 0 | 0 | 0 | 2 | - | **keep** |
| `is_member` | 1477 | `_runtime.sharing.is_member` | delegate | 0 | 0 | 0 | 4 | 4 | - | **test-only** |
| `issue_agent_token` | 1284 | `_runtime.memory_service.issue_agent_token` | delegate | 1 | 0 | 2 | 10 | 8 | - | **keep** |
| `join_shared` | 1501 | `_runtime.sharing.join_shared` | delegate | 0 | 0 | 0 | 1 | 2 | - | **test-only** |
| `kg_analysis` (property) | 1063 | `_runtime.kg_analysis` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `kg_cluster_size_histogram` | 1019 | `_runtime.unified_kg.cluster_size_histogram` | delegate | 0 | 0 | 0 | 16 | 0 | - | **test-only** |
| `kg_community_overview` | 1030 | `_runtime.unified_kg.community_overview` | delegate | 0 | 0 | 0 | 11 | 0 | - | **test-only** |
| `kg_largest_clusters` | 1024 | `_runtime.unified_kg.largest_clusters` | delegate | 0 | 0 | 0 | 15 | 0 | - | **test-only** |
| `kg_neighbors` | 2546 | `_runtime.knowledge_lifecycle.kg_neighbors` | delegate | 1 | 0 | 0 | 6 | 2 | - | **keep** |
| `kg_relation_provenance_counts` | 1039 | `_runtime.unified_kg.relation_provenance_counts` | delegate | 0 | 0 | 0 | 11 | 0 | - | **test-only** |
| `kg_search` | 1578 | `_runtime.knowledge_query.search` | delegate | 1 | 0 | 0 | 9 | 1 | - | **keep** |
| `kick_all_members` | 1495 | `_runtime.sharing.kick_all_members` | delegate | 0 | 0 | 0 | 1 | 1 | - | **test-only** |
| `knowhow_cell_history` | 4155 | `_runtime.knowhow_history_store.cell_history` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowhow_changes_between` | 4148 | `_runtime.knowhow_history_store.changes_between` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowhow_history_head_seq` | 4128 | `_runtime.knowhow_history_store.head_seq` | delegate | 0 | 0 | 0 | 3 | 0 | - | **test-only** |
| `knowhow_history_page` | 4138 | `_runtime.knowhow_history_store.history_page` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `knowledge_graph` | 2062 | `_runtime.knowledge_query.graph` | delegate | 1 | 0 | 3 | 3 | 0 | - | **keep** |
| `knowledge_types` | 1964 | `_runtime.knowledge_query.knowledge_types` | delegate | 1 | 0 | 4 | 0 | 0 | - | **keep** |
| `leave_notebook` | 1504 | `_runtime.sharing.leave_notebook` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `list_agent_profiles` | 1270 | `_runtime.memory_service.list_agent_profiles` | delegate | 2 | 0 | 0 | 0 | 2 | - | **keep** |
| `list_agent_tokens` | 1302 | `_runtime.memory_service.list_agent_tokens` | delegate | 1 | 0 | 0 | 0 | 6 | - | **keep** |
| `list_communities` | 2646 | `_runtime.knowledge_lifecycle.list_communities` | delegate | 0 | 0 | 0 | 4 | 0 | - | **test-only** |
| `list_conversations` | 3654 | `_runtime.ask_component.list_conversations_current` | delegate | 1 | 0 | 3 | 7 | 7 | - | **keep** |
| `list_knowhow_cell_code` | 4121 | `_runtime.knowhow_store.list_knowhow_cell_code` | delegate | 4 | 0 | 0 | 0 | 0 | - | **keep** |
| `list_knowhow_changes` | 4131 | `_runtime.knowhow_history_store.list_changes` | delegate | 0 | 0 | 0 | 7 | 0 | - | **test-only** |
| `list_knowhow_milestones` | 4182 | `_runtime.knowhow_history_store.list_milestones` | delegate | 0 | 0 | 0 | 1 | 0 | - | **test-only** |
| `list_knowhow_tables` | 3928 | `_runtime.knowhow_store.list_knowhow_tables` | delegate | 1 | 0 | 1 | 35 | 7 | - | **keep** |
| `list_knowledge` | 1978 | `_runtime.knowledge_query.list_knowledge` | delegate | 1 | 0 | 14 | 6 | 0 | - | **keep** |
| `list_members` | 1498 | `_runtime.sharing.list_members` | delegate | 0 | 0 | 0 | 4 | 14 | - | **test-only** |
| `list_memories` | 1397 | `_runtime.memory_service.list_memories` | delegate | 2 | 0 | 0 | 0 | 12 | - | **keep** |
| `list_notebook_bases` | 1241 | `_runtime.notebook_store.list_mount_edges_for_notebook` | delegate | 3 | 0 | 0 | 12 | 0 | - | **keep** |
| `list_notebook_object_schemas` | 2011 | `_runtime.schema_registry.list_notebook_object_schemas` | delegate | 1 | 0 | 0 | 2 | 3 | - | **keep** |
| `list_notebook_templates` | 1223 | `_runtime.catalog.list_notebook_templates` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `list_notebooks` | 1220 | `_runtime.catalog.list_notebooks` | delegate | 0 | 0 | 1 | 10 | 1 | - | **keep** |
| `list_object_schemas` | 2008 | `_runtime.schema_registry.list_object_schemas` | delegate | 1 | 0 | 3 | 1 | 0 | - | **keep** |
| `list_promotion_queue` | 2722 | `_runtime.knowledge_governance.list_promotion_queue` | delegate | 1 | 0 | 0 | 14 | 0 | - | **keep** |
| `list_reports` | 3778 | `_runtime.report_store.list_reports` | delegate | 1 | 0 | 1 | 5 | 0 | - | **keep** |
| `list_sources` | 1591 | `_runtime.source_ingestion.list_sources` | delegate | 0 | 0 | 3 | 36 | 7 | - | **keep** |
| `list_sources_page` | 1594 | `_runtime.source_ingestion.list_sources_page` | delegate | 2 | 0 | 0 | 10 | 4 | - | **keep** |
| `list_user_activity` | 790 | `_runtime.queries.list_user_activity` | delegate | 0 | 0 | 0 | 35 | 26 | - | **test-only** |
| `list_user_notebooks` | 784 | `_runtime.queries.list_user_notebooks` | delegate | 0 | 0 | 0 | 6 | 4 | - | **test-only** |
| `list_user_usage` | 781 | `_runtime.queries.list_user_usage` | delegate | 0 | 0 | 0 | 4 | 3 | - | **test-only** |
| `load_notebook_scale_facts` | 811 | `_runtime.queries.load_notebook_scale_facts` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `mark_notebook_base` | 1235 | `_runtime.catalog.mark_notebook_base` | delegate | 0 | 0 | 1 | 137 | 1 | - | **keep** |
| `maybe_auto_index` | 3071 | `_runtime.scale_artifacts.maybe_auto_index` | delegate | 0 | 1 | 0 | 11 | 6 | - | **keep** |
| `memory_kg_eligible` | 1366 | `_runtime.source_ingestion.memory_kg_eligible` | delegate | 2 | 0 | 0 | 2 | 5 | - | **keep** |
| `memory_revisions` | 1418 | `_runtime.memory_service.revisions` | delegate | 0 | 0 | 0 | 3 | 0 | - | **test-only** |
| `merge_knowledge` | 2820 | `_runtime.knowledge_governance.merge_knowledge` | delegate | 1 | 0 | 1 | 6 | 0 | - | **keep** |
| `merge_review_job_status` | 2433 | `_runtime.knowledge_governance.merge_review_job_status` | delegate | 2 | 0 | 0 | 4 | 0 | - | **keep** |
| `mountable_notebooks` | 1244 | `_runtime.notebook_store.mountable_for_notebook` | delegate | 2 | 0 | 0 | 7 | 2 | - | **keep** |
| `mounted_by_count` | 1247 | `_runtime.notebook_store.mounted_by_count_for_notebook` | delegate | 1 | 0 | 0 | 6 | 2 | - | **keep** |
| `node_context` | 2677 | `_runtime.knowledge_query.node_context` | delegate | 1 | 0 | 1 | 8 | 10 | - | **keep** |
| `notebook_analytics` | 3831 | `_runtime.catalog.notebook_analytics` | delegate | 0 | 0 | 1 | 4 | 3 | - | **keep** |
| `notebook_copy_stats` | 1446 | `_runtime.scale_artifacts.notebook_copy_stats` | delegate | 0 | 2 | 0 | 7 | 21 | - | **keep** |
| `notebook_exists_for_owner` | 787 | `_runtime.queries.notebook_exists_for_owner` | delegate | 0 | 0 | 0 | 4 | 1 | - | **test-only** |
| `notebook_owner` | 778 | `_runtime.identity.notebook_owner` | delegate | 1 | 0 | 0 | 2 | 2 | - | **keep** |
| `notebook_relink_status` | 2108 | `_runtime.knowledge_lifecycle.notebook_relink_status` | delegate | 1 | 0 | 0 | 12 | 1 | - | **keep** |
| `paper_meta_backfill_progress` | 4193 | `_runtime.source_ingestion.paper_meta_backfill_progress` | delegate | 0 | 0 | 0 | 7 | 1 | - | **test-only** |
| `paper_meta_backfilling` | 4189 | `_runtime.source_ingestion.paper_meta_backfilling` | delegate | 0 | 0 | 0 | 6 | 1 | - | **test-only** |
| `parallelism` | 823 | `_runtime.models.parallelism` | delegate | 1 | 0 | 0 | 0 | 18 | - | **keep** |
| `parse_source` | 1775 | `_runtime.source_ingestion.parse_source_compat` | delegate | 2 | 0 | 2 | 0 | 1 | - | **keep** |
| `participant_notebook_ids` | 3451 | `_runtime.notebook_store.participant_notebook_ids` | delegate | 1 | 0 | 0 | 17 | 12 | - | **keep** |
| `pending_actions` | 3811 | `_runtime.pending_actions_service.list_for_user` | delegate | 3 | 0 | 0 | 14 | 0 | - | **keep** |
| `pending_actions_projection_rows` | 814 | `_runtime.queries.pending_actions_projection_rows` | delegate | 0 | 0 | 0 | 0 | 4 | - | **ambiguous** |
| `pending_conflicts` | 2349 | `_runtime.knowledge_governance.pending_conflicts` | delegate | 1 | 0 | 0 | 14 | 1 | - | **keep** |
| `pending_merges` | 2281 | `_runtime.knowledge_governance.pending_merges` | delegate | 1 | 0 | 0 | 16 | 2 | - | **keep** |
| `prepare_notebook_kg_job` | 4210 | `_runtime.knowledge_lifecycle.prepare_notebook_kg_job` | delegate | 3 | 0 | 0 | 35 | 2 | - | **keep** |
| `preview_reasoning_intent` | 3366 | `_runtime.ask_component.preview_reasoning_intent` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `process_source` | 1743 | `_runtime.source_ingestion.process_source_compat` | delegate | 9 | 0 | 2 | 43 | 5 | - | **keep** |
| `propose_memory_promotion` | 1421 | `_runtime.memory_service.propose_promotion` | delegate | 1 | 0 | 0 | 27 | 1 | - | **keep** |
| `propose_promotion` | 2708 | `_runtime.knowledge_governance.propose_promotion` | delegate | 1 | 0 | 0 | 47 | 0 | - | **keep** |
| `propose_schemas` | 2055 | `_runtime.schema_registry.propose_schemas` | delegate | 1 | 0 | 1 | 10 | 0 | - | **keep** |
| `prune_knowhow_history` | 4185 | `_runtime.knowhow_history_store.prune` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `public_conversation_by_token` | 3723 | `_runtime.ask_state.public_conversation_by_token` | delegate | 1 | 0 | 0 | 0 | 22 | - | **keep** |
| `public_report_by_token` | 3801 | `_runtime.report_store.public_report_by_token` | delegate | 1 | 0 | 0 | 0 | 3 | - | **keep** |
| `rebuild_canonical_relations` | 2626 | `_runtime.knowledge_lifecycle.rebuild_canonical_relations` | delegate | 0 | 0 | 0 | 5 | 2 | - | **test-only** |
| `rebuild_communities` | 2640 | `_runtime.knowledge_lifecycle.rebuild_communities` | delegate | 0 | 0 | 0 | 94 | 2 | - | **test-only** |
| `rebuild_mention_bridge` | 2633 | `_runtime.knowledge_lifecycle.rebuild_mention_bridge` | delegate | 0 | 0 | 0 | 6 | 2 | - | **test-only** |
| `rebuild_notebook_kg` | 1731 | `_runtime.knowledge_lifecycle.rebuild_notebook_kg` | delegate | 0 | 0 | 0 | 5 | 1 | - | **test-only** |
| `rebuild_unified_kg` | 2593 | `_runtime.knowledge_lifecycle.rebuild_unified_kg` | delegate | 4 | 0 | 1 | 183 | 2 | - | **keep** |
| `refresh_agent_principal` | 1315 | `_runtime.memory_service.refresh_agent_principal` | delegate | 1 | 0 | 0 | 0 | 3 | - | **keep** |
| `reject_conflict` | 2404 | `_runtime.knowledge_governance.reject_conflict` | delegate | 1 | 0 | 0 | 1 | 0 | - | **keep** |
| `reject_memory` | 1382 | `_runtime.memory_service.reject` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `reject_merge` | 2311 | `_runtime.knowledge_governance.reject_merge` | delegate | 1 | 0 | 0 | 5 | 0 | - | **keep** |
| `reject_promotion` | 2761 | `_runtime.knowledge_governance.reject_promotion` | delegate | 0 | 0 | 0 | 12 | 0 | - | **test-only** |
| `reject_promotion_as_reviewer` | 2768 | `_runtime.knowledge_governance.reject_promotion` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `relations_for_notebook` | 2157 | `_runtime.knowledge_query.relations_for_notebook` | delegate | 0 | 1 | 0 | 15 | 4 | - | **keep** |
| `relink_notebook_kg` | 2099 | `_runtime.knowledge_lifecycle.relink_notebook_kg` | delegate | 0 | 0 | 0 | 20 | 1 | - | **test-only** |
| `remove_member` | 1492 | `_runtime.sharing.remove_member` | delegate | 0 | 0 | 0 | 11 | 23 | - | **test-only** |
| `rename_conversation` | 3659 | `_runtime.ask_state.rename_conversation` | delegate | 1 | 0 | 1 | 1 | 0 | - | **keep** |
| `rename_knowhow_column` | 4069 | `_runtime.knowhow_store.rename_knowhow_column` | delegate | 1 | 0 | 0 | 0 | 2 | - | **keep** |
| `replace_notebook_bases` | 1250 | `_runtime.notebook_store.replace_mounts` | delegate | 1 | 0 | 0 | 107 | 0 | - | **keep** |
| `report_execution` (property) | 3805 | `_runtime.report_execution` | delegate | 0 | 0 | 0 | 0 | 0 | - | **keep** |
| `report_share_token` | 3798 | `_runtime.report_store.report_share_token` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `report_source_identity_rows` | 1640 | `_runtime.source_store.report_source_identity_rows` | delegate | 0 | 0 | 0 | 0 | 4 | - | **ambiguous** |
| `report_source_rows` | 1630 | `_runtime.source_store.report_source_rows` | delegate | 0 | 0 | 0 | 0 | 12 | - | **ambiguous** |
| `require_agent_access` | 1318 | `_runtime.memory_service.require_agent_access` | delegate | 6 | 0 | 0 | 0 | 5 | - | **keep** |
| `resolve_agent_token` | 1312 | `_runtime.memory_service.resolve_agent_token` | delegate | 1 | 0 | 0 | 0 | 12 | - | **keep** |
| `resolve_notebook_conflicts` | 2412 | `_runtime.knowledge_governance.resolve_notebook_conflicts` | delegate | 0 | 0 | 0 | 22 | 3 | - | **test-only** |
| `resolve_session` | 757 | `_runtime.identity.resolve_session` | delegate | 2 | 0 | 0 | 5 | 19 | - | **keep** |
| `retrieval` (property) | 3175 | `_runtime.retrieval_component` | delegate | 0 | 59 | 0 | 0 | 2 | - | **keep** |
| `retrieval_experiences` (property) | 1099 | `_runtime.retrieval_experiences` | delegate | 0 | 0 | 0 | 0 | 0 | - | **test-only** |
| `revert_knowhow_table` | 4162 | `_runtime.knowhow_history_store.revert_to` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `review_pending_merges` | 2418 | `_runtime.knowledge_governance.review_pending_merges` | delegate | 1 | 0 | 0 | 10 | 1 | - | **keep** |
| `review_queue` | 2212 | `_runtime.knowledge_governance.review_queue` | delegate | 1 | 0 | 0 | 27 | 0 | - | **keep** |
| `revoke_agent_token` | 1307 | `_runtime.memory_service.revoke_agent_token` | delegate | 1 | 0 | 0 | 1 | 6 | - | **keep** |
| `run_conflict_resolution_job` | 2142 | `_runtime.knowledge_lifecycle.run_conflict_resolution_job` | delegate | 1 | 0 | 0 | 4 | 2 | - | **keep** |
| `run_merge_review_job` | 2436 | `_runtime.knowledge_governance.run_merge_review_job` | delegate | 1 | 0 | 0 | 4 | 0 | - | **keep** |
| `run_notebook_relink_job` | 2112 | `_runtime.knowledge_lifecycle.run_notebook_relink_job` | delegate | 1 | 0 | 0 | 5 | 2 | - | **keep** |
| `run_unified_kg_rebuild_job` | 2612 | `_runtime.knowledge_lifecycle.run_unified_kg_rebuild_job` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `scale_index_status` | 3016 | `_runtime.scale_artifacts.status` | delegate | 1 | 0 | 0 | 32 | 0 | - | **keep** |
| `scale_ppr` | 3108 | `retrieval.graph.scale_ppr` | delegate | 0 | 0 | 0 | 7 | 9 | - | **test-only** |
| `search_notebook` | 2829 | `_runtime.catalog.search_notebook` | delegate | 1 | 0 | 3 | 9 | 4 | - | **keep** |
| `set_conflict_status` | 2353 | `_runtime.knowledge_governance.set_conflict_status` | delegate | 0 | 1 | 0 | 7 | 1 | - | **keep** |
| `set_edge_review` | 2218 | `_runtime.knowledge_governance.set_edge_review` | delegate | 1 | 0 | 0 | 14 | 1 | - | **keep** |
| `set_knowhow_anchor_column` | 4053 | `_runtime.knowhow_store.set_knowhow_anchor_column` | delegate | 1 | 0 | 0 | 3 | 3 | - | **keep** |
| `set_knowhow_column_kind` | 4076 | `_runtime.knowhow_store.set_knowhow_column_kind` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `set_knowhow_hidden_source` | 3993 | `_runtime.knowhow_store.set_knowhow_hidden_source` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `set_merge_decision` | 2298 | `_runtime.knowledge_governance.set_merge_decision` | delegate | 0 | 0 | 0 | 4 | 2 | - | **test-only** |
| `set_notebook_personal` | 1238 | `_runtime.catalog.set_notebook_personal` | delegate | 0 | 0 | 2 | 10 | 1 | - | **keep** |
| `share_conversation` | 3694 | `_runtime.ask_state.share_conversation` | delegate | 1 | 0 | 0 | 0 | 37 | - | **keep** |
| `share_notebook` | 1434 | `_runtime.sharing.share_notebook` | delegate | 0 | 0 | 0 | 8 | 1 | - | **test-only** |
| `share_report` | 3792 | `_runtime.report_store.share_report` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `share_state` | 1437 | `_runtime.sharing.share_state` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `shared_by_me` | 1452 | `_runtime.sharing.shared_by_me` | delegate | 0 | 0 | 1 | 1 | 2 | - | **keep** |
| `shared_preview` | 1449 | `_runtime.sharing.shared_preview` | delegate | 0 | 0 | 1 | 4 | 1 | - | **keep** |
| `source_asset_ids` | 4017 | `_runtime.knowhow_store.source_asset_ids` | delegate | 0 | 0 | 0 | 12 | 1 | - | **test-only** |
| `source_elements` | 1802 | `_runtime.source_store.source_elements` | delegate | 2 | 1 | 10 | 14 | 8 | - | **keep** |
| `source_elements_page` | 1805 | `_runtime.source_store.source_elements_page` | delegate | 2 | 0 | 0 | 0 | 0 | - | **keep** |
| `source_id_by_hash` | 1816 | `_runtime.source_store.source_id_by_hash` | delegate | 1 | 0 | 0 | 0 | 14 | - | **keep** |
| `source_metadata` | 1830 | `_runtime.source_store.source_metadata` | delegate | 1 | 0 | 0 | 0 | 8 | - | **keep** |
| `source_notebook_id` | 1510 | `_runtime.sharing.source_notebook_id` | delegate | 0 | 0 | 0 | 0 | 4 | - | **ambiguous** |
| `source_owner` | 1507 | `_runtime.sharing.source_owner` | delegate | 0 | 0 | 0 | 0 | 1 | - | **ambiguous** |
| `source_parse_busy` | 1746 | `<complex>` | adapter | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `sources_missing_paper_meta` | 4027 | `_runtime.source_store.sources_missing_paper_meta` | delegate | 1 | 0 | 0 | 0 | 1 | - | **keep** |
| `start_ask_stream` | 3893 | `_runtime.ask_execution.start` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `start_conflict_resolution` | 2132 | `_runtime.knowledge_lifecycle.start_conflict_resolution` | delegate | 1 | 0 | 0 | 14 | 2 | - | **keep** |
| `start_notebook_relink` | 2104 | `_runtime.knowledge_lifecycle.start_notebook_relink` | delegate | 1 | 0 | 0 | 23 | 2 | - | **keep** |
| `start_unified_kg_rebuild` | 2604 | `_runtime.knowledge_lifecycle.start_unified_kg_rebuild` | delegate | 1 | 0 | 0 | 17 | 1 | - | **keep** |
| `storage_dir` (property+setter) | 3875 | `_runtime.storage_dir` | delegate | 0 | 2 | 0 | 0 | 0 | - | **keep** |
| `store_kg` | 2090 | `_runtime.knowledge_lifecycle.store_kg` | delegate | 0 | 0 | 5 | 292 | 16 | - | **keep** |
| `submit_feedback` | 3828 | `_runtime.ask_state.submit_feedback` | delegate | 1 | 0 | 2 | 0 | 0 | - | **keep** |
| `summarize_communities` | 2650 | `_runtime.knowledge_lifecycle.summarize_communities` | delegate | 0 | 0 | 0 | 4 | 0 | - | **test-only** |
| `transfer_memories` | 4197 | `_runtime.memory_service.transfer` | delegate | 1 | 0 | 0 | 28 | 0 | - | **keep** |
| `trigger_scale_index_rebuild` | 3052 | `_runtime.scale_artifacts.trigger` | delegate | 2 | 0 | 0 | 6 | 0 | - | **keep** |
| `unified_graph` | 2521 | `_runtime.knowledge_lifecycle.unified_graph` | delegate | 1 | 0 | 0 | 24 | 0 | - | **keep** |
| `unified_kg_rebuild_status` | 2608 | `_runtime.knowledge_lifecycle.unified_kg_rebuild_status` | delegate | 1 | 0 | 0 | 10 | 1 | - | **keep** |
| `unified_kg_status` | 2502 | `_runtime.knowledge_lifecycle.unified_kg_status` | delegate | 2 | 0 | 1 | 8 | 1 | - | **keep** |
| `unshare_conversation` | 3717 | `_runtime.ask_state.unshare_conversation` | delegate | 1 | 0 | 0 | 0 | 2 | - | **keep** |
| `unshare_notebook` | 1440 | `_runtime.sharing.unshare_notebook` | delegate | 0 | 0 | 0 | 2 | 1 | - | **test-only** |
| `unshare_report` | 3795 | `_runtime.report_store.unshare_report` | delegate | 1 | 0 | 0 | 0 | 0 | - | **keep** |
| `update_agent_profile` | 1277 | `_runtime.memory_service.update_agent_profile` | delegate | 1 | 0 | 0 | 0 | 3 | - | **keep** |
| `update_knowhow_cell` | 3949 | `_runtime.knowhow_store.update_knowhow_cell` | delegate | 1 | 0 | 0 | 14 | 34 | - | **keep** |
| `update_knowhow_cells` | 3958 | `_runtime.knowhow_store.update_knowhow_cells` | delegate | 1 | 0 | 0 | 1 | 3 | - | **keep** |
| `update_knowhow_cells_bulk_guarded` | 3967 | `_runtime.knowhow_store.update_knowhow_cells_bulk_guarded` | delegate | 0 | 0 | 1 | 0 | 3 | - | **keep** |
| `update_knowhow_cells_guarded_atomic` | 3975 | `_runtime.knowhow_store.update_knowhow_cells_guarded_atomic` | delegate | 2 | 0 | 0 | 0 | 4 | - | **keep** |
| `update_knowhow_table_meta` | 4044 | `_runtime.knowhow_store.update_knowhow_table_meta` | delegate | 1 | 0 | 0 | 4 | 1 | - | **keep** |
| `update_knowledge` | 2776 | `_runtime.knowledge_governance.update_knowledge` | delegate | 1 | 0 | 5 | 9 | 2 | - | **keep** |
| `update_memory` | 1369 | `_runtime.memory_service.update` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `update_notebook` | 1232 | `_runtime.catalog.update_notebook` | delegate | 0 | 0 | 1 | 0 | 1 | - | **keep** |
| `update_notebook_object_schema` | 2025 | `_runtime.schema_registry.update_notebook_object_schema` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `update_object_schema` | 2047 | `_runtime.schema_registry.update_object_schema` | delegate | 1 | 0 | 2 | 5 | 0 | - | **keep** |
| `update_report` | 3741 | `_runtime.report_store.update_report` | delegate | 3 | 0 | 1 | 46 | 42 | - | **keep** |
| `upload_sources` | 1668 | `_runtime.source_ingestion.upload_sources_compat` | delegate | 5 | 0 | 7 | 24 | 2 | - | **keep** |
| `upsert_knowhow_cell_code` | 4100 | `_runtime.knowhow_store.upsert_knowhow_cell_code` | delegate | 1 | 0 | 0 | 5 | 5 | - | **keep** |
| `user_can_access_notebook` | 1474 | `_runtime.sharing.user_can_access_notebook` | delegate | 1 | 0 | 0 | 10 | 8 | - | **keep** |
| `user_can_admin_notebook` | 1480 | `_runtime.sharing.user_can_admin_notebook` | delegate | 0 | 0 | 0 | 19 | 8 | - | **test-only** |
| `user_can_read_answer` | 1519 | `_runtime.sharing.user_can_read_answer` | delegate | 0 | 0 | 0 | 3 | 3 | - | **test-only** |
| `user_can_read_notebook` | 1483 | `_runtime.sharing.user_can_read_notebook` | delegate | 5 | 0 | 0 | 33 | 17 | - | **keep** |
| `user_can_read_source` | 1486 | `_runtime.sharing.user_can_read_source` | delegate | 0 | 0 | 0 | 3 | 3 | - | **test-only** |
| `user_document_limit_override` | 772 | `_runtime.identity.user_document_limit_override` | delegate | 0 | 0 | 0 | 2 | 4 | - | **test-only** |
| `validate_cell_target` | 4097 | `_runtime.knowhow_store.validate_cell_target` | delegate | 7 | 0 | 0 | 0 | 0 | - | **keep** |
| `validate_reasoning_submission` | 3375 | `_runtime.ask_component.validate_reasoning_submission` | delegate | 0 | 0 | 0 | 0 | 3 | - | **ambiguous** |
| `visible_document_count` | 766 | `_runtime.source_store.visible_document_count` | delegate | 1 | 0 | 0 | 2 | 0 | - | **keep** |
| `visible_source_count` | 1616 | `_runtime.source_store.visible_document_count` | delegate | 0 | 0 | 0 | 0 | 2 | - | **ambiguous** |
| `visible_source_ids` | 1602 | `_runtime.index_projections.visible_source_ids` | delegate | 0 | 0 | 0 | 0 | 3 | - | **ambiguous** |
| `visible_source_scope_snapshot` | 1619 | `_runtime.source_store.visible_source_scope_snapshot` | delegate | 1 | 0 | 0 | 0 | 6 | - | **keep** |
| `warm_open_path_caches` | 3834 | `_runtime.catalog.warm_open_path_caches` | delegate | 1 | 0 | 0 | 1 | 1 | - | **keep** |
| `write_clusters` | 2232 | `_runtime.knowledge_lifecycle.write_clusters` | delegate | 0 | 0 | 0 | 12 | 0 | - | **test-only** |
| `write_conflict_candidate` | 2328 | `_runtime.knowledge_governance.write_conflict_candidate` | delegate | 0 | 0 | 0 | 16 | 2 | - | **test-only** |
| `write_merge_candidate` | 2274 | `_runtime.knowledge_governance.write_merge_candidate` | delegate | 0 | 0 | 0 | 13 | 1 | - | **test-only** |
