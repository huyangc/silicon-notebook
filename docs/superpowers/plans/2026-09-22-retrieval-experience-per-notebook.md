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

## 收尾

- `bash scripts/check.sh` 全绿；G3 本机 `-n 1` 全绿。
- `git fetch` + rebase 到最新 `origin/master`，重跑门禁。
- 开 PR（描述含：规格链接、分区语义、守卫改动、验证证据、刻意偏离五条）；codex 评审闭环按 CLAUDE.md。
