# Claude Code 在本仓库的操作规范

本文件对 **Claude Code** 生效，每个会话自动加载。

`AGENTS.md` 是本仓库完整的开发契约，但 **Claude Code 不会自动加载它**（只加载 `CLAUDE.md` 与 `.claude/rules/`），所以本文件承担两件事：把必须随时在线的红线内联在这里，再给出 `AGENTS.md` 的章节索引供按需查阅。**两者冲突时以 `AGENTS.md` 为准**——它是真源，本文件是摘要（穷举的例外见第一节末尾）。改动开发约束时，本文件与 `AGENTS.md`、两份 README 一起改。

---

## 一、红线

不读 `AGENTS.md` 也必须遵守的部分。

### 工作区

- 凡任务会写仓库代码、测试、文档或配置，第一次写入前必须新开 **git worktree** 和分支；该任务期间主 checkout 只读，小修也不例外。当前目录若已是隔离 worktree，就在原地继续。纯调研、设计、状态汇报和只读审查除外。
- **在 worktree 里跑 `npm install` 之前先 `ls -l frontend/node_modules`**：若它是**软链**（指向主 checkout 的共享安装树），装依赖会写穿真树，绝不能跑——改依赖去主 checkout，或用 `cp -Rc` 拿隔离副本。若它是真目录或不存在，照常装即可。软链由开发者本机的 SessionStart hook 建立，不是仓库产物，所以这条**必须先看一眼再决定**，不能当成无条件红线。
- 不回滚用户的改动；不删生成物或用户提供的文件，除非用户明确要求。
- 改文件用 Edit/Write，不要用 shell 重定向整体覆写。

### 交付完整性

- **全栈对等**：面向用户的后端能力必须在同一次改动里带上前端 UI。不接受只做一侧。
- **公式渲染**：紧邻正文、独占一行的单行 `$$...$$` 仍按块级公式处理；Ask、报告、Memory、Knowhow 的宽公式只在所属内容块内横向滚动。来源详情、知识对象标题及知识图谱「出处」卡中的 `formula` 元素共用 KaTeX 渲染；直接调用 KaTeX 前先剥除包裹整值的 Markdown 数学定界符，解析失败必须显示原始内容，不能留下空白或仅错误态的可视化。
- **文档同步**：影响安装、产品行为、架构或开发约束时，`README.md`、`README_zh.md`、`AGENTS.md`、`CLAUDE.md` **四份**一起改，并同步 `docs/` 下负责该主题的中英文权威文档。根 README 只保留精简入口，详细契约不得重新堆回 README。漏掉本文件，Claude Code 侧的规范就会悄悄过期——那正是本文件存在的原因。
- **用户使用总览**：先对 `/admin/users` 返回的完整用户集合排序，再做前端分页；默认每页 20 条，可切换 20/50/100 条。用户名、角色、注册时间、各类用量、最近活跃和文档上限表头可切换升降序，排序或翻页时清理已隐藏行的临时交互态。
- 完成 `silicon_notebook_fangan.md` 里定义的特性时，同批更新 `fangan_done.md`。
- 提交的文档保持通用口径：绝对解释器路径、本机端口占用这类**机器特定细节不进 git**。

### 硬门

- 完整本地门是 `bash scripts/check.sh`（后端 pytest + 语法/契约/harness + 前端 test/typecheck/build 三条并发泳道）。CI 只是它的只读包装，不要在 workflow 里另起测试根。
- **仅 Codex 的沙箱规则（Claude Code 不适用）**：Codex 第一次运行 `scripts/check.sh` 就必须申请沙箱外执行，因为后端生命周期测试会绑定 loopback 端口并管理子进程；不得先在沙箱内试错。GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）也必须直接申请沙箱外执行；普通本地只读 Git 检查仍在沙箱内完成。
- 数据库专项门只覆盖直接 PostgreSQL 后端；已退役的 SQLite 后端实现专项测试、SQLite→PostgreSQL 导入/正向 shadow 测试与跨后端 parity 测试不得重新加入当前套件。
- **schema**：加表或改结构必须**追加** `_migration_N` 并 bump `SCHEMA_VERSION`，不要塞进已封版的旧迁移——版本闸会对已部署库短路，`IF NOT EXISTS` 救不了没被执行到的语句。当前 SQLite schema 为 v34；PostgreSQL checksummed schema 为 v12；除关系端点 keyset 覆盖索引外，两侧还包含关系补全的来源代次水位与 `(source_id,id)` 对象索引。
- **界面词汇**：面向用户的文案只用「界面词」，不得出现 `projection`/`tier`/`canonical`/`chunk`/`KG`/`schema` 这类内部黑话。真源是 `AGENTS.md`「界面词汇表」，`scripts/check_ui_vocabulary.py` 是硬门。**唯一放行的英文界面词是「图谱 Schema」**（图谱对象类型/字段管理，原「内容类型」，现从知识图谱视图头部进入）——守卫的 `SANCTIONED_UI` 只放行这一个复合短语（带 CJK 前置断言，不吞「知识图谱」尾字），裸 `schema`/`Schema` 仍拦。
- **错误文案**：deny by default，信任按**出处**判定而非文本形状。后端中文用户文案必须走 `backend/app/api/deps.py` 的 `user_error()`（打 `X-User-Message` 头），前端翻译只在 `frontend/app/errors.ts`。
- **Knowhow 导入校验**：属性按行/按列必须在解析前按用户选择统一；横向合并记录分组导致展开后首行重复时，应提示改选「属性按行」。只有 `GridParseError` / `KnowhowImportValidationError` 的固定可操作文案可经 `user_error()` 上屏，其他 `ValueError` 保持诊断专用。
- **`object_type` 标签**：后端 `OBJECT_TYPE_LABELS` 与前端 `KG_TYPE_LABELS` 必须逐字一致，改一侧就要改另一侧。
- **架构守卫**是语义化的（`{path, scope, kind, target}`），**不含行号**——仓库里任何提到「行号钉死」的注释都是过时残留。重生成走 `--rebaseline-surface` / `--rebaseline-callers`；新端点必须跑默认模式刷 `api_contract`。
- **knowhow 变更历史**（`knowhow_changes`/`knowhow_milestones`，schema v26）：knowhow 表的**每条写路径**必须在写事务**最后一步**经模块级 `record_change` 追加一条流水（存 before/after + 变更后整表指纹，复用传输守卫的 `_FINGERPRINT_SQL`）；`test_knowhow_history_coverage_guard.py` 是硬门，新增写方法漏挂就报红。回退是逆序 delta 重放 + 前后置指纹守卫（行/列复用原 id 保引用与代码附件）；里程碑创建与 `create_milestone` 的复检必须在 `BEGIN IMMEDIATE` 之内，清理只删最老连续前缀且永留 head。完整契约见 `AGENTS.md`「Architecture Baseline」的 knowhow 历史条目与 `architecture.md` § 3.7。
- **Knowhow 批量规整/审计/列宽**：候选生成并发固定为 `min(3, knowhow_reformat 实时服务容量)`，状态失败回退 2；相同 `(column_id, trimmed 原文)` 只允许 single-flight，只有成功且未 stale 的结果可复用，取消后停止调度并丢弃迟到 epoch。保存仍按既有完整物理/共享单元串行，必须保留 frozen snapshot、`expected_before`、anchor/精确成员守卫、409 stale 和关闭弹窗后 reload。diff 在同一弹窗做有界 Markdown 行级+token 级比较，超长降级；若批次已观察 stale，打开已保存项必须先关闭弹窗、等待带 epoch 守卫的父表 reload 完成，再按新 detail 重算并打开 `position,id` 最小代表格，禁止闪开旧目标；reload/epoch 失败或行/列消失时不得打开，并走现有可恢复的表操作错误横幅。整体确认不变成逐项 accept/reject。主表只从有界可见行样本 memoize `colgroup` 宽度，且必须在 Markdown 正则/分行/grapheme 之前截断固定 code-unit 前缀，保留 fixed layout、横向滚动与 sticky 首列。owner/权限/`created_by` 继续用稳定 user id；普通 session 审计 label 统一 username→display_name→id，Agent 用 profile_name，复制/导入/转移必须拆 identity id 与 actor label；历史 user-id 只在读时有界批量解析且同步 identity 查询必须在线程池。`origin=agent` 的 change 只解析 `payload.before` 中被替换的旧人类 updater，actor 与 `after/current` Agent label 原样保留。禁止 N+1、破坏性迁移或为显示重写参与 fingerprint 的 `knowhow_cell_code.updated_by`；单格代码 GET/PUT 响应必须返回可读 `updated_by`，保证 session 保存后立即显示来源。
- **knowhow 知识对象检索**：`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED` 默认 true；格子对象进入 reasoning/graph 的节点检索并保留行跳转引用，false 只回滚该直接节点路径，格子 chunk 仍可搜索。动态类型必须按 `knowhow_tables.hidden_source_id` 收窄；chunk-vector→KO 旁挂版本同时覆盖图变更序号和 Knowhow 向量代次（纯向量修复不 bump KG），并随 KG 变更显式失效。
- **数据库后端选择**：DATABASE_URL selects the formal repository backend through one repository factory. Exactly one active repository backend is selected centrally from `DATABASE_URL`. SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default. 选择只存在于 factory；service/store 不判断 dialect，也不 import 对侧 adapter。`SHADOW_DATABASE_URL` 单独设置仍不参与选择/同步；SQLite34/PG12/epoch1 临时 shadow 边界已有 preflight/control/guard、run-bound 原子 snapshot、有界可续跑 baseline COPY/H0，以及 fail-stop 单消费者正向 apply 原语。正向引擎从 checkpoint+1 连续读全局 SQLite seq，在短只读 snapshot 中仅为 upsert hydration 当前行，delete 保持 key-only、hydrated bytes 为零，并显式保留七张表的 rowid ordinal；同一 stable key 在 accepted prefix 内保留最后 event 并按全局最后 seq 排序，raw seq/checkpoint 仍连续，每个 identity 的最终 actual apply 覆盖 synthetic dependency contribution，只有 dependency-only identity 才计一次 synthetic 行与 bytes；短读窗口若在 allocated high-water 前结束，会在 hydration/apply 前立即判为 suffix gap；满窗口低于 high-water 时在同一 snapshot 探测相邻 seq，缺失即失败；批次硬上限 4096 events/64 MiB；仅一个 final bundle 可独占超限，同 key replacement 若在已有其他 actual bundle 时使 bytes 超限则回滚并留到下一批。FK 父闭包只读同一已验证 source snapshot，每事件最多 64 行；固定 v12 图按 FK constraint branch 计数的上界为 9 个 row slots，依赖行计入 bytes 且批内去重，不扫描 suffix log。PG 仅延后 FK/UNIQUE ordering SQLSTATE；CHECK/NOT NULL 立即 poison。精确 PG12 catalog 派生的 83 个 unique surface 均有静态停车方案：nullable 列用 NULL；无 FK/CHECK 的 text/bigint 列使用按其他唯一列的非 NULL 等值/NULL `IS NULL` 和固定 predicate 限定范围的确定性候选（`C` collation 文本 max 拼 `chr(1)`，或先走可索引 bigint MIN/MAX 快速路径选择 min−1/max+1，仅在两个 int64 边界都已占用时扫描首个 gap）；仅对无入向 FK 且批内存在 current-final 恢复行的叶表做同事务 delete/reinsert。停车状态以 `(unique surface, row identity)` 为单位，单个 stagnant pass 停车所有可独立停车的冲突，final apply 成功后清除该 identity 的所有停车面。限制为 8 passes、32 actual statements/apply、16384 actual statements 总量；每次候选查询都计入预算，ordering、statement、pass、候选搜索或候选 UPDATE 容量耗尽均 non-poison，而最终窗口仍无法停车的 UNIQUE 冲突按最早实际 seq poison。`run_forever` 从 256 events/8 MiB 自适应倍增到硬上限，仍阻塞则 non-poison。PG apply 事务 claim worker 后、业务 DML 前复查既有 run/direction poison；poison 发布在 binding/checkpoint 校验后锁定检查该方向任意既有记录，完全相同视为 ACK-loss 成功，不同则 stale 且绝不新增第二条；apply、ack-loss 识别和 poison 发布都绑定 snapshot source/target 与 live target identity，snapshot 与业务 apply 前均要求 `progress.applied_seq == checkpoint.last_seq`；事务按 migration→control→run→worker→checkpoint 锁序，锁 ledger+61 表并复核精确 catalog，再同事务提交业务收敛、脱敏 progress 与 checkpoint CAS。`ProgramLimitExceeded`/`DataError` 候选失败按 non-poison capacity 处理，`QueryCanceled` 保持瞬态并整事务有界重试，SQLite path/file binding 失败通过专用 identity 异常分类，不依赖异常文本；已证明的确定性错误只记录实际阻断 seq 的一条脱敏 poison。每个有效 batch 结局恰好记录一条 metric，batch events 使用实际 accepted/observed raw-event 数而非 lag，并尽可能保留 retries；指标不得带表名、key、行值、URL、worker id 或异常文本。禁止持有 SQLite 时等待 PG。显式运维 CLI 负责 preflight/start-forward/status/verify；前台 worker 使用数据库时钟排他 lease、SIGTERM/INT 批次边界，并仅在 FULL 校验、barrier/replay/poison、至少 7 天/100,000 events tail 等边界之后保守清理。这只是 SQLite-active 正向 shadow；cutover、反向复制和自动 `DATABASE_URL` 交换仍未实现。PG 向量存 `bytea`，不需要 pgvector，生产仍固定 `--workers 1`。
- **Shadow 校验**：在 SQLite 只读 snapshot 记录 `Hv` 并流式落入 owner-private 临时 spool，释放 SQLite 后再等 PG checkpoint；以 `REPEATABLE READ, READ ONLY` 固定 `Ht`，再用新 SQLite 事务扫描 `(Hv,Hseen]` 的 retained dirty key，只排除这些 concurrent key，且 verifier barrier 保持到报告提交。Structural 覆盖精确 schema/guard、stable key/hash、FK/unique/cascade、storage-root 文件引用；Full 增加领域投影、float32 bytes/dimension/norm/抽样 cosine 和固定中英检索门禁；Cutover 复核 write-frozen，并要求 `Hv=Ht=MAX(seq)`、零 concurrent、100% coverage 与前一轮完整 full/cutover。报告不得含原文、Memory、token、密码或 URL；仅同级或更强的 clean run 可 supersede drift。
- **SQLite source open 分类边界**：只在 `open_fresh_live_sqlite` 调用处把非瞬态 `sqlite3.OperationalError` 归为 source-binding identity；locked、busy、interrupted open 仍按瞬态整批重试，后续 SQLite operational error 保持原 schema/query 分类。
- **Shadow baseline 安全边界**：snapshot 目录必须 owner-only 且不可为 symlink；snapshot/live fence 必须 fresh 打开 `SqliteDatabase.db_path` 当前文件，禁止复用线程缓存连接，并在 open/transaction 前后及 snapshot 发布/PG commit 前复核 resolved path + `(st_dev, st_ino)`；这是合作式运维边界，检查窗外替换文件不受支持且下一次检查 fail closed。COPY 的所有业务 SQL 全限定到 run 绑定 schema，起始绑定、每个提交批次/完成点和最终 H0 前均在短 `BEGIN IMMEDIATE` 中复核 live capture 仍启用，且不得把该 SQLite 栅栏跨 PG prefix proof/`ANALYZE` 持有。JSONB prefix proof 只在 JSON 子树内统一有限 int/float/Decimal 的精确十进制语义（bool 排除、负零归零），普通 SQL 数值列仍保持类型差异。Resume 使用有界 named server cursor，PG statement 有 timeout，取消在 proof/migration/analysis 间轮询；起始/最终完整验证由 checksummed packaged migrations 派生并覆盖 v9 table/column/PK/FK/unique/check、operational+GIN index 与 `public.pg_trgm`，逐批路径不得反复扫描 61 表 catalog。
- **最终 H0 lease**：最终 SQLite fence 不是瞬时检查；只能在 PG 双锁/run/table lock 与 61 表长 proof/`ANALYZE` 全部完成后取得，必须保持到 PG H0 checkpoint + run progress 事务实际 commit 成功再释放。PG 事务或 commit 失败时 PG 不落 H0，并释放 SQLite；持 fence 时不得再等 PG pool/advisory lock 或执行长 PG 工作。
- **切换与批处理边界**：只改 `DATABASE_URL` 不会复制、迁移或同步既有数据；停机 importer/activation 使用 `scripts/migrate_sqlite_to_postgres.py`，SQLite-active 连续 shadow 使用 `scripts/shadow_sqlite_to_postgres.py`，两者不得共用 target。停机 importer 只排除 SQLite-only 的 `shadow_capture_control` / `shadow_change_log` 运维表并把排除写入 receipt；退役用户数据表仍要求为空。正式切换必须停写、停服务、验证备份、只改一个 URL、启动后校验 readiness/认证/数量/代表性读取再放流量。`batch_ingest` 除 `vectors-to-blob` 外的 mutation phases 同时支持 SQLite/PostgreSQL；PostgreSQL 直连维护必须在构造 repository 前要求 `--confirm-service-stopped`，再由独立非池化 session 持有数据库级 advisory lock，结束时释放锁并关闭 repository。该 flag 只是运维确认，不会停服务；生产 wrapper 共用同一安全 opener。`vectors-to-blob` 因 PostgreSQL 已用 `bytea` 而仅适用于 SQLite。存量部分 KG 用显式 `kg --retry-partial` 修复：模型窗口未全成功或新结果为空时保留旧图，仅“零失败窗口且非空”的结果事务替换。`scripts/check.sh` 保持离线，PostgreSQL 16 只走独立本地/CI integration lane。
- **离线批处理大库边界**：完整/限量 KG、metadata、reextract、向量、关系和反向索引的 PostgreSQL inventory 都必须用 `C` collation 的有界 keyset 页；完整 KG 仍保留 durable `kg_build_jobs` 单飞语义，只把 inventory/提交窗口分页。非 durable 池化阶段收到 Ctrl-C/SystemExit 时必须在 advisory lock/repository 释放前取消排队任务并排空已接受任务；SIGTERM/SIGHUP 保持默认进程级终止。
- **Knowhow 智能补全空列**：记录型表从行详情、带行标题分组的表从概念矩阵的物理分支显式发起；只有缺失格或精确空串可补，已存纯空白文本不算空。一轮请求把同表最多 8 条参考行（同 anchor/行标题分组优先，再比较已知列相似度与覆盖度）与一次有界 `ReasoningRetriever` 全库检索合并；全库只指当前 notebook + 当前有效显式挂载库。它复用 Ask `reasoning` 的规划/联邦检索/反思/扩展/查询期推导，但补全专用策略会在候选进入模型反思前排除私有 Memory 与当前表自身投影，并关闭来源归属不透明的 PPR/社区扩展；绝不能调用 `ask_reasoning`/`ask_answer`、创建对话/job 或保存 Ask 答案。响应必须带最终推理轨迹和服务端生成的库内证据 key；模型只能引用合法 evidence key 或同表行 id，无合法引用的建议强制 abstain。`reasoning_agent` 负责检索，`knowhow_complete` 负责结构化合成；两阶段都用 system 级不可信证据指令。推理响应畸形、任一 provider 未配置/执行失败，或合成响应不可解析/顶层结构不可用时显式失败；单条建议畸形则过滤、降级或转成 abstain，任何情况都不能静默退成同表补全或伪造离线结果。审阅弹窗分开显示同表参考与禁用链接/图片的库内 Markdown 证据，保持可拖动和逐项人工确认；确认仍走普通 cell PATCH，固定携带 `expected_before=""` 和 `origin="llm_complete"`，并保留正常历史与同步语义。
- **Ask 会话即时入历史**：历史按带亚秒精度的最近活动时间排序（SQLite 还须按绝对时刻处理 UTC offset 变化），不按创建时间。`/ask/stream` 的首个 `started` 事件必须同时带持久化的 `job_id` 与 `conversation_id`；前端当场将它置顶并设为当前会话。即使用户在 `started` 抵达前就切到同库旧会话，新会话仍须发布并可重新打开；历史发布、请求 generation 与终态摘要刷新都跟随当前 notebook，不依赖旧 run 或会话 epoch，同库旧调用要追随最新请求（包括跳过失败的中间请求），其他 notebook 的延迟请求也不能使当前库的有效响应失效。加载 notebook 最近会话或用户选择的会话详情期间，输入框和模式控制必须保持禁用，避免迟到详情接管新 run。若用户在 `started` 前点击中断，界面立即恢复草稿，但该 run 自己的 controller 仍继续读到 `started`、按返回的 job id 调取消端点后才 abort transport，不能先断流而遗失取消句柄，也不能与重连到其他 job 的取消控制串台。
- **问答引用定位图谱节点**：从知识对象引用打开全屏知识图谱时必须选中并居中目标；目标不在有界高连接度核心图时，按引用真实来源 notebook（包括挂载 base）定向拉取并叠加其一跳邻域，纯 graph-BFS anchor 也必须保留所属 base id。浏览器始终用当前 active notebook 过权限；后端只在它的有效 participant 集内校验/解析对象并内部代理读取，挂载公共 base 不等于获得该 base 的直接成员权限。引用携带的原始 Concept id 通过有界 `cluster_fold_rows` 解析为接口返回的 canonical `focus_id`，同时保留用于 context 的 raw object id，不能拿合成 `K-*` id 查询 `knowledge_objects`；大库 viz 产物不可用时显式返回暂不可定位，不得进入会物化全 notebook cluster map 的 DB fallback。前端保持图谱打开并给出可重试提示，不得挂一个永远消费不了的 raw-id focus。
- **逐步推理档位与完整枚举是两套合同**：`retrieval_effort` 的稳定 id 为 `overview` / `standard`（默认）/ `deep` / `thorough` / `exhaustive`。五档的每查询相关性结果数依次为 `4/8/8/12/16`；最终 `floor/aspect/cap` 为 `8/2/12`、`20/3/36`、`24/4/48`、`32/5/64`、`40/6/96`；最大 `步骤/首轮子查询` 为 `4/2`、`8/5`、`16/6`、`32/8`、`50/10`；KG/原文 prompt 字符为 `4000/12000`、`6000/30000`、`8000/50000`、`12000/80000`、`16000/120000`。最终相关性预算按 `min(cap, max(floor, aspect × 实际查询数))` 计算，模型只可提前停，不能突破上限。候选召回不随档位变化，但由部署参数独立控制：`CHUNK_RECALL` 默认 200，分别约束带索引 Chunk/KG 的 ANN 与词法窗；`RELATION_RECALL` 默认 200，分别约束 Relation ANN 与词法关系 ID 总窗，后者内部仍为 source/target 方向预留份额。这些默认值不是请求级硬上限。意图合同用 `ranked` / `complete` / `aggregate` / `hybrid` 区分范围；Knowhow 的显式完整请求必须走稳定游标而非放大 Top-N。五档共用每页 25 行、最多 50 页/1,250 物理行、8 表、每表 8 列、模型单元格摘录 1,000 字符、结构化载荷 256,000 字符、正文内联 100 行、结果卡初始 20 行；只有游标耗尽且表目录、行数、列元数据和所选表范围均稳定才是 `complete=true`。触及任一安全线或并发改表必须返回 `complete=false` + `explicit_partial`，不得声称“全部”；100 行 Knowhow 表可返回 `100/100`，低档位也不得降低显式完整枚举上限。当前只有 Knowhow 支持完整枚举，其他对象集合仍是相关性结果并须披露边界。
- **完整语义与 prompt 硬预算补充**：确认时按最终编辑措辞与权威澄清答案重算 scope；结构化执行器只处理整表物理行/方法清单、直接物理行/记录计数及其 hybrid。“多少种”等去重/种类计数、条件筛选、group-by 没有确定性计划时回退并披露不支持完整。轻量 catalog 最多返回 8 个表描述，不读取格、代码附件或健康大字段，并在截窗前优先纳入显式点名表。响应分开 per-table、batch、synthesis coverage：枚举 200/200 而模型预览 100/200 时写“枚举完整、分析部分”，8 表截断不污染已耗尽单表。KG 对象/关系、confirmed Memory、查询期链共享 KG 字符硬预算；结构化预览、chunk、direct element 共享原文硬预算；最终证据块不超过两者之和。

### 工程约束

- **效率是一等约束**：新增 LLM / embedding / DB 调用前先问代价——能否合并、缓存、异步、按需 gate。强一致做成 opt-in，默认走低开销路径。
- **大库检索 hydration 必须按候选有界**：ANN 后的孤立节点判断只能对当前候选做带索引的逐候选 `EXISTS`，返回候选 id，不得物化高出度节点的完整邻边；canonical fold 只能经 `cluster_fold_rows` 查询本轮 scored id，不能加载全 notebook cluster map；同一 scale-index 实例同一工件类型的惰性 ANN open 必须单飞。这三项只优化执行成本，不得顺手改 score、阈值、PPR 或召回。
- **词法候选属于索引检索契约**：SQLite FTS5 对每个用户派生 clause 单独按数据转义，可保留整句精确匹配加分，但必须 OR 拉丁字母/数字词项与重叠中文三字片段，禁止把多词查询只包装成一个强制连续短语；PostgreSQL 在原生 trigram 候选生成前拆分同一组有界词项。带索引的 Chunk/KG 检索合并有界 ANN 与词法窗口；带索引的 Relation 检索通过 source/target 索引补入与词法命中 KG 端点相邻的有界有效关系，并保留 FTS 端点顺序、为两个关系方向预留预算，禁止高出度 source 饿死 target-only 命中。候选 id 与语义分 map 必须分开，纯词法候选按 keyword-only 计分，不得写入伪造的零语义分。
- **KG 边契约、补全与来源追踪**：内置 12 种边及核心四类端点只认 `app/services/kg/edge_schema.py`；prompt、抽取验证、trust、graph/PPR/canonical/relation hydration、`follow_chain` 和工件版本都从它派生，非法 core→core 历史边只留审计、不进查询。已知边触及管理员扩展类型时保持兼容。`scripts/audit_kg_edge_contract.py` 只读且不得输出证据正文。跨元素关系补全默认 `off`；`shadow/write` 还必须命中 notebook allowlist 或稳定灰度，按模式和来源代次的持久 keyset 水位逐页处理。每个任务只 hydrate 有界对象/证据 ID 窗口，仅做索引化 relation `EXISTS` 与有界同源 FTS/ANN overfetch，受 section/pair/batch/字符上限约束；未完水位重新入队且启动恢复当前 pending 代次，模式切换用同一个 generation-CAS 事务先发布新模式可恢复游标再把旧 pending 游标标成 `stale`，无索引时保留水位并 fail closed。模型只能选服务端 id，独立 verifier 后在短事务复核 source/run/object/element 并保存 verifier 看到的同一 excerpt，稳定 relation id 保证重试幂等；非法零值护栏不推进，且绝不全扫整本书或整库。检索候选来源必须按 producer 累积，不能从最终 score 反推；`CHUNK_GRAPH_RESERVE` 只为已过原门槛的 graph-only chunk 预留既有预算席位，不扩 token/item 上限。
- **大库冷加载前置 readiness**：默认 `STARTUP_PRELOAD_SCALE_INDEXES=true`，服务必须在 `/api/ready` 前加载全部已发布 scale 索引、启用的 ANN handle 与可安全复用的单索引 PPR core；加载失败或索引数超过 `SCALE_IDX_CACHE_MAX` 必须 not-ready，不能把工件冷成本留给首位用户。单一 self-index 组合图复用原 node list/map/idf/PPR core，且 core 随专用 ScaleIndex LRU 常驻；不得在启动时全量物化跨库 mounted 组合图（现有拼接会成倍复制千万节点图并 OOM）。严格保证只覆盖启动时已经发布的工件集合；运行中 build/fold 或新增第 `SCALE_IDX_CACHE_MAX+1` 个索引仍走既有在线路径，稳定部署应提前扩容并重启以重新建立 readiness 保证。关闭 preload 只用于修复坏索引的临时恢复。
- **检索索引立即构建覆盖排队**：手动 `when=now` 只有在原子认领到新的立即构建时才移除同 notebook 的旧 idle 项；已有构建保留后续排队，认领后的新 idle 项也保留，daemon 启动失败则恢复被覆盖项。idle scheduler 必须逐项认领，禁止先清空整批：忙碌项继续排队，单项启动失败不得丢失或阻断其余项。`AskResponse.index_required` 只是回答时快照，前端必须结合实时 `ScaleIndexStatus.exists`；有界前台轮询结束后仍由 `index_done` 刷新当前 notebook，索引发布后立即隐藏历史降级提示。
- **LLM 响应缓存是 opt-in**：`chat_json` 的内容寻址缓存默认开，但**只有传 `response_validator` 的调用方才读写它**——不传就既不读也不写（对调用方透明、正确性保留、只失去性能）。占成本大头的 KG 抽取三处传 validator 保持缓存；Ask、paper_meta、summary 等不传的调用方刻意不缓存，一次关掉「偶发坏值被固化整个 TTL」的投毒类。健康探针走 `bypass_cache`；admin 清缓存 `tag` 与 `clear_all` 二选一（同时传即 400，绝不静默全清）。UI 上传按内容哈希做**同 notebook 内**去重（对齐 `batch_ingest`）；同内容不同后缀重传复用既有源、保留原解析（要换解析器请删除该源再重传）。本特性追加迁移 v30（`sources(notebook_id, file_hash)` 去重索引）。
- **构建任务必须落终态**：KG 构建的每条退出路径都要把 `kg_build_jobs` 那行写成终态，**包括 Ctrl-C/SIGTERM**——它们继承 `BaseException`，`except Exception` 接不到。留在 `running` 会被 notebook 级条件唯一索引把后续每次构建挡成 `KgBuildAlreadyRunning`，界面还一直显示「分析中」；启动期崩溃兜底只属于服务端 lifespan，离线 CLI 刻意无权自清，只能等后端重启。中断要 abort run control 停掉在飞窗口并**先排空再落终态**（守卫是跨进程的，随那行一起释放：不排空，活着的后端可以起新分析，而垂死进程的 worker 还在写图，会把新任务状态冲掉；顺带线程池 atexit join 也会把进程按住不退）；失败事件只在 `finish()` 真的把 `running` 落成 `failed` 时才发（信号可能落在成功提交之后）。但**不得**记 `kg_build_circuit_opened`——那是模型熔断，人工中断不是。真源见 `AGENTS.md`「Architecture Baseline」。
- 不引入 Docker 作为一期默认工作流；装新包前先问。
- **浮动弹窗**：新增的居中浮动弹窗要可拖标题栏移动——复用 `frontend/app/use-floating-window.ts`（`page.tsx` 内联弹窗走 `FloatingModalCard` 包装：只接管卡片、把 `dragHandleProps` 交给标题栏），不要另造一套拖动实现；侧边贴边抽屉、锚定 popover、全屏视图除外，窄屏（<720px）自动停用。真源见 `AGENTS.md` 前端章节。
- **长任务按钮的忙碌态**：点一下就触发长任务的按钮（排后台 job、同步等完的重活、大体积上传），点完必须**立刻**不可点并换成按该动作语义写的进行态文案（「补齐向量」→「补齐中…」，不是笼统的「处理中」）；纯图标按钮改用 `.busy-spin` 转圈并同步 `title`/`aria-label`。理由是硬的：`backfill-vectors`/`reparse` 后端**没有单飞守卫**，POST 立即返回而活在后台跑，按钮不变化用户就反复点、每点一次就再排一份同样的活。两种形态二选一：`disabled={busy}` + 文案切换，或忙碌时整排 CTA 换「取消」/不渲染；不能什么都不做。解除忙碌位要**按证据**，且证据按**修复形状**选、不能一刀切（真源=`frontend/app/checkup-view.ts` 的 `repairRelease`/`isRepairing`，有单测）：逐轮修一批的（`reparse`）存触发时的 count，count 一变就恢复可点；一次修全库的（`backfill_vectors`）⚠**不能**用 count——H4/H5 口径排除活跃租约、job 跑的过程中 count 就递减，按它解锁会排出并发全库补齐（正是后端 `checkup.py` H4/H5 注释点名要防的重复模型调用），只在该组从体检结果消失时解除。忙碌位还要随体检结果收敛（摘掉已消失的分组键），否则旧条目会被重新出现的同名问题继承、锁死新卡片。有界轮询窗口关闭与切库是共用兜底。⚠ `.sort-button`/`.icon-button`/`.ghost-button` 写死了 background/color，浏览器默认 `:disabled` 变灰对它们无效——`globals.css` 那条共用 `:disabled { opacity: .55 }` 是让禁用看得见的必要条件。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/long-task-button-guard.test.mjs`（按 `onClick` 源码文本认入口、不含行号；`disabled={false}` 这类恒假值同样报红）。
- **五档强度控件**：深度报告的「研究深度」与逐步推理的「检索档位」档名相同，必须复用 `frontend/app/effort-picker.tsx` 的 `EffortPicker`（chip + 滑块 popover + 该档一句说明），不得再造平铺 chip；档位的**数值阈值不上屏**，精确上限只在 `docs/product-and-api*.md` 的契约表。popover 必须打开时量一次再夹回最近的裁剪祖先（静态锚点做不到：`right:0` 与 `left:0` 各只在一段宽度成立），夹取算术在 `effort-picker-logic.ts`。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/effort-picker.test.mjs`（含「滑块只许存在于共享控件」的移动变异守卫）。
- **异常提示分级**：来源异常小字（解析失败/抽取缺口/待补全等）分三档（integrity=红/retrieval=黄/info 中性），唯一渲染路径是 `AnomalyBadge` + `sourceAnomalies()` + `--danger`/`--warning`/`--warning-solid` token；新增异常小字必须走它，不得手搓内联样式或裸 `⚠`。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/anomaly-guard.test.mjs`。
- **加了守卫 ≠ 有效**：必须做**变异验证**——把代码改回违规形态，确认它真的报红。只做「删除」变异不够，还要做「移动」变异。变异本身极易打空（替了字面量但代码用的是常量、按行号插入而行号已漂），先 `grep -c` 确认改到了再跑。
- 收尾提 PR，不直接合 `master`。分支先 rebase 到基分支保持线性，再 push、`gh pr create --base <基分支>`（通常是 `master`，stacked PR 不是，见第三节）。提 PR 与合入都必须经过 codex 评审，见第三节。

### 刻意不遵从 `AGENTS.md` 的几处

本文件其余部分都服从「冲突时以 `AGENTS.md` 为准」。以下是**穷举**的例外，各有理由：

1. **合入方式**：`AGENTS.md`「Feature Completion」第 1 步写的是把最新 `master` **3-way merge 进特性分支**，与本仓库实际使用的 Rebase and merge 冲突——把 `master` 合进来会破坏分支线性，GitHub 会报 `cannot be rebased`。以 **rebase** 为准，并提醒用户订正 `AGENTS.md`。
2. **worktree 里的 `npm install`**：`AGENTS.md`「Frontend/UI」写「缺 `frontend/node_modules` 就先跑 `npm install`」，在软链共享安装树的 worktree 里照做会写穿主 checkout。以上面「先 `ls -l` 再决定」为准。
3. **编辑方式**：`AGENTS.md`「Git And File Safety」写 `apply_patch`，那是 codex 语境；Claude Code 的等价物是 Edit/Write。这条是载体差异不是规则分歧。

---

## 二、子代理规范

### 1. 起子代理必须显式选模型

**不得默认继承主 agent 的模型。** 每次 `Agent` 调用要么显式传 `model`，要么用 `.claude/agents/` 里已钉好模型的角色。

`.claude/hooks/require-subagent-model.py` 是这条的 PreToolUse 硬门：没显式选模型且角色未钉模型的调用会被拦下。唯一的豁免是 `subagent_type: "fork"`——fork 语义上必须继承父模型，传 `model` 本就无效。省略 `subagent_type` **不算** fork，它等于默认 general-purpose，照样要选模型。

**判据是任务需要多少判断力，不是任务有多大。**

| 模型 | 适用 | 典型任务 |
| --- | --- | --- |
| `opus` | 需要判断力，要能推翻既有结论 | 写实现计划、规格/代码评审、架构取舍、疑难 bug 归因、安全审查、跨文件因果推理 |
| `sonnet` | 规格已定死的转录型工作 | 计划已写明改哪些文件怎么改、机械重构、补测试、文档同步、照既定模式扩展 |
| `haiku` | 纯检索定位清点 | 找文件、列符号、grep 汇总、清点调用点——只需汇报不需推理 |

内置角色（`Explore`、`general-purpose`、`Plan` 等）同样要传 `model`：`Explore` 做的是检索，一般 `haiku` 或 `sonnet` 就够。

**拿不准就上 `opus`**：返工一次的成本远高于模型差价。

**返工时先查计划，别默认怪模型。** 实测归因（PR#288）：`sonnet` 实现者反而抓到了计划里的 bug，问题出在计划不在执行。所以升级路径是——子代理报告「计划有歧义」或返工 ≥1 次时，**回去用 `opus` 重写计划**，而不是把同一份烂计划换 `opus` 再跑一遍。

### 2. 仓库角色（模型已钉在定义里）

| `subagent_type` | 模型 | 用途 |
| --- | --- | --- |
| `impl-task` | sonnet | 执行单个规格已定死的实现任务 |
| `spec-review` | opus | 任务级规格符合性评审 |
| `code-quality-review` | opus | 任务级代码质量与回归风险评审 |

实现任务本身需要设计取舍时，不要用 `impl-task`，改用通用子代理并显式传 `model: opus`。

⚠️ 可用的 `subagent_type` 列表在**会话启动时枚举**：刚新增或改名的角色定义，要重启会话才会出现，否则调用会报 `Agent type not found`。这是**响亮失败**不是静默降级——未知的 `subagent_type` 一律直接报错，不会悄悄落回默认角色。撞上时的兜底是用内置角色并显式传 `model`，判据不变。

同理，`subagent_type` 是**精确匹配**，不做大小写与分隔符的宽松归一：`Impl Task` 不会解析成 `impl-task`，只会报错。

### 3. 何时**不**起子代理

子代理会重建上下文、重新探索、写报告，然后你还要再读一遍报告。收益不明显就自己做：

- 几个文件的读改、简单搜索、小范围验证 —— 自己做更快更准。
- 一个不大的任务不要拆给多个并行子代理。
- 能一个子代理做完的，就不要用多个。

### 4. 本仓库刻意覆盖的通用默认：评审外包

Claude Code 的通用默认是「评审和验证留在主 agent 循环里，不要外包给子代理」。**本仓库刻意相反**：

> 已批准的多步实现计划按「子代理逐任务」执行——每个任务用一个全新的实现子代理，完成后跑任务级的规格评审与代码质量评审，再推进下一个任务。

理由是实测的（PR#288）：opus 的价值主要兑现在评审环节——变异验证、CSS 推演、推翻错误诊断都出自评审子代理。这是用户的明确决定，别按通用默认改回去。

例外：纯研究、设计、状态查询、只读评审类工作**不需要** worktree，也不需要子代理。

### 5. 子代理简报必备字段

一次把上下文喂足，避免「起 → 等 → 补简报」的往返：

1. **目标与验收标准** —— 怎样算完成。
2. **相关文件的绝对路径** —— 别让它自己猜。
3. **适用的红线** —— 从上面第一节挑相关的贴过去；子代理看不到你的对话历史。
4. **输出格式** —— 改了什么 / 没改什么 / 跑了什么验证命令与真实结果 / 阻塞与存疑。

多个互相独立的子代理放在**同一条消息**里并发发起。默认并发 ≤ 4；超过 20 个必须用户明确要求。

### 6. 委托后不返工

派出去了就认结果：不要自己再重做一遍它的工作，也不要在它汇报后重新推导它的结论。有疑问就针对性复核具体某一条，而不是整体重来。

---

## 三、提 PR 与合入：codex 评审闭环

**提 PR 之后、合入之前必须有 codex 评审，每一轮的原始输出逐字贴回 PR。**

执行由本机全局的 PostToolUse hook `~/.claude/hooks/codex-pr-review.sh` 承担。**它不是仓库产物**，`git grep` 在本仓库里找不到它；换机器或新 clone 上没有它时规则依然成立，那就手动跑。

### 自动触发只有两个点

1. `gh pr create` 成功 → 第 1 轮评审。
2. `git push` → **仅当该 PR 状态为 `awaiting_fix`（即上一轮判了 P0/P1）时**才重审。这是刻意设计：无关推送不烧额度。

**推论，很容易踩：上一轮是 🟡 或 🟢 之后再 push 修复，不会自动重审。** 这时必须自己补跑并补贴——hook 只代贴它自己跑的那几轮。手动命令与 hook 内部一致：

```bash
codex exec review --base <base> --ephemeral \
  -c 'model_reasoning_effort=medium' -c 'notify=[]' -o <out>
```

`review --base` 与自定义 PROMPT 位置参数互斥（报 `cannot be used with '[PROMPT]'`），所以不传自定义指令，正文是 codex 原生英文输出。

### 判成败要双判据

**退出码 0 且输出非空。** codex 被 SIGTERM 杀掉时退出码也是 0，只看退出码会贴出一条空评论、假装评审通过。

### 每轮都贴原文

包括零意见的轮次，也包括手动补跑的轮次。评论里带上：触发方式（自动 / 手动）、完整命令、head SHA、退出码与输出字节数。**贴原始输出，不贴我的转述**——用户要能自己核对我确实跑了、且结论没被我复述失真。

### 分级与闸门

| 判定 | 含义 | 动作 |
| --- | --- | --- |
| 🔴 P0/P1 | 阻塞 | 状态自动置 `awaiting_fix`；停下来问人。改完 push 会自动触发下一轮 |
| 🟡 P2/P3 | 非阻塞 | 可以如实说明后不改 |
| ⚪️ 解析不出标签但正文很长 | 格式可能变了 | 保守拦人，绝不因解析失败默认放行 |
| 🟢 无输出标签且正文很短 | 通过 | 问人是否合入 |

自动评审上限 5 轮（`CODEX_REVIEW_MAX_ROUNDS`）。人决定放弃修复时，由我显式落状态：

```bash
~/.claude/hooks/codex-pr-review.sh set-state <PR号> <waived|awaiting_fix|passed>
~/.claude/hooks/codex-pr-review.sh show-state <PR号>
```

### 意见不是照单全收

codex 的评审对象是 diff，它未必了解本 harness 的运行时事实。核实后可以驳回，但必须三件事齐全：在 PR 上写明驳回理由与证据、在代码里留下这条取舍的注释、加反向护栏用例钉住它。（PR#322 驳回过「豁免省略 `subagent_type` 的隐式 fork」，照做会把守卫整个掏空。）

### 空 diff 是硬失败

`base..HEAD` 算成空 diff 时 hook 会硬失败，而不是跑出一句「未发现问题」——假绿比不跑更糟。多半是当前目录不是 PR 改动所在的 worktree，`cd` 到正确目录再跑。

### 合入

- **必须先拿到用户明确同意**，绝不自作主张合。
- 本仓库用 **Rebase and merge**：`gh pr merge <PR号> --rebase`（也有 squash 合入的历史，标题带 `(#NNN)` 后缀的即是）。
- base 不一定是 `master`——stacked PR 的 base 是它的基分支，别硬写 `master`。
- 判断分支是否已进 `master`，**只认 `gh pr view --json state` 为 `MERGED`**。别用 SHA 祖先判断（rebase 会改 SHA），也别只信 `git cherry`（squash 合并会把提交全报成未合）。

---

## 四、深度报告与逐步推理准确性契约

深度报告创建后先做完全不读取语料的问题理解，并始终停在 `intent_ready` 等 owner 确认；模糊问题必须回答阻断性歧义，清晰问题也要确认可编辑的最终研究问题，`auto_generate` 不可绕过此门。确认端点必须以数据库原子转换认领 `intent_ready → planning`，确定性保留用户已经看过的全部合同字段，不得在确认后用第二次 LLM 输出静默替换；确认后的问题/答案才是后续检索、规划和生成的权威输入，补充答案可进入内部检索串但不得污染报告可见标题。此后才侦察语料；语料只能影响检索措辞、证据覆盖和章节排序，不能替换或收窄用户明确要求的主题。大纲中每个必答主题都要有可见绑定，最后一个绑定在 UI 与 API 两层都不可删除，确认后的检索方向必须实际执行。`SourceElement` 在 reasoning Ask 与报告中都是可 `[k]` 引用的一等证据，报告点击引用要能看到绑定的来源、位置和原文片段；小库可直接评分 element，大库禁止全表加载元素文本/向量，必须从有界 chunk ANN/FTS 候选的 `element_ids` 做有界 PK hydration。若来源已被接地判定为论文且解析出非空 `paper_title`，Ask 与报告引用优先显示论文标题，否则保留普通来源名/文件名。章节的 grounded 状态由真实锚点与证据相关度重算，不能相信模型自报，也不能把同源的不同 chunk/element 折叠成一个引用。最终编辑器只可摘要并报告遗漏/冲突，不可改写正文或新增事实。完整约束见 `AGENTS.md` 的 Product Flow。

逐步推理的正式界面也必须先调用 `/ask/intent` 做不读取 notebook / 参考库语料的问题理解；该预检只可使用当前会话最近的用户问题，不可把语料派生的助手回答回灌为意图依据，也不得创建 conversation 或 Ask job。意图清晰时自动确认，但因没有人工审阅，原始用户问题必须保留为第一条权威检索种子，模型规范化问题只能补充；关键歧义暂停并由用户补充后，审阅过的最终问题才成为权威。必答主题/检索方向、实体、比较轴、约束、排除项、前提、期望输出和答案确定性冻结，并统一用于 Memory、PPR、首轮子查询、证据检索与答案合成；原始输入仍作为可见会话问题。首轮先执行完整权威问题，再在配置预算内按主题轮询确认方向，确保每个必答主题先得到一个种子；第二个规划模型不得替换，后续反思只可按证据补充查询。无效确认必须在创建持久 job 前返回 422，预检取消要把取消事件传到意图模型调用。

**轨迹必须覆盖整轮，不只是检索段。** 问题理解跑在持久 job 之前，前端自行合成理解阶段的前几步（理解中 → 已理解/待澄清 → 已确认）并让后端步骤接在其后，不得再为这一阶段另起轨迹之外的独立提示条；该阶段的客户端墙钟以 `intent.understanding_ms`（可选、有上限、绝不参与检索）回传，成为持久 `intent` 步的 `duration_ms`，重开会话回放时不会凭空少掉这一段。后端必须在 Memory 检索**之前**推送 `intent` 步；命中私有记忆时记 `memory` 步——它记录的是**召回**而非归因（记忆进了 prompt 却没被引用是常态，按锚点过滤会漏报；推迟到合成之后又会离开事情真正发生的位置），归因由答案里的 `[k]` 引用承担，措辞不得声称被采用；零命中改记一条**带耗时的 `skip`**，候选查询与 embedding 调用照样发生，丢掉这段就与「覆盖整轮」矛盾。答案生成之后记 `synthesis` 步——那次生成通常是整轮最长的一段，既不能不可见，也不能被排除在轨迹总耗时之外；它的引用数取绑定锚点，不取检索到的证据卡数。实时轨迹面板按引擎是否真的流式推轨迹（后端 `AskMode.streaming`，前端镜像为 `streamsTrace`，由 `scripts/check_ask_modes_contract.py` 锁同步）判断，**不得按显示分组判断**：深入分析组里的非流式引擎会因此全程只显示「等待后端事件…」。

## 五、`AGENTS.md` 章节索引

按需查阅，用章节标题定位（不要依赖行号）：

| 要查什么 | `AGENTS.md` 章节 |
| --- | --- |
| 文档同步规则（四份） | Documentation Sync |
| 完成 spec 特性怎么记账 | Tracking Completed Spec Features |
| 后端能力必须配前端 | Full-Stack Parity (Backend ⇄ Frontend) |
| 产品形态、四个页签、上传到问答的完整流程 | Product Flow / MVP Scope |
| 分层架构、repository facade、端口与适配器 | Architecture Baseline |
| Python 环境、`PYTHON_BIN`、后端启动命令 | Python Environment / Backend Commands |
| 前端约定、`object_type` 标签契约、错误层三段规则 | Frontend/UI |
| 界面词 ↔ 内部词对照表（词汇守卫的真源） | 界面词汇表 (User-Facing Vocabulary) |
| 依赖政策、Python 版本下限、不加 Docker | Dependency Policy / No Docker In First Version |
| LLM 环境变量、模型服务状态与诊断 | LLM Configuration |
| 事件日志、按用户隔离的日志目录 | Logging / Observability |
| `scripts/check.sh` 三泳道、暖门时间目标、CI | Verification / GitHub Actions CI |
| 测试怎么分层、测试根在哪、并发 worker | Test Architecture |
| worktree、子代理逐任务、文件安全 | Git And File Safety |
| 收尾提 PR 的标准流程 | Feature Completion (Finish With a PR) |
