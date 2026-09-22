# 实现计划：检索策略经验库按笔记本分区（PR-1 后端）

状态：执行中（2026-09-22）。规格：`docs/superpowers/specs/2026-09-22-retrieval-experience-per-notebook-design_zh.md`
（规格优先于本文任何相反表述）。分支 `claude/silicon-notebook-agent-memory-afb04a`，worktree
`R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/user-issue-source-tracking-712f93`。

## 全局约定

- 本 PR 只做规格 §14 的 **PR-1 后端**：迁移、存储、取数、蒸馏双链路、注入合并读取、守卫、
  生命周期登记、文档。不做界面（PR-2）、不动注入闸默认值（PR-3）。
- `PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`；后端测试在 `$R/backend` 下
  `SILICON_NOTEBOOK_ENV_FILE="" MODEL_SERVICES_CONFIG="" EXTENSIONS_CONFIG="" $PY -m pytest -p no:cacheprovider -n 4 <files>`。
  G1 全门 `bash scripts/check.sh`（在 `$R` 下）。
- G3 PostgreSQL 泳道本机跑法：一次性建库 `silicon_notebook_<x>_test` + `pg_trgm`/`btree_gin`，
  `TEST_POSTGRES_URL="postgresql://huzhifeng@localhost/<db>" $PY -m pytest tests/postgres -m postgres_integration -q -n 1`。
- 基线（改动前）：五个经验库测试文件 156 项全绿；本机 PG 库 `retrieval_experiences` 0 行。
- 隐私守卫 `backend/tests/test_retrieval_experience_privacy_guard.py` 判据二在 projection / job /
  block 三个模块里禁止 `notebook_id` 这个名字出现——**T2 的守卫改动与 job 改动必须同一任务落地**，
  否则中间态守卫红。projection 与 block 两个模块自始至终零豁免。
- 任务顺序 T1 → (T2 ∥ T3) → T4；T2/T3 改动的文件互不相交（T2：ports 取数/ask_state_store/job/
  repository_runtime/config/守卫；T3：reasoning_retrieval/block/注入测试）。每任务收尾相关测试绿；
  T4 后整条 `scripts/check.sh` 绿 + G3 本机绿。
- 每任务完成后各跑一次 `spec-review` 与 `code-quality-review`，修完再推进。
- 热函数行数上限：`scripts/architecture_boundary_baseline.json` 登记的函数（含 `ReasoningRetriever`
  里的若干）新增语句要放在尾注释之前，且不得超上限；超了就抽 helper，不改 baseline。

## T1 — 迁移 + 存储分区 + 内容寻址 id + 生命周期登记（opus）

改动：

1. `backend/app/repositories/sqlite/migrations.py`：`_migration_79`（`add_column_if_missing(
   "retrieval_experiences", "notebook_id", "TEXT NOT NULL DEFAULT ''")` + 非唯一索引
   `idx_retrieval_experiences_notebook(notebook_id)`），`SCHEMA_VERSION = 79`，模块头的版本注释加一行。
   `backend/app/repositories/postgres/migrations/0059_retrieval_experience_notebook.sql`：
   `ALTER TABLE retrieval_experiences ADD COLUMN notebook_id text COLLATE "C" NOT NULL DEFAULT '';`
   + `CREATE INDEX idx_retrieval_experiences_notebook ON retrieval_experiences(notebook_id);`。
   凡是钉最新版本号的测试（`grep -rn "== 78\|migrate() == 58" backend/tests`）同步到 79/59。
2. `backend/app/repositories/ports.py`：常量 `RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES = 100`
   （紧邻 `RETRIEVAL_EXPERIENCE_MAX_ENTRIES`，注释说明分区语义）。
   `RetrievalExperienceStorePort`：`read_all(limit)` **改名** `read_partition(notebook_id, limit)`
   （`''` = 全局分区；只返回该分区的行，`ORDER BY id`）；`upsert_experience(..., notebook_id="")`
   keyword-only；`evict_to_limit(max_entries, notebook_id="")` 只在该分区内计数与删除；
   `count(notebook_id=None)`（None = 全表）；`read_experience`/`note_adopted`/`version_signal` 不变。
   Protocol 方法总数若变动，同 diff 调 `scripts/architecture_boundary_baseline.json`（只许下降或持平——
   这里是改名不是新增，应持平）。docstring 改写「部署级全局」措辞为「按分区」。
3. `backend/app/services/retrieval_experience_projection.py::experience_id(situation, action,
   notebook_id="")`：`notebook_id == ""` 时哈希输入与今天**逐字节相同**；非空时输入
   `{"notebook_id", "situation", "action"}` 排序序列化。⚠ 本模块受判据二禁键扫描：`notebook_id`
   这个名字在 projection 模块里**不许出现**——参数命名为 `partition`（这是 projection 模块唯一
   例外，理由写进 docstring：它是内容寻址输入的一部分，不是任何 run 的属性），T2 的 job 模块
   调用时传 `partition=notebook_id`。
4. 两侧 store（`backend/app/repositories/sqlite/retrieval_experience_store.py`、
   `backend/app/repositories/postgres/retrieval_experience_store.py`）：行投影加 `notebook_id`；
   上述端口方法；INSERT 写 `notebook_id`；`upsert` 的 UPDATE 分支不改 `notebook_id`（id 已含分区）。
5. 调用点适配（只做改名，不改语义，T2/T3 再改语义）：`retrieval_experience_job.py` 的
   `read_all(...)` → `read_partition("", ...)`；`reasoning_retrieval.py::_cached_experiences` 同；
   `scripts/merge_dbs.py::_evict_experiences_to_limit` 改按分区收容（全局 300、每个非空分区 100），
   注释改写；`backend/tests/test_merge_dbs_taxonomy.py` 若钉了淘汰行为则补分区用例。
6. `backend/app/services/notebook_delete_tables.py`：Group D 加 `DirectTable("retrieval_experiences",
   "one", pk_column="id")`（按 `notebook_id=?` 直删，`''` 行永不匹配）；跑
   `tests/test_notebook_delete_rows_and_files.py`、`tests/test_notebook_delete_review_fixes.py`、
   `tests/postgres/test_notebook_delete_rows_and_files_pg.py` 看登记/覆盖断言。
7. 深拷贝：两侧 `sharing_store.py` 注释段（sqlite:111、postgres:96 附近）改写为「有 `notebook_id`
   列但**刻意不复制**——镜像 `agent_notebook_profile`，副本从零攒」；若有「带 notebook_id 的表必须
   在复制清单或刻意缺席清单里」的守卫测试，把它登记进刻意缺席清单。
8. `backend/app/repositories/postgres/schema_manifest.py` / `backend/app/migration/shadow/manifest.py`：
   按现有守卫要求登记新列（非唯一索引不改 unique surface 计数；表数/row slot 不变）。
9. `docs/development_zh.md:87` 与 `docs/development.md:68` 所在的 schema 历史段追加一句
   「SQLite v79 / PostgreSQL 0059 给 `retrieval_experiences` 加 `notebook_id` 分区列（`''`=全局）与
   非唯一索引；不新增表/外键/unique surface」，并把 `docs/development_zh.md:114`/`.md:346` 的 v54
   段里「刻意没有 notebook_id」「刻意不建索引」两句改成指向 v79。

测试：`tests/test_retrieval_experience_store.py`（新增：分区读写隔离、`evict_to_limit` 只删本分区、
`count(None)` 与 `count("")`、全局 id 与改动前逐字相同的固定值断言、非空分区同 (情境,动作) 得到不同 id）、
`tests/postgres/test_retrieval_experience_store_conformance.py` 同步、迁移升级测试、删除/深拷贝/合库
守卫、shadow/manifest 守卫、`test_retrieval_experience_job.py`/`_injection.py` 只做改名适配后仍绿。

验收：T1 结束时行为与改动前**等价**（所有写入落 `''` 分区，注入仍读全局分区）。

## T2 — 取数谓词 + 蒸馏双链路 + 运行时接线 + 配置 + 守卫（opus）

改动：

1. `ports.py::AskStateStorePort.recent_completed_ask_runs(*, job_limit, step_limit, notebook_id=None)`；
   两侧 `ask_state_store.py`：`notebook_id` 非 None 时加 `AND notebook_id = ?` 谓词（其余逐字不变）。
   docstring「刻意无 notebook 谓词」改写为「全局链路不传、单库链路传；两者都无 user 谓词」。
   `project_run_row` **不变**（仍不带 notebook_id）。
2. `backend/app/core/config.py`：`retrieval_experience_notebook_trigger: int = Field(10, ge=1,
   validation_alias="RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER")`，注释写明「10 < 批上限 40，相邻批重叠
   由 provenance 去重吸收」（规格 §4）。
3. `backend/app/services/retrieval_experience_job.py`：
   - `note_ask_completed(notebook_id: str)`：全局计数与该库计数（`dict[str, int]`）同时 +1；各自到
     阈值各自入队。单飞槽位仍是一个 `_running`；待跑分区进有界去重队列（`collections.deque` +
     `set`，上限 64；满了丢最旧并记 `_emit("skipped", reason="queue_full")`）。跑完在 `finally` 里
     原子取下一项（沿用 `_maybe_requeue` 的临界区形态），全局链路也走同一队列（分区 `""`）。
   - `run(partition)` / `_distill(partition)`：取数传 `notebook_id=partition or None`；既有条目
     `read_partition(partition, cap)`；`upsert_experience(..., notebook_id=partition)`；
     `experience_id(situation, action, partition=partition)`；`evict_to_limit(cap, notebook_id=partition)`，
     cap 按分区选 300/100。
   - `distill_now(notebook_id) -> bool`：自行过 `distillation_wiring_active`，busy 返回 False；供 PR-2。
   - 事件 `retrieval_experience_distilled` 增加 `partition: "global" | "notebook"`，**不带 id**。
   - `_ID_SHAPE` 之外追加 `_NOTEBOOK_ID_SHAPE = re.compile(r"\bnb-[0-9a-f]{6,}")`，两者任一命中即丢弃。
   - 模块 docstring 与 `note_ask_completed` docstring 改写：明确「触发器现在知道是哪个库、仍不知道是谁；
     分区 id 只作为参数流经本模块，从不进 `RunObservation`/`ObservedRun`/prompt」。
4. `backend/app/services/repository_runtime.py:1225` 与 `:1253`：改传 `notebook_id`（镜像 `agent_profile`
   那一条的 lambda 写法）。`_AskCompletedAccess` 若是零参 callable 包装则按现有形状保持零参、在
   lambda 里闭包 `notebook_id`。
5. 守卫 `backend/tests/test_retrieval_experience_privacy_guard.py`：
   - 判据二：禁键表**保留** `notebook_id`；对 job 模块加显式白名单——`notebook_id` 只许作为
     `ast.arg`（形参）、`ast.Name`（读该形参）、`ast.keyword`（实参名）出现；`Subscript`/`Attribute`/
     `Constant` 字符串形态一律仍违规。projection/block 零豁免。加一条变异用例：往 job 模块源码里
     注入 `row["notebook_id"]` 读，断言判据二能抓住。
   - 判据八（静态）：`RunObservation`/`ObservedRun` 字段名集合与 `project_run_row` 返回键集合都不含
     `notebook_id`。
   - 判据九（运行时）：用特征 id（如 `nb-guard9deadbeef`）跑一遍分区蒸馏（假模型记录收到的 prompt），
     断言 `render_observations`/`render_existing` 输出与完整 prompt 都不含该 id；再跑一遍全局分区，
     两份 prompt 逐字节相同。
   - 判据四补：rationale 含 `nb-abc123` 形状被拒。

测试：`tests/test_retrieval_experience_job.py` 改签名 + 新增：单库满 10 触发且只采样该库；另一库 0 行；
全局链路 40 不变；队列去重与满队列 skip；busy 期间攒满不丢；`distill_now` 关闭态返回 False 且零查询；
事件 `partition` 值；`tests/test_repository_runtime*.py`（或其接线测试）断言完成通知带 notebook_id。
两侧 `ask_state_store` 的谓词用例（SQLite 单测 + PG conformance）。

验收：规格 §11 第 1、2、7、8、9 条。

## T3 — 注入合并读取（opus）

改动：

1. `backend/app/services/reasoning_retrieval.py::_cached_experiences(store, notebook_id)`：
   缓存改为 `OrderedDict` LRU（上限 64 个分区）+ 全局分区单独一项；键 `(notebook_id, version_signal)`，
   `store_ref` weakref 语义不变；任何 `version_signal` 变化整体失效。
2. `backend/app/services/retrieval_experience_block.py`：新增纯函数
   `select_experiences_layered(primary, fallback, situation, top_k=...)`：先在 `primary` 上跑
   `select_experiences`，名额未满再在 `fallback` 上跑并跳过已占动作；返回顺序 primary 在前。
   ⚠ block 模块受判据二零豁免，参数名用 `primary`/`fallback`，不出现 `notebook_id`。
3. 三个消费点（`:4576` 任务级、`:6735` consult_memory、`:7155` 步级提示）改读
   `_cached_experiences(store, notebook_id)` 与全局两份，任务级用 `select_experiences_layered`；
   consult_memory 差集面与 `worst_experience_for` 读「本库 + 全局」拼接列表（本库在前）。
4. trace 步 `experience` 的 detail 增加 `notebook_entries`（送达行里来自本库分区的条数；按送达顺序
   与 primary 长度求交）。`rendered_experience_count` 不变。
5. 采用回写不变。

测试：`tests/test_retrieval_experience_injection.py` 新增：库 A 有条目、库 B 只有全局 → A 的 run 块里
本库条目在前、B 的 run 看不到 A 的条目；同动作跨分区唯一；LRU 淘汰与 version_signal 失效；
`tests/test_step_level_experience_nudge.py` 与 consult_memory 用例在分区条目上通过。

验收：规格 §11 第 3、4 条；`RETRIEVAL_EXPERIENCE_INJECT_ENABLED=false` 时注入侧零查询、零 trace 步（不变）。

## T4 — 文档同步（sonnet，impl-task）

1. `docs/superpowers/specs/2026-08-18-agentic-memory-design.md`：§12-Q3 追加「2026-09-22 翻案，见
   本规格」；新增 §10.4「按笔记本分区（2026-09-22）」偏离登记，内容照抄本规格 §12 五条。
2. `docs/product-and-api_zh.md`「检索策略经验」节与 `docs/product-and-api.md` 对应节：作用域改写
   （本库分区优先 + 全局回退）、单库阈值 10/上限 100、事件 `partition`、trace `notebook_entries`。
   数值只在这里登记。
3. `docs/deployment-and-configuration_zh.md:980-984` 与 `.md` 对应行：新增 `RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER`
   行；`RETRIEVAL_EXPERIENCE_TRIGGER` 行「部署级全局」措辞改为「全局分区链路」。
4. `architecture.md` 若有「经验库全局」表述同步；`fangan_todo.md:120` 那条改写为「PR-1 分区已合入，
   PR-2 界面 / PR-3 开闸待做」（合入后再改状态，本任务先改措辞）。
5. 跑 `tests/test_architecture_documentation.py`、`tests/test_gap_consult_docs_contract.py` 等文档
   对账测试；`scripts/check_ui_vocabulary.py` 不涉及（无界面改动）。

## 收尾（PR-1）

- `bash scripts/check.sh` 全绿；G3 本机 `-n 1` 全绿。
- `git fetch` + rebase 到最新 `origin/master`，重跑门禁。
- 开 PR（描述含：规格链接、分区语义、守卫改动、验证证据、刻意偏离五条）；codex 评审闭环按 CLAUDE.md。

**PR-1 已合入：#769（2026-09-22，codex 1 轮零意见，CI 7 check 全绿）。**

---

# PR-2 界面（分支 `claude/experience-partition-ui`，基线 `69004a24`）

规格 §8 + §13-Q2（条目列表给全体成员看）。用户裁决「不用等我决策，把这一系列 PR 做完」，
以下取舍由主 agent 拍板：

- 三个端点挂在 P1 理解面板的路由家族下（`backend/app/api/agent_profile_routes.py`），复用
  `require_notebook_read` 与 `agent_profile:write` 能力判据；运行时 jobs 服务经 `deps.py` 的
  既定写法 `repository()._runtime.retrieval_experience_jobs` 取（facade 公开面只可收缩，不加席位）。
- 「立即整理」与「清空」都要求 `agent_profile:write`（与 P1 共享底座「重新整理」同一口径：
  owner / 可写成员），并过 `notebook_mirror_fence`；读列表只要读权。
- 清空 = `store.evict_to_limit(0, notebook_id=…)`（不新增端口方法：ports Protocol 计数是零余量棘轮）。
- 动作 id 是封闭词表，后端原样返回；界面用自己的词表映射成中文（同 `reasoning-trace.ts` 的步标签口径），
  **不得**把 id 直接上屏（词汇守卫）。

## 接口契约（前后端共用，两个子代理各按此实现）

`GET /notebooks/{id}/understanding/experiences` → `ExperiencePartitionResponse`

```json
{
  "enabled": true,               // distillation_wiring_active(settings, store)；false 时 entries=[]、count=0
  "count": 3,
  "updated_at": "2026-09-22T10:00:00+08:00" | null,   // 分区内 MAX(updated_at)
  "can_manage": true,            // agent_profile:write 且未被镜像围栏拦下
  "entries": [
    {"action": "exact_lookup", "polarity": "bad", "rationale": "…", "support": 3, "adopted": 0,
     "updated_at": "…"}
  ]                              // 按 (support desc, updated_at desc, id asc)，最多 100 条（分区上限）
}
```

`POST /notebooks/{id}/understanding/experiences/distill` → `{"started": true}`；总闸关 → 409
`_DISABLED_MESSAGE` 同款文案「此功能已关闭」；单飞占用 → 409「正在整理，请稍后再试」；
无 `agent_profile:write` → 404（镜像 rebuild）；镜像围栏 → 同 rebuild。

`DELETE /notebooks/{id}/understanding/experiences` → `{"removed": 3}`；权限口径同上；总闸关也允许
清空（既有行照常能删，镜像 Agent 记录「关开关是从现在起不记，不是把记过的藏起来」）。

## T5 — 后端端点（opus）

`backend/app/models/agent_profile.py` 三个响应模型；`backend/app/api/deps.py` 新增
`retrieval_experience_jobs_service()`；`agent_profile_routes.py` 三个 handler；
`docs/product-and-api_zh.md:1996` 起的端点表与 `.md` 对应表各加三行；测试
（`grep -rln "understanding/rebuild" backend/tests` 找到既有路由测试文件，镜像其 fixture）：
读权/无读权、`enabled=false` 形状、排序与上限、`can_manage` 三态、distill 的 409 两种、
delete 只清本库分区且全局分区不动、镜像围栏。API 面变化跑 `tests/test_openapi*`/契约快照
（`grep -rln "agent-observations" backend/tests` 找齐）。

## PR-2 之后的本机试跑发现（2026-09-22，为 PR-3 定范围）

在 worktree 起 8010 试跑实例（deepseek-flash，仓库 docs 作语料，一个笔记本 15 次 reasoning 提问）：

1. **同步 `POST /notebooks/{id}/ask` 与 MCP `ask_notebook` 都直接调 `repo.ask(...)`，不经过
   `AskExecutionCoordinator`，因此从不触发 `_note_ask_completed`**——P1 覆盖层巡固、经验蒸馏、
   回答偏好归纳三条链路对这两类提问全部零计数。只有网页走的 `ask/stream`（durable 协调器）会计。
   MCP 是一等写入侧（Agent 的提问正是「越用越熟」的输入），这是缺陷。
2. **主 checkout 的 `.local/model-services.toml` 生成于 P1/P2 之前，缺 `agent_profile_consolidate`
   与 `retrieval_experience_distill` 两个绑定**；注册表对未绑定工作负载返回 `None`、无任何启动告警，
   P1 巡固作业直接落 `failed:模型未配置，无法整理`。生产大概率同样过期——这就是 P1 本机
   `runs=0`/生产没动静的真因。`scripts/migrate_legacy_model_env.py` 本身会遍历全部 WORKLOADS，
   重新生成即可；但缺少告警意味着下一个新增工作负载会重蹈覆辙。
3. 每题固定形状的歧义问题会被意图闸 422 拦下（正常行为，试跑脚本跳过）。

# PR-3 开闸与两处缺陷修复（分支 `claude/experience-partition-enable`）

- **T7 提问完成钩子覆盖同步与 MCP 路径（opus）**：在 `RepositoryFacade.ask`（同步 `/ask` 与 MCP
  `ask_notebook` 的共同入口）返回后调用 `runtime._note_ask_completed(notebook_id, user_id, mode_id)`；
  durable 路径调的是 `service.ask`，不会双计。fail-open 口径同协调器（已交付的答案不因记账失败改判）。
  用例：同步 `/ask` 与 MCP 各一条，断言三条链路的 `note_ask_completed` 被调且参数正确；durable 路径
  仍只调一次。`test_agent_profile_job_overlay.py` 的 AST 守卫扩到这个新座位（同样钉 `mode_id == "reasoning"` 门）。
- **T8 未绑定工作负载启动告警（sonnet）**：`startup_warmup` 在 READY 之前对每个 chat 工作负载检查
  `models.configured(id)`；未绑定的按 `_WORKLOAD_LABELS` 列一条 WARNING（一行汇总，不逐条刷屏），
  其中 `agent_profile_consolidate` / `retrieval_experience_distill` 在对应特性开关为开时升格为
  独立一行「特性已开但模型未绑定」。同时给 `/admin` 模型服务状态页已有的未配置态加同一句提示
  （若该页已能显示未绑定则不改前端）。文档：`docs/deployment-and-configuration*.md` 加「升级后重新
  生成 model-services.toml 或补两行绑定」的运维提示。
- **T9 注入闸默认值**：本机试跑（走 `ask/stream`）确认单库分区真的蒸出条目、开闸后 trace 出现
  `experience` 步且 `notebook_entries>0` 后，把 `retrieval_experience_inject_enabled` 默认改 `True`，
  `REASONING_CONSULT_MEMORY_ENABLED` 不动；文档 zh/en（deployment、product-and-api、设计文档 §12-Q3 与
  新规格 §13-Q3）与 `fangan_todo.md:120` 收官。若试跑蒸不出条目（全 NOOP），默认值**不翻**，
  只合 T7/T8，并把原因写进 fangan_todo。

## T6 — 前端面板（opus）

`frontend/features/agent-profile/profile-model.ts`：类型 + `EXPERIENCE_ACTION_LABELS`
（retrieve 检索 / ppr 漫游 / exact_lookup 精查 / expand 扩展 / expand_community 对比 /
follow_chain 推导 / enumerate 枚举 / outline 大纲 / search_chunks 段落 / read_document 整篇取样，
未知 id 显示「其他」）+ `EXPERIENCE_POLARITY_LABELS`（good「好用」/ bad「不好用」）；
`profile-api.ts` 三个函数；`agent-profile-panel.tsx` 新增第四张卡「检索经验」（模块名
`understanding-module-name` 同款），`<details>` 懒加载（镜像「Agent 记录」的 epoch 守卫）；
头部一句「这个库攒下的检索经验 N 条，最近更新 …」；列表每行：动作词 · 好用/不好用 · 理由 ·
「N 次提问支持」；按钮「立即整理」（按下即 disabled + 文案「整理中…」，结果落在按钮旁：
成功「已开始整理，稍后刷新」/ 409 原文）与「清空」（两步确认，镜像 Agent 记录的
`confirmingAll`；成功后列表清空并在按钮旁显示「已清空 N 条」）；`enabled=false` 时卡内只显示
「此功能已关闭」且不显示两个按钮；`can_manage=false` 不渲染按钮。词汇守卫：不得出现
蒸馏 / 打法 / 画像 / 巡固 / 分区 / 全局库 等内部词。测试：`tests/component/agent-profile-panel.component.test.tsx`
新增用例（列表渲染与标签映射、立即整理的按下态与结果文案、409 文案、清空两步与结果、
enabled=false、can_manage=false）；`tests/guards/agent-profile-guard.test.mjs` 的③（只经
profile-api）与②（无黑话）自动覆盖新代码，确认其绿；`scripts/check_ui_vocabulary.py` 绿。
